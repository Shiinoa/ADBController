"""
Database Manager - SQLite for Users, Settings, and Logs
"""
import sqlite3
import os
import json
import csv
import logging
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import secrets

import bcrypt

# Setup logging
logger = logging.getLogger(__name__)

# Database path
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB_PATH = os.path.join(CURRENT_DIR, "app_data.db")
REBUILT_DB_PATH = os.path.join(CURRENT_DIR, "app_data_rebuilt.db")
DB_POINTER_PATH = os.path.join(CURRENT_DIR, ".db_path")


def _resolve_db_path() -> str:
    env_path = os.environ.get("WEBAPP_DB_PATH")
    if env_path:
        return env_path

    if os.path.exists(DB_POINTER_PATH):
        try:
            pointed_path = open(DB_POINTER_PATH, "r", encoding="utf-8").read().strip()
            if pointed_path:
                return pointed_path
        except OSError:
            pass

    if os.path.exists(REBUILT_DB_PATH):
        return REBUILT_DB_PATH

    return DEFAULT_DB_PATH


DB_PATH = _resolve_db_path()

DEVICE_CSV_FIELD_ORDER = [
    'Asset Name', 'Asset Tag', 'IP', 'MAC Address', 'Model', 'Category',
    'Manufacturer', 'Serial', 'Default Location', 'Project', 'Work Center', 'Monotor'
]

DEVICE_COLUMN_MAP = {
    'Asset Name': 'asset_name',
    'Asset Tag': 'asset_tag',
    'IP': 'ip',
    'MAC Address': 'mac_address',
    'Model': 'model',
    'Category': 'category',
    'Manufacturer': 'manufacturer',
    'Serial': 'serial',
    'Default Location': 'default_location',
    'Project': 'project',
    'Work Center': 'work_center',
    'Monotor': 'monotor',
}

DEVICE_DB_TO_OUTPUT_MAP = {value: key for key, value in DEVICE_COLUMN_MAP.items()}
DEFAULT_DEVICE_PLANT_ID = "KCEE"
DEFAULT_DEVICE_OWNER_USERNAME = "admin"


def _find_legacy_device_csv_path() -> Optional[str]:
    """Locate the legacy device CSV without depending on import context."""
    candidate_names = ["devices_data.csv", "devices.csv", "device.csv", "list.csv", "data.csv"]
    base_dir = os.path.dirname(CURRENT_DIR)
    search_dirs = [
        CURRENT_DIR,
        os.path.join(base_dir, "scrcpy", "scrcpy-win64-v3.2"),
        base_dir,
    ]

    for search_dir in search_dirs:
        for name in candidate_names:
            path = os.path.join(search_dir, name)
            if os.path.exists(path):
                return path
    return None


