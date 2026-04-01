"""
SQLite-backed device inventory service.
"""
from typing import Dict, List

try:
    from database import get_all_devices, replace_all_devices
except ImportError:
    from ..database import get_all_devices, replace_all_devices

_device_cache: List[Dict] = []


def invalidate_device_cache():
    """Force in-memory cache invalidation."""
    global _device_cache
    _device_cache = []


def load_devices(owner_username: str = None) -> List[Dict]:
    """Load devices from SQLite inventory using legacy-compatible keys."""
    global _device_cache
    if not _device_cache:
        _device_cache = get_all_devices()

    if not owner_username:
        return list(_device_cache)

    return [device for device in _device_cache if device.get("Owner Username") == owner_username]


def save_devices(devices: List[Dict]) -> bool:
    """Persist the complete device inventory to SQLite."""
    success = replace_all_devices(devices)
    if success:
        invalidate_device_cache()
    return success
