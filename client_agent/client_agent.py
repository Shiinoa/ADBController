"""
Scrcpy Client Agent - Clientless Edition
Runs on client PCs to launch native scrcpy windows via HTTP API.
Auto-downloads scrcpy from the main server if not found locally.

Usage:
    python client_agent.py [--port 9000] [--server http://server:8000]

Scrcpy is auto-installed to: %LOCALAPPDATA%\\Temp\\SCRCPY
The browser on the same PC can call http://localhost:9000/api/launch/{device_ip}
to open a native scrcpy window on the client's desktop.
"""
import asyncio
import subprocess
import shutil
import os
import sys
import argparse
import logging
import tempfile
import zipfile
import io
import json
import socket
import platform
from typing import Optional

try:
    import urllib.request
except ImportError:
    pass

try:
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
except ImportError:
    print("ERROR: FastAPI and uvicorn are required.")
    print("Install with: pip install fastapi uvicorn")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="[Agent] %(message)s")
logger = logging.getLogger(__name__)

# ============================================
# Path Detection & Auto-Install
# ============================================
IS_WINDOWS = os.name == 'nt'

# When running as PyInstaller EXE, __file__ points to temp extraction dir.
# Use the directory where the actual EXE lives instead.
if getattr(sys, 'frozen', False):
    AGENT_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    AGENT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.dirname(AGENT_DIR)

# Default install location: %LOCALAPPDATA%\Temp\SCRCPY (Windows)
if IS_WINDOWS:
    SCRCPY_INSTALL_DIR = os.path.join(os.environ.get('LOCALAPPDATA', tempfile.gettempdir()), "Temp", "SCRCPY")
else:
    SCRCPY_INSTALL_DIR = os.path.join(tempfile.gettempdir(), "SCRCPY")


def _to_abs(path: str) -> str:
    """Ensure path is absolute"""
    return os.path.abspath(path)


