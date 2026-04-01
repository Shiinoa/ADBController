"""
Rebuild the application SQLite database from the current schema and
best-effort recover any readable data from the old database/backups.

Usage:
    py webapp/scripts/rebuild_database.py
"""
import csv
import importlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
WEBAPP_DIR = SCRIPT_DIR.parent
BACKUP_ROOT = WEBAPP_DIR / "db_backups"
DB_PATH = WEBAPP_DIR / "app_data.db"
JOURNAL_PATH = WEBAPP_DIR / "app_data.db-journal"
CREDENTIALS_PATH = WEBAPP_DIR / ".admin_credentials"
TEMPLATES_BACKUP_PATH = WEBAPP_DIR / "report_templates.json.backup"
DB_POINTER_PATH = WEBAPP_DIR / ".db_path"

TABLES_TO_RECOVER = [
    "users",
    "settings",
    "ping_status",
    "alert_logs",
    "daily_reports",
    "sessions",
    "report_templates",
    "device_locks",
    "automation_workflows",
    "automation_logs",
]


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return cleaned[:64] or "plant"


def backup_existing_files(timestamp: str) -> Tuple[Path, Optional[Path], Optional[Path], bool]:
    backup_dir = BACKUP_ROOT / f"rebuild_{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    db_backup = None
    journal_backup = None
    moved_original = True

    if DB_PATH.exists():
        db_backup = backup_dir / DB_PATH.name
        try:
            shutil.move(str(DB_PATH), str(db_backup))
        except PermissionError:
            shutil.copy2(str(DB_PATH), str(db_backup))
            moved_original = False

    if JOURNAL_PATH.exists():
        journal_backup = backup_dir / JOURNAL_PATH.name
        try:
            shutil.move(str(JOURNAL_PATH), str(journal_backup))
        except PermissionError:
            shutil.copy2(str(JOURNAL_PATH), str(journal_backup))
            moved_original = False

    if CREDENTIALS_PATH.exists():
        shutil.copy2(str(CREDENTIALS_PATH), str(backup_dir / CREDENTIALS_PATH.name))

    return backup_dir, db_backup, journal_backup, moved_original


