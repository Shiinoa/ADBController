"""
Device CSV file operations — with mtime-based cache
"""
import csv
import os
from typing import List, Dict, Optional
from config import CSV_PATH

# ============================================
# In-memory CSV cache (invalidated on file change)
# ============================================
_csv_cache: List[Dict] = []
_csv_mtime: float = 0.0


def _get_file_mtime() -> float:
    """Get CSV file modification time (0 if missing)"""
    try:
        return os.path.getmtime(CSV_PATH) if CSV_PATH and os.path.exists(CSV_PATH) else 0.0
    except OSError:
        return 0.0


def invalidate_csv_cache():
    """Force cache invalidation (call after writes)"""
    global _csv_cache, _csv_mtime
    _csv_cache = []
    _csv_mtime = 0.0


def load_devices_from_csv() -> List[Dict]:
    """Load all devices from CSV file (cached by mtime)"""
    global _csv_cache, _csv_mtime

    if not CSV_PATH or not os.path.exists(CSV_PATH):
        return []

    current_mtime = _get_file_mtime()
    if _csv_cache and current_mtime == _csv_mtime:
        return list(_csv_cache)

    # Cache miss or file changed — reload
    devices = []
    try:
        with open(CSV_PATH, mode='r', encoding='utf-8-sig', errors='ignore') as f:
            reader = csv.DictReader(f)
            for row in reader:
                devices.append({k.strip(): v.strip() if v else '' for k, v in row.items() if k})
    except Exception as e:
        print(f"[Devices] Error loading CSV: {e}")
        return devices

    _csv_cache = devices
    _csv_mtime = current_mtime
    return list(_csv_cache)


def save_devices_to_csv(devices: List[Dict]) -> bool:
    """Save devices to CSV file (invalidates cache)"""
    if not CSV_PATH:
        return False

    fieldnames = ['Asset Name', 'Asset Tag', 'IP', 'MAC Address', 'Model', 'Category',
                  'Manufacturer', 'Serial', 'Default Location', 'Project', 'Work Center', 'Monotor']

    try:
        with open(CSV_PATH, mode='w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            for device in devices:
                writer.writerow(device)
        invalidate_csv_cache()
        return True
    except Exception as e:
        print(f"[Devices] Error saving CSV: {e}")
        return False
