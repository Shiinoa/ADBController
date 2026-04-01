"""
Configuration & Auto-Path Finder for ADB Control Center Web App
Supports both Windows and Linux/Ubuntu
"""
import os
import sys
import shutil

# Detect operating system
IS_WINDOWS = os.name == 'nt'
IS_LINUX = os.name == 'posix'

# Executable extensions based on OS
EXE_EXT = '.exe' if IS_WINDOWS else ''

# Get current directory
if getattr(sys, 'frozen', False):
    CURRENT_DIR = os.path.dirname(sys.executable)
else:
    CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

# Base directory (parent of webapp)
BASE_DIR = os.path.dirname(CURRENT_DIR)

# ============================================
# ADB Path Detection
# ============================================
ADB_PATH = None
SCRCPY_PATH = None
SCRCPY_SERVER_PATH = None

# Build possible ADB locations based on OS
POSSIBLE_ADB_LOCATIONS = []

if IS_WINDOWS:
    # Windows locations
    POSSIBLE_ADB_LOCATIONS = [
        os.path.join(BASE_DIR, "scrcpy", "scrcpy-win64-v3.2", "adb.exe"),
        os.path.join(BASE_DIR, "scrcpy", "ADB platform-tools", "adb.exe"),
        os.path.join(CURRENT_DIR, "adb.exe"),
        os.path.join(CURRENT_DIR, "bin", "adb.exe"),
        os.path.join(os.environ.get('LOCALAPPDATA', ''), "Android", "Sdk", "platform-tools", "adb.exe"),
        os.path.join(os.environ.get('PROGRAMFILES', ''), "Android", "android-sdk", "platform-tools", "adb.exe"),
    ]
else:
    # Linux/Ubuntu locations
    POSSIBLE_ADB_LOCATIONS = [
        "/usr/bin/adb",                              # apt install android-tools-adb
        "/usr/local/bin/adb",
        os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        os.path.expanduser("~/.android/sdk/platform-tools/adb"),
        "/opt/android-sdk/platform-tools/adb",
        os.path.join(CURRENT_DIR, "adb"),
        os.path.join(CURRENT_DIR, "bin", "adb"),
        os.path.join(BASE_DIR, "platform-tools", "adb"),
    ]

# Environment variable override (for Docker)
_env_adb = os.environ.get('ADB_PATH')
if _env_adb and os.path.exists(_env_adb):
    ADB_PATH = _env_adb
else:
    # Try to find ADB in PATH first (works on both OS)
    adb_in_path = shutil.which('adb')
    if adb_in_path:
        ADB_PATH = adb_in_path
    else:
        # Search in predefined locations
        for path in POSSIBLE_ADB_LOCATIONS:
            if path and os.path.exists(path):
                ADB_PATH = path
                break

# ============================================
# Scrcpy Path Detection
# ============================================
POSSIBLE_SCRCPY_LOCATIONS = []

if IS_WINDOWS:
    POSSIBLE_SCRCPY_LOCATIONS = [
        os.path.join(BASE_DIR, "scrcpy", "scrcpy-win64-v3.2", "scrcpy.exe"),
        os.path.join(CURRENT_DIR, "scrcpy.exe"),
    ]
else:
    POSSIBLE_SCRCPY_LOCATIONS = [
        "/usr/bin/scrcpy",                           # apt install scrcpy
        "/usr/local/bin/scrcpy",
        "/snap/bin/scrcpy",                          # snap install scrcpy
        os.path.expanduser("~/.local/bin/scrcpy"),
        os.path.join(CURRENT_DIR, "scrcpy"),
    ]

# Environment variable override (for Docker)
_env_scrcpy = os.environ.get('SCRCPY_PATH')
if _env_scrcpy and os.path.exists(_env_scrcpy):
    SCRCPY_PATH = _env_scrcpy
    SCRCPY_SERVER_PATH = os.path.join(os.path.dirname(_env_scrcpy), "scrcpy-server")