def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def init_db():
    """Initialize database tables"""
    conn = get_db()
    cursor = conn.cursor()

    # Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'user',
            plant_code TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP,
            FOREIGN KEY (plant_code) REFERENCES plants(code)
        )
    ''')

    cursor.execute("PRAGMA table_info(users)")
    user_columns = [col[1] for col in cursor.fetchall()]
    if 'plant_code' not in user_columns:
        cursor.execute("ALTER TABLE users ADD COLUMN plant_code TEXT")
        cursor.execute(
            "UPDATE users SET plant_code = ? WHERE role != 'admin' AND (plant_code IS NULL OR TRIM(plant_code) = '')",
            (DEFAULT_DEVICE_PLANT_ID,)
        )
        logger.info("[DB] Added plant_code column to users")

    # Settings table (key-value store)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Plant table - supports multiple factories/sites
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS plants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE,
            name TEXT UNIQUE NOT NULL,
            location TEXT DEFAULT '',
            timezone TEXT DEFAULT 'Asia/Bangkok',
            description TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Device inventory - migrated from legacy CSV storage
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plant_id TEXT NOT NULL,
            owner_username TEXT NOT NULL DEFAULT 'admin',
            asset_name TEXT DEFAULT '',
            asset_tag TEXT DEFAULT '',
            ip TEXT UNIQUE,
            mac_address TEXT DEFAULT '',
            model TEXT DEFAULT '',
            category TEXT DEFAULT '',
            manufacturer TEXT DEFAULT '',
            serial TEXT DEFAULT '',
            default_location TEXT DEFAULT '',
            project TEXT DEFAULT '',
            work_center TEXT DEFAULT '',
            monotor TEXT DEFAULT '',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (plant_id) REFERENCES plants(code),
            FOREIGN KEY (owner_username) REFERENCES users(username)
        )
    ''')

    cursor.execute("PRAGMA table_info(device_inventory)")
    device_columns = [col[1] for col in cursor.fetchall()]
    if 'owner_username' not in device_columns:
        cursor.execute("ALTER TABLE device_inventory ADD COLUMN owner_username TEXT DEFAULT 'admin'")
        cursor.execute("UPDATE device_inventory SET owner_username = ? WHERE owner_username IS NULL OR TRIM(owner_username) = ''",
                       (DEFAULT_DEVICE_OWNER_USERNAME,))
        logger.info("[DB] Added owner_username column to device_inventory")

    # Device ping status table (IP as primary key - stores latest status only)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ping_status (
            ip TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            response_time REAL,
            last_online TIMESTAMP,
            consecutive_failures INTEGER DEFAULT 0,
            check_count INTEGER DEFAULT 0,
            cache_mb REAL DEFAULT 0,
            data_mb REAL DEFAULT 0,
            cache_alert INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add cache columns if they don't exist (migration for existing databases)
    cursor.execute("PRAGMA table_info(ping_status)")
    columns = [col[1] for col in cursor.fetchall()]
    if 'cache_mb' not in columns:
        cursor.execute('ALTER TABLE ping_status ADD COLUMN cache_mb REAL DEFAULT 0')
        cursor.execute('ALTER TABLE ping_status ADD COLUMN data_mb REAL DEFAULT 0')
        cursor.execute('ALTER TABLE ping_status ADD COLUMN cache_alert INTEGER DEFAULT 0')
        logger.info("[DB] Added cache columns to ping_status table")

    # Migrate old ping_logs to ping_status if exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ping_logs'")
    if cursor.fetchone():
        # Migrate latest status from old table
        cursor.execute('''
            INSERT OR IGNORE INTO ping_status (ip, status, response_time, updated_at)
            SELECT ip, status, response_time, checked_at
            FROM ping_logs
            WHERE id IN (SELECT MAX(id) FROM ping_logs GROUP BY ip)
        ''')
        # Drop old table
        cursor.execute('DROP TABLE ping_logs')
        logger.info("[DB] Migrated ping_logs to ping_status")

    # Alert logs table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS alert_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_to TEXT
        )
    ''')

    # Daily reports table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date DATE NOT NULL,
            total_devices INTEGER,
            online_count INTEGER,
            offline_count INTEGER,
            report_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Sessions table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')

    # Report templates table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS report_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            elements TEXT NOT NULL,
            settings TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT,
            updated_at TIMESTAMP,
            updated_by TEXT
        )
    ''')

    # Device locks table - tracks which devices are being used
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_locks (
            ip TEXT PRIMARY KEY,
            locked_by TEXT NOT NULL,
            lock_type TEXT NOT NULL,
            hostname TEXT,
            locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP
        )
    ''')

    # Automation workflows table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS automation_workflows (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            enabled INTEGER DEFAULT 1,
            device_scope TEXT NOT NULL,
            nodes TEXT NOT NULL,
            cooldown_minutes INTEGER DEFAULT 5,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_by TEXT,
            updated_at TIMESTAMP,
            last_triggered_at TIMESTAMP,
            trigger_count INTEGER DEFAULT 0
        )
    ''')

    # Automation execution logs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS automation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id TEXT NOT NULL,
            workflow_name TEXT,
            device_ip TEXT,
            trigger_type TEXT,
            trigger_detail TEXT,
            nodes_executed TEXT,
            status TEXT NOT NULL,
            error_message TEXT,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            duration_ms INTEGER
        )
    ''')

    conn.commit()

    # Create default admin user if not exists
    cursor.execute("SELECT * FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        # Use environment variable for admin password (more secure)
        default_password = os.environ.get('ADB_ADMIN_PASSWORD', '')
        if default_password:
            create_user('admin', default_password, 'admin')
            logger.info("[DB] Created admin user with password from ADB_ADMIN_PASSWORD env")
        else:
            # Generate secure random password using secrets module
            import string
            alphabet = string.ascii_letters + string.digits + "!@#$%"
            random_password = ''.join(secrets.choice(alphabet) for _ in range(16))
            create_user('admin', random_password, 'admin')

            # Write password to secure file instead of printing to console
            cred_file = os.path.join(os.path.dirname(DB_PATH), ".admin_credentials")
            try:
                with open(cred_file, 'w') as f:
                    f.write(f"Username: admin\n")
                    f.write(f"Password: {random_password}\n")
                    f.write("CHANGE THIS PASSWORD IMMEDIATELY!\n")
                    f.write("Then delete this file.\n")
                # Set restrictive permissions (owner only)
                if os.name != 'nt':  # Unix/Linux
                    os.chmod(cred_file, 0o600)
                logger.warning("=" * 60)
                logger.warning("DEFAULT ADMIN CREDENTIALS GENERATED")
                logger.warning(f"   Credentials saved to: {cred_file}")
                logger.warning("   Set ADB_ADMIN_PASSWORD env to customize")
                logger.warning("   DELETE the credentials file after reading!")
                logger.warning("=" * 60)
                print("=" * 60)
                print("DEFAULT ADMIN CREDENTIALS GENERATED")
                print(f"   Credentials saved to: {cred_file}")
                print("   Set ADB_ADMIN_PASSWORD env to customize")
                print("   DELETE the credentials file after reading!")
                print("=" * 60)
            except (IOError, OSError) as e:
                logger.error(f"Failed to write credentials file: {e}")
                # Fallback: log to logger only (not print)
                logger.warning(f"Admin password (CHANGE IMMEDIATELY): {random_password}")

    # Create default settings if not exists
    default_settings = {
        'smtp_host': '',
        'smtp_port': '587',
        'smtp_user': '',
        'smtp_password': '',
        'smtp_from': '',
        'smtp_to': '',
        'interchat_url': '',
        'interchat_token': '',
        'interchat_username': 'ADB Control Center',
        'interchat_icon_url': '',
        'interchat_skip_ssl_verification': 'false',
        'syno_chat_url': '',
        'syno_chat_token': '',
        'alert_enabled': 'false',
        'ping_interval': '60',
        'report_time': '08:00',
        'ntp_server': '',
        'ntp_sync_enabled': 'true',
        'ntp_sync_interval': '900',
        'ntp_sync_status': 'idle',
        'ntp_last_server': '',
        'ntp_last_sync': '',
        'ntp_last_error': '',
        'ntp_offset_ms': '0',
        'auto_reconnect_enabled': 'false',
        'auto_reconnect_max_retries': '10',
        'auto_reconnect_initial_delay': '5',
        'auto_reconnect_max_delay': '300',
        'auto_reconnect_disabled_ips': '[]',
        'health_history_retention_days': '30',
    }
    for key, value in default_settings.items():
        cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # APK deployment tracking tables
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS apk_deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deployment_id TEXT UNIQUE NOT NULL,
            filename TEXT NOT NULL,
            file_size INTEGER DEFAULT 0,
            target_devices TEXT NOT NULL,
            total_devices INTEGER DEFAULT 0,
            success_count INTEGER DEFAULT 0,
            failed_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_by TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS apk_deployment_devices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            deployment_id TEXT NOT NULL,
            ip TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            error_message TEXT,
            attempts INTEGER DEFAULT 0,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (deployment_id) REFERENCES apk_deployments(deployment_id)
        )
    ''')

    # Device health history table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS device_health_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            status TEXT NOT NULL,
            response_time REAL,
            app_status TEXT DEFAULT 'unknown',
            cache_mb REAL DEFAULT 0,
            wifi_rssi INTEGER,
            wifi_link_speed INTEGER,
            cpu_usage REAL,
            ram_usage_percent REAL,
            ram_total_mb REAL,
            ram_available_mb REAL,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_health_ip_time ON device_health_history(ip, recorded_at)')

    # Migration: add new columns if missing
    cursor.execute("PRAGMA table_info(device_health_history)")
    existing_cols = {col[1] for col in cursor.fetchall()}
    for col, coltype in [('wifi_rssi', 'INTEGER'), ('wifi_link_speed', 'INTEGER'),
                         ('cpu_usage', 'REAL'), ('ram_usage_percent', 'REAL'),
                         ('ram_total_mb', 'REAL'), ('ram_available_mb', 'REAL')]:
        if col not in existing_cols:
            cursor.execute(f'ALTER TABLE device_health_history ADD COLUMN {col} {coltype}')
            logger.info(f"[DB] Added column {col} to device_health_history")

    cursor.execute('''
        INSERT OR IGNORE INTO plants (code, name, location, timezone, description, is_active)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', ('default', 'Default Plant', '', 'Asia/Bangkok', 'Auto-created default plant', 1))
    cursor.execute('''
        INSERT OR IGNORE INTO plants (code, name, location, timezone, description, is_active)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (DEFAULT_DEVICE_PLANT_ID, DEFAULT_DEVICE_PLANT_ID, '', 'Asia/Bangkok', 'Primary plant for imported device inventory', 1))

    # Migrate legacy CSV device inventory into SQLite once
    cursor.execute("SELECT COUNT(*) FROM device_inventory")
    device_count = cursor.fetchone()[0]
    if device_count == 0:
        try:
            csv_path = _find_legacy_device_csv_path()
            if csv_path and os.path.exists(csv_path):
                with open(csv_path, mode='r', encoding='utf-8-sig', errors='ignore') as f:
                    reader = csv.DictReader(f)
                    migrated = 0
                    now = datetime.now()
                    for row in reader:
                        if not row:
                            continue
                        normalized = normalize_device_dict({k.strip(): v.strip() if v else '' for k, v in row.items() if k})
                        cursor.execute('''
                            INSERT OR IGNORE INTO device_inventory
                            (plant_id, owner_username, asset_name, asset_tag, ip, mac_address, model, category,
                             manufacturer, serial, default_location, project, work_center, monotor,
                             created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            normalized['plant_id'],
                            normalized['owner_username'],
                            normalized['asset_name'],
                            normalized['asset_tag'],
                            normalized['ip'],
                            normalized['mac_address'],
                            normalized['model'],
                            normalized['category'],
                            normalized['manufacturer'],
                            normalized['serial'],
                            normalized['default_location'],
                            normalized['project'],
                            normalized['work_center'],
                            normalized['monotor'],
                            now,
                            now,
                    ))
                        migrated += cursor.rowcount
                    if migrated > 0:
                        logger.info(f"[DB] Migrated {migrated} devices from CSV to device_inventory")
        except Exception as e:
            logger.warning(f"[DB] Device inventory CSV migration skipped: {e}")

    conn.commit()
    conn.close()
    print("[DB] Database initialized")


def hash_password(password: str) -> str:
    """Hash password using bcrypt (secure adaptive hashing)"""
    # Encode to bytes, hash with bcrypt, return as string
    password_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password_bytes, salt)
    return hashed.decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify password against hash using bcrypt"""
    try:
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)
    except (ValueError, TypeError) as e:
        logger.warning(f"Password verification error: {e}")
        return False


def create_user(username: str, password: str, role: str = 'user', plant_code: Optional[str] = None) -> bool:
    """Create a new user"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO users (username, password_hash, role, plant_code) VALUES (?, ?, ?, ?)",
            (username, hash_password(password), role, plant_code)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def verify_user(username: str, password: str) -> Optional[Dict]:
    """Verify user credentials using bcrypt"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()

    if row and verify_password(password, row['password_hash']):
        return dict(row)
    return None


def get_all_users() -> List[Dict]:
    """Get all users"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, plant_code, created_at, last_login FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def update_user(
    user_id: int,
    username: Optional[str] = None,
    password: Optional[str] = None,
    role: Optional[str] = None,
    plant_code: Optional[str] = None,
) -> bool:
    """Update user"""
    conn = get_db()
    cursor = conn.cursor()
    updates = []
    params = []

    if username:
        updates.append("username = ?")
        params.append(username)
    if password:
        updates.append("password_hash = ?")
        params.append(hash_password(password))
    if role:
        updates.append("role = ?")
        params.append(role)
    if plant_code is not None:
        updates.append("plant_code = ?")
        params.append(plant_code)

    if not updates:
        return False

    params.append(user_id)
    cursor.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()
    success = cursor.rowcount > 0
    conn.close()
    return success


def delete_user(user_id: int) -> bool:
    """Delete user"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = ? AND username != 'admin'", (user_id,))
    conn.commit()
    success = cursor.rowcount > 0
    conn.close()
    return success


def create_session(user_id: int) -> str:
    """Create a new session token"""
    token = secrets.token_hex(32)
    expires_at = datetime.now() + timedelta(hours=4)  # Reduced from 24h for security

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
        (token, user_id, expires_at)
    )
    # Update last login
    cursor.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.now(), user_id))
    conn.commit()
    conn.close()
    return token


def verify_session(token: str) -> Optional[Dict]:
    """Verify session token and return user"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT u.id, u.username, u.role, u.plant_code
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ? AND s.expires_at > ?
    ''', (token, datetime.now()))
    row = cursor.fetchone()
    conn.close()
    if row:
        return dict(row)
    return None


