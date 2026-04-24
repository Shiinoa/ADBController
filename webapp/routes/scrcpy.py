"""
Scrcpy Real-time Remote API routes
Supports:
  1. Native scrcpy window (launch on server desktop)
  2. Client agent package download (clientless install)
"""
import os
import io
import zipfile
import logging
from datetime import datetime
from typing import List

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from config import BASE_DIR, CURRENT_DIR
from websocket_manager import ws_manager
from scrcpy_manager import scrcpy_window
from auth import require_authenticated_user

logger = logging.getLogger(__name__)
router = APIRouter(tags=["scrcpy"])

# Cached zip package (avoid rebuilding every request)
_package_cache = {"data": None, "mtime": 0}
AGENT_SHARED_TOKEN = os.environ.get("SCRCPY_AGENT_TOKEN", "").strip()

# ============================================
# Agent Heartbeat Registry
# ============================================
AGENT_ONLINE_TIMEOUT = 60  # seconds — mark offline if no heartbeat within this


class HeartbeatPayload(BaseModel):
    hostname: str
    port: int = 18080
    version: str = "1.0.0"
    scrcpy_exists: bool = False
    adb_exists: bool = False
    active_sessions: List[str] = []


# key = client IP, value = agent info dict
_connected_agents: dict = {}


# Essential scrcpy files to package for client agent
SCRCPY_ESSENTIAL_FILES = [
    "scrcpy.exe",
    "adb.exe",
    "scrcpy-server",
    "AdbWinApi.dll",
    "AdbWinUsbApi.dll",
    "SDL2.dll",
    "libusb-1.0.dll",
    "avcodec-61.dll",
    "avformat-61.dll",
    "avutil-59.dll",
    "swresample-5.dll",
]


def _build_package_zip() -> bytes:
    """Build the scrcpy agent package zip and return as bytes."""
    scrcpy_dir = None
    candidates = [
        os.path.join(BASE_DIR, "scrcpy", "scrcpy-win64-v3.2"),
        os.path.join(BASE_DIR, "scrcpy"),
        os.path.join(CURRENT_DIR, "scrcpy", "scrcpy-win64-v3.2"),
        os.path.join(CURRENT_DIR, "scrcpy"),
    ]
    for d in candidates:
        scrcpy_exe_path = os.path.join(d, "scrcpy.exe")
        logger.info(f"[Package] Checking: {scrcpy_exe_path} -> exists={os.path.exists(scrcpy_exe_path)}")
        if os.path.exists(scrcpy_exe_path):
            scrcpy_dir = d
            break

    if not scrcpy_dir:
        logger.warning(f"[Package] scrcpy.exe not found. BASE_DIR={BASE_DIR}, candidates={candidates}")
        return None

    # Find client_agent directory (check both BASE_DIR and CURRENT_DIR for Docker compatibility)
    client_agent_dir = None
    for base in [BASE_DIR, CURRENT_DIR]:
        candidate = os.path.join(base, "client_agent")
        if os.path.isdir(candidate):
            client_agent_dir = candidate
            break

    agent_file = os.path.join(client_agent_dir, "client_agent.py") if client_agent_dir else ""
    setup_bat = os.path.join(client_agent_dir, "setup_agent.bat") if client_agent_dir else ""
    agent_exe = os.path.join(client_agent_dir, "dist", "client_agent.exe") if client_agent_dir else ""

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for filename in SCRCPY_ESSENTIAL_FILES:
            filepath = os.path.join(scrcpy_dir, filename)
            if os.path.exists(filepath):
                zf.write(filepath, f"SCRCPY/{filename}")

        if agent_exe and os.path.exists(agent_exe):
            zf.write(agent_exe, "SCRCPY/client_agent.exe")
        if agent_file and os.path.exists(agent_file):
            zf.write(agent_file, "client_agent.py")

        if setup_bat and os.path.exists(setup_bat):
            zf.write(setup_bat, "setup_agent.bat")

        req_file = os.path.join(client_agent_dir, "requirements.txt") if client_agent_dir else ""
        if req_file and os.path.exists(req_file):
            zf.write(req_file, "requirements.txt")

    return buf.getvalue()


def _get_latest_mtime() -> float:
    """Get the newest mtime of key source files for cache invalidation."""
    paths = []
    for base in [BASE_DIR, CURRENT_DIR]:
        ca = os.path.join(base, "client_agent")
        if os.path.isdir(ca):
            paths = [
                os.path.join(ca, "dist", "client_agent.exe"),
                os.path.join(ca, "client_agent.py"),
                os.path.join(ca, "setup_agent.bat"),
                os.path.join(ca, "requirements.txt"),
            ]
            break
    latest = 0
    for p in paths:
        try:
            latest = max(latest, os.path.getmtime(p))
        except OSError:
            pass
    return latest


