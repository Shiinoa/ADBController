"""
Device management API routes
"""
from fastapi import APIRouter, Request, HTTPException
from typing import Optional, Any, Dict
import asyncio

from models import PingRequest, DeviceActionRequest, RenameRequest, DeviceImportRequest
from models import validate_ip_address
from adb_manager import adb_manager
from websocket_manager import ws_manager
from connection_checker import connection_checker
from services.device_inventory import load_devices, save_devices
from auth import require_authenticated_user
from database import (
    DEFAULT_DEVICE_PLANT_ID,
    DEFAULT_DEVICE_OWNER_USERNAME,
    DEVICE_CSV_FIELD_ORDER,
    get_plant_by_code,
    get_user_by_username,
)

router = APIRouter(prefix="/api/devices", tags=["devices"])


DEVICE_INPUT_FIELDS = ["Owner Username", "Plant ID", *DEVICE_CSV_FIELD_ORDER]


def _require_inventory_admin(request: Request) -> Dict[str, Any]:
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user

def _get_visible_devices(user: Dict[str, Any], search: Optional[str] = None) -> list[Dict[str, str]]:
    devices = adb_manager.get_devices_from_csv(search)
    if user.get("role") == "admin":
        return devices

    plant_code = (user.get("plant_code") or "").strip()
    return [
        device for device in devices
        if (device.get("Plant ID") or device.get("plant_id") or DEFAULT_DEVICE_PLANT_ID) == plant_code
    ]


def _get_visible_device_ips(user: Dict[str, Any]) -> set[str]:
    return {device.get("IP", "") for device in _get_visible_devices(user) if device.get("IP")}


def _assert_ip_access(user: Dict[str, Any], ips: list[str]):
    if user.get("role") == "admin":
        return
    allowed_ips = _get_visible_device_ips(user)
    denied = [ip for ip in ips if ip not in allowed_ips]
    if denied:
        raise HTTPException(status_code=403, detail=f"Device access denied: {', '.join(denied)}")


def _resolve_device_index(full_devices: list[Dict[str, str]], visible_devices: list[Dict[str, str]], index: int) -> int:
    if index < 0 or index >= len(visible_devices):
        raise HTTPException(status_code=404, detail="Invalid device index")

    target = visible_devices[index]
    target_id = str(target.get("id", "")).strip()
    target_ip = target.get("IP", "")
    for full_index, device in enumerate(full_devices):
        if target_id and str(device.get("id", "")).strip() == target_id:
            return full_index
        if target_ip and device.get("IP") == target_ip:
            return full_index
    raise HTTPException(status_code=404, detail="Device not found")