def delete_session(token: str):
    """Delete session (logout)"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def get_setting(key: str) -> Optional[str]:
    """Get a setting value"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row['value'] if row else None


def set_setting(key: str, value: str):
    """Set a setting value"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (key, value, datetime.now())
    )
    conn.commit()
    conn.close()


def get_all_settings() -> Dict[str, str]:
    """Get all settings as dictionary"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    conn.close()
    return {row['key']: row['value'] for row in rows}


def normalize_device_dict(
    device: Dict,
    default_plant_id: str = DEFAULT_DEVICE_PLANT_ID,
    default_owner_username: str = DEFAULT_DEVICE_OWNER_USERNAME,
) -> Dict[str, str]:
    """Normalize device payload from legacy CSV shape into DB shape."""
    normalized = {
        "plant_id": (device.get("plant_id") or device.get("Plant ID") or default_plant_id or DEFAULT_DEVICE_PLANT_ID).strip(),
        "owner_username": (device.get("owner_username") or device.get("Owner Username") or default_owner_username or DEFAULT_DEVICE_OWNER_USERNAME).strip(),
        "asset_name": "",
        "asset_tag": "",
        "ip": "",
        "mac_address": "",
        "model": "",
        "category": "",
        "manufacturer": "",
        "serial": "",
        "default_location": "",
        "project": "",
        "work_center": "",
        "monotor": "",
    }

    for csv_key, db_key in DEVICE_COLUMN_MAP.items():
        value = device.get(csv_key, "")
        if value in (None, ""):
            alt_key = csv_key.replace(" ", "_")
            value = device.get(alt_key, "")
        normalized[db_key] = str(value).strip() if value is not None else ""

    return normalized


def device_row_to_dict(row: Dict) -> Dict[str, str]:
    """Convert DB device row back into legacy CSV-compatible API shape."""
    output = {
        "id": row.get("id"),
        "plant_id": row.get("plant_id", DEFAULT_DEVICE_PLANT_ID),
        "Plant ID": row.get("plant_id", DEFAULT_DEVICE_PLANT_ID),
        "owner_username": row.get("owner_username", DEFAULT_DEVICE_OWNER_USERNAME),
        "Owner Username": row.get("owner_username", DEFAULT_DEVICE_OWNER_USERNAME),
    }
    for db_key, csv_key in DEVICE_DB_TO_OUTPUT_MAP.items():
        output[csv_key] = row.get(db_key, "") or ""
    return output


