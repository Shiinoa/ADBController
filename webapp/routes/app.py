"""
App management API routes
"""
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException
from typing import List
import asyncio
import os
import tempfile
import zipfile

from models import AppRequest, validate_ip_address
from adb_manager import adb_manager
from websocket_manager import ws_manager
from connection_checker import connection_checker
from config import CURRENT_DIR, CACHE_ALERT_THRESHOLD_MB
from auth import require_auth, require_admin_user

router = APIRouter(prefix="/api/app", tags=["app"])
MAX_APK_SIZE = 200 * 1024 * 1024
UPLOAD_CHUNK_SIZE = 1024 * 1024


@router.post("/open")
async def open_app(http_request: Request, request: AppRequest):
    """Open app on multiple devices"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    ips = request.ips
    package = request.package
    await ws_manager.log(f"Opening {package} on {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def open_single(ip: str):
        nonlocal completed_count
        result = await adb_manager.open_app(ip, package)
        async with lock:
            completed_count += 1
            await ws_manager.log(f"[{ip}] App launched")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [open_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete("open_app", True, "App opened")
    return {"results": results}


@router.post("/status")
async def check_app_status(http_request: Request, request: AppRequest):
    """Check app status on multiple devices (PARALLEL)"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    ips = request.ips
    package = request.package
    await ws_manager.log(f"Checking app status on {len(ips)} devices (parallel)...")

    # Run all status checks in parallel
    tasks = [adb_manager.check_app_status(ip, package) for ip in ips]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results and log
    processed_results = []
    running_count = 0
    foreground_count = 0

    for i, result in enumerate(results):
        ip = ips[i]

        if isinstance(result, Exception):
            processed_results.append({
                "ip": ip,
                "running": False,
                "status": "ERROR",
                "message": str(result)
            })
            await ws_manager.log(f"[{ip}] Error: {result}", "error")
            continue

        processed_results.append(result)

        # Use new status field (FOREGROUND/BACKGROUND/STOPPED)
        status = result.get("status", "UNKNOWN")
        running = result.get("running", False)

        if running:
            running_count += 1
            if result.get("foreground"):
                foreground_count += 1

    # Summary log
    await ws_manager.log(
        f"App status: {running_count}/{len(ips)} running ({foreground_count} foreground)",
        "success" if running_count > 0 else "warning"
    )
    # Sync running devices to dashboard cache
    online_ips = [r["ip"] for r in processed_results if r.get("running")]
    if online_ips:
        await connection_checker.mark_online(online_ips)

    await ws_manager.task_complete("app_status", True, "Status check completed")

    return {
        "results": processed_results,
        "summary": {
            "total": len(ips),
            "running": running_count,
            "foreground": foreground_count,
            "stopped": len(ips) - running_count
        }
    }


