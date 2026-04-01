"""
Screenshots API routes
"""
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse
import os
import base64
import subprocess
import tempfile
import asyncio
import logging
import shutil
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from config import SCREENSHOT_DIR, SCREENSHOT_RETENTION_HOURS, SCREENSHOT_KEEP_MIN
from adb_manager import adb_manager, validate_ip
from auth import require_authenticated_user, require_admin_user, require_ws_auth

logger = logging.getLogger(__name__)
router = APIRouter(tags=["screenshots"])

# Thread pool for parallel screenshot capture
screenshot_executor = ThreadPoolExecutor(max_workers=10)


def _safe_screenshot_path(folder: str, filename: str) -> str:
    """Resolve a screenshot path and ensure it stays within the screenshot directory."""
    safe_folder = os.path.basename(folder)
    safe_name = os.path.basename(filename)
    resolved = os.path.abspath(os.path.join(SCREENSHOT_DIR, safe_folder, safe_name))
    base = os.path.abspath(SCREENSHOT_DIR)
    if not resolved.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Invalid screenshot path")
    return resolved


@router.get("/api/screenshots")
async def list_screenshots(request: Request):
    """List all screenshot folders"""
    require_authenticated_user(request)
    folders = []
    if os.path.exists(SCREENSHOT_DIR):
        for f in os.listdir(SCREENSHOT_DIR):
            path = os.path.join(SCREENSHOT_DIR, f)
            if os.path.isdir(path):
                files = [x for x in os.listdir(path) if x.endswith(".png")]
                folders.append({
                    "folder": f,
                    "count": len(files),
                    "files": files
                })
    return {"folders": sorted(folders, key=lambda x: x["folder"], reverse=True)}