def get_all_devices(keyword: Optional[str] = None, owner_username: Optional[str] = None) -> List[Dict]:
    """Get all devices from SQLite inventory, preserving legacy output keys."""
    conn = get_db()
    cursor = conn.cursor()
    if owner_username:
        cursor.execute('''
            SELECT id, plant_id, owner_username, asset_name, asset_tag, ip, mac_address, model, category,
                   manufacturer, serial, default_location, project, work_center, monotor
            FROM device_inventory
            WHERE owner_username = ?
            ORDER BY COALESCE(ip, ''), COALESCE(asset_name, '')
        ''', (owner_username,))
    else:
        cursor.execute('''
            SELECT id, plant_id, owner_username, asset_name, asset_tag, ip, mac_address, model, category,
                   manufacturer, serial, default_location, project, work_center, monotor
            FROM device_inventory
            ORDER BY COALESCE(ip, ''), COALESCE(asset_name, '')
        ''')
    rows = [dict(row) for row in cursor.fetchall()]
    conn.close()

    devices = [device_row_to_dict(row) for row in rows if row.get("ip")]
    if not keyword:
        return devices

    search_terms = [term.strip().lower() for term in keyword.split(',') if term.strip()]
    if not search_terms:
        return devices

    filtered = []
    for device in devices:
        all_text = " ".join(str(value) for value in device.values()).lower()
        if any(term in all_text for term in search_terms):
            filtered.append(device)
    return filtered


