"""
ADB Manager - Backend logic for ADB device management
Migrated from original Tkinter app with async support
"""
import asyncio
import subprocess
import csv
import os
import re
import datetime
import time
import socket
import logging
import shlex
import ipaddress
from typing import List, Dict, Optional, Callable
from config import (
    ADB_PATH, SCRCPY_PATH, CSV_PATH, SCREENSHOT_DIR, REPORT_DIR,
    CACHE_ALERT_THRESHOLD_MB, RAM_LOW_THRESHOLD_MB, STORAGE_LOW_THRESHOLD_GB,
    DEFAULT_APP_PACKAGE, IS_WINDOWS
)
from services.device_inventory import load_devices as _load_csv_cached

# Setup logging
logger = logging.getLogger(__name__)


def validate_ip(ip: str) -> bool:
    """Validate IP address format"""
    try:
        # Handle IP:port format (e.g., 192.168.1.1:5555)
        ip_only = ip.split(':')[0] if ':' in ip else ip
        ipaddress.ip_address(ip_only)
        return True
    except ValueError:
        return False


def sanitize_device_name(name: str, max_length: int = 32) -> str:
    """Sanitize device name to prevent command injection"""
    # Only allow alphanumeric, dash, underscore
    safe = "".join(c for c in name if c.isalnum() or c in ('-', '_', ' '))
    # Replace spaces with underscores
    safe = safe.replace(' ', '_')
    # Limit length
    return safe[:max_length] if len(safe) > max_length else safe


PACKAGE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def validate_package_name(package: str) -> bool:
    """Validate Android package name format."""
    return bool(PACKAGE_NAME_RE.fullmatch(package or ""))