def find_adb() -> Optional[str]:
    """Auto-detect ADB path (always returns absolute path)"""
    adb_name = "adb.exe" if IS_WINDOWS else "adb"

    # Check installed location first
    installed = os.path.join(SCRCPY_INSTALL_DIR, adb_name)
    if os.path.exists(installed):
        return _to_abs(installed)

    # Check same directory as this script/exe
    local = os.path.join(AGENT_DIR, adb_name)
    if os.path.exists(local):
        return _to_abs(local)

    # Check SCRCPY subfolder next to script
    local_sub = os.path.join(AGENT_DIR, "SCRCPY", adb_name)
    if os.path.exists(local_sub):
        return _to_abs(local_sub)

    # Check PATH
    adb_in_path = shutil.which('adb')
    if adb_in_path:
        return _to_abs(adb_in_path)

    # Check known project locations
    candidates = []
    if IS_WINDOWS:
        candidates = [
            os.path.join(BASE_DIR, "scrcpy", "scrcpy-win64-v3.2", "adb.exe"),
            os.path.join(BASE_DIR, "scrcpy", "ADB platform-tools", "adb.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/adb",
            "/usr/local/bin/adb",
            os.path.expanduser("~/Android/Sdk/platform-tools/adb"),
        ]

    for path in candidates:
        if path and os.path.exists(path):
            return _to_abs(path)
    return None


def find_scrcpy() -> Optional[str]:
    """Auto-detect scrcpy path (always returns absolute path)"""
    scrcpy_name = "scrcpy.exe" if IS_WINDOWS else "scrcpy"

    # Check installed location first
    installed = os.path.join(SCRCPY_INSTALL_DIR, scrcpy_name)
    if os.path.exists(installed):
        return _to_abs(installed)

    # Check same directory as this script/exe
    local = os.path.join(AGENT_DIR, scrcpy_name)
    if os.path.exists(local):
        return _to_abs(local)

    # Check SCRCPY subfolder next to script
    local_sub = os.path.join(AGENT_DIR, "SCRCPY", scrcpy_name)
    if os.path.exists(local_sub):
        return _to_abs(local_sub)

    # Check PATH
    scrcpy_in_path = shutil.which('scrcpy')
    if scrcpy_in_path:
        return _to_abs(scrcpy_in_path)

    # Check known project locations
    candidates = []
    if IS_WINDOWS:
        candidates = [
            os.path.join(BASE_DIR, "scrcpy", "scrcpy-win64-v3.2", "scrcpy.exe"),
        ]
    else:
        candidates = [
            "/usr/bin/scrcpy",
            "/usr/local/bin/scrcpy",
            "/snap/bin/scrcpy",
        ]

    for path in candidates:
        if path and os.path.exists(path):
            return _to_abs(path)
    return None


def download_scrcpy_from_server(server_url: str) -> bool:
    """Download scrcpy package from main server and extract to SCRCPY_INSTALL_DIR"""
    download_url = f"{server_url}/api/scrcpy/download-package"
    print(f"[Agent] Downloading scrcpy from {download_url}...")

    try:
        req = urllib.request.Request(download_url)
        with urllib.request.urlopen(req, timeout=60) as response:
            if response.status != 200:
                print(f"[Agent] Download failed: HTTP {response.status}")
                return False

            zip_data = response.read()
            print(f"[Agent] Downloaded {len(zip_data) / 1024 / 1024:.1f} MB")

        # Extract zip
        os.makedirs(SCRCPY_INSTALL_DIR, exist_ok=True)

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            for info in zf.infolist():
                # Files in zip are under SCRCPY/ subfolder
                if info.filename.startswith("SCRCPY/") and not info.is_dir():
                    # Extract to SCRCPY_INSTALL_DIR (strip SCRCPY/ prefix)
                    filename = info.filename[len("SCRCPY/"):]
                    if filename:
                        target = os.path.join(SCRCPY_INSTALL_DIR, filename)
                        with zf.open(info) as src, open(target, 'wb') as dst:
                            dst.write(src.read())
                        print(f"  Extracted: {filename}")

        # Verify
        scrcpy_name = "scrcpy.exe" if IS_WINDOWS else "scrcpy"
        if os.path.exists(os.path.join(SCRCPY_INSTALL_DIR, scrcpy_name)):
            print(f"[Agent] Scrcpy installed to: {SCRCPY_INSTALL_DIR}")
            return True
        else:
            print("[Agent] Installation failed: scrcpy executable not found after extraction")
            return False

    except urllib.error.URLError as e:
        print(f"[Agent] Download failed: {e}")
        return False
    except Exception as e:
        print(f"[Agent] Download error: {e}")
        return False


# ============================================
# Agent State
# ============================================
class ScrcpyAgent:
    def __init__(self, scrcpy_path: str, adb_path: str, server_url: str = ""):
        self.scrcpy_path = scrcpy_path
        self.adb_path = adb_path
        self.server_url = server_url
        self.processes = {}  # ip -> subprocess.Popen

    async def launch(self, ip: str, title: Optional[str] = None) -> dict:
        """Launch native scrcpy window for a device"""
        if not os.path.exists(self.scrcpy_path):
            return {"success": False, "message": f"scrcpy not found at: {self.scrcpy_path}"}

        # Connect ADB to device
        logger.info(f"Connecting ADB to {ip}...")
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [self.adb_path, "connect", ip],
                capture_output=True, text=True, timeout=10
            )
            connect_output = result.stdout + result.stderr
            logger.info(f"ADB connect: {connect_output.strip()}")

            if "connected" not in connect_output.lower() and "already" not in connect_output.lower():
                return {"success": False, "message": f"ADB connect failed: {connect_output.strip()}"}
        except subprocess.TimeoutExpired:
            return {"success": False, "message": "ADB connect timed out"}
        except Exception as e:
            return {"success": False, "message": f"ADB error: {e}"}

        # Close existing session for this IP
        await self.close(ip)

        title = title or f"Remote: {ip}"

        # Set CWD to scrcpy directory so it can find its DLLs
        scrcpy_dir = os.path.dirname(self.scrcpy_path)

        try:
            # Use Popen directly (not 'start') so we can capture errors
            cmd = [
                self.scrcpy_path,
                "-s", ip,
                "--window-title", title,
                "--max-size", "1280",
                "--max-fps", "30",
                "--no-audio"
            ]
            logger.info(f"Launching: {' '.join(cmd)}")
            logger.info(f"  CWD: {scrcpy_dir}")

            if IS_WINDOWS:
                # CREATE_NEW_PROCESS_GROUP for process isolation
                # Do NOT use DETACHED_PROCESS — it prevents SDL GUI window from appearing
                # Use DEVNULL to avoid pipe buffer blocking on long-running process
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=scrcpy_dir,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
                )
            else:
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=scrcpy_dir
                )

            # Wait briefly to check if it started successfully
            await asyncio.sleep(2.0)

            if process.poll() is not None:
                logger.error(f"scrcpy exited immediately with code {process.returncode}")
                return {
                    "success": False,
                    "message": f"scrcpy exited immediately (code {process.returncode})"
                }

            self.processes[ip] = process
            logger.info(f"Scrcpy window opened for {ip}")
            return {"success": True, "message": f"Scrcpy window opened for {ip}"}

        except FileNotFoundError:
            return {"success": False, "message": f"scrcpy not found at: {self.scrcpy_path}"}
        except Exception as e:
            return {"success": False, "message": f"Launch error: {e}"}

    async def close(self, ip: str) -> dict:
        """Close scrcpy window for a device"""
        process = self.processes.pop(ip, None)

        if process:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        elif IS_WINDOWS:
            try:
                title = f"Remote: {ip}"
                subprocess.run(
                    ["taskkill", "/FI", f"WINDOWTITLE eq {title}", "/F"],
                    capture_output=True, timeout=5
                )
            except Exception:
                pass

        logger.info(f"Closed scrcpy for {ip}")
        return {"success": True, "message": f"Scrcpy closed for {ip}"}

    async def close_all(self) -> dict:
        """Close all scrcpy windows"""
        ips = list(self.processes.keys())
        for ip in ips:
            await self.close(ip)
        return {"success": True, "message": f"Closed {len(ips)} sessions"}

    def status(self) -> dict:
        """Get agent status"""
        return {
            "agent": "scrcpy-client-agent",
            "version": "1.0.0",
            "scrcpy_path": self.scrcpy_path,
            "scrcpy_exists": os.path.exists(self.scrcpy_path),
            "adb_path": self.adb_path,
            "adb_exists": os.path.exists(self.adb_path),
            "install_dir": SCRCPY_INSTALL_DIR,
            "active_sessions": list(self.processes.keys()),
        }