def replace_all_devices(
    devices: List[Dict],
    default_plant_id: str = DEFAULT_DEVICE_PLANT_ID,
    default_owner_username: str = DEFAULT_DEVICE_OWNER_USERNAME,
) -> bool:
    """Replace the SQLite-backed device inventory with the provided device list."""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    try:
        cursor.execute("INSERT OR IGNORE INTO plants (code, name, timezone, description, is_active) VALUES (?, ?, ?, ?, ?)",
                       (default_plant_id, default_plant_id, 'Asia/Bangkok', 'Auto-created for device inventory', 1))
        cursor.execute("DELETE FROM device_inventory")

        for device in devices:
            normalized = normalize_device_dict(
                device,
                default_plant_id=default_plant_id,
                default_owner_username=default_owner_username,
            )
            cursor.execute('''
                INSERT INTO device_inventory
                (plant_id, owner_username, asset_name, asset_tag, ip, mac_address, model, category,
                 manufacturer, serial, default_location, project, work_center, monotor,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                normalized["plant_id"] or default_plant_id,
                normalized["owner_username"] or default_owner_username,
                normalized["asset_name"],
                normalized["asset_tag"],
                normalized["ip"],
                normalized["mac_address"],
                normalized["model"],
                normalized["category"],
                normalized["manufacturer"],
                normalized["serial"],
                normalized["default_location"],
                normalized["project"],
                normalized["work_center"],
                normalized["monotor"],
                now,
                now,
            ))

        conn.commit()
        return True
    except sqlite3.Error as e:
        logger.error(f"[DB] Failed to replace device inventory: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_user_by_username(username: str) -> Optional[Dict]:
    """Get a user by username."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, username, role, plant_code, created_at, last_login FROM users WHERE username = ?", (username,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


# ============== Plants ==============

def create_plant(name: str, code: Optional[str] = None, location: str = "",
                 timezone: str = "Asia/Bangkok", description: str = "") -> Optional[Dict]:
    """Create a plant/site record"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()
    try:
        cursor.execute('''
            INSERT INTO plants (code, name, location, timezone, description, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (code, name, location, timezone, description, 1, now, now))
        conn.commit()
        plant_id = cursor.lastrowid
        conn.close()
        return get_plant_by_id(plant_id)
    except sqlite3.IntegrityError:
        conn.close()
        return None


def get_plant_by_id(plant_id: int) -> Optional[Dict]:
    """Get a single plant by id"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plants WHERE id = ?", (plant_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_plant_by_code(code: str) -> Optional[Dict]:
    """Get a single plant by code"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plants WHERE code = ?", (code,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_plants(active_only: bool = False) -> List[Dict]:
    """Get all plants"""
    conn = get_db()
    cursor = conn.cursor()
    if active_only:
        cursor.execute("SELECT * FROM plants WHERE is_active = 1 ORDER BY name")
    else:
        cursor.execute("SELECT * FROM plants ORDER BY name")
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_device_count_by_plant(code: str) -> int:
    """Count devices assigned to a plant code."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM device_inventory WHERE plant_id = ?", (code,))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def update_plant(plant_id: int, name: Optional[str] = None, code: Optional[str] = None,
                 location: Optional[str] = None, timezone: Optional[str] = None,
                 description: Optional[str] = None, is_active: Optional[bool] = None) -> bool:
    """Update a plant"""
    conn = get_db()
    cursor = conn.cursor()
    updates = []
    params = []

    if name is not None:
        updates.append("name = ?")
        params.append(name)
    if code is not None:
        updates.append("code = ?")
        params.append(code)
    if location is not None:
        updates.append("location = ?")
        params.append(location)
    if timezone is not None:
        updates.append("timezone = ?")
        params.append(timezone)
    if description is not None:
        updates.append("description = ?")
        params.append(description)
    if is_active is not None:
        updates.append("is_active = ?")
        params.append(1 if is_active else 0)

    if not updates:
        conn.close()
        return False

    updates.append("updated_at = ?")
    params.append(datetime.now())
    params.append(plant_id)

    try:
        cursor.execute(f"UPDATE plants SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success
    except sqlite3.IntegrityError:
        conn.close()
        return False


def delete_plant(plant_id: int) -> bool:
    """Delete a plant, but keep the default seed plant"""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM plants WHERE id = ? AND code != 'default'", (plant_id,))
        conn.commit()
        success = cursor.rowcount > 0
        conn.close()
        return success
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return False


def log_ping(ip: str, status: str, response_time: Optional[float] = None,
             is_online: bool = False, consecutive_failures: int = 0,
             cache_mb: float = 0, data_mb: float = 0, cache_alert: bool = False):
    """Update ping status for a device (upsert) - includes cache data"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    # Get current record to update check_count and last_online
    cursor.execute("SELECT check_count, last_online FROM ping_status WHERE ip = ?", (ip,))
    row = cursor.fetchone()

    check_count = (row['check_count'] + 1) if row else 1
    last_online = now if is_online else (row['last_online'] if row else None)

    cursor.execute('''
        INSERT OR REPLACE INTO ping_status
        (ip, status, response_time, last_online, consecutive_failures, check_count,
         cache_mb, data_mb, cache_alert, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (ip, status, response_time, last_online, consecutive_failures, check_count,
          cache_mb, data_mb, 1 if cache_alert else 0, now))

    conn.commit()
    conn.close()


def get_ping_status(ip: Optional[str] = None) -> List[Dict]:
    """Get ping status for device(s)"""
    conn = get_db()
    cursor = conn.cursor()

    if ip:
        cursor.execute("SELECT * FROM ping_status WHERE ip = ?", (ip,))
    else:
        cursor.execute("SELECT * FROM ping_status ORDER BY updated_at DESC")

    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_ping_status() -> Dict[str, Dict]:
    """Get all device ping statuses as dictionary"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM ping_status")
    rows = cursor.fetchall()
    conn.close()
    return {row['ip']: dict(row) for row in rows}


def clear_ping_status(ip: Optional[str] = None):
    """Clear ping status for device(s)"""
    conn = get_db()
    cursor = conn.cursor()
    if ip:
        cursor.execute("DELETE FROM ping_status WHERE ip = ?", (ip,))
    else:
        cursor.execute("DELETE FROM ping_status")
    conn.commit()
    conn.close()


def log_alert(ip: str, alert_type: str, message: str, sent_to: str):
    """Log alert sent"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO alert_logs (ip, alert_type, message, sent_to) VALUES (?, ?, ?, ?)",
        (ip, alert_type, message, sent_to)
    )
    conn.commit()
    conn.close()


def save_daily_report(report_date: str, total: int, online: int, offline: int, data: str):
    """Save daily report"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR REPLACE INTO daily_reports (report_date, total_devices, online_count, offline_count, report_data) VALUES (?, ?, ?, ?, ?)",
        (report_date, total, online, offline, data)
    )
    conn.commit()
    conn.close()


def get_daily_reports(days: int = 30, include_data: bool = False) -> List[Dict]:
    """Get daily reports for last N days (exclude heavy report_data by default)"""
    conn = get_db()
    cursor = conn.cursor()
    if include_data:
        cursor.execute(
            "SELECT * FROM daily_reports ORDER BY report_date DESC LIMIT ?",
            (days,)
        )
    else:
        cursor.execute(
            "SELECT id, report_date, total_devices, online_count, offline_count, created_at FROM daily_reports ORDER BY report_date DESC LIMIT ?",
            (days,)
        )
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ============== Device Locks ==============

def acquire_device_lock(ip: str, locked_by: str, lock_type: str,
                        hostname: Optional[str] = None, duration_minutes: int = 30) -> Dict:
    """
    Try to acquire a lock on a device.
    Returns: {"success": bool, "message": str, "lock": dict or None}
    """
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    # Clean up expired locks first
    cursor.execute("DELETE FROM device_locks WHERE expires_at < ?", (now,))

    # Check if device is already locked
    cursor.execute("SELECT * FROM device_locks WHERE ip = ?", (ip,))
    existing = cursor.fetchone()

    if existing:
        # Device is locked by someone else
        lock_info = dict(existing)
        conn.close()
        return {
            "success": False,
            "message": f"Device is in use by {existing['locked_by']} ({existing['lock_type']}) since {existing['locked_at']}",
            "lock": lock_info
        }

    # Acquire lock
    expires_at = now + timedelta(minutes=duration_minutes)
    cursor.execute('''
        INSERT INTO device_locks (ip, locked_by, lock_type, hostname, locked_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (ip, locked_by, lock_type, hostname, now, expires_at))

    conn.commit()
    conn.close()

    return {
        "success": True,
        "message": "Lock acquired",
        "lock": {
            "ip": ip,
            "locked_by": locked_by,
            "lock_type": lock_type,
            "hostname": hostname,
            "locked_at": now.isoformat(),
            "expires_at": expires_at.isoformat()
        }
    }


def release_device_lock(ip: str, locked_by: Optional[str] = None) -> Dict:
    """
    Release a device lock.
    If locked_by is specified, only release if it matches.
    """
    conn = get_db()
    cursor = conn.cursor()

    if locked_by:
        cursor.execute("DELETE FROM device_locks WHERE ip = ? AND locked_by = ?", (ip, locked_by))
    else:
        cursor.execute("DELETE FROM device_locks WHERE ip = ?", (ip,))

    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    return {
        "success": deleted > 0,
        "message": "Lock released" if deleted > 0 else "No lock found or not owned by you"
    }


def get_device_lock(ip: str) -> Optional[Dict]:
    """Get lock info for a specific device"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    # Clean expired locks
    cursor.execute("DELETE FROM device_locks WHERE expires_at < ?", (now,))
    conn.commit()

    cursor.execute("SELECT * FROM device_locks WHERE ip = ?", (ip,))
    row = cursor.fetchone()
    conn.close()

    return dict(row) if row else None


def get_all_device_locks() -> List[Dict]:
    """Get all active device locks"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    # Clean expired locks
    cursor.execute("DELETE FROM device_locks WHERE expires_at < ?", (now,))
    conn.commit()

    cursor.execute("SELECT * FROM device_locks ORDER BY locked_at DESC")
    rows = cursor.fetchall()
    conn.close()

    return [dict(row) for row in rows]


def extend_device_lock(ip: str, locked_by: str, additional_minutes: int = 30) -> Dict:
    """Extend an existing lock"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    cursor.execute("SELECT * FROM device_locks WHERE ip = ? AND locked_by = ?", (ip, locked_by))
    existing = cursor.fetchone()

    if not existing:
        conn.close()
        return {"success": False, "message": "Lock not found or not owned by you"}

    new_expires = now + timedelta(minutes=additional_minutes)
    cursor.execute("UPDATE device_locks SET expires_at = ? WHERE ip = ?", (new_expires, ip))
    conn.commit()
    conn.close()

    return {"success": True, "message": f"Lock extended until {new_expires.isoformat()}"}


def cleanup_expired_locks():
    """Remove all expired locks"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM device_locks WHERE expires_at < ?", (datetime.now(),))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============== Report Templates ==============

def get_all_templates() -> List[Dict]:
    """Get all report templates"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM report_templates ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()

    templates = []
    for row in rows:
        template = dict(row)
        # Parse JSON fields
        template['elements'] = json.loads(template['elements']) if template['elements'] else []
        template['settings'] = json.loads(template['settings']) if template['settings'] else {}
        templates.append(template)
    return templates


def get_template_by_id(template_id: str) -> Optional[Dict]:
    """Get a specific template by ID"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM report_templates WHERE id = ?", (template_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        template = dict(row)
        template['elements'] = json.loads(template['elements']) if template['elements'] else []
        template['settings'] = json.loads(template['settings']) if template['settings'] else {}
        return template
    return None


def create_template(template_id: str, name: str, description: str, elements: List,
                    settings: Dict, created_by: str) -> Optional[Dict]:
    """Create a new report template"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    try:
        cursor.execute('''
            INSERT INTO report_templates (id, name, description, elements, settings, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (template_id, name, description, json.dumps(elements), json.dumps(settings), now, created_by))
        conn.commit()

        return {
            "id": template_id,
            "name": name,
            "description": description,
            "elements": elements,
            "settings": settings,
            "created_at": now.isoformat(),
            "created_by": created_by
        }
    except sqlite3.IntegrityError as e:
        logger.error(f"Error creating template: {e}")
        return None
    finally:
        conn.close()


def update_template(template_id: str, name: str, description: str, elements: List,
                    settings: Dict, updated_by: str) -> Optional[Dict]:
    """Update an existing report template"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    cursor.execute('''
        UPDATE report_templates
        SET name = ?, description = ?, elements = ?, settings = ?, updated_at = ?, updated_by = ?
        WHERE id = ?
    ''', (name, description, json.dumps(elements), json.dumps(settings), now, updated_by, template_id))
    conn.commit()

    if cursor.rowcount > 0:
        conn.close()
        return get_template_by_id(template_id)

    conn.close()
    return None


def delete_template(template_id: str) -> Optional[str]:
    """Delete a report template, returns template name if successful"""
    conn = get_db()
    cursor = conn.cursor()

    # Get template name before deleting
    cursor.execute("SELECT name FROM report_templates WHERE id = ?", (template_id,))
    row = cursor.fetchone()

    if not row:
        conn.close()
        return None

    template_name = row['name']
    cursor.execute("DELETE FROM report_templates WHERE id = ?", (template_id,))
    conn.commit()
    conn.close()

    return template_name


def migrate_templates_from_json(json_file_path: str) -> int:
    """Migrate templates from JSON file to database"""
    if not os.path.exists(json_file_path):
        return 0

    try:
        with open(json_file_path, 'r', encoding='utf-8') as f:
            templates = json.load(f)

        if not templates:
            return 0

        conn = get_db()
        cursor = conn.cursor()
        migrated = 0

        for t in templates:
            try:
                cursor.execute('''
                    INSERT OR REPLACE INTO report_templates
                    (id, name, description, elements, settings, created_at, created_by, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    t.get('id'),
                    t.get('name'),
                    t.get('description', ''),
                    json.dumps(t.get('elements', [])),
                    json.dumps(t.get('settings', {})),
                    t.get('created_at'),
                    t.get('created_by'),
                    t.get('updated_at'),
                    t.get('updated_by')
                ))
                migrated += 1
            except Exception as e:
                logger.error(f"Error migrating template {t.get('id')}: {e}")

        conn.commit()
        conn.close()

        # Rename old JSON file as backup
        backup_path = json_file_path + '.backup'
        os.rename(json_file_path, backup_path)
        logger.info(f"[DB] Migrated {migrated} templates. Old file backed up to {backup_path}")

        return migrated
    except Exception as e:
        logger.error(f"Error migrating templates: {e}")
        return 0


# ============== Automation Workflows ==============

def get_all_workflows() -> List[Dict]:
    """Get all automation workflows"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM automation_workflows ORDER BY created_at DESC")
    rows = cursor.fetchall()
    conn.close()

    workflows = []
    for row in rows:
        wf = dict(row)
        wf['device_scope'] = json.loads(wf['device_scope']) if wf['device_scope'] else {}
        wf['nodes'] = json.loads(wf['nodes']) if wf['nodes'] else []
        workflows.append(wf)
    return workflows


def get_workflow_by_id(workflow_id: str) -> Optional[Dict]:
    """Get a specific workflow by ID"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM automation_workflows WHERE id = ?", (workflow_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        wf = dict(row)
        wf['device_scope'] = json.loads(wf['device_scope']) if wf['device_scope'] else {}
        wf['nodes'] = json.loads(wf['nodes']) if wf['nodes'] else []
        return wf
    return None


def create_workflow(workflow_id: str, name: str, description: str,
                    device_scope: Dict, nodes: List, cooldown_minutes: int,
                    created_by: str) -> Optional[Dict]:
    """Create a new automation workflow"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    try:
        cursor.execute('''
            INSERT INTO automation_workflows
            (id, name, description, device_scope, nodes, cooldown_minutes, created_at, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (workflow_id, name, description, json.dumps(device_scope),
              json.dumps(nodes), cooldown_minutes, now, created_by))
        conn.commit()
        return {
            "id": workflow_id, "name": name, "description": description,
            "enabled": 1, "device_scope": device_scope, "nodes": nodes,
            "cooldown_minutes": cooldown_minutes,
            "created_at": now.isoformat(), "created_by": created_by
        }
    except sqlite3.IntegrityError as e:
        logger.error(f"Error creating workflow: {e}")
        return None
    finally:
        conn.close()


def update_workflow(workflow_id: str, name: str, description: str,
                    device_scope: Dict, nodes: List, cooldown_minutes: int) -> Optional[Dict]:
    """Update an existing workflow"""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()

    cursor.execute('''
        UPDATE automation_workflows
        SET name = ?, description = ?, device_scope = ?, nodes = ?,
            cooldown_minutes = ?, updated_at = ?
        WHERE id = ?
    ''', (name, description, json.dumps(device_scope), json.dumps(nodes),
          cooldown_minutes, now, workflow_id))
    conn.commit()

    if cursor.rowcount > 0:
        conn.close()
        return get_workflow_by_id(workflow_id)
    conn.close()
    return None


def delete_workflow(workflow_id: str) -> bool:
    """Delete a workflow"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM automation_workflows WHERE id = ?", (workflow_id,))
    deleted = cursor.rowcount > 0
    # Also clean up logs
    cursor.execute("DELETE FROM automation_logs WHERE workflow_id = ?", (workflow_id,))
    conn.commit()
    conn.close()
    return deleted


def set_workflow_enabled(workflow_id: str, enabled: bool) -> bool:
    """Enable or disable a workflow"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE automation_workflows SET enabled = ? WHERE id = ?",
                   (1 if enabled else 0, workflow_id))
    conn.commit()
    success = cursor.rowcount > 0
    conn.close()
    return success


def update_workflow_trigger_stats(workflow_id: str, triggered_at: Optional[datetime] = None):
    """Update last triggered time and increment count"""
    conn = get_db()
    cursor = conn.cursor()
    trigger_time = triggered_at or datetime.now()
    cursor.execute('''
        UPDATE automation_workflows
        SET last_triggered_at = ?, trigger_count = trigger_count + 1
        WHERE id = ?
    ''', (trigger_time, workflow_id))
    conn.commit()
    conn.close()


def log_automation_execution(workflow_id: str, workflow_name: str,
                             device_ip: str, trigger_type: str,
                             trigger_detail: Dict, nodes_executed: List,
                             status: str, error_message: str = None,
                             duration_ms: int = 0,
                             started_at: Optional[datetime] = None,
                             completed_at: Optional[datetime] = None) -> int:
    """Log a workflow execution"""
    conn = get_db()
    cursor = conn.cursor()
    started = started_at or datetime.now()
    completed = completed_at or started
    cursor.execute('''
        INSERT INTO automation_logs
        (workflow_id, workflow_name, device_ip, trigger_type, trigger_detail,
         nodes_executed, status, error_message, started_at, completed_at, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (workflow_id, workflow_name, device_ip, trigger_type,
          json.dumps(trigger_detail), json.dumps(nodes_executed),
          status, error_message, started, completed, duration_ms))
    conn.commit()
    log_id = cursor.lastrowid
    conn.close()
    return log_id


def get_automation_logs(workflow_id: str = None, limit: int = 100,
                        offset: int = 0) -> List[Dict]:
    """Get automation execution logs"""
    conn = get_db()
    cursor = conn.cursor()

    if workflow_id:
        cursor.execute('''
            SELECT * FROM automation_logs
            WHERE workflow_id = ?
            ORDER BY started_at DESC LIMIT ? OFFSET ?
        ''', (workflow_id, limit, offset))
    else:
        cursor.execute('''
            SELECT * FROM automation_logs
            ORDER BY started_at DESC LIMIT ? OFFSET ?
        ''', (limit, offset))

    rows = cursor.fetchall()
    conn.close()

    logs = []
    for row in rows:
        log = dict(row)
        log['trigger_detail'] = json.loads(log['trigger_detail']) if log['trigger_detail'] else {}
        log['nodes_executed'] = json.loads(log['nodes_executed']) if log['nodes_executed'] else []
        logs.append(log)
    return logs


def get_automation_stats() -> Dict:
    """Get automation statistics"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM automation_workflows WHERE enabled = 1")
    active_workflows = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) as total FROM automation_logs")
    total_executions = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) as total FROM automation_logs WHERE status = 'success'")
    success_count = cursor.fetchone()['total']

    cursor.execute("SELECT COUNT(*) as total FROM automation_logs WHERE status = 'failed'")
    failed_count = cursor.fetchone()['total']

    cursor.execute("SELECT * FROM automation_logs ORDER BY started_at DESC LIMIT 1")
    last_log = cursor.fetchone()

    conn.close()
    return {
        "active_workflows": active_workflows,
        "total_executions": total_executions,
        "success_count": success_count,
        "failed_count": failed_count,
        "success_rate": round(success_count / total_executions * 100, 1) if total_executions > 0 else 0,
        "last_execution": dict(last_log) if last_log else None
    }