else:
    # Try to find scrcpy in PATH first
    scrcpy_in_path = shutil.which('scrcpy')
    if scrcpy_in_path:
        SCRCPY_PATH = scrcpy_in_path
        SCRCPY_SERVER_PATH = os.path.join(os.path.dirname(scrcpy_in_path), "scrcpy-server")
    else:
        for path in POSSIBLE_SCRCPY_LOCATIONS:
            if path and os.path.exists(path):
                SCRCPY_PATH = path
                SCRCPY_SERVER_PATH = os.path.join(os.path.dirname(path), "scrcpy-server")
                break

# ============================================
# CSV File Path Detection
# ============================================
CSV_PATH = os.environ.get('CSV_PATH') or None
POSSIBLE_CSV_NAMES = ["devices_data.csv", "devices.csv", "device.csv", "list.csv", "data.csv"]

# Search directories
SEARCH_DIRS = [
    CURRENT_DIR,
    os.path.join(BASE_DIR, "scrcpy", "scrcpy-win64-v3.2") if IS_WINDOWS else BASE_DIR,
    BASE_DIR,
]

if not CSV_PATH:
    for search_dir in SEARCH_DIRS:
        for name in POSSIBLE_CSV_NAMES:
            temp = os.path.join(search_dir, name)
            if os.path.exists(temp):
                CSV_PATH = temp
                break
        if CSV_PATH:
            break

# Default CSV path if not found
if not CSV_PATH:
    CSV_PATH = os.path.join(CURRENT_DIR, "devices_data.csv")

# ============================================
# Output Directories
# ============================================
SCREENSHOT_DIR = os.path.join(CURRENT_DIR, "screenshots")
REPORT_DIR = os.path.join(CURRENT_DIR, "reports")
DOCUMENT_DIR = os.path.join(CURRENT_DIR, "documents")

# Create output directories if they don't exist
for d in [SCREENSHOT_DIR, REPORT_DIR, DOCUMENT_DIR]:
    if not os.path.exists(d):
        os.makedirs(d)

# ============================================
# App Monitoring Settings
# ============================================

# Default app package to monitor
DEFAULT_APP_PACKAGE = "asd.kce.machinemonitor"

# Cache alert threshold in MB (alert when cache exceeds this)
CACHE_ALERT_THRESHOLD_MB = 100

# Health check thresholds
RAM_LOW_THRESHOLD_MB = 300      # Alert when free RAM below this
STORAGE_LOW_THRESHOLD_GB = 0.3  # Alert when free storage below this

# Screenshot cleanup settings
SCREENSHOT_RETENTION_HOURS = 24  # Delete screenshot folders older than this
SCREENSHOT_KEEP_MIN = 5         # Always keep at least this many folders

# ============================================
# Utility Functions
# ============================================

def print_config():
    """Print configuration status for debugging"""
    print("-" * 60)
    print(f"Operating System: {'Windows' if IS_WINDOWS else 'Linux/Unix'}")
    print(f"Working Directory: {CURRENT_DIR}")
    print(f"Base Directory: {BASE_DIR}")
    print(f"ADB Path: {ADB_PATH or 'NOT FOUND'}")
    print(f"  -> Exists: {os.path.exists(ADB_PATH) if ADB_PATH else False}")
    print(f"Scrcpy Path: {SCRCPY_PATH or 'NOT FOUND'}")
    print(f"  -> Exists: {os.path.exists(SCRCPY_PATH) if SCRCPY_PATH else False}")
    print(f"CSV Path: {CSV_PATH}")
    print(f"Screenshot Dir: {SCREENSHOT_DIR}")
    print(f"Report Dir: {REPORT_DIR}")
    print("-" * 60)


def check_dependencies():
    """Check if required dependencies are installed"""
    issues = []

    if not ADB_PATH or not os.path.exists(ADB_PATH):
        if IS_LINUX:
            issues.append("ADB not found. Install with: sudo apt install android-tools-adb")
        else:
            issues.append("ADB not found. Download Android SDK Platform Tools")

    return issues


if __name__ == "__main__":
    print_config()

    issues = check_dependencies()
    if issues:
        print("\nDependency Issues:")
        for issue in issues:
            print(f"  - {issue}")