def load_existing_admin_password() -> Optional[str]:
    if not CREDENTIALS_PATH.exists():
        return None

    try:
        for line in CREDENTIALS_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.lower().startswith("password:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None


def import_database_module():
    sys.path.insert(0, str(WEBAPP_DIR))
    if "database" in sys.modules:
        del sys.modules["database"]
    return importlib.import_module("database")


def can_open_sqlite(db_path: Path) -> Tuple[bool, str]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' LIMIT 1")
        cur.fetchall()
        conn.close()
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def get_table_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    return [row[1] for row in cur.fetchall()]


def open_writable_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = MEMORY")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


def recover_table_rows(old_db_path: Path, table_name: str) -> Tuple[List[Dict], Optional[str]]:
    try:
        conn = sqlite3.connect(f"file:{old_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table_name}")
        rows = [dict(row) for row in cur.fetchall()]
        conn.close()
        return rows, None
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def insert_rows(new_db_path: Path, table_name: str, rows: List[Dict]) -> int:
    if not rows:
        return 0

    conn = open_writable_db(new_db_path)
    cur = conn.cursor()
    table_columns = get_table_columns(conn, table_name)
    inserted = 0

    for row in rows:
        filtered = {key: value for key, value in row.items() if key in table_columns}
        if not filtered:
            continue
        columns = list(filtered.keys())
        placeholders = ", ".join(["?"] * len(columns))
        quoted_columns = ", ".join(columns)
        values = [filtered[column] for column in columns]
        cur.execute(
            f"INSERT OR REPLACE INTO {table_name} ({quoted_columns}) VALUES ({placeholders})",
            values
        )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def recover_templates_from_backup(new_db_path: Path) -> int:
    if not TEMPLATES_BACKUP_PATH.exists():
        return 0

    try:
        templates = json.loads(TEMPLATES_BACKUP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return 0

    if not isinstance(templates, list):
        return 0

    conn = open_writable_db(new_db_path)
    cur = conn.cursor()
    inserted = 0
    now = datetime.now().isoformat()

    for item in templates:
        if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
            continue
        cur.execute('''
            INSERT OR REPLACE INTO report_templates
            (id, name, description, elements, settings, created_at, created_by, updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item.get("id"),
            item.get("name"),
            item.get("description", ""),
            json.dumps(item.get("elements", [])),
            json.dumps(item.get("settings", {})),
            item.get("created_at") or now,
            item.get("created_by", "rebuild_script"),
            item.get("updated_at"),
            item.get("updated_by"),
        ))
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


def recover_plants_from_csv(new_db_path: Path) -> int:
    try:
        config = importlib.import_module("config")
    except Exception:
        return 0

    csv_path = Path(config.CSV_PATH)
    if not csv_path.exists():
        return 0

    discovered = {}
    try:
        with csv_path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not row:
                    continue
                plant_name = (row.get("Plant") or row.get("Project") or "").strip()
                if not plant_name:
                    continue
                discovered.setdefault(plant_name, {
                    "code": slugify(plant_name),
                    "name": plant_name,
                    "location": (row.get("Default Location") or "").strip(),
                    "timezone": "Asia/Bangkok",
                    "description": "Recovered from device CSV",
                    "is_active": 1,
                    "created_at": datetime.now().isoformat(),
                    "updated_at": datetime.now().isoformat(),
                })
    except Exception:
        return 0

    if not discovered:
        return 0

    conn = open_writable_db(new_db_path)
    cur = conn.cursor()
    inserted = 0
    for plant in discovered.values():
        cur.execute('''
            INSERT OR IGNORE INTO plants
            (code, name, location, timezone, description, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            plant["code"],
            plant["name"],
            plant["location"],
            plant["timezone"],
            plant["description"],
            plant["is_active"],
            plant["created_at"],
            plant["updated_at"],
        ))
        inserted += cur.rowcount

    conn.commit()
    conn.close()
    return inserted


def write_report(backup_dir: Path, report: Dict):
    report_path = backup_dir / "rebuild_report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")


def prepare_target_db(target_db_path: Path, backup_dir: Path):
    target_journal = target_db_path.parent / f"{target_db_path.name}-journal"

    if target_db_path.exists():
        try:
            shutil.move(str(target_db_path), str(backup_dir / f"{target_db_path.name}.pre_rebuild"))
        except PermissionError:
            shutil.copy2(str(target_db_path), str(backup_dir / f"{target_db_path.name}.pre_rebuild"))
    if target_journal.exists():
        try:
            shutil.move(str(target_journal), str(backup_dir / f"{target_journal.name}.pre_rebuild"))
        except PermissionError:
            shutil.copy2(str(target_journal), str(backup_dir / f"{target_journal.name}.pre_rebuild"))


def main():
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    admin_password = load_existing_admin_password()
    if admin_password and not os.environ.get("ADB_ADMIN_PASSWORD"):
        os.environ["ADB_ADMIN_PASSWORD"] = admin_password

    backup_dir, old_db_backup, old_journal_backup, moved_original = backup_existing_files(timestamp)

    target_db_path = DB_PATH if moved_original else (WEBAPP_DIR / f"app_data_rebuilt_{timestamp}.db")
    os.environ["WEBAPP_DB_PATH"] = str(target_db_path)
    prepare_target_db(target_db_path, backup_dir)

    database = import_database_module()
    database.init_db()
    DB_POINTER_PATH.write_text(str(target_db_path), encoding="utf-8")

    report = {
        "timestamp": timestamp,
        "new_db_path": str(target_db_path),
        "backup_dir": str(backup_dir),
        "old_db_backup": str(old_db_backup) if old_db_backup else None,
        "old_journal_backup": str(old_journal_backup) if old_journal_backup else None,
        "db_pointer_path": str(DB_POINTER_PATH),
        "used_rebuilt_db_path": target_db_path != DB_PATH,
        "recovered_tables": {},
        "warnings": [],
    }

    if not moved_original:
        report["warnings"].append(
            "Original app_data.db was locked; created app_data_rebuilt.db instead and configured WEBAPP_DB_PATH during rebuild"
        )

    if old_db_backup and old_db_backup.exists():
        readable, error = can_open_sqlite(old_db_backup)
        report["old_db_readable"] = readable
        if not readable and error:
            report["warnings"].append(f"Old DB not readable: {error}")

        if readable:
            for table_name in TABLES_TO_RECOVER:
                rows, read_error = recover_table_rows(old_db_backup, table_name)
                if read_error:
                    report["recovered_tables"][table_name] = {
                        "readable": False,
                        "recovered_rows": 0,
                        "error": read_error,
                    }
                    continue

                inserted = insert_rows(target_db_path, table_name, rows)
                report["recovered_tables"][table_name] = {
                    "readable": True,
                    "recovered_rows": inserted,
                    "source_rows": len(rows),
                }
    else:
        report["old_db_readable"] = False
        report["warnings"].append("No previous app_data.db found to recover from")

    if report["recovered_tables"].get("report_templates", {}).get("recovered_rows", 0) == 0:
        template_count = recover_templates_from_backup(target_db_path)
        if template_count:
            report["recovered_tables"]["report_templates_json_backup"] = {
                "readable": True,
                "recovered_rows": template_count,
            }

    plant_count = recover_plants_from_csv(target_db_path)
    report["recovered_tables"]["plants_from_csv"] = {
        "readable": plant_count > 0,
        "recovered_rows": plant_count,
    }

    write_report(backup_dir, report)

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