def cleanup_automation_logs(days: int = 30) -> int:
    """Remove logs older than N days"""
    conn = get_db()
    cursor = conn.cursor()
    cutoff = datetime.now() - timedelta(days=days)
    cursor.execute("DELETE FROM automation_logs WHERE started_at < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ============================================
# APK Deployments
# ============================================


def create_deployment(deployment_id: str, filename: str, file_size: int,
                      target_ips: List[str], created_by: str) -> Dict:
    """Create a new APK deployment record with per-device entries."""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()
    cursor.execute(
        "INSERT INTO apk_deployments (deployment_id, filename, file_size, target_devices, "
        "total_devices, status, created_by, created_at) VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?)",
        (deployment_id, filename, file_size, json.dumps(target_ips), len(target_ips), created_by, now)
    )
    for ip in target_ips:
        cursor.execute(
            "INSERT INTO apk_deployment_devices (deployment_id, ip, status) VALUES (?, ?, 'pending')",
            (deployment_id, ip)
        )
    conn.commit()
    conn.close()
    return {"deployment_id": deployment_id, "total_devices": len(target_ips)}


def update_deployment_device(deployment_id: str, ip: str, status: str,
                             error_message: str = None):
    """Update status of a single device in a deployment."""
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now()
    if status == "installing":
        cursor.execute(
            "UPDATE apk_deployment_devices SET status = ?, started_at = ?, "
            "attempts = attempts + 1 WHERE deployment_id = ? AND ip = ?",
            (status, now, deployment_id, ip)
        )
    else:
        cursor.execute(
            "UPDATE apk_deployment_devices SET status = ?, error_message = ?, "
            "completed_at = ? WHERE deployment_id = ? AND ip = ?",
            (status, error_message, now, deployment_id, ip)
        )
    # Update summary counts
    cursor.execute(
        "UPDATE apk_deployments SET "
        "success_count = (SELECT COUNT(*) FROM apk_deployment_devices WHERE deployment_id = ? AND status = 'success'), "
        "failed_count = (SELECT COUNT(*) FROM apk_deployment_devices WHERE deployment_id = ? AND status = 'failed') "
        "WHERE deployment_id = ?",
        (deployment_id, deployment_id, deployment_id)
    )
    conn.commit()
    conn.close()


def complete_deployment(deployment_id: str):
    """Mark a deployment as completed."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE apk_deployments SET status = 'completed', completed_at = ? WHERE deployment_id = ?",
        (datetime.now(), deployment_id)
    )
    conn.commit()
    conn.close()


def get_deployment(deployment_id: str) -> Optional[Dict]:
    """Get deployment detail with per-device status."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM apk_deployments WHERE deployment_id = ?", (deployment_id,))
    dep = cursor.fetchone()
    if not dep:
        conn.close()
        return None
    result = dict(dep)
    cursor.execute(
        "SELECT ip, status, error_message, attempts, started_at, completed_at "
        "FROM apk_deployment_devices WHERE deployment_id = ? ORDER BY ip",
        (deployment_id,)
    )
    result["devices"] = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return result


