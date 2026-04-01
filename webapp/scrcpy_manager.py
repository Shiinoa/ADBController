"""
Scrcpy Manager for Real-time Screen Streaming
Supports:
  1. Native scrcpy window (ScrcpyWindowManager) - opens on server desktop
"""
import asyncio
import subprocess
import socket
import os
import logging
import shlex
from typing import Optional, AsyncGenerator
from config import ADB_PATH, SCRCPY_PATH, SCRCPY_SERVER_PATH
from adb_manager import validate_ip

logger = logging.getLogger(__name__)

# ADB forward host: use 127.0.0.1 locally, or host.docker.internal in Docker
ADB_FORWARD_HOST = os.environ.get('ADB_FORWARD_HOST', '127.0.0.1')

# Scrcpy server settings
SCRCPY_SERVER_VERSION = "3.2"
SCRCPY_SERVER_FILENAME = "scrcpy-server"
DEVICE_SERVER_PATH = "/data/local/tmp/scrcpy-server.jar"

class ScrcpyManager:
    def __init__(self):
        self.adb_path = ADB_PATH
        self.scrcpy_path = SCRCPY_PATH
        self.server_path = SCRCPY_SERVER_PATH
        self.active_sessions = {}

    def _run_adb(self, command: str) -> str:
        """Run ADB command synchronously"""
        try:
            # Use list-based command (more secure than shell=True)
            cmd_parts = shlex.split(command, posix=False)
            full_cmd = [self.adb_path] + cmd_parts
            result = subprocess.run(
                full_cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return "Error: Command timed out"
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"ADB command error: {e}")
            return f"Error: {e}"

    async def _run_adb_async(self, command: str) -> str:
        """Run ADB command asynchronously"""
        return await asyncio.to_thread(self._run_adb, command)

    async def push_server(self, ip: str) -> bool:
        """Push scrcpy-server to device"""
        if not self.server_path or not os.path.exists(self.server_path):
            print(f"[Scrcpy] Server not found: {self.server_path}")
            return False

        # Connect first
        await self._run_adb_async(f"connect {ip}")

        # Push server file
        result = await self._run_adb_async(f'-s {ip} push "{self.server_path}" {DEVICE_SERVER_PATH}')
        success = "pushed" in result.lower() or "skipped" in result.lower()
        print(f"[Scrcpy] Push server: {result.strip()}")
        return success

    async def start_server(self, ip: str, max_size: int = 1280, bit_rate: int = 2000000) -> Optional[subprocess.Popen]:
        """Start scrcpy-server on device and return process"""
        # Push server first
        if not await self.push_server(ip):
            return None

        # Set up port forwarding
        local_port = 27183 + hash(ip) % 1000  # Unique port per device
        await self._run_adb_async(f"-s {ip} forward tcp:{local_port} localabstract:scrcpy")

        # Start scrcpy-server on device
        # Using CLASSPATH method
        server_cmd = (
            f'-s {ip} shell CLASSPATH={DEVICE_SERVER_PATH} '
            f'app_process / com.genymobile.scrcpy.Server {SCRCPY_SERVER_VERSION} '
            f'tunnel_forward=true video_bit_rate={bit_rate} max_size={max_size} '
            f'log_level=info max_fps=30 video_codec=h264 audio=false control=true'
        )

        logger.info(f"[Scrcpy] Starting server on {ip}, port {local_port}")

        # Start server process (using list-based command)
        # Note: For complex shell commands with environment variables, we need shell=True on the device side
        # but we can still use list-based command for the local adb invocation
        full_cmd = [
            self.adb_path, "-s", ip, "shell",
            f"CLASSPATH={DEVICE_SERVER_PATH}",
            "app_process", "/", "com.genymobile.scrcpy.Server", SCRCPY_SERVER_VERSION,
            "tunnel_forward=true", f"video_bit_rate={bit_rate}", f"max_size={max_size}",
            "log_level=info", "max_fps=30", "video_codec=h264", "audio=false", "control=true"
        ]

        process = subprocess.Popen(
            full_cmd,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # Wait a bit for server to start
        await asyncio.sleep(1)

        self.active_sessions[ip] = {
            "process": process,
            "port": local_port
        }

        return process

    async def connect_to_stream(self, ip: str) -> Optional[socket.socket]:
        """Connect to scrcpy server video stream"""
        session = self.active_sessions.get(ip)
        if not session:
            return None

        local_port = session["port"]

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((ADB_FORWARD_HOST, local_port))
            sock.setblocking(False)

            # Read device info (first 64 bytes)
            device_info = sock.recv(64)
            device_name = device_info[:64].decode('utf-8', errors='ignore').strip('\x00')
            print(f"[Scrcpy] Connected to: {device_name}")

            return sock
        except Exception as e:
            print(f"[Scrcpy] Connection error: {e}")
            return None

    async def stream_frames(self, ip: str) -> AsyncGenerator[bytes, None]:
        """Generator that yields H.264 video frames"""
        # Start server if not running
        if ip not in self.active_sessions:
            await self.start_server(ip)
            await asyncio.sleep(1)

        sock = await self.connect_to_stream(ip)
        if not sock:
            print(f"[Scrcpy] Failed to connect stream for {ip}")
            return

        buffer = b""
        try:
            while ip in self.active_sessions:
                try:
                    # Read data from socket
                    data = await asyncio.to_thread(sock.recv, 65536)
                    if not data:
                        break

                    buffer += data

                    # Find NAL units (H.264 frames start with 0x00 0x00 0x00 0x01 or 0x00 0x00 0x01)
                    while True:
                        # Look for NAL unit separator
                        start_code_3 = buffer.find(b'\x00\x00\x01')
                        start_code_4 = buffer.find(b'\x00\x00\x00\x01')

                        if start_code_4 >= 0 and (start_code_3 < 0 or start_code_4 < start_code_3):
                            start = start_code_4
                            start_len = 4
                        elif start_code_3 >= 0:
                            start = start_code_3
                            start_len = 3
                        else:
                            break

                        # Look for next NAL unit
                        next_3 = buffer.find(b'\x00\x00\x01', start + start_len)
                        next_4 = buffer.find(b'\x00\x00\x00\x01', start + start_len)

                        if next_4 >= 0 and (next_3 < 0 or next_4 < next_3):
                            end = next_4
                        elif next_3 >= 0:
                            end = next_3
                        else:
                            # No complete NAL unit yet, wait for more data
                            break

                        # Extract NAL unit
                        nal_unit = buffer[start:end]
                        buffer = buffer[end:]

                        if len(nal_unit) > 4:
                            yield nal_unit

                except BlockingIOError:
                    await asyncio.sleep(0.01)
                except socket.timeout:
                    await asyncio.sleep(0.01)
                except Exception as e:
                    print(f"[Scrcpy] Stream error: {e}")
                    break

        finally:
            sock.close()

    async def stop_session(self, ip: str):
        """Stop scrcpy session for device"""
        session = self.active_sessions.pop(ip, None)
        if session:
            process = session.get("process")
            if process:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

            # Remove port forwarding
            local_port = session.get("port")
            if local_port:
                await self._run_adb_async(f"-s {ip} forward --remove tcp:{local_port}")

            logger.info(f"[Scrcpy] Session stopped for {ip}")

    async def stop_all(self):
        """Stop all active sessions"""
        for ip in list(self.active_sessions.keys()):
            await self.stop_session(ip)


# Alternative: Simple scrcpy launch (opens window)
class ScrcpyWindowManager:
    """Manages scrcpy windows - simpler but opens desktop window"""

    def __init__(self):
        self.scrcpy_path = SCRCPY_PATH
        self.adb_path = ADB_PATH
        self.processes = {}
        self.last_error = None

    def _run_adb_sync(self, command: str) -> str:
        """Run ADB command synchronously"""
        try:
            # Use list-based command (more secure than shell=True)
            cmd_parts = shlex.split(command, posix=False)
            full_cmd = [self.adb_path] + cmd_parts
            result = subprocess.run(
                full_cmd,
                shell=False,
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return "Error: Command timed out"
        except (OSError, subprocess.SubprocessError) as e:
            logger.error(f"ADB command error: {e}")
            return f"Error: {e}"

    async def launch(self, ip: str, title: Optional[str] = None) -> dict:
        """Launch scrcpy window for device"""
        self.last_error = None

        if not validate_ip(ip):
            self.last_error = "Invalid IP address"
            return {"success": False, "message": self.last_error}

        # Check if scrcpy path exists
        if not self.scrcpy_path:
            self.last_error = "scrcpy.exe not found in config"
            print(f"[Scrcpy] {self.last_error}")
            return {"success": False, "message": self.last_error}

        if not os.path.exists(self.scrcpy_path):
            self.last_error = f"scrcpy.exe not found at: {self.scrcpy_path}"
            print(f"[Scrcpy] {self.last_error}")
            return {"success": False, "message": self.last_error}

        # Connect to device via ADB first
        print(f"[Scrcpy] Connecting to {ip} via ADB...")
        connect_result = await asyncio.to_thread(self._run_adb_sync, f"connect {ip}")
        print(f"[Scrcpy] ADB connect result: {connect_result.strip()}")

        # Check if connected
        if "connected" not in connect_result.lower() and "already" not in connect_result.lower():
            self.last_error = f"Failed to connect ADB to {ip}: {connect_result.strip()}"
            print(f"[Scrcpy] {self.last_error}")
            return {"success": False, "message": self.last_error}

        # Close existing if any
        await self.close(ip)

        title = title or f"Remote: {ip}"

        cmd = [
            self.scrcpy_path,
            "-s", ip,
            "--window-title", title,
            "--max-size", "1280",
            "--max-fps", "30",
            "--no-audio"
        ]

        print(f"[Scrcpy] Launching: {' '.join(cmd)}")

        try:
            process = subprocess.Popen(
                cmd,
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            await asyncio.sleep(1.0)

            if process.poll() is not None:
                self.last_error = f"Scrcpy exited immediately (code: {process.returncode})"
                print(f"[Scrcpy] {self.last_error}")
                return {"success": False, "message": self.last_error}

            self.processes[ip] = process
            print(f"[Scrcpy] Window launched for {ip} (PID: {process.pid})")
            return {"success": True, "message": "Scrcpy window opened - check your desktop!"}
        except FileNotFoundError:
            self.last_error = f"scrcpy.exe not found at: {self.scrcpy_path}"
            print(f"[Scrcpy] {self.last_error}")
            return {"success": False, "message": self.last_error}
        except Exception as e:
            self.last_error = f"Launch error: {str(e)}"
            print(f"[Scrcpy] {self.last_error}")
            return {"success": False, "message": self.last_error}

    async def close(self, ip: str):
        """Close scrcpy window for device"""
        process = self.processes.pop(ip, None)

        # If we have a process object, terminate it directly
        if process:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
            logger.info(f"[Scrcpy] Closed window for {ip}")

    async def close_all(self):
        """Close all scrcpy windows"""
        for ip in list(self.processes.keys()):
            await self.close(ip)


# Global instances
scrcpy_manager = ScrcpyManager()
scrcpy_window = ScrcpyWindowManager()