@router.post("/api/scrcpy/heartbeat")
async def agent_heartbeat(payload: HeartbeatPayload, request: Request):
    """Receive heartbeat from a client agent"""
    if AGENT_SHARED_TOKEN:
        provided = request.headers.get("x-agent-token", "").strip()
        if provided != AGENT_SHARED_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid agent token")

    client_ip = request.client.host if request.client else "unknown"
    now = datetime.now()

    _connected_agents[client_ip] = {
        "ip": client_ip,
        "hostname": payload.hostname,
        "port": payload.port,
        "version": payload.version,
        "scrcpy_exists": payload.scrcpy_exists,
        "adb_exists": payload.adb_exists,
        "active_sessions": payload.active_sessions,
        "last_heartbeat": now.isoformat(),
        "_last_heartbeat_dt": now,
    }
    return {"success": True}


@router.get("/api/scrcpy/agents")
async def get_connected_agents(request: Request):
    """Get list of all known agents with online/offline status"""
    require_authenticated_user(request)
    now = datetime.now()
    agents = []
    for ip, info in _connected_agents.items():
        dt = info.get("_last_heartbeat_dt", now)
        elapsed = (now - dt).total_seconds()
        agents.append({
            "ip": info["ip"],
            "hostname": info["hostname"],
            "port": info["port"],
            "version": info["version"],
            "scrcpy_exists": info["scrcpy_exists"],
            "adb_exists": info["adb_exists"],
            "active_sessions": info["active_sessions"],
            "last_heartbeat": info["last_heartbeat"],
            "online": elapsed <= AGENT_ONLINE_TIMEOUT,
            "elapsed_seconds": round(elapsed),
        })
    # Online first, then by hostname
    agents.sort(key=lambda a: (not a["online"], a["hostname"].lower()))
    return {"agents": agents}


@router.get("/api/scrcpy/download-package")
async def download_scrcpy_package(request: Request):
    """Download scrcpy + client_agent as a cached zip package"""
    current_mtime = _get_latest_mtime()

    if _package_cache["data"] is None or current_mtime > _package_cache["mtime"]:
        logger.info("[Package] Building zip package (first request or files updated)...")
        data = _build_package_zip()
        if data is None:
            return {"success": False, "message": "scrcpy files not found on server"}
        _package_cache["data"] = data
        _package_cache["mtime"] = current_mtime
        logger.info(f"[Package] Cached: {len(data) / 1024 / 1024:.1f} MB")
    else:
        logger.info(f"[Package] Serving from cache: {len(_package_cache['data']) / 1024 / 1024:.1f} MB")

    return Response(
        content=_package_cache["data"],
        media_type="application/zip",
        headers={
            "Content-Disposition": "attachment; filename=scrcpy-agent-package.zip",
            "Content-Length": str(len(_package_cache["data"])),
        }
    )


@router.get("/api/scrcpy/download-agent")
async def download_agent_script(request: Request):
    """Download just the client_agent.py script"""
    agent_file = None
    for base in [BASE_DIR, CURRENT_DIR]:
        candidate = os.path.join(base, "client_agent", "client_agent.py")
        if os.path.exists(candidate):
            agent_file = candidate
            break
    if not agent_file:
        return {"success": False, "message": "client_agent.py not found"}

    with open(agent_file, 'r', encoding='utf-8') as f:
        content = f.read()

    return StreamingResponse(
        io.BytesIO(content.encode('utf-8')),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=client_agent.py"}
    )


@router.post("/api/scrcpy/launch/{ip}")
async def launch_scrcpy(request: Request, ip: str):
    """Launch native scrcpy window for real-time remote control"""
    require_authenticated_user(request)
    await ws_manager.log(f"Launching scrcpy for {ip}...")
    result = await scrcpy_window.launch(ip)
    if result.get("success"):
        await ws_manager.log(f"[{ip}] Scrcpy window opened - check your desktop!", "success")
    else:
        await ws_manager.log(f"[{ip}] Failed: {result.get('message', 'Unknown error')}", "error")
    return result


@router.post("/api/scrcpy/close/{ip}")
async def close_scrcpy(request: Request, ip: str):
    """Close scrcpy window"""
    require_authenticated_user(request)
    await scrcpy_window.close(ip)
    await ws_manager.log(f"[{ip}] Scrcpy stopped")
    return {"success": True, "message": "Scrcpy stopped"}
