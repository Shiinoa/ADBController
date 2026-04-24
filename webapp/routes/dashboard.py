"""
Dashboard API routes
"""
from fastapi import APIRouter, Request
from datetime import datetime
from typing import Optional, List
import asyncio
import time

from models import PingRequest
from adb_manager import adb_manager
from database import log_ping
from connection_checker import connection_checker
from auth import require_authenticated_user, require_admin_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.post("/ping")
async def dashboard_ping(http_request: Request, request: PingRequest):
    """Check device connectivity using ADB connect"""
    require_authenticated_user(http_request)
    results = {}

    async def check_single(ip: str):
        """Check single device via fast TCP + ADB connect"""
        try:
            start_time = time.time()
            # Fast connect and check (TCP port check -> ADB connect -> status) in one call
            check_result = await adb_manager.fast_connect_and_check(ip)
            response_time = (time.time() - start_time) * 1000

            is_online = check_result["online"]
            status = check_result["status"]

            result = {
                "online": is_online,
                "status": status,
                "response_time": response_time if is_online else None,
                "checked_at": datetime.now().isoformat()
            }

            log_ping(ip, status, response_time if is_online else None)
            return ip, result
        except asyncio.CancelledError:
            return ip, {"online": False, "status": "cancelled", "response_time": None, "checked_at": datetime.now().isoformat()}
        except Exception as e:
            return ip, {"online": False, "status": "error", "response_time": None, "checked_at": datetime.now().isoformat(), "error": str(e)}

    try:
        tasks = [check_single(ip) for ip in request.ips]
        completed = await asyncio.gather(*tasks, return_exceptions=True)

        for item in completed:
            if isinstance(item, tuple):
                ip, result = item
                results[ip] = result
            elif isinstance(item, Exception):
                print(f"[ADB Check] Error: {item}")

    except asyncio.CancelledError:
        print("[ADB Check] Request cancelled, returning partial results")

    return {"results": results}


# ============================================
# Cached Status Endpoints (Background Checker)
# ============================================

@router.get("/status")
async def get_cached_status(request: Request):
    """
    Get cached connection status for all devices.
    This is FAST because it reads from cache (no ADB calls).
    Updated automatically every 30 seconds by background checker.
    """
    user = require_authenticated_user(request)
    status = await connection_checker.get_all_status()
    if user.get("role") == "admin":
        return status

    visible_ips = {
        device.get("IP")
        for device in adb_manager.get_devices_from_csv()
        if device.get("IP")
        and (device.get("Plant ID") or device.get("plant_id")) == user.get("plant_code")
    }
    filtered_devices = {
        ip: device_status
        for ip, device_status in status.get("devices", {}).items()
        if ip in visible_ips
    }
    return {
        **status,
        "devices": filtered_devices,
        "total": len(filtered_devices),
    }


@router.get("/status/{ip}")
async def get_device_status(request: Request, ip: str):
    """Get cached status for a single device"""
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        visible_ips = {
            device.get("IP")
            for device in adb_manager.get_devices_from_csv()
            if device.get("IP")
            and (device.get("Plant ID") or device.get("plant_id")) == user.get("plant_code")
        }
        if ip not in visible_ips:
            return {"ip": ip, "online": None, "status": "forbidden", "message": "Device access denied"}
    status = await connection_checker.get_device_status(ip)
    if status:
        return status
    return {"ip": ip, "online": None, "status": "unknown", "message": "Device not in cache"}


@router.post("/status/refresh")
async def refresh_status(http_request: Request, request: Optional[PingRequest] = None):
    """
    Manually trigger a status check.
    If IPs provided, check only those devices.
    Otherwise, check all devices.
    """
    user = require_authenticated_user(http_request)
    ips = request.ips if request else None
    if ips and user.get("role") != "admin":
        visible_ips = {
            device.get("IP")
            for device in adb_manager.get_devices_from_csv()
            if device.get("IP")
            and (device.get("Plant ID") or device.get("plant_id")) == user.get("plant_code")
        }
        denied = [ip for ip in ips if ip not in visible_ips]
        if denied:
            return {"success": False, "message": f"Device access denied: {', '.join(denied)}"}
    await connection_checker.trigger_check(ips)
    return {"success": True, "message": "Status refresh triggered"}


@router.get("/checker/info")
async def get_checker_info(request: Request):
    """Get connection checker service info"""
    require_authenticated_user(request)
    return {
        "running": connection_checker.is_running,
        "check_interval": connection_checker.check_interval,
        "monitored_devices": connection_checker.monitored_count
    }


@router.post("/checker/interval")
async def set_checker_interval(request: Request, interval: int):
    """Set check interval (10-300 seconds)"""
    require_admin_user(request)
    connection_checker.set_check_interval(interval)
    return {
        "success": True,
        "check_interval": connection_checker.check_interval
    }


@router.get("/reconnect/status")
async def get_reconnect_status(request: Request):
    """Get auto-reconnect state for all devices."""
    require_authenticated_user(request)
    return {"devices": connection_checker.get_reconnect_status()}


@router.post("/reconnect/reset/{ip}")
async def reset_reconnect(request: Request, ip: str):
    """Reset circuit breaker for a device."""
    require_admin_user(request)
    await connection_checker.reset_reconnect_state(ip)
    return {"success": True, "message": f"Reconnect state reset for {ip}"}