@router.get("/api/screenshots/{folder}/{filename}")
async def get_screenshot(request: Request, folder: str, filename: str):
    """Get specific screenshot"""
    require_authenticated_user(request)
    file_path = _safe_screenshot_path(folder, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Screenshot not found")
    return FileResponse(file_path, media_type="image/png")


@router.post("/api/screenshot/{ip}")
async def capture_screenshot_direct(request: Request, ip: str):
    """Capture screenshot directly to memory (faster for multiview)"""
    require_authenticated_user(request)
    try:
        await adb_manager.ensure_connected(ip)
        adb_path = adb_manager.adb_path

        device_serial = ip if ':' in ip else f"{ip}:5555"

        # Method 1: Try exec-out first (fastest)
        try:
            result = subprocess.run(
                [adb_path, '-s', device_serial, 'exec-out', 'screencap', '-p'],
                capture_output=True,
                timeout=10
            )

            if result.returncode == 0 and len(result.stdout) > 1000:
                png_data = result.stdout

                first_bytes = png_data[:8].hex() if len(png_data) >= 8 else png_data.hex()
                print(f"[Screenshot] {device_serial}: {len(png_data)} bytes, first bytes: {first_bytes}")

                PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

                if png_data[:8] == PNG_SIGNATURE:
                    screenshot_base64 = base64.b64encode(png_data).decode('utf-8')
                    print(f"[Screenshot] Success for {device_serial}: {len(screenshot_base64)} base64 chars")
                    return {"success": True, "ip": ip, "screenshot": screenshot_base64}

                png_start = png_data.find(PNG_SIGNATURE)
                if png_start > 0:
                    print(f"[Screenshot] Found PNG at offset {png_start} for {device_serial}")
                    png_data = png_data[png_start:]
                    screenshot_base64 = base64.b64encode(png_data).decode('utf-8')
                    return {"success": True, "ip": ip, "screenshot": screenshot_base64}

                if b'\x89PNG\r\r\n' in png_data[:20]:
                    print(f"[Screenshot] Fixing double-CR corruption for {device_serial}")
                    png_data = png_data.replace(b'\r\r\n', b'\r\n')
                    if png_data[:8] == PNG_SIGNATURE:
                        screenshot_base64 = base64.b64encode(png_data).decode('utf-8')
                        return {"success": True, "ip": ip, "screenshot": screenshot_base64}

                print(f"[Screenshot] No valid PNG signature found for {device_serial}")
            else:
                print(f"[Screenshot] exec-out returned {len(result.stdout)} bytes, code={result.returncode}")
        except Exception as e:
            print(f"[Screenshot] exec-out failed for {device_serial}: {e}")

        # Method 2: Fallback to file-based approach
        print(f"[Screenshot] Using fallback method for {device_serial}")
        safe_ip = ip.replace('.', '_').replace(':', '_')
        temp_file = os.path.join(tempfile.gettempdir(), f"screenshot_{safe_ip}.png")

        subprocess.run(
            [adb_path, '-s', device_serial, 'shell', 'screencap', '-p', '/sdcard/screenshot.png'],
            capture_output=True,
            timeout=10
        )

        pull_result = subprocess.run(
            [adb_path, '-s', device_serial, 'pull', '/sdcard/screenshot.png', temp_file],
            capture_output=True,
            timeout=10
        )

        if pull_result.returncode == 0 and os.path.exists(temp_file):
            with open(temp_file, 'rb') as f:
                screenshot_base64 = base64.b64encode(f.read()).decode('utf-8')
            try:
                os.remove(temp_file)
            except (OSError, IOError) as e:
                logger.debug(f"Failed to remove temp file: {e}")
            return {"success": True, "ip": ip, "screenshot": screenshot_base64}
        else:
            error_msg = pull_result.stderr.decode() if pull_result.stderr else 'Unknown error'
            logger.debug(f"[Screenshot] Pull failed for {device_serial}: {error_msg}")
            return {
                "success": False,
                "ip": ip,
                "message": f"Pull failed: {error_msg}",
                "screenshot": None
            }

    except subprocess.TimeoutExpired:
        return {"success": False, "ip": ip, "message": "Timeout", "screenshot": None}
    except Exception as e:
        print(f"[Screenshot] Error for {ip}: {e}")
        return {"success": False, "ip": ip, "message": str(e), "screenshot": None}


def capture_screenshot_sync(ip: str) -> dict:
    """Synchronous screenshot capture for thread pool execution"""
    try:
        if not validate_ip(ip):
            return {"success": False, "ip": ip, "message": "Invalid IP address", "screenshot": None}

        adb_path = adb_manager.adb_path
        device_serial = ip if ':' in ip else f"{ip}:5555"

        # Method 1: Try exec-out first (fastest)
        try:
            result = subprocess.run(
                [adb_path, '-s', device_serial, 'exec-out', 'screencap', '-p'],
                capture_output=True,
                timeout=10
            )

            if result.returncode == 0 and len(result.stdout) > 1000:
                png_data = result.stdout
                PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'

                if png_data[:8] == PNG_SIGNATURE:
                    screenshot_base64 = base64.b64encode(png_data).decode('utf-8')
                    return {"success": True, "ip": ip, "screenshot": screenshot_base64}

                png_start = png_data.find(PNG_SIGNATURE)
                if png_start > 0:
                    png_data = png_data[png_start:]
                    screenshot_base64 = base64.b64encode(png_data).decode('utf-8')
                    return {"success": True, "ip": ip, "screenshot": screenshot_base64}

                if b'\x89PNG\r\r\n' in png_data[:20]:
                    png_data = png_data.replace(b'\r\r\n', b'\r\n')
                    if png_data[:8] == PNG_SIGNATURE:
                        screenshot_base64 = base64.b64encode(png_data).decode('utf-8')
                        return {"success": True, "ip": ip, "screenshot": screenshot_base64}
        except subprocess.TimeoutExpired:
            pass  # Will fallback to file method
        except (OSError, subprocess.SubprocessError) as e:
            logger.debug(f"exec-out failed for {ip}: {e}")

        # Method 2: Fallback to file-based approach
        safe_ip = ip.replace('.', '_').replace(':', '_')
        temp_file = os.path.join(tempfile.gettempdir(), f"screenshot_{safe_ip}.png")

        subprocess.run(
            [adb_path, '-s', device_serial, 'shell', 'screencap', '-p', '/sdcard/screenshot.png'],
            capture_output=True,
            timeout=10
        )

        pull_result = subprocess.run(
            [adb_path, '-s', device_serial, 'pull', '/sdcard/screenshot.png', temp_file],
            capture_output=True,
            timeout=10
        )

        if pull_result.returncode == 0 and os.path.exists(temp_file):
            with open(temp_file, 'rb') as f:
                screenshot_base64 = base64.b64encode(f.read()).decode('utf-8')
            try:
                os.remove(temp_file)
            except (OSError, IOError) as e:
                logger.debug(f"Failed to remove temp file: {e}")
            return {"success": True, "ip": ip, "screenshot": screenshot_base64}

        return {"success": False, "ip": ip, "message": "Capture failed", "screenshot": None}

    except subprocess.TimeoutExpired:
        return {"success": False, "ip": ip, "message": "Timeout", "screenshot": None}
    except Exception as e:
        return {"success": False, "ip": ip, "message": str(e), "screenshot": None}


@router.websocket("/ws/multiview/screenshots")
async def websocket_screenshot_stream(websocket: WebSocket):
    """
    WebSocket endpoint for streaming screenshots one by one.

    Client sends: {"ips": ["192.168.1.1", "192.168.1.2", ...]}
    Server sends: {"type": "screenshot", "ip": "...", "success": true, "screenshot": "base64..."}
                  {"type": "progress", "completed": 5, "total": 10}
                  {"type": "done", "total": 10, "success_count": 8}
    """
    user = await require_ws_auth(websocket)
    if not user:
        return
    await websocket.accept()
    print("[WS Screenshots] Client connected")

    try:
        while True:
            # Wait for client to send IPs
            data = await websocket.receive_json()
            ips = data.get("ips", [])

            if not ips:
                await websocket.send_json({"type": "error", "message": "No IPs provided"})
                continue

            print(f"[WS Screenshots] Starting capture for {len(ips)} devices")
            await websocket.send_json({
                "type": "start",
                "total": len(ips),
                "message": f"Starting capture for {len(ips)} devices"
            })

            # Run all screenshot captures concurrently in thread pool
            loop = asyncio.get_event_loop()
            counter = {"completed": 0, "success": 0}
            counter_lock = asyncio.Lock()

            # Create tasks for all IPs
            async def capture_and_send(ip: str):
                result = await loop.run_in_executor(screenshot_executor, capture_screenshot_sync, ip)

                async with counter_lock:
                    counter["completed"] += 1
                    if result.get("success"):
                        counter["success"] += 1

                    # Send screenshot result immediately
                    await websocket.send_json({
                        "type": "screenshot",
                        **result
                    })

                    # Send progress update
                    await websocket.send_json({
                        "type": "progress",
                        "completed": counter["completed"],
                        "total": len(ips)
                    })

            # Run all captures concurrently
            tasks = [capture_and_send(ip) for ip in ips]
            await asyncio.gather(*tasks, return_exceptions=True)
            success_count = counter["success"]

            # Send completion message
            await websocket.send_json({
                "type": "done",
                "total": len(ips),
                "success_count": success_count
            })
            print(f"[WS Screenshots] Completed: {success_count}/{len(ips)} successful")

    except WebSocketDisconnect:
        logger.info("[WS Screenshots] Client disconnected")
    except Exception as e:
        logger.error(f"[WS Screenshots] Error: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except (RuntimeError, ConnectionError) as send_error:
            logger.debug(f"Failed to send error message: {send_error}")


def cleanup_old_screenshots() -> dict:
    """
    Delete screenshot folders older than SCREENSHOT_RETENTION_HOURS.
    Always keeps at least SCREENSHOT_KEEP_MIN folders.
    """
    if not os.path.exists(SCREENSHOT_DIR):
        return {"deleted": 0, "kept": 0, "message": "Screenshot directory does not exist"}

    folders = []
    for f in os.listdir(SCREENSHOT_DIR):
        path = os.path.join(SCREENSHOT_DIR, f)
        if os.path.isdir(path):
            try:
                # Parse folder timestamp (format: YYYY-MM-DD_HH-MM-SS)
                folder_time = datetime.strptime(f, "%Y-%m-%d_%H-%M-%S")
                folders.append((f, path, folder_time))
            except ValueError:
                # Skip folders with unexpected names
                continue

    if not folders:
        return {"deleted": 0, "kept": 0, "message": "No screenshot folders found"}

    # Sort by time (newest first)
    folders.sort(key=lambda x: x[2], reverse=True)

    # Calculate cutoff time
    cutoff = datetime.now() - timedelta(hours=SCREENSHOT_RETENTION_HOURS)

    deleted = 0
    kept = 0

    for i, (name, path, folder_time) in enumerate(folders):
        # Always keep minimum number of folders
        if i < SCREENSHOT_KEEP_MIN:
            kept += 1
            continue

        # Delete folders older than retention period
        if folder_time < cutoff:
            try:
                shutil.rmtree(path)
                deleted += 1
                logger.info(f"[Cleanup] Deleted old screenshot folder: {name}")
            except (OSError, IOError) as e:
                logger.error(f"[Cleanup] Failed to delete {name}: {e}")
                kept += 1
        else:
            kept += 1

    return {
        "deleted": deleted,
        "kept": kept,
        "retention_hours": SCREENSHOT_RETENTION_HOURS,
        "min_keep": SCREENSHOT_KEEP_MIN,
        "message": f"Cleaned up {deleted} folders, kept {kept}"
    }


@router.post("/api/screenshots/cleanup")
async def trigger_cleanup(request: Request):
    """Manually trigger screenshot cleanup"""
    require_admin_user(request)
    result = cleanup_old_screenshots()
    return result


@router.get("/api/screenshots/stats")
async def get_screenshot_stats(request: Request):
    """Get screenshot storage statistics"""
    require_authenticated_user(request)
    if not os.path.exists(SCREENSHOT_DIR):
        return {
            "total_folders": 0,
            "total_files": 0,
            "total_size_mb": 0,
            "oldest_folder": None,
            "newest_folder": None
        }

    folders = []
    total_files = 0
    total_size = 0

    for f in os.listdir(SCREENSHOT_DIR):
        path = os.path.join(SCREENSHOT_DIR, f)
        if os.path.isdir(path):
            files = [x for x in os.listdir(path) if x.endswith(".png")]
            folder_size = sum(
                os.path.getsize(os.path.join(path, file))
                for file in files
                if os.path.isfile(os.path.join(path, file))
            )
            total_files += len(files)
            total_size += folder_size
            folders.append(f)

    folders.sort()

    return {
        "total_folders": len(folders),
        "total_files": total_files,
        "total_size_mb": round(total_size / (1024 * 1024), 2),
        "oldest_folder": folders[0] if folders else None,
        "newest_folder": folders[-1] if folders else None,
        "retention_hours": SCREENSHOT_RETENTION_HOURS,
        "min_keep": SCREENSHOT_KEEP_MIN
    }
