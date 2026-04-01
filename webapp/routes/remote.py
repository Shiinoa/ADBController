"""
Remote Control API routes
"""
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.templating import Jinja2Templates
from typing import Dict, Set
import asyncio
import base64
import os

from models import TapRequest, SwipeRequest, KeyRequest, TextRequest
from config import CURRENT_DIR
from adb_manager import adb_manager
from auth import require_auth, require_ws_auth

router = APIRouter(tags=["remote"])

# Setup templates
templates = Jinja2Templates(directory=os.path.join(CURRENT_DIR, "templates"))

# Store active remote sessions
remote_sessions: Dict[str, Set[int]] = {}


def _assert_remote_access(user: Dict, ip: str):
    if user.get("role") == "admin":
        return
    visible_ips = {
        device.get("IP")
        for device in adb_manager.get_devices_from_csv()
        if device.get("IP")
        and (device.get("Plant ID") or device.get("plant_id")) == user.get("plant_code")
    }
    if ip not in visible_ips:
        raise HTTPException(status_code=403, detail="Device access denied")


@router.get("/remote/{ip}")
async def remote_page(request: Request, ip: str):
    """Remote control page for a device"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_remote_access(user, ip)
    return templates.TemplateResponse("remote.html", {"request": request, "ip": ip})


@router.websocket("/ws/remote/{ip}")
async def remote_websocket(websocket: WebSocket, ip: str):
    """WebSocket for streaming screenshots"""
    user = await require_ws_auth(websocket)
    if not user:
        return
    if user.get("role") != "admin":
        visible_ips = {
            device.get("IP")
            for device in adb_manager.get_devices_from_csv()
            if device.get("IP")
            and (device.get("Plant ID") or device.get("plant_id")) == user.get("plant_code")
        }
        if ip not in visible_ips:
            await websocket.close(code=1008, reason="Device access denied")
            return
    await websocket.accept()
    session_id = id(websocket)
    remote_sessions.setdefault(ip, set()).add(session_id)
    print(f"[Remote] Starting session for {ip}")

    try:
        connect_result = await adb_manager.connect_device(ip)
        print(f"[Remote] Connect result: {connect_result}")

        if connect_result.get("status") == "unauthorized":
            await websocket.send_json({
                "type": "error",
                "message": "Device unauthorized! Check TV for authorization dialog."
            })
            return

        width, height = await adb_manager.get_screen_size(ip)
        print(f"[Remote] Screen size: {width}x{height}")
        await websocket.send_json({"type": "screen_size", "width": width, "height": height})

        frame_count = 0
        while session_id in remote_sessions.get(ip, set()):
            try:
                screenshot_bytes = await adb_manager.get_screenshot_bytes(ip)
                if screenshot_bytes and len(screenshot_bytes) > 100:
                    b64 = base64.b64encode(screenshot_bytes).decode('utf-8')
                    await websocket.send_json({"type": "frame", "data": b64})
                    frame_count += 1
                    if frame_count % 10 == 0:
                        print(f"[Remote] Sent {frame_count} frames to {ip}")
                else:
                    print(f"[Remote] Empty screenshot from {ip}")
                    await websocket.send_json({"type": "error", "message": "Failed to capture screen"})
                await asyncio.sleep(0.5)
            except (WebSocketDisconnect, RuntimeError):
                print(f"[Remote] Client disconnected during streaming: {ip}")
                break
            except Exception as e:
                err_msg = str(e) or type(e).__name__
                print(f"[Remote] Frame error ({type(e).__name__}): {err_msg}")
                try:
                    await websocket.send_json({"type": "error", "message": err_msg})
                except Exception:
                    print(f"[Remote] WebSocket closed, stopping stream for {ip}")
                    break
                await asyncio.sleep(1)

    except WebSocketDisconnect:
        print(f"[Remote] Client disconnected: {ip}")
    except Exception as e:
        err_msg = str(e) or type(e).__name__
        print(f"[Remote] Session error ({type(e).__name__}): {err_msg}")
    finally:
        sessions = remote_sessions.get(ip, set())
        sessions.discard(session_id)
        if not sessions:
            remote_sessions.pop(ip, None)
        print(f"[Remote] Session ended for {ip}")


@router.post("/api/remote/tap")
async def remote_tap(http_request: Request, request: TapRequest):
    """Send tap input to device"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_remote_access(user, request.ip)
    result = await adb_manager.send_tap(request.ip, request.x, request.y)
    return result


@router.post("/api/remote/swipe")
async def remote_swipe(http_request: Request, request: SwipeRequest):
    """Send swipe input to device"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_remote_access(user, request.ip)
    result = await adb_manager.send_swipe(
        request.ip, request.x1, request.y1,
        request.x2, request.y2, request.duration
    )
    return result


@router.post("/api/remote/key")
async def remote_key(http_request: Request, request: KeyRequest):
    """Send key event to device"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_remote_access(user, request.ip)
    result = await adb_manager.send_key(request.ip, request.keycode)
    return result


@router.post("/api/remote/text")
async def remote_text(http_request: Request, request: TextRequest):
    """Send text input to device"""
    user = require_auth(http_request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_remote_access(user, request.ip)
    result = await adb_manager.send_text(request.ip, request.text)
    return result


@router.post("/api/remote/stop/{ip}")
async def stop_remote(request: Request, ip: str):
    """Stop remote session"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    _assert_remote_access(user, ip)
    remote_sessions.pop(ip, None)
    return {"success": True, "message": f"Remote session for {ip} stopped"}