@router.post("/clear")
async def clear_app_data(http_request: Request, request: AppRequest):
    """Clear app data on multiple devices"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    ips = request.ips
    package = request.package
    await ws_manager.log(f"Clearing app data on {len(ips)} devices...")

    completed_count = 0
    lock = asyncio.Lock()

    async def clear_single(ip: str):
        nonlocal completed_count
        result = await adb_manager.clear_app_data(ip, package)
        async with lock:
            completed_count += 1
            status = "Success" if result.get("success") else "Failed"
            await ws_manager.log(f"[{ip}] Clear data: {status}")
            await ws_manager.progress_update(completed_count, len(ips), ip)
        return result

    tasks = [clear_single(ip) for ip in ips]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)
    results = [r for r in all_results if isinstance(r, dict)]

    await ws_manager.task_complete("clear_data", True, "Clear data completed")
    return {"results": results}


@router.post("/install")
async def install_apk(request: Request, ips: List[str] = Form(...), file: UploadFile = File(...)):
    """Install APK on multiple devices"""
    require_admin_user(request)
    temp_path = None
    try:
        if not ips:
            raise HTTPException(status_code=400, detail="At least one IP is required")
        validated_ips = [validate_ip_address(ip) for ip in ips]

        filename = (file.filename or "").lower()
        if not filename.endswith(".apk"):
            raise HTTPException(status_code=400, detail="Only APK files are allowed")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".apk", dir=CURRENT_DIR) as tmp_file:
            temp_path = tmp_file.name
            total_size = 0
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                total_size += len(chunk)
                if total_size > MAX_APK_SIZE:
                    raise HTTPException(status_code=400, detail="APK file too large")
                tmp_file.write(chunk)

        if not zipfile.is_zipfile(temp_path):
            raise HTTPException(status_code=400, detail="Invalid APK file")
        with zipfile.ZipFile(temp_path, "r") as apk_file:
            if "AndroidManifest.xml" not in apk_file.namelist():
                raise HTTPException(status_code=400, detail="Invalid APK structure")

        await ws_manager.log(f"Installing APK on {len(validated_ips)} devices...")

        completed_count = 0
        lock = asyncio.Lock()

        async def install_single(ip: str):
            nonlocal completed_count
            result = await adb_manager.install_apk(ip, temp_path)
            async with lock:
                completed_count += 1
                status = "Success" if result.get("success") else "Failed"
                level = "success" if result.get("success") else "error"
                await ws_manager.log(f"[{ip}] Install: {status}", level)
                await ws_manager.progress_update(completed_count, len(validated_ips), ip)
            return result

        tasks = [install_single(ip) for ip in validated_ips]
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        results = [r for r in all_results if isinstance(r, dict)]

        await ws_manager.task_complete("install", True, "APK installation completed")
        return {"results": results}

    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        await file.close()


# ============================================
# Cache Management Endpoints (Parallel)
# ============================================


@router.post("/cache/info")
async def get_cache_info(http_request: Request, request: AppRequest):
    """Get app cache/storage info for multiple devices (PARALLEL)"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    ips = request.ips
    package = request.package
    await ws_manager.log(f"Getting cache info on {len(ips)} devices (parallel)...")

    # Run all cache checks in parallel
    tasks = [adb_manager.get_app_storage_info(ip, package) for ip in ips]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    processed_results = []
    total_cache_mb = 0
    alerts = []

    for i, result in enumerate(results):
        ip = ips[i]

        # Handle exceptions
        if isinstance(result, Exception):
            processed_results.append({
                "ip": ip,
                "success": False,
                "message": str(result)
            })
            continue

        processed_results.append(result)

        if result.get("success"):
            cache_mb = result.get("cache_mb", 0)
            total_cache_mb += cache_mb

            # Check threshold (from config)
            if cache_mb > CACHE_ALERT_THRESHOLD_MB:
                alerts.append({
                    "ip": ip,
                    "cache_mb": round(cache_mb, 2),
                    "threshold_mb": CACHE_ALERT_THRESHOLD_MB,
                    "message": f"High cache: {cache_mb:.1f}MB"
                })

    await ws_manager.log(f"Cache check complete: {len(processed_results)} devices, {total_cache_mb:.1f}MB total")
    return {
        "results": processed_results,
        "summary": {
            "total_devices": len(ips),
            "total_cache_mb": round(total_cache_mb, 2),
            "avg_cache_mb": round(total_cache_mb / len(ips), 2) if ips else 0,
            "alerts": alerts
        }
    }


@router.post("/cache/clear")
async def clear_cache(http_request: Request, request: AppRequest):
    """Clear app cache (not data) on multiple devices (PARALLEL)"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    ips = request.ips
    package = request.package
    await ws_manager.log(f"Clearing app cache on {len(ips)} devices (parallel)...")

    # Run all clear operations in parallel
    tasks = [adb_manager.clear_app_cache(ip, package) for ip in ips]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results
    processed_results = []
    success_count = 0

    for i, result in enumerate(results):
        ip = ips[i]

        if isinstance(result, Exception):
            processed_results.append({
                "ip": ip,
                "success": False,
                "message": str(result)
            })
        else:
            processed_results.append(result)
            if result.get("success"):
                success_count += 1

    await ws_manager.log(f"Clear cache complete: {success_count}/{len(ips)} success")
    return {"results": processed_results, "success_count": success_count}