def _sanitize_device_payload(
    body: Dict[str, Any],
    current_user: Dict[str, Any],
    require_required_fields: bool = True,
) -> Dict[str, str]:
    """Normalize and validate legacy device payloads before persisting them."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid device payload")

    device: Dict[str, str] = {}
    for field in DEVICE_INPUT_FIELDS:
        raw_value = body.get(field)
        if field == "Plant ID" and raw_value in (None, ""):
            raw_value = body.get("plant_id")
        if field == "Owner Username" and raw_value in (None, ""):
            raw_value = body.get("owner_username")
        if raw_value is None:
            raw_value = ""
        device[field] = str(raw_value).strip()

    requested_owner = device["Owner Username"] or current_user.get("username") or DEFAULT_DEVICE_OWNER_USERNAME
    if not get_user_by_username(requested_owner):
        raise HTTPException(status_code=400, detail=f"Owner '{requested_owner}' does not exist")
    device["Owner Username"] = requested_owner

    requested_plant = device["Plant ID"] or current_user.get("plant_code") or DEFAULT_DEVICE_PLANT_ID
    if current_user.get("role") != "admin":
        requested_plant = current_user.get("plant_code") or DEFAULT_DEVICE_PLANT_ID
    if not get_plant_by_code(requested_plant):
        raise HTTPException(status_code=400, detail=f"Plant ID '{requested_plant}' does not exist")
    device["Plant ID"] = requested_plant

    if require_required_fields:
        if not device["Asset Name"]:
            raise HTTPException(status_code=400, detail="Asset Name is required")
        if not device["IP"]:
            raise HTTPException(status_code=400, detail="IP is required")
        if not device["Default Location"]:
            raise HTTPException(status_code=400, detail="Default Location is required")

    if device["IP"]:
        try:
            validate_ip_address(device["IP"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return device


@router.get("")
async def get_devices(request: Request, search: Optional[str] = None):
    """Get device list from CSV"""
    user = require_authenticated_user(request)
    devices = _get_visible_devices(user, search)
    return {"devices": devices, "total": len(devices)}


@router.get("/connected")
async def get_connected_devices(request: Request):
    """Get currently connected devices"""
    user = require_authenticated_user(request)
    connected = adb_manager.get_connected_ips()
    if user.get("role") != "admin":
        allowed_ips = _get_visible_device_ips(user)
        connected = [ip for ip in connected if ip in allowed_ips]
    return {"connected": connected, "total": len(connected)}


@router.post("/add")
async def add_device(request: Request, device: Dict[str, Any] = None):
    """Add a new device to CSV"""
    user = _require_inventory_admin(request)

    body = _sanitize_device_payload(await request.json(), user)
    devices = load_devices()

    new_ip = body.get('IP', '')
    if new_ip:
        for d in devices:
            if d.get('IP') == new_ip:
                return {"success": False, "message": f"Device with IP {new_ip} already exists"}

    devices.append(body)

    if save_devices(devices):
        return {"success": True, "message": "Device added successfully"}
    else:
        return {"success": False, "message": "Failed to save device"}


@router.put("/{index}")
async def update_device(request: Request, index: int):
    """Update an existing device in CSV"""
    user = _require_inventory_admin(request)

    body = _sanitize_device_payload(await request.json(), user)
    devices = load_devices()
    visible_devices = _get_visible_devices(user)
    full_index = _resolve_device_index(devices, visible_devices, index)

    new_ip = body.get('IP', '')
    if new_ip:
        for i, d in enumerate(devices):
            if i != full_index and d.get('IP') == new_ip:
                return {"success": False, "message": f"Device with IP {new_ip} already exists"}

    devices[full_index] = body

    if save_devices(devices):
        return {"success": True, "message": "Device updated successfully"}
    else:
        return {"success": False, "message": "Failed to save device"}


@router.delete("/{index}")
async def delete_device(request: Request, index: int):
    """Delete a device from CSV"""
    user = _require_inventory_admin(request)

    devices = load_devices()
    visible_devices = _get_visible_devices(user)
    full_index = _resolve_device_index(devices, visible_devices, index)
    deleted = devices.pop(full_index)

    if save_devices(devices):
        return {"success": True, "message": f"Device {deleted.get('Asset Name', '')} deleted"}
    else:
        return {"success": False, "message": "Failed to save changes"}


@router.post("/import")
async def import_devices(request: Request, data: DeviceImportRequest):
    """Import multiple devices from CSV data"""
    user = _require_inventory_admin(request)

    devices = load_devices()
    existing_ips = {d.get('IP') for d in devices if d.get('IP')}

    added = 0
    for index, device in enumerate(data.devices, start=1):
        try:
            sanitized_device = _sanitize_device_payload(device, user, require_required_fields=False)
        except HTTPException as exc:
            raise HTTPException(status_code=exc.status_code, detail=f"Import row {index}: {exc.detail}") from exc
        if not (sanitized_device.get('Asset Name') or sanitized_device.get('IP')):
            continue

        ip = sanitized_device.get('IP', '')
        if ip and ip in existing_ips:
            continue
        devices.append(sanitized_device)
        if ip:
            existing_ips.add(ip)
        added += 1

    if save_devices(devices):
        return {"success": True, "count": added, "message": f"Imported {added} devices"}
    else:
        return {"success": False, "message": "Failed to save imported devices"}


@router.post("/connect")
async def connect_devices(http_request: Request, request: PingRequest):
    """Connect to multiple devices"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    await ws_manager.log(f"Connecting {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def connect_single(ip: str):
        nonlocal completed_count
        result = await adb_manager.connect_device(ip)
        async with lock:
            completed_count += 1
            if result["success"]:
                await ws_manager.log(f"[{ip}] Connected & Authorized", "success")
            elif result.get("status") == "unauthorized":
                await ws_manager.log(f"[{ip}] UNAUTHORIZED - Check TV for authorization dialog!", "warning")
            elif result.get("status") == "offline":
                await ws_manager.log(f"[{ip}] Offline", "error")
            else:
                await ws_manager.log(f"[{ip}] Failed: {result.get('message', 'Unknown error')}", "error")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [connect_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete("connect", True, f"Connected {len([r for r in results if r.get('success')])}/{len(ips)} devices")
    return {"results": results}


@router.post("/ping")
async def ping_devices(http_request: Request, request: PingRequest):
    """Check multiple devices using fast TCP + ADB connect (parallel)"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    await ws_manager.log(f"Checking {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def check_single(ip: str):
        nonlocal completed_count
        # Use fast TCP port check + ADB connect (much faster for offline devices)
        check_result = await adb_manager.fast_connect_and_check(ip)
        is_online = check_result["online"]
        status = check_result["status"]

        async with lock:
            completed_count += 1
            display_status = status.capitalize()
            level = "success" if is_online else ("warning" if status == "unauthorized" else "error")
            await ws_manager.log(f"[{ip}] {display_status}", level)
            await ws_manager.progress_update(completed_count, len(ips), ip)

        return {"ip": ip, "status": status, "online": is_online}

    tasks = [check_single(ip) for ip in ips]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid_results = [r for r in results if isinstance(r, dict)]

    online_count = len([r for r in valid_results if r.get("online")])
    await ws_manager.task_complete("ping", True, f"{online_count}/{len(ips)} online")
    return {"results": valid_results}


@router.post("/health")
async def health_check(http_request: Request, request: PingRequest):
    """Health check for multiple devices"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    await ws_manager.log(f"Starting Health Check for {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def check_single(ip: str):
        nonlocal completed_count
        try:
            result = await adb_manager.health_check(ip)
        except Exception as e:
            result = {"ip": ip, "status": "error", "network_type": "unknown", "network_status": "error", "rssi": None, "ram_mb": 0, "storage_free_gb": 0, "action": None, "message": str(e)}
        async with lock:
            completed_count += 1
            net_status = f"{result['network_type']} ({result['network_status']})"
            if result['rssi']:
                net_status += f" RSSI: {result['rssi']}"
            await ws_manager.log(f"[{ip}] Network: {net_status} | RAM: {result['ram_mb']:.0f}MB | Storage: {result['storage_free_gb']:.2f}GB | Status: {result['status']}")
            if result['action']:
                await ws_manager.log(f"[{ip}] ACTION: {result['action']}", "warning")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [check_single(ip) for ip in ips]
    results = await asyncio.gather(*tasks)

    # Sync successful results to dashboard cache
    online_ips = [r["ip"] for r in results if r.get("status") not in ("error",)]
    if online_ips:
        await connection_checker.mark_online(online_ips)

    await ws_manager.task_complete("health", True, "Health Check Finished")
    return {"results": results}


@router.post("/action")
async def device_action(http_request: Request, request: DeviceActionRequest):
    """Execute action on multiple devices"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    mode = request.mode
    await ws_manager.log(f"Executing '{mode}' on {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def action_single(ip: str):
        nonlocal completed_count
        result = {"ip": ip, "mode": mode, "success": False}
        log_msg = ""
        log_level = "info"

        if mode == "wake":
            result = await adb_manager.wake_device(ip)
            log_msg = f"[{ip}] Wake command sent"
        elif mode == "sleep":
            result = await adb_manager.sleep_device(ip)
            log_msg = f"[{ip}] Sleep command sent"
        elif mode == "dim":
            result = await adb_manager.set_brightness(ip, 1)
            log_msg = f"[{ip}] Brightness set to 1%"
        elif mode == "bright":
            result = await adb_manager.set_brightness(ip, 255)
            log_msg = f"[{ip}] Brightness set to 100%"
        elif mode == "check_screen":
            result = await adb_manager.check_screen_status(ip)
            status = "ON" if result.get("screen_on") else "OFF"
            log_msg = f"[{ip}] Screen is {status}"
        elif mode == "check_memory":
            result = await adb_manager.check_memory(ip)
            log_msg = f"[{ip}] RAM: {result.get('ram', 'N/A')} | Storage: {result.get('storage', 'N/A')}"
        elif mode == "full_info":
            result = await adb_manager.get_full_info(ip)
            log_msg = f"[{ip}] Name: {result.get('device_name')} | Model: {result.get('model')} | MAC: {result.get('mac')}"
        elif mode == "screenshot":
            result = await adb_manager.take_screenshot(ip)
            if result.get("success"):
                log_msg = f"[{ip}] Screenshot saved: {result.get('path')}"
                log_level = "success"
            else:
                log_msg = f"[{ip}] Screenshot failed: {result.get('message', 'Unknown error')}"
                log_level = "error"

        async with lock:
            completed_count += 1
            if log_msg:
                await ws_manager.log(log_msg, log_level)
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [action_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete(mode, True, f"'{mode}' completed")
    return {"results": results}


@router.post("/rename")
async def rename_devices(http_request: Request, request: RenameRequest):
    """Rename multiple devices"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    new_name = request.new_name
    await ws_manager.log(f"Renaming {len(ips)} devices to '{new_name}'...")

    completed_count = 0
    lock = asyncio.Lock()

    async def rename_single(ip: str):
        nonlocal completed_count
        result = await adb_manager.rename_device(ip, new_name)
        async with lock:
            completed_count += 1
            await ws_manager.log(f"[{ip}] Renamed to '{result['new_name']}'. Reboot recommended.")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [rename_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete("rename", True, "Rename completed")
    return {"results": results}


@router.post("/reboot")
async def reboot_devices(http_request: Request, request: PingRequest):
    """Reboot multiple devices"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    await ws_manager.log(f"Rebooting {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def reboot_single(ip: str):
        nonlocal completed_count
        result = await adb_manager.reboot_device(ip)
        async with lock:
            completed_count += 1
            await ws_manager.log(f"[{ip}] Reboot command sent")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [reboot_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete("reboot", True, "Reboot commands sent")
    return {"results": results}


@router.post("/shutdown")
async def shutdown_devices(http_request: Request, request: PingRequest):
    """Shutdown multiple devices"""
    user = require_authenticated_user(http_request)
    _assert_ip_access(user, request.ips)
    ips = request.ips
    await ws_manager.log(f"Shutting down {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def shutdown_single(ip: str):
        nonlocal completed_count
        result = await adb_manager.shutdown_device(ip)
        async with lock:
            completed_count += 1
            await ws_manager.log(f"[{ip}] Shutdown command sent")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [shutdown_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete("shutdown", True, "Shutdown commands sent")
    return {"results": results}


# ============== Device Locks ==============

from database import (
    acquire_device_lock, release_device_lock, get_device_lock,
    get_all_device_locks, extend_device_lock
)
import socket


def get_hostname():
    """Get current machine hostname"""
    try:
        return socket.gethostname()
    except (socket.error, OSError):
        return "unknown"


@router.get("/locks")
async def list_device_locks(request: Request):
    """Get all active device locks"""
    require_authenticated_user(request)
    locks = get_all_device_locks()
    return {"locks": locks, "total": len(locks)}


@router.get("/locks/{ip}")
async def check_device_lock(request: Request, ip: str):
    """Check if a specific device is locked"""
    user = require_authenticated_user(request)
    _assert_ip_access(user, [ip])
    lock = get_device_lock(ip)
    return {
        "ip": ip,
        "locked": lock is not None,
        "lock": lock
    }


@router.post("/locks/{ip}")
async def lock_device(request: Request, ip: str):
    """Acquire a lock on a device before using scrcpy"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_ip_access(user, [ip])

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    lock_type = body.get("lock_type", "scrcpy")
    duration = body.get("duration_minutes", 30)

    hostname = get_hostname()
    locked_by = f"{user['username']}@{hostname}"

    result = acquire_device_lock(ip, locked_by, lock_type, hostname, duration)
    return result


@router.delete("/locks/{ip}")
async def unlock_device(request: Request, ip: str):
    """Release a lock on a device"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_ip_access(user, [ip])

    hostname = get_hostname()
    locked_by = f"{user['username']}@{hostname}"

    # Allow admins to release any lock
    if user.get("role") == "admin":
        result = release_device_lock(ip)
    else:
        result = release_device_lock(ip, locked_by)

    return result


@router.post("/locks/{ip}/extend")
async def extend_lock(request: Request, ip: str):
    """Extend an existing lock"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_ip_access(user, [ip])

    body = await request.json() if request.headers.get("content-type") == "application/json" else {}
    additional_minutes = body.get("minutes", 30)

    hostname = get_hostname()
    locked_by = f"{user['username']}@{hostname}"

    result = extend_device_lock(ip, locked_by, additional_minutes)
    return result


@router.post("/locks/{ip}/force-release")
async def force_release_lock(request: Request, ip: str):
    """Force release a lock (admin only)"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    result = release_device_lock(ip)
    return result