def get_deployments(limit: int = 20, offset: int = 0) -> List[Dict]:
    """List deployment history."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT deployment_id, filename, file_size, total_devices, success_count, failed_count, "
        "status, created_by, created_at, completed_at FROM apk_deployments "
        "ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset)
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_failed_deployment_devices(deployment_id: str) -> List[str]:
    """Get IPs of failed devices in a deployment."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ip FROM apk_deployment_devices WHERE deployment_id = ? AND status = 'failed'",
        (deployment_id,)
    )
    ips = [r["ip"] for r in cursor.fetchall()]
    conn.close()
    return ips


# ============================================
# Health History
# ============================================


def log_health_record(ip: str, status: str, response_time: float = None,
                      app_status: str = "unknown", cache_mb: float = 0):
    """Append a health record for a device."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO device_health_history (ip, status, response_time, app_status, cache_mb, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (ip, status, response_time, app_status, cache_mb, datetime.now())
    )
    conn.commit()
    conn.close()


def log_health_records_batch(records: list):
    """Batch insert health records.
    Each record is a tuple: (ip, status, response_time, app_status, cache_mb,
    wifi_rssi, wifi_link_speed, cpu_usage, ram_usage_percent, ram_total_mb, ram_available_mb, recorded_at)
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.executemany(
        "INSERT INTO device_health_history "
        "(ip, status, response_time, app_status, cache_mb, "
        "wifi_rssi, wifi_link_speed, cpu_usage, ram_usage_percent, ram_total_mb, ram_available_mb, recorded_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        records
    )
    conn.commit()
    conn.close()