# ============================================
# FastAPI Application
# ============================================
DEFAULT_PORT = 18080
HEARTBEAT_INTERVAL = 30  # seconds


def create_app(scrcpy_path: str, adb_path: str, server_url: str = "", port: int = DEFAULT_PORT) -> FastAPI:
    agent = ScrcpyAgent(scrcpy_path, adb_path, server_url)

    app = FastAPI(title="Scrcpy Client Agent", version="1.0.0")

    async def _heartbeat_loop():
        """Send periodic heartbeat to main server"""
        if not server_url:
            logger.info("No server URL configured, heartbeat disabled.")
            return
        heartbeat_url = f"{server_url}/api/scrcpy/heartbeat"
        hostname = platform.node() or socket.gethostname()
        logger.info(f"Heartbeat started → {heartbeat_url} (every {HEARTBEAT_INTERVAL}s)")

        while True:
            try:
                payload = json.dumps({
                    "hostname": hostname,
                    "port": port,
                    "version": "1.0.0",
                    "scrcpy_exists": os.path.exists(agent.scrcpy_path),
                    "adb_exists": os.path.exists(agent.adb_path),
                    "active_sessions": list(agent.processes.keys()),
                }).encode("utf-8")

                req = urllib.request.Request(
                    heartbeat_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                await asyncio.to_thread(urllib.request.urlopen, req, timeout=10)
            except Exception as e:
                logger.warning(f"Heartbeat failed: {e}")

            await asyncio.sleep(HEARTBEAT_INTERVAL)

    @app.on_event("startup")
    async def start_heartbeat():
        asyncio.create_task(_heartbeat_loop())

    # Allow CORS from any origin (browser on same PC calls localhost)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/status")
    async def get_status():
        """Health check and agent status"""
        return agent.status()

    @app.post("/api/launch/{ip}")
    async def launch_scrcpy(ip: str, title: Optional[str] = None):
        """Launch native scrcpy window for device"""
        return await agent.launch(ip, title)

    @app.post("/api/close/{ip}")
    async def close_scrcpy(ip: str):
        """Close scrcpy window for device"""
        return await agent.close(ip)

    @app.post("/api/close-all")
    async def close_all():
        """Close all scrcpy windows"""
        return await agent.close_all()

    @app.post("/api/install")
    async def install_scrcpy():
        """Download and install scrcpy from main server"""
        if not agent.server_url:
            return {"success": False, "message": "Server URL not configured. Use --server flag."}

        success = await asyncio.to_thread(download_scrcpy_from_server, agent.server_url)
        if success:
            # Update paths
            scrcpy_name = "scrcpy.exe" if IS_WINDOWS else "scrcpy"
            adb_name = "adb.exe" if IS_WINDOWS else "adb"
            agent.scrcpy_path = os.path.join(SCRCPY_INSTALL_DIR, scrcpy_name)
            agent.adb_path = os.path.join(SCRCPY_INSTALL_DIR, adb_name)
            return {"success": True, "message": f"Scrcpy installed to {SCRCPY_INSTALL_DIR}"}
        return {"success": False, "message": "Download failed. Check server connection."}

    return app


# ============================================
# Main Entry Point
# ============================================
MAX_PORT_RETRIES = 5


def try_bind_port(port: int) -> bool:
    """Check if a port is available for binding"""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
            return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Scrcpy Client Agent")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument("--server", type=str, default="", help="Main server URL for auto-download (e.g. http://192.168.1.100:8000)")
    parser.add_argument("--scrcpy-path", type=str, default=None, help="Path to scrcpy executable")
    parser.add_argument("--adb-path", type=str, default=None, help="Path to adb executable")
    args = parser.parse_args()

    server_url = args.server.rstrip('/') if args.server else ""

    # Resolve paths
    scrcpy_path = args.scrcpy_path or find_scrcpy()
    adb_path = args.adb_path or find_adb()

    # Auto-download if not found and server URL provided
    if (not scrcpy_path or not adb_path) and server_url:
        print(f"[Agent] Scrcpy not found locally. Downloading from {server_url}...")
        if download_scrcpy_from_server(server_url):
            scrcpy_path = scrcpy_path or find_scrcpy()
            adb_path = adb_path or find_adb()

    if not scrcpy_path:
        print("=" * 50)
        print("  WARNING: scrcpy not found!")
        print(f"  Expected location: {SCRCPY_INSTALL_DIR}")
        print("  Use --server http://server:8000 to auto-download")
        print("  Or use --scrcpy-path to specify manually")
        print("=" * 50)
        # Still start the agent so /api/install endpoint is available
        scrcpy_path = os.path.join(SCRCPY_INSTALL_DIR, "scrcpy.exe" if IS_WINDOWS else "scrcpy")

    if not adb_path:
        adb_path = os.path.join(SCRCPY_INSTALL_DIR, "adb.exe" if IS_WINDOWS else "adb")

    # Find available port (auto-retry if blocked)
    port = args.port
    if not try_bind_port(port):
        print(f"[Agent] Port {port} is not available, trying alternatives...")
        found = False
        for offset in range(1, MAX_PORT_RETRIES + 1):
            candidate = port + offset
            if try_bind_port(candidate):
                print(f"[Agent] Using port {candidate} instead.")
                port = candidate
                found = True
                break
        if not found:
            print(f"[Agent] ERROR: Could not find an available port ({args.port}-{args.port + MAX_PORT_RETRIES}).")
            print(f"         Try specifying a different port with --port <number>")
            sys.exit(1)

    print("=" * 50)
    print("  Scrcpy Client Agent")
    print(f"  Port:    {port}")
    print(f"  Server:  {server_url or '(not set)'}")
    print(f"  Scrcpy:  {scrcpy_path}")
    print(f"    Exists: {os.path.exists(scrcpy_path)}")
    print(f"  ADB:     {adb_path}")
    print(f"    Exists: {os.path.exists(adb_path)}")
    print(f"  Install: {SCRCPY_INSTALL_DIR}")
    print("=" * 50)

    app = create_app(scrcpy_path, adb_path, server_url, port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