class ADBManager:
    """Manages ADB commands and device operations"""

    def __init__(self):
        self.adb_path = ADB_PATH
        self.scrcpy_path = SCRCPY_PATH
        # Connection cache: skip redundant ensure_connected within TTL window
        self._connect_cache: Dict[str, float] = {}
        self._connect_ttl = 30  # seconds

    def run_adb(self, command: str) -> str:
        """Execute ADB command synchronously (using list-based command for security)"""
        if not self.adb_path or not os.path.exists(self.adb_path):
            return "Error: ADB executable not found"
        try:
            # Build command as list (safer than shell=True)
            # Use shlex.split with posix mode based on OS
            cmd_parts = shlex.split(command, posix=not IS_WINDOWS)
            full_cmd = [self.adb_path] + cmd_parts

            # Build subprocess arguments (creationflags only on Windows)
            run_kwargs = {
                'shell': False,  # Secure: no shell injection
                'capture_output': True,
                'text': True,
                'encoding': 'utf-8',
                'errors': 'ignore',
                'timeout': 60  # Add timeout to prevent hanging
            }

            # Windows-specific: hide console window
            if IS_WINDOWS:
                run_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW

            result = subprocess.run(full_cmd, **run_kwargs)

            if result.stdout.strip():
                return result.stdout.strip()
            elif result.stderr.strip():
                return f"Error: {result.stderr.strip()}"
            else:
                return ""
        except subprocess.TimeoutExpired:
            logger.warning(f"ADB command timed out: {command}")
            return "Error: Command timed out"
        except FileNotFoundError:
            logger.error(f"ADB executable not found: {self.adb_path}")
            return "Error: ADB executable not found"
        except (OSError, ValueError) as e:
            logger.error(f"ADB command error: {e}")
            return f"Error: {str(e)}"
        except Exception as e:
            logger.exception(f"Unexpected error in run_adb: {e}")
            return f"Exception: {str(e)}"

    async def run_adb_async(self, command: str) -> str:
        """Execute ADB command asynchronously using thread pool"""
        return await asyncio.to_thread(self.run_adb, command)

    async def fast_port_check(self, ip: str, port: int = 5555, timeout: float = 3.0) -> bool:
        """Fast TCP port check - much faster than adb connect for offline devices"""
        def _check():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                result = sock.connect_ex((ip, port))
                return result == 0
            except (socket.timeout, socket.error, OSError) as e:
                logger.debug(f"Port check failed for {ip}:{port} - {e}")
                return False
            finally:
                if sock:
                    try:
                        sock.close()
                    except (socket.error, OSError):
                        pass
        return await asyncio.to_thread(_check)

    async def ensure_connected(self, ip: str, fast_check: bool = True) -> bool:
        """Ensure device is connected before running commands"""
        # Validate IP format first
        if not validate_ip(ip):
            logger.warning(f"Invalid IP address format: {ip}")
            return False

        # Fast port check first (skip slow adb connect for offline devices)
        if fast_check:
            port_open = await self.fast_port_check(ip)
            if not port_open:
                return False

        # Try to connect
        result = await self.run_adb_async(f"connect {ip}")
        return "connected" in result.lower() or "already connected" in result.lower()

    async def check_device_status(self, ip: str) -> str:
        """Check device authorization status: 'device', 'unauthorized', 'offline', or 'disconnected'"""
        devices_output = await self.run_adb_async("devices")
        for line in devices_output.splitlines():
            if ip in line:
                if "\tdevice" in line:
                    return "device"
                elif "\tunauthorized" in line:
                    return "unauthorized"
                elif "\toffline" in line:
                    return "offline"
        return "disconnected"

    async def fast_connect_and_check(self, ip: str) -> Dict:
        """
        Fast connection check: TCP port check -> ADB connect -> status
        Returns: {"online": bool, "status": str}
        """
        # Step 1: Fast TCP port check (1 second timeout)
        port_open = await self.fast_port_check(ip)
        if not port_open:
            return {"online": False, "status": "offline"}

        # Step 2: ADB connect (port is open, so this should be fast)
        connect_result = await self.run_adb_async(f"connect {ip}")
        connected = "connected" in connect_result.lower() or "already connected" in connect_result.lower()

        if not connected:
            return {"online": False, "status": "offline"}

        # Step 3: Check status from adb devices
        devices_output = await self.run_adb_async("devices")
        for line in devices_output.splitlines():
            if ip in line:
                if "\tunauthorized" in line:
                    return {"online": True, "status": "unauthorized"}
                elif "\toffline" in line:
                    return {"online": False, "status": "offline"}
                elif "\tdevice" in line:
                    return {"online": True, "status": "online"}

        return {"online": True, "status": "online"}

    async def run_adb_on_device(self, ip: str, command: str) -> str:
        """Run ADB command on device, auto-connect if needed (cached)"""
        if not validate_ip(ip):
            return "Error: Invalid IP address"

        now = time.time()
        if now - self._connect_cache.get(ip, 0) > self._connect_ttl:
            if not await self.ensure_connected(ip):
                return "Error: Device not reachable or not authorized"
            self._connect_cache[ip] = now
        return await self.run_adb_async(f"-s {ip} {command}")

    def get_devices_from_csv(self, keyword: Optional[str] = None, owner_username: Optional[str] = None) -> List[Dict]:
        """Load device list from CSV (mtime-cached) with optional search filter"""
        all_devices = _load_csv_cached(owner_username=owner_username)

        # Ensure each row has an IP (auto-detect if missing)
        data_list = []
        for row in all_devices:
            t_ip = row.get("IP")
            if not t_ip:
                for v in row.values():
                    if v and isinstance(v, str) and (v.startswith("10.10.") or v.startswith("192.168.")):
                        t_ip = v
                        break
            if not t_ip:
                continue

            clean_row = dict(row)
            clean_row["IP"] = t_ip

            if keyword:
                all_text = " ".join(str(v) for v in clean_row.values() if v is not None).lower()
                search_terms = [k.strip().lower() for k in keyword.split(',') if k.strip()]
                if not search_terms or any(term in all_text for term in search_terms):
                    data_list.append(clean_row)
            else:
                data_list.append(clean_row)

        return sorted(data_list, key=lambda x: x.get("IP", ""))

    def get_connected_ips(self) -> List[str]:
        """Get list of currently connected device IPs"""
        res = self.run_adb("devices")
        connected = []
        for line in res.splitlines():
            if "\tdevice" in line:
                connected.append(line.split("\t")[0])
        return connected

    async def ping_device(self, ip: str) -> bool:
        """Ping a device to check if it's online"""
        def _ping():
            try:
                # Use list-based command (secure, no shell injection)
                # Different flags for Windows vs Linux
                if IS_WINDOWS:
                    cmd = ["ping", "-n", "2", "-w", "2500", ip]
                else:
                    cmd = ["ping", "-c", "2", "-W", "3", ip]

                # Build subprocess arguments
                run_kwargs = {
                    'shell': False,  # Secure: no shell injection
                    'stdout': subprocess.DEVNULL,
                    'stderr': subprocess.DEVNULL,
                    'timeout': 10
                }

                # Windows-specific: hide console window
                if IS_WINDOWS:
                    run_kwargs['creationflags'] = 0x08000000  # CREATE_NO_WINDOW

                result = subprocess.run(cmd, **run_kwargs)
                return result.returncode == 0
            except subprocess.TimeoutExpired:
                logger.debug(f"Ping timeout for {ip}")
                return False
            except (OSError, subprocess.SubprocessError) as e:
                logger.debug(f"Ping failed for {ip}: {e}")
                return False

        return await asyncio.to_thread(_ping)

    async def connect_device(self, ip: str) -> Dict:
        """Connect to a device via ADB"""
        if not validate_ip(ip):
            return {"ip": ip, "success": False, "message": "Invalid IP address", "status": "invalid"}

        result = await self.run_adb_async(f"connect {ip}")
        success = "connected" in result.lower()

        # Check authorization status
        status = await self.check_device_status(ip)
        if status == "unauthorized":
            return {"ip": ip, "success": False, "message": "Connected but UNAUTHORIZED - Check TV for authorization dialog", "status": status}
        elif status == "offline":
            return {"ip": ip, "success": False, "message": "Device offline", "status": status}

        return {"ip": ip, "success": success, "message": result, "status": status}

    async def health_check(self, ip: str, include_cache: bool = True) -> Dict:
        """Perform health check on a device (RAM, Storage, Network, Cache)"""
        result = {
            "ip": ip,
            "ram_mb": 0,
            "ram_total_gb": 0,
            "ram_free_gb": 0,
            "storage_free_gb": 0,
            "network_type": "Unknown",
            "network_status": "Unknown",
            "rssi": None,
            "status": "Unknown",
            "action": "",
            # Cache info (new)
            "cache_mb": 0,
            "cache_alert": False
        }

        # Check RAM
        mem_res = await self.run_adb_on_device(ip, "shell cat /proc/meminfo")
        try:
            total_match = re.search(r"MemTotal:\s+(\d+)", mem_res)
            avail_match = re.search(r"MemAvailable:\s+(\d+)", mem_res) or re.search(r"MemFree:\s+(\d+)", mem_res)
            if total_match:
                result["ram_total_gb"] = int(total_match.group(1)) / 1024 / 1024
            if avail_match:
                result["ram_mb"] = int(avail_match.group(1)) / 1024
                result["ram_free_gb"] = int(avail_match.group(1)) / 1024 / 1024
        except (ValueError, AttributeError, IndexError) as e:
            logger.debug(f"RAM parse error for {ip}: {e}")

        # Check Storage
        df_res = await self.run_adb_on_device(ip, "shell df -h /data")
        try:
            parts = df_res.splitlines()[1].split()
            raw = parts[-3]
            if 'G' in raw:
                result["storage_free_gb"] = float(raw.replace('G', ''))
            else:
                result["storage_free_gb"] = float(raw.replace('M', '')) / 1024
        except (ValueError, IndexError) as e:
            logger.debug(f"Storage parse error for {ip}: {e}")

        # Determine status (using config thresholds)
        if result["ram_mb"] < RAM_LOW_THRESHOLD_MB:
            result["status"] = "Laggy (Low RAM)"
            result["action"] = "REBOOT needed"
        elif result["storage_free_gb"] < STORAGE_LOW_THRESHOLD_GB:
            result["status"] = "Full Storage"
            result["action"] = "Clear Data"
        else:
            result["status"] = "Good"

        # Check Network (Ethernet or Wi-Fi)
        eth = await self.run_adb_on_device(ip, "shell ip addr show eth0")
        if "inet " in eth:
            result["network_type"] = "Ethernet"
            result["network_status"] = "Stable"
        else:
            # Check Wi-Fi
            rssi = -100
            wifi_out = await self.run_adb_on_device(ip, "shell cmd wifi status")
            if "RSSI:" in wifi_out:
                try:
                    rssi = int(wifi_out.split("RSSI:")[1].split()[0].strip())
                except (ValueError, IndexError) as e:
                    logger.debug(f"RSSI parse error for {ip}: {e}")
            else:
                wifi_out = await self.run_adb_on_device(ip, "shell dumpsys wifi")
                try:
                    match = re.findall(r"(-\d+)", wifi_out)
                    if match:
                        rssi = int(match[0])
                except (ValueError, IndexError) as e:
                    logger.debug(f"WiFi RSSI parse error for {ip}: {e}")

            result["rssi"] = rssi
            result["network_type"] = "Wi-Fi"
            if rssi > -65:
                result["network_status"] = "Excellent"
            elif rssi > -85:
                result["network_status"] = "Fair"
            elif rssi > -100:
                result["network_status"] = "Weak"
            else:
                result["network_status"] = "Connected"

        # Check App Cache (MachineMonitor)
        if include_cache:
            cache_info = await self.get_app_storage_info(ip, DEFAULT_APP_PACKAGE)
            if cache_info.get("success"):
                result["cache_mb"] = round(cache_info.get("cache_mb", 0), 2)
                result["data_mb"] = round(cache_info.get("data_mb", 0), 2)
                # Alert if cache exceeds threshold (from config)
                if result["cache_mb"] > CACHE_ALERT_THRESHOLD_MB:
                    result["cache_alert"] = True
                    if result["status"] == "Good":
                        result["status"] = "High Cache"
                        result["action"] = "Clear Cache"

        return result

    async def check_screen_status(self, ip: str) -> Dict:
        """Check if device screen is on or off"""
        res = await self.run_adb_on_device(ip, "shell dumpsys power")
        is_on = "mWakefulness=Awake" in res
        return {"ip": ip, "screen_on": is_on}

    async def check_memory(self, ip: str) -> Dict:
        """Get RAM and storage info"""
        mem_res = await self.run_adb_on_device(ip, "shell cat /proc/meminfo")
        df_res = await self.run_adb_on_device(ip, "shell df -h /data")

        result = {"ip": ip, "ram": "N/A", "storage": "N/A"}

        # Check for authorization error
        if "unauthorized" in mem_res.lower() or "error" in mem_res.lower():
            result["ram"] = "Device Unauthorized"
            result["storage"] = "Device Unauthorized"
            return result

        try:
            total_match = re.search(r"MemTotal:\s+(\d+)", mem_res)
            avail_match = re.search(r"MemAvailable:\s+(\d+)", mem_res)
            if total_match:
                total_gb = int(total_match.group(1)) / 1024 / 1024
                avail_gb = int(avail_match.group(1)) / 1024 / 1024 if avail_match else 0
                result["ram"] = f"{avail_gb:.2f}/{total_gb:.2f} GB Free"
        except (ValueError, AttributeError, IndexError) as e:
            logger.debug(f"Memory parse error for {ip}: {e}")

        try:
            # Validate df output before parsing
            lines = df_res.splitlines()
            if len(lines) >= 2 and "unauthorized" not in df_res.lower():
                parts = lines[1].split()
                if len(parts) >= 4:
                    result["storage"] = f"{parts[-3]} Free / {parts[1]} Total"
        except (ValueError, IndexError) as e:
            logger.debug(f"Storage parse error for {ip}: {e}")

        return result

    async def get_full_info(self, ip: str) -> Dict:
        """Get full device information"""
        # Try eth0 first, then wlan0
        info = await self.run_adb_on_device(ip, "shell ip addr show eth0")
        if "link/ether" not in info:
            info = await self.run_adb_on_device(ip, "shell ip addr show wlan0")

        device_name = await self.run_adb_on_device(ip, "shell settings get global device_name")
        model = await self.run_adb_on_device(ip, "shell getprop ro.product.model")

        mac = "N/A"
        if "link/ether" in info:
            try:
                mac = info.split("link/ether ")[1].split()[0]
            except (IndexError, ValueError) as e:
                logger.debug(f"MAC parse error for {ip}: {e}")

        # Clean up the values
        device_name_clean = device_name.strip() if device_name else ""
        model_clean = model.strip() if model else ""

        return {
            "ip": ip,
            "device_name": device_name_clean if device_name_clean and "Error" not in device_name_clean and "null" not in device_name_clean.lower() else "Unknown",
            "model": model_clean if model_clean and "Error" not in model_clean else "Unknown",
            "mac": mac,
            "network_info": info
        }

    async def wake_device(self, ip: str) -> Dict:
        """Wake device (turn screen on)"""
        await self.run_adb_on_device(ip, "shell input keyevent 224")
        return {"ip": ip, "action": "wake", "success": True}

    async def sleep_device(self, ip: str) -> Dict:
        """Sleep device (turn screen off)"""
        await self.run_adb_on_device(ip, "shell input keyevent 223")
        return {"ip": ip, "action": "sleep", "success": True}

    async def set_brightness(self, ip: str, level: int) -> Dict:
        """Set screen brightness (0-255)"""
        await self.run_adb_on_device(ip, f"shell settings put system screen_brightness {level}")
        return {"ip": ip, "action": "brightness", "level": level, "success": True}

    async def take_screenshot(self, ip: str) -> Dict:
        """Take screenshot from device"""
        import base64

        folder = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(SCREENSHOT_DIR, folder)
        if not os.path.exists(path):
            os.makedirs(path)

        fname = f"{ip.replace(':', '_')}.png"
        local_path = os.path.join(path, fname)

        # Take screenshot on device
        cap_result = await self.run_adb_on_device(ip, "shell screencap -p /sdcard/screenshot.png")

        # Pull the file
        pull_result = await self.run_adb_async(f'-s {ip} pull /sdcard/screenshot.png "{local_path}"')

        success = os.path.exists(local_path)

        # Read file and convert to base64 for web display
        screenshot_base64 = None
        if success:
            try:
                with open(local_path, 'rb') as f:
                    screenshot_base64 = base64.b64encode(f.read()).decode('utf-8')
            except Exception as e:
                print(f"[Screenshot] Error reading file: {e}")

        return {
            "ip": ip,
            "action": "screenshot",
            "path": local_path if success else None,
            "success": success,
            "message": pull_result if not success else "OK",
            "screenshot": screenshot_base64
        }

    async def open_app(self, ip: str, package: str = "asd.kce.machinemonitor") -> Dict:
        """Open an app on device"""
        if not validate_package_name(package):
            return {"ip": ip, "action": "open_app", "package": package, "success": False, "message": "Invalid package name"}
        await self.run_adb_on_device(ip, f"shell monkey -p {package} -c android.intent.category.LAUNCHER 1")
        return {"ip": ip, "action": "open_app", "package": package, "success": True}

    async def check_app_status(self, ip: str, package: str = "asd.kce.machinemonitor") -> Dict:
        """Check if an app is running (foreground or background)"""
        if not validate_package_name(package):
            return {
                "ip": ip,
                "package": package,
                "running": False,
                "pid": None,
                "foreground": False,
                "status": "INVALID_PACKAGE",
                "message": "Invalid package name"
            }
        # Method 1: Check if process exists using pidof (most reliable)
        pidof_result = await self.run_adb_on_device(ip, f'shell pidof {package}')
        pid = pidof_result.strip()

        if pid and pid.isdigit():
            # Process is running - always treat as foreground
            return {
                "ip": ip,
                "package": package,
                "running": True,
                "pid": pid,
                "foreground": True,
                "status": "FOREGROUND"
            }

        # Method 2: Fallback - check activity manager
        am_result = await self.run_adb_on_device(ip, f'shell "dumpsys activity processes | grep {package}"')
        if package in am_result and "pid=" in am_result:
            # Extract PID from activity manager output
            pid_match = re.search(r"pid=(\d+)", am_result)
            pid = pid_match.group(1) if pid_match else "unknown"
            return {
                "ip": ip,
                "package": package,
                "running": True,
                "pid": pid,
                "foreground": True,
                "status": "FOREGROUND"
            }

        return {
            "ip": ip,
            "package": package,
            "running": False,
            "pid": None,
            "foreground": False,
            "status": "STOPPED"
        }

    async def clear_app_data(self, ip: str, package: str = "asd.kce.machinemonitor") -> Dict:
        """Clear app data"""
        if not validate_package_name(package):
            return {"ip": ip, "package": package, "success": False, "message": "Invalid package name"}
        result = await self.run_adb_on_device(ip, f"shell pm clear {package}")
        success = "Success" in result
        return {"ip": ip, "package": package, "success": success, "message": result}

    async def clear_app_cache(self, ip: str, package: str = "asd.kce.machinemonitor") -> Dict:
        """Clear app cache only (not data) - Android 8+"""
        if not validate_package_name(package):
            return {"ip": ip, "package": package, "success": False, "message": "Invalid package name"}
        # Try cmd package clear-cache-files first (Android 8+)
        result = await self.run_adb_on_device(ip, f"shell cmd package clear-cache-files {package}")
        if "Exception" in result or "Error" in result:
            # Fallback: clear via pm (clears all data, not just cache)
            return {"ip": ip, "package": package, "success": False, "message": "Clear cache not supported, use clear data instead"}
        return {"ip": ip, "package": package, "success": True, "message": "Cache cleared"}

    async def get_app_storage_info(self, ip: str, package: str = "asd.kce.machinemonitor") -> Dict:
        """
        Get app storage info (cache, data, code size)
        Tries multiple methods for different Android versions
        """
        result = {
            "ip": ip,
            "package": package,
            "cache_bytes": 0,
            "data_bytes": 0,
            "code_bytes": 0,
            "cache_mb": 0,
            "data_mb": 0,
            "code_mb": 0,
            "total_mb": 0,
            "success": False,
            "message": "",
            "method": ""
        }

        if not validate_package_name(package):
            result["message"] = "Invalid package name"
            return result

        # Method 1: Try dumpsys diskstats (works on non-rooted Android TV)
        try:
            diskstats_output = await self.run_adb_on_device(ip, "shell dumpsys diskstats")

            # Parse Package Names array
            pkg_match = re.search(r'Package Names: \[([^\]]+)\]', diskstats_output)
            cache_match = re.search(r'Cache Sizes: \[([^\]]+)\]', diskstats_output)
            app_match = re.search(r'App Sizes: \[([^\]]+)\]', diskstats_output)

            if pkg_match and cache_match:
                # Parse package names (remove quotes)
                packages_str = pkg_match.group(1)
                packages = [p.strip().strip('"') for p in packages_str.split(',')]

                # Parse cache sizes
                cache_sizes = [int(s.strip()) for s in cache_match.group(1).split(',')]

                # Parse app sizes if available
                app_sizes = []
                if app_match:
                    app_sizes = [int(s.strip()) for s in app_match.group(1).split(',')]

                # Find package index
                if package in packages:
                    idx = packages.index(package)

                    if idx < len(cache_sizes):
                        result["cache_bytes"] = cache_sizes[idx]
                        result["cache_mb"] = cache_sizes[idx] / (1024 * 1024)

                    if idx < len(app_sizes):
                        result["data_bytes"] = app_sizes[idx]
                        result["data_mb"] = app_sizes[idx] / (1024 * 1024)

                    result["total_mb"] = result["cache_mb"] + result["data_mb"]
                    result["success"] = True
                    result["method"] = "diskstats"
                    logger.debug(f"[{ip}] diskstats: cache={result['cache_mb']:.2f}MB, data={result['data_mb']:.2f}MB")
                    return result
                else:
                    logger.debug(f"[{ip}] Package {package} not found in diskstats")
        except Exception as e:
            logger.debug(f"[{ip}] diskstats method failed: {e}")

        # Method 2: Try 'du' command on app directories (requires root)
        try:
            # Get cache size
            cache_result = await self.run_adb_on_device(
                ip, f'shell "du -s /data/data/{package}/cache 2>/dev/null || echo 0"'
            )
            cache_kb = self._parse_du_output(cache_result)

            # Get data size (entire app data directory)
            data_result = await self.run_adb_on_device(
                ip, f'shell "du -s /data/data/{package} 2>/dev/null || echo 0"'
            )
            data_kb = self._parse_du_output(data_result)

            if data_kb > 0:
                result["cache_bytes"] = cache_kb * 1024
                result["cache_mb"] = cache_kb / 1024
                result["data_bytes"] = (data_kb - cache_kb) * 1024  # Data without cache
                result["data_mb"] = (data_kb - cache_kb) / 1024
                result["total_mb"] = data_kb / 1024
                result["success"] = True
                result["method"] = "du"
                return result
        except Exception as e:
            logger.debug(f"[{ip}] du method failed: {e}")

        # Method 3: Try dumpsys package (for newer Android)
        output = await self.run_adb_on_device(ip, f"shell dumpsys package {package}")

        if "Unable to find package" in output:
            result["message"] = "Package not found"
            return result

        # Try multiple regex patterns
        patterns = [
            # Pattern 1: codeSize=12345 dataSize=67890 cacheSize=11111
            (r"codeSize=(\d+)", r"dataSize=(\d+)", r"cacheSize=(\d+)"),
            # Pattern 2: code=12345 data=67890 cache=11111
            (r"\bcode=(\d+)", r"\bdata=(\d+)", r"\bcache=(\d+)"),
        ]

        for code_pat, data_pat, cache_pat in patterns:
            code_match = re.search(code_pat, output)
            data_match = re.search(data_pat, output)
            cache_match = re.search(cache_pat, output)

            if code_match or data_match or cache_match:
                if code_match:
                    result["code_bytes"] = int(code_match.group(1))
                    result["code_mb"] = result["code_bytes"] / (1024 * 1024)
                if data_match:
                    result["data_bytes"] = int(data_match.group(1))
                    result["data_mb"] = result["data_bytes"] / (1024 * 1024)
                if cache_match:
                    result["cache_bytes"] = int(cache_match.group(1))
                    result["cache_mb"] = result["cache_bytes"] / (1024 * 1024)

                result["total_mb"] = result["code_mb"] + result["data_mb"] + result["cache_mb"]
                result["success"] = True
                result["method"] = "dumpsys_package"
                return result

        result["message"] = "Could not parse storage info"
        result["success"] = True  # Mark success but with 0 values (app exists but no cache)
        result["method"] = "fallback"
        return result

    def _parse_du_output(self, output: str) -> int:
        """Parse 'du' command output, returns size in KB"""
        try:
            # du output format: "12345\t/path/to/dir" or just "12345"
            line = output.strip().split('\n')[0]
            parts = line.split()
            if parts and parts[0].isdigit():
                return int(parts[0])
        except (ValueError, IndexError):
            pass
        return 0

    async def install_apk(self, ip: str, apk_path: str) -> Dict:
        """Install APK on device"""
        if not os.path.exists(apk_path):
            return {"ip": ip, "success": False, "message": "APK file not found"}
        await self.ensure_connected(ip)
        result = await self.run_adb_async(f'-s {ip} install -r "{apk_path}"')
        success = "Success" in result
        return {"ip": ip, "success": success, "message": result}

    async def reboot_device(self, ip: str) -> Dict:
        """Reboot device"""
        await self.run_adb_on_device(ip, "reboot")
        return {"ip": ip, "action": "reboot", "success": True}

    async def shutdown_device(self, ip: str) -> Dict:
        """Shutdown device"""
        await self.run_adb_on_device(ip, "reboot -p")
        return {"ip": ip, "action": "shutdown", "success": True}

    async def rename_device(self, ip: str, new_name: str) -> Dict:
        """Rename device (hostname, bluetooth name, device name)"""
        # Validate IP
        if not validate_ip(ip):
            return {"ip": ip, "success": False, "message": "Invalid IP address"}

        # Sanitize device name to prevent command injection
        safe_name = sanitize_device_name(new_name)
        if not safe_name:
            return {"ip": ip, "success": False, "message": "Invalid device name"}

        # Use shell quoting for extra safety
        await self.run_adb_on_device(ip, f"shell settings put global device_name '{safe_name}'")
        await self.run_adb_on_device(ip, f"shell setprop net.hostname '{safe_name}'")
        await self.run_adb_on_device(ip, f"shell settings put global bluetooth_name '{safe_name}'")

        return {"ip": ip, "new_name": safe_name, "success": True, "message": "Reboot recommended"}

    async def generate_report(self, devices: List[Dict], log_callback: Optional[Callable] = None) -> str:
        """Generate CSV report for selected devices"""
        # Load CSV map for asset lookup
        csv_map = {}
        if CSV_PATH and os.path.exists(CSV_PATH):
            try:
                with open(CSV_PATH, mode='r', encoding='utf-8-sig', errors='ignore') as f:
                    reader = csv.DictReader(f)
                    if reader.fieldnames:
                        reader.fieldnames = [n.strip() for n in reader.fieldnames]
                    for row in reader:
                        cln = {k: v.strip() for k, v in row.items() if k}
                        if "IP" in cln:
                            csv_map[cln["IP"]] = cln
            except (IOError, csv.Error) as e:
                logger.warning(f"CSV load error: {e}")

        data_rows = []
        for index, device in enumerate(devices, 1):
            ip = device.get("IP", device.get("ip", ""))
            gui_name = device.get("Asset Name", "")
            loc = device.get("Default Location", "")
            wc = device.get("Work Center", "")
            model = device.get("Model", "")

            row_data = csv_map.get(ip, {})
            asset_num = row_data.get("Asset", "") or row_data.get("Asset Number", "") or row_data.get("Inventory", "")

            # Ping check
            is_online = await self.ping_device(ip)

            status, note, app_status = "Offline", "Request timed out.", "Unknown"
            real_name, mac = gui_name, "N/A"

            if is_online:
                status, note = "Online", ""
                try:
                    n = await self.run_adb_async(f"-s {ip} shell settings get global device_name")
                    if n and "Error" not in n:
                        real_name = n.strip()

                    w = await self.run_adb_async(f"-s {ip} shell ip addr show wlan0")
                    if "link/ether" in w:
                        mac = w.split("link/ether ")[1].split()[0]

                    a = await self.run_adb_async(f'-s {ip} shell "dumpsys window | grep mCurrentFocus"')
                    app_status = "RUNNING" if "asd.kce.machinemonitor" in a else "STOPPED"
                except (IndexError, ValueError) as e:
                    logger.debug(f"Report data fetch error for {ip}: {e}")

            data_rows.append({
                "NO": index,
                "Device Name": real_name,
                "Asset": asset_num,
                "IP Address": ip,
                "MAC Address": mac,
                "Location": loc,
                "Type": model,
                "Work Center": wc,
                "Note": note,
                "Status": status,
                "App Status": app_status
            })

            if log_callback:
                await log_callback(f"Fetched {ip} - {status}")

        # Save report
        report_path = os.path.join(REPORT_DIR, f"Final_Report_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            with open(report_path, 'w', newline='', encoding='utf-8-sig') as f:
                fieldnames = ["NO", "Device Name", "Asset", "IP Address", "MAC Address", "Location", "Type", "Work Center", "Note", "Status", "App Status"]
                w = csv.DictWriter(f, fieldnames=fieldnames)
                w.writeheader()
                w.writerows(data_rows)
            return report_path
        except Exception as e:
            return f"Error: {str(e)}"

    # ==================== Remote Control Methods ====================

    async def get_screenshot_bytes(self, ip: str) -> bytes:
        """Get screenshot as bytes for streaming"""
        await self.ensure_connected(ip)

        def _capture():
            try:
                import tempfile
                temp_path = os.path.join(tempfile.gettempdir(), f"remote_{ip.replace(':', '_')}.png")

                # Use file-based method (more reliable on Windows)
                # Capture screenshot on device
                subprocess.run(
                    [self.adb_path, "-s", ip, "shell", "screencap", "-p", "/sdcard/remote_screen.png"],
                    capture_output=True,
                    timeout=10
                )

                # Pull file to local
                pull_result = subprocess.run(
                    [self.adb_path, "-s", ip, "pull", "/sdcard/remote_screen.png", temp_path],
                    capture_output=True,
                    timeout=10
                )

                if pull_result.returncode == 0 and os.path.exists(temp_path):
                    with open(temp_path, 'rb') as f:
                        data = f.read()
                    try:
                        os.remove(temp_path)
                    except (OSError, IOError) as e:
                        logger.debug(f"Failed to remove temp file {temp_path}: {e}")
                    if len(data) > 1000:  # Valid PNG should be > 1KB
                        return data

                return b""
            except subprocess.TimeoutExpired:
                logger.warning(f"Screenshot timeout for {ip}")
                return b""
            except (OSError, IOError, subprocess.SubprocessError) as e:
                logger.warning(f"Screenshot error for {ip}: {e}")
                return b""

        return await asyncio.to_thread(_capture)

    async def get_screen_size(self, ip: str) -> tuple:
        """Get device screen size"""
        result = await self.run_adb_on_device(ip, "shell wm size")
        # Output: "Physical size: 1920x1080"
        try:
            size_str = result.split(":")[1].strip()
            w, h = size_str.split("x")
            return int(w), int(h)
        except (ValueError, IndexError) as e:
            logger.debug(f"Screen size parse error for {ip}: {e}")
            return 1920, 1080  # Default

    async def send_tap(self, ip: str, x: int, y: int) -> Dict:
        """Send tap input at coordinates"""
        if not validate_ip(ip):
            return {"ip": ip, "action": "tap", "x": x, "y": y, "success": False, "message": "Invalid IP address"}
        if x < 0 or y < 0:
            return {"ip": ip, "action": "tap", "x": x, "y": y, "success": False, "message": "Coordinates must be non-negative"}
        await self.run_adb_on_device(ip, f"shell input tap {x} {y}")
        return {"ip": ip, "action": "tap", "x": x, "y": y, "success": True}

    async def send_swipe(self, ip: str, x1: int, y1: int, x2: int, y2: int, duration: int = 300) -> Dict:
        """Send swipe input"""
        if not validate_ip(ip):
            return {"ip": ip, "action": "swipe", "success": False, "message": "Invalid IP address"}
        if min(x1, y1, x2, y2, duration) < 0:
            return {"ip": ip, "action": "swipe", "success": False, "message": "Coordinates and duration must be non-negative"}
        await self.run_adb_on_device(ip, f"shell input swipe {x1} {y1} {x2} {y2} {duration}")
        return {"ip": ip, "action": "swipe", "success": True}

    async def send_key(self, ip: str, keycode: int) -> Dict:
        """Send key event (KEYCODE_HOME=3, KEYCODE_BACK=4, etc.)"""
        if not validate_ip(ip):
            return {"ip": ip, "action": "key", "keycode": keycode, "success": False, "message": "Invalid IP address"}
        if keycode < 0 or keycode > 9999:
            return {"ip": ip, "action": "key", "keycode": keycode, "success": False, "message": "Invalid keycode"}
        await self.run_adb_on_device(ip, f"shell input keyevent {keycode}")
        return {"ip": ip, "action": "key", "keycode": keycode, "success": True}

    async def send_text(self, ip: str, text: str) -> Dict:
        """Send text input"""
        if not validate_ip(ip):
            return {"ip": ip, "action": "text", "success": False, "message": "Invalid IP address"}
        if not text or len(text) > 200:
            return {"ip": ip, "action": "text", "success": False, "message": "Text must be between 1 and 200 characters"}
        if any(ord(ch) < 32 for ch in text):
            return {"ip": ip, "action": "text", "success": False, "message": "Control characters are not allowed"}
        if any(ch in text for ch in ['"', "'", '`', ';', '&', '|', '<', '>', '$']):
            return {"ip": ip, "action": "text", "success": False, "message": "Text contains unsupported characters"}
        # Escape special characters
        escaped = text.replace(" ", "%s")
        await self.run_adb_on_device(ip, f'shell input text "{escaped}"')
        return {"ip": ip, "action": "text", "success": True}


# Global instance
adb_manager = ADBManager()