def get_health_history(ip: str, start: datetime, end: datetime, limit: int = 500) -> List[Dict]:
    """Get health history for a device in a time range."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ip, status, response_time, app_status, cache_mb, "
        "wifi_rssi, wifi_link_speed, cpu_usage, ram_usage_percent, ram_total_mb, ram_available_mb, "
        "recorded_at "
        "FROM device_health_history WHERE ip = ? AND recorded_at BETWEEN ? AND ? "
        "ORDER BY recorded_at DESC LIMIT ?",
        (ip, start, end, limit)
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_health_summary(ip: str, start: datetime, end: datetime) -> Dict:
    """Get uptime summary for a device."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN status = 'online' THEN 1 ELSE 0 END) as online_count, "
        "SUM(CASE WHEN status = 'offline' THEN 1 ELSE 0 END) as offline_count, "
        "AVG(response_time) as avg_response_time, "
        "AVG(wifi_rssi) as avg_wifi_rssi, "
        "AVG(cpu_usage) as avg_cpu_usage, "
        "AVG(ram_usage_percent) as avg_ram_usage "
        "FROM device_health_history WHERE ip = ? AND recorded_at BETWEEN ? AND ?",
        (ip, start, end)
    )
    row = cursor.fetchone()
    conn.close()
    total = row["total"] or 0
    online = row["online_count"] or 0
    return {
        "ip": ip,
        "total_checks": total,
        "online_count": online,
        "offline_count": row["offline_count"] or 0,
        "uptime_percent": round(online / total * 100, 1) if total > 0 else 0,
        "avg_response_time": round(row["avg_response_time"] or 0, 1),
        "avg_wifi_rssi": round(row["avg_wifi_rssi"] or 0) if row["avg_wifi_rssi"] else None,
        "avg_cpu_usage": round(row["avg_cpu_usage"] or 0, 1) if row["avg_cpu_usage"] else None,
        "avg_ram_usage": round(row["avg_ram_usage"] or 0, 1) if row["avg_ram_usage"] else None,
    }


def get_all_devices_health_summary(start: datetime, end: datetime) -> List[Dict]:
    """Get uptime summary for all devices with device name and location."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT h.ip, COUNT(*) as total, "
        "SUM(CASE WHEN h.status = 'online' THEN 1 ELSE 0 END) as online_count, "
        "SUM(CASE WHEN h.status = 'offline' THEN 1 ELSE 0 END) as offline_count, "
        "AVG(h.response_time) as avg_response_time, "
        "AVG(h.wifi_rssi) as avg_wifi_rssi, "
        "AVG(h.cpu_usage) as avg_cpu_usage, "
        "AVG(h.ram_usage_percent) as avg_ram_usage, "
        "d.asset_name, d.default_location "
        "FROM device_health_history h "
        "LEFT JOIN device_inventory d ON h.ip = d.ip "
        "WHERE h.recorded_at BETWEEN ? AND ? "
        "GROUP BY h.ip ORDER BY h.ip",
        (start, end)
    )
    rows = cursor.fetchall()
    conn.close()
    result = []
    for r in rows:
        total = r["total"] or 0
        online = r["online_count"] or 0
        result.append({
            "ip": r["ip"],
            "name": r["asset_name"] or "",
            "location": r["default_location"] or "",
            "total_checks": total,
            "online_count": online,
            "offline_count": r["offline_count"] or 0,
            "uptime_percent": round(online / total * 100, 1) if total > 0 else 0,
            "avg_response_time": round(r["avg_response_time"] or 0, 1),
            "avg_wifi_rssi": round(r["avg_wifi_rssi"]) if r["avg_wifi_rssi"] else None,
            "avg_cpu_usage": round(r["avg_cpu_usage"], 1) if r["avg_cpu_usage"] else None,
            "avg_ram_usage": round(r["avg_ram_usage"], 1) if r["avg_ram_usage"] else None,
        })
    return result


def cleanup_health_history(retention_days: int = 30) -> int:
    """Delete health history older than retention_days."""
    conn = get_db()
    cursor = conn.cursor()
    cutoff = datetime.now() - timedelta(days=retention_days)
    cursor.execute("DELETE FROM device_health_history WHERE recorded_at < ?", (cutoff,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted > 0:
        logger.info(f"[DB] Cleaned up {deleted} health history records older than {retention_days} days")
    return deleted


# ============================================
# Backup & Export
# ============================================

REQUIRED_TABLES = {"users", "settings", "device_inventory"}


def backup_database(dest_path: str) -> bool:
    """Create a safe backup of the live database using SQLite backup API."""
    try:
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(dest_path)
        src.backup(dst)
        dst.close()
        src.close()
        logger.info(f"[DB] Backup created: {dest_path}")
        return True
    except Exception as e:
        logger.error(f"[DB] Backup failed: {e}")
        return False


def export_settings_json() -> Dict[str, str]:
    """Export all settings as a dictionary (excludes sensitive keys)."""
    SENSITIVE_EXPORT_KEYS = {"smtp_password", "interchat_token", "syno_chat_token"}
    all_settings = get_all_settings()
    return {k: v for k, v in all_settings.items() if k not in SENSITIVE_EXPORT_KEYS}


def import_settings_json(data: Dict[str, str]) -> int:
    """Import settings from a dictionary. Returns count of keys imported."""
    SKIP_KEYS = {"smtp_password", "interchat_token", "syno_chat_token"}
    count = 0
    for key, value in data.items():
        if key in SKIP_KEYS:
            continue
        set_setting(key, str(value))
        count += 1
    logger.info(f"[DB] Imported {count} settings")
    return count


def validate_backup_db(db_path: str) -> bool:
    """Check if a file is a valid app database with required tables."""
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}
        conn.close()
        return REQUIRED_TABLES.issubset(tables)
    except Exception:
        return False


def restore_database(src_path: str) -> bool:
    """Replace the current database with a backup file.
    Creates a safety backup before replacing.
    """
    if not validate_backup_db(src_path):
        logger.error("[DB] Restore failed: invalid database file")
        return False

    # Safety backup of current DB
    safety_path = DB_PATH + f".before_restore_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if not backup_database(safety_path):
        logger.error("[DB] Restore aborted: could not create safety backup")
        return False

    try:
        src = sqlite3.connect(src_path, timeout=30)
        dst = sqlite3.connect(DB_PATH, timeout=30)
        src.backup(dst)
        dst.close()
        src.close()
        logger.info(f"[DB] Database restored from {src_path}")
        return True
    except Exception as e:
        logger.error(f"[DB] Restore failed: {e}")
        return False


# Initialize database on import
init_db()
