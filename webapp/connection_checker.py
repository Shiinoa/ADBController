"""
Background Connection Checker Service
Periodically checks device connectivity and caches results for fast dashboard/multiview updates
Also checks app cache for online devices
"""
import asyncio
import json
import socket
import time
import logging
from typing import Dict, List, Optional, Set
from datetime import datetime
from dataclasses import dataclass, field

from adb_manager import adb_manager
from websocket_manager import ws_manager
from config import DEFAULT_APP_PACKAGE, CACHE_ALERT_THRESHOLD_MB
from database import log_ping, get_setting, log_health_records_batch, cleanup_health_history

logger = logging.getLogger(__name__)


@dataclass
class DeviceStatus:
    """Cached device connection status"""
    ip: str
    online: bool
    status: str  # "online", "offline", "unauthorized", "error"
    response_time: Optional[float] = None
    last_checked: datetime = field(default_factory=datetime.now)
    last_online: Optional[datetime] = None
    check_count: int = 0
    consecutive_failures: int = 0
    # App status (for online devices)
    app_status: str = "unknown"  # "RUNNING", "STOPPED", "unknown"
    # Cache info (for online devices)
    cache_mb: float = 0.0
    data_mb: float = 0.0
    cache_alert: bool = False
    # Auto-reconnect state
    reconnect_attempts: int = 0
    reconnect_next_at: Optional[float] = None  # timestamp
    reconnect_circuit_open: bool = False


class ConnectionChecker:
    """
    Background service that periodically checks device connectivity
    and broadcasts updates via WebSocket
    """

    def __init__(self):
        # Cached status for all devices
        self._status_cache: Dict[str, DeviceStatus] = {}
        # Lock for thread-safe cache access
        self._cache_lock = asyncio.Lock()
        # Background task reference
        self._checker_task: Optional[asyncio.Task] = None
        # Control flags
        self._running = False
        self._check_interval = 30  # seconds between full checks
        self._fast_check_timeout = 3.0  # TCP check timeout (supports high-latency devices)
        self._offline_threshold = 3  # consecutive failures before marking offline
        # IPs to monitor (loaded from CSV)
        self._monitored_ips: Set[str] = set()
        # App cache monitoring
        self._cache_check_enabled = True
        self._app_package = DEFAULT_APP_PACKAGE
        # Screenshot cleanup (every 60 checks = ~30 min at 30s interval)
        self._cleanup_interval = 60
        self._cleanup_counter = 0
        # Health history logging (every 5 checks = ~2.5 min at 30s interval)
        self._history_interval = 5
        self._history_counter = 0

    async def start(self, check_interval: int = 30):
        """Start the background checker"""
        if self._running:
            logger.warning("[ConnectionChecker] Already running")
            return

        self._running = True
        self._check_interval = check_interval
        self._checker_task = asyncio.create_task(self._check_loop())
        logger.info(f"[ConnectionChecker] Started with {check_interval}s interval")

    async def stop(self):
        """Stop the background checker"""
        self._running = False
        if self._checker_task:
            self._checker_task.cancel()
            try:
                await self._checker_task
            except asyncio.CancelledError:
                pass
        logger.info("[ConnectionChecker] Stopped")

    async def _check_loop(self):
        """Main check loop"""
        while self._running:
            try:
                # Load devices from CSV
                await self._load_devices()

                if self._monitored_ips:
                    # Check all devices in parallel
                    await self._check_all_devices()

                    # Broadcast online/offline status immediately (fast update)
                    await self._broadcast_status()

                    # Check app running status for online devices (lightweight pidof)
                    await self._check_online_app_status()

                    # Broadcast again with app status
                    await self._broadcast_status()

                    # Check app cache for online devices only (heavy, runs last)
                    if self._cache_check_enabled:
                        await self._check_online_cache()

                    # Final broadcast with all data (connection + app + cache)
                    await self._broadcast_status()

                    # Evaluate automation triggers
                    try:
                        from automation_engine import automation_engine
                        if automation_engine.is_running:
                            await automation_engine.evaluate_events(self._status_cache)
                    except Exception as e:
                        logger.debug(f"[ConnectionChecker] Automation evaluation error: {e}")

                    # Save status to database
                    await self._save_to_database()

                    # Log health history (downsampled)
                    self._history_counter += 1
                    if self._history_counter >= self._history_interval:
                        self._history_counter = 0
                        await self._log_health_history()

                    # Auto-reconnect offline devices
                    await self._auto_reconnect_pass()

                # Periodic screenshot cleanup + health history cleanup
                self._cleanup_counter += 1
                if self._cleanup_counter >= self._cleanup_interval:
                    self._cleanup_counter = 0
                    try:
                        from routes.screenshots import cleanup_old_screenshots
                        result = cleanup_old_screenshots()
                        if result["deleted"] > 0:
                            logger.info(f"[ConnectionChecker] Screenshot cleanup: {result['message']}")
                    except Exception as e:
                        logger.debug(f"[ConnectionChecker] Cleanup error: {e}")
                    try:
                        retention = int(get_setting("health_history_retention_days") or "30")
                        await asyncio.to_thread(cleanup_health_history, retention)
                    except Exception as e:
                        logger.debug(f"[ConnectionChecker] Health history cleanup error: {e}")

                # Wait for next interval
                await asyncio.sleep(self._check_interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[ConnectionChecker] Check loop error: {e}")
                await asyncio.sleep(5)  # Short delay on error

    async def _load_devices(self):
        """Load device IPs from CSV (in thread pool to avoid blocking event loop)"""
        try:
            devices = await asyncio.to_thread(adb_manager.get_devices_from_csv)
            self._monitored_ips = {ip for d in devices if (ip := d.get('IP'))}
            logger.debug(f"[ConnectionChecker] Monitoring {len(self._monitored_ips)} devices")
        except Exception as e:
            logger.error(f"[ConnectionChecker] Failed to load devices: {e}")

    async def _fast_port_check(self, ip: str, port: int = 5555) -> bool:
        """Fast TCP port check - much faster than ADB connect"""
        def _check():
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(self._fast_check_timeout)
                result = sock.connect_ex((ip, port))
                return result == 0
            except (socket.timeout, socket.error, OSError):
                return False
            finally:
                if sock:
                    try:
                        sock.close()
                    except (socket.error, OSError):
                        pass
        return await asyncio.to_thread(_check)

    async def _check_single_device(self, ip: str) -> DeviceStatus:
        """Check a single device's connection status with retry for unstable connections"""
        start_time = time.time()
        max_retries = 2  # Total attempts: 1 initial + 1 retry

        for attempt in range(max_retries):
            # Step 1: Fast TCP port check
            port_open = await self._fast_port_check(ip)

            if not port_open:
                if attempt < max_retries - 1:
                    # Retry after short delay for unstable Wi-Fi devices
                    await asyncio.sleep(1)
                    continue
                # Port closed after all retries = definitely offline
                response_time = (time.time() - start_time) * 1000
                return DeviceStatus(
                    ip=ip,
                    online=False,
                    status="offline",
                    response_time=response_time,
                    last_checked=datetime.now()
                )

            # Step 2: Port open - try ADB connect for accurate status
            try:
                result = await adb_manager.fast_connect_and_check(ip)
                response_time = (time.time() - start_time) * 1000

                if result["online"]:
                    return DeviceStatus(
                        ip=ip,
                        online=True,
                        status=result["status"],
                        response_time=response_time,
                        last_checked=datetime.now(),
                        last_online=datetime.now()
                    )

                # ADB check failed but port was open - retry for flaky connections
                if attempt < max_retries - 1:
                    logger.debug(f"[ConnectionChecker] {ip} port open but ADB failed, retrying...")
                    await asyncio.sleep(1)
                    continue

                return DeviceStatus(
                    ip=ip,
                    online=False,
                    status=result["status"],
                    response_time=response_time,
                    last_checked=datetime.now()
                )
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.debug(f"[ConnectionChecker] {ip} check error, retrying: {e}")
                    await asyncio.sleep(1)
                    continue
                logger.debug(f"[ConnectionChecker] ADB check failed for {ip}: {e}")
                response_time = (time.time() - start_time) * 1000
                return DeviceStatus(
                    ip=ip,
                    online=False,
                    status="error",
                    response_time=response_time,
                    last_checked=datetime.now()
                )

        # Fallback (should not reach here)
        response_time = (time.time() - start_time) * 1000
        return DeviceStatus(
            ip=ip,
            online=False,
            status="error",
            response_time=response_time,
            last_checked=datetime.now()
        )

    async def _check_all_devices(self):
        """Check all monitored devices in parallel"""
        if not self._monitored_ips:
            return

        # Create tasks for all devices
        tasks = [self._check_single_device(ip) for ip in self._monitored_ips]

        # Run all checks in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Update cache
        async with self._cache_lock:
            for result in results:
                if isinstance(result, DeviceStatus):
                    # Update existing or create new
                    old_status = self._status_cache.get(result.ip)
                    if old_status:
                        result.check_count = old_status.check_count + 1
                        # Preserve app_status and cache info until updated by later steps
                        result.app_status = old_status.app_status
                        result.cache_mb = old_status.cache_mb
                        result.data_mb = old_status.data_mb
                        result.cache_alert = old_status.cache_alert
                        if result.online:
                            result.consecutive_failures = 0
                            result.last_online = datetime.now()
                            # Reset reconnect state only on confirmed online (not unstable)
                            if result.status == "online":
                                result.reconnect_attempts = 0
                                result.reconnect_next_at = None
                                result.reconnect_circuit_open = False
                            else:
                                # Preserve reconnect state for unstable
                                result.reconnect_attempts = old_status.reconnect_attempts
                                result.reconnect_next_at = old_status.reconnect_next_at
                                result.reconnect_circuit_open = old_status.reconnect_circuit_open
                        else:
                            result.consecutive_failures = old_status.consecutive_failures + 1
                            result.last_online = old_status.last_online
                            # Preserve reconnect state
                            result.reconnect_attempts = old_status.reconnect_attempts
                            result.reconnect_next_at = old_status.reconnect_next_at
                            result.reconnect_circuit_open = old_status.reconnect_circuit_open
                            # Grace period: keep as online (unstable) if recently online
                            if old_status.online and result.consecutive_failures < self._offline_threshold:
                                result.online = True
                                result.status = "unstable"
                    else:
                        result.check_count = 1
                        if result.online:
                            result.last_online = datetime.now()

                    self._status_cache[result.ip] = result

        # Log summary
        online_count = sum(1 for s in self._status_cache.values() if s.online)
        unstable_count = sum(1 for s in self._status_cache.values() if s.status == "unstable")
        logger.debug(f"[ConnectionChecker] Check complete: {online_count}/{len(self._status_cache)} online ({unstable_count} unstable)")

    async def _check_online_cache(self):
        """Check app cache for all online devices"""
        # Get list of online IPs
        online_ips = []
        async with self._cache_lock:
            online_ips = [ip for ip, status in self._status_cache.items() if status.online]

        if not online_ips:
            return

        logger.debug(f"[ConnectionChecker] Checking cache for {len(online_ips)} online devices")

        # Check cache for all online devices in parallel
        tasks = [adb_manager.get_app_storage_info(ip, self._app_package) for ip in online_ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Update cache info in status
        async with self._cache_lock:
            for i, result in enumerate(results):
                ip = online_ips[i]
                if isinstance(result, dict) and result.get("success"):
                    if ip in self._status_cache:
                        self._status_cache[ip].cache_mb = result.get("cache_mb", 0)
                        self._status_cache[ip].data_mb = result.get("data_mb", 0)
                        self._status_cache[ip].cache_alert = result.get("cache_mb", 0) > CACHE_ALERT_THRESHOLD_MB
                elif isinstance(result, Exception):
                    logger.debug(f"[ConnectionChecker] Cache check failed for {ip}: {result}")

        # Log cache summary
        alert_count = sum(1 for s in self._status_cache.values() if s.cache_alert)
        total_cache = sum(s.cache_mb for s in self._status_cache.values() if s.online)
        logger.debug(f"[ConnectionChecker] Cache check complete: {total_cache:.1f}MB total, {alert_count} alerts")

    async def _check_online_app_status(self):
        """Check app running status for all online devices using pidof (lightweight)"""
        online_ips = []
        async with self._cache_lock:
            online_ips = [ip for ip, status in self._status_cache.items() if status.online]

        if not online_ips:
            return

        async def _quick_pidof(ip: str) -> dict:
            """Single pidof command - very fast"""
            try:
                result = await adb_manager.run_adb_on_device(ip, f'shell pidof {self._app_package}')
                pid = result.strip()
                running = bool(pid and pid.split()[0].isdigit())
                return {"ip": ip, "running": running}
            except Exception:
                return {"ip": ip, "running": False}

        tasks = [_quick_pidof(ip) for ip in online_ips]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        async with self._cache_lock:
            for result in results:
                if isinstance(result, dict):
                    ip = result["ip"]
                    if ip in self._status_cache:
                        self._status_cache[ip].app_status = "RUNNING" if result["running"] else "STOPPED"

        running_count = sum(1 for r in results if isinstance(r, dict) and r.get("running"))
        logger.debug(f"[ConnectionChecker] App status: {running_count}/{len(online_ips)} running")

    async def _broadcast_status(self):
        """Broadcast status update via WebSocket (includes cache data)"""
        status_data = await self.get_all_status()

        # Send via WebSocket
        await ws_manager.broadcast_json({
            "type": "connection_status",
            "timestamp": datetime.now().isoformat(),
            "summary": {
                "total": status_data["total"],
                "online": status_data["online"],
                "offline": status_data["offline"],
                "unauthorized": status_data["unauthorized"]
            },
            "cache_summary": status_data.get("cache_summary", {}),
            "devices": status_data["devices"]
        })

    async def _save_to_database(self):
        """Save all device statuses to database (batch write in thread pool)"""
        # Collect data while holding lock (fast)
        async with self._cache_lock:
            statuses = [
                (ip, s.status, s.response_time, s.online,
                 s.consecutive_failures, s.cache_mb, s.data_mb, s.cache_alert)
                for ip, s in self._status_cache.items()
            ]

        if not statuses:
            return

        # Batch write in thread pool — single UPSERT per row, no SELECT needed
        def _batch_save():
            from database import get_db
            from datetime import datetime
            conn = get_db()
            cursor = conn.cursor()
            now = datetime.now()
            try:
                cursor.execute("BEGIN")
                for (ip, status, response_time, is_online,
                     consecutive_failures, cache_mb, data_mb, cache_alert) in statuses:
                    cursor.execute('''
                        INSERT INTO ping_status
                        (ip, status, response_time, last_online, consecutive_failures, check_count,
                         cache_mb, data_mb, cache_alert, updated_at)
                        VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                        ON CONFLICT(ip) DO UPDATE SET
                            status = excluded.status,
                            response_time = excluded.response_time,
                            last_online = CASE WHEN excluded.last_online IS NOT NULL
                                               THEN excluded.last_online
                                               ELSE ping_status.last_online END,
                            consecutive_failures = excluded.consecutive_failures,
                            check_count = ping_status.check_count + 1,
                            cache_mb = excluded.cache_mb,
                            data_mb = excluded.data_mb,
                            cache_alert = excluded.cache_alert,
                            updated_at = excluded.updated_at
                    ''', (ip, status, response_time,
                          now if is_online else None,
                          consecutive_failures,
                          cache_mb, data_mb, 1 if cache_alert else 0, now))
                conn.commit()
            except Exception as e:
                logger.error(f"[ConnectionChecker] Batch save error: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
            finally:
                conn.close()

        await asyncio.to_thread(_batch_save)
        logger.debug(f"[ConnectionChecker] Saved {len(statuses)} device statuses to database")

    async def _log_health_history(self):
        """Log current device statuses to health history table (downsampled)."""
        async with self._cache_lock:
            now = datetime.now()
            records = [
                (s.ip, s.status, s.response_time, s.app_status, s.cache_mb, now)
                for s in self._status_cache.values()
            ]
        if records:
            try:
                await asyncio.to_thread(log_health_records_batch, records)
                logger.debug(f"[ConnectionChecker] Logged {len(records)} health history records")
            except Exception as e:
                logger.error(f"[ConnectionChecker] Health history log error: {e}")

    async def get_all_status(self) -> Dict:
        """Get all cached device statuses including cache info"""
        async with self._cache_lock:
            devices = {}
            online_count = 0
            offline_count = 0
            unauthorized_count = 0
            total_cache_mb = 0
            cache_alert_count = 0

            for ip, status in self._status_cache.items():
                devices[ip] = {
                    "online": status.online,
                    "status": status.status,
                    "response_time": status.response_time,
                    "last_checked": status.last_checked.isoformat(),
                    "last_online": status.last_online.isoformat() if status.last_online else None,
                    "consecutive_failures": status.consecutive_failures,
                    # App status
                    "app_status": status.app_status,
                    # Cache info
                    "cache_mb": status.cache_mb,
                    "data_mb": status.data_mb,
                    "cache_alert": status.cache_alert
                }

                if status.online:
                    online_count += 1
                    total_cache_mb += status.cache_mb
                    if status.cache_alert:
                        cache_alert_count += 1
                elif status.status == "unauthorized":
                    unauthorized_count += 1
                else:
                    offline_count += 1

            return {
                "total": len(devices),
                "online": online_count,
                "offline": offline_count,
                "unauthorized": unauthorized_count,
                "devices": devices,
                "last_update": datetime.now().isoformat(),
                # Cache summary
                "cache_summary": {
                    "total_cache_mb": round(total_cache_mb, 2),
                    "avg_cache_mb": round(total_cache_mb / online_count, 2) if online_count > 0 else 0,
                    "alert_count": cache_alert_count
                }
            }

    async def get_device_status(self, ip: str) -> Optional[Dict]:
        """Get cached status for a single device"""
        async with self._cache_lock:
            status = self._status_cache.get(ip)
            if status:
                return {
                    "ip": ip,
                    "online": status.online,
                    "status": status.status,
                    "response_time": status.response_time,
                    "last_checked": status.last_checked.isoformat(),
                    "last_online": status.last_online.isoformat() if status.last_online else None,
                    "consecutive_failures": status.consecutive_failures
                }
            return None

    async def trigger_check(self, ips: Optional[List[str]] = None):
        """Manually trigger a check for specific IPs or all devices"""
        if ips:
            # Check specific devices
            tasks = [self._check_single_device(ip) for ip in ips]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            async with self._cache_lock:
                for result in results:
                    if isinstance(result, DeviceStatus):
                        self._status_cache[result.ip] = result

            # Broadcast update
            await self._broadcast_status()
        else:
            # Check all devices
            await self._load_devices()
            await self._check_all_devices()
            await self._broadcast_status()

    async def mark_online(self, ips: List[str]):
        """Mark devices as online when manual operations succeed (sync with dashboard cache)"""
        async with self._cache_lock:
            for ip in ips:
                if ip in self._status_cache:
                    self._status_cache[ip].online = True
                    self._status_cache[ip].status = "online"
                    self._status_cache[ip].consecutive_failures = 0
                    self._status_cache[ip].last_online = datetime.now()
                    self._status_cache[ip].last_checked = datetime.now()
                else:
                    self._status_cache[ip] = DeviceStatus(
                        ip=ip,
                        online=True,
                        status="online",
                        last_checked=datetime.now(),
                        last_online=datetime.now()
                    )
        await self._broadcast_status()

    def set_check_interval(self, interval: int):
        """Update check interval (in seconds)"""
        self._check_interval = max(10, min(300, interval))  # 10s to 5min
        logger.info(f"[ConnectionChecker] Check interval set to {self._check_interval}s")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def check_interval(self) -> int:
        return self._check_interval

    @property
    def monitored_count(self) -> int:
        return len(self._monitored_ips)

    @property
    def cache_check_enabled(self) -> bool:
        return self._cache_check_enabled

    def set_cache_check_enabled(self, enabled: bool):
        """Enable or disable cache checking"""
        self._cache_check_enabled = enabled
        logger.info(f"[ConnectionChecker] Cache check {'enabled' if enabled else 'disabled'}")

    # ============================================
    # Auto-Reconnect
    # ============================================

    def _load_reconnect_settings(self) -> dict:
        """Load auto-reconnect settings from database."""
        return {
            "enabled": get_setting("auto_reconnect_enabled") == "true",
            "max_retries": int(get_setting("auto_reconnect_max_retries") or "10"),
            "initial_delay": float(get_setting("auto_reconnect_initial_delay") or "5"),
            "max_delay": float(get_setting("auto_reconnect_max_delay") or "300"),
            "disabled_ips": set(json.loads(get_setting("auto_reconnect_disabled_ips") or "[]")),
        }

    async def _auto_reconnect_pass(self):
        """Attempt to reconnect offline devices with exponential backoff."""
        try:
            settings = await asyncio.to_thread(self._load_reconnect_settings)
        except Exception:
            return

        if not settings["enabled"]:
            return

        now = time.time()
        reconnect_tasks = []

        async with self._cache_lock:
            candidates = [
                s for s in self._status_cache.values()
                if not s.online
                and not s.reconnect_circuit_open
                and s.ip not in settings["disabled_ips"]
            ]

        for status in candidates:
            # Check backoff timer
            if status.reconnect_next_at and now < status.reconnect_next_at:
                continue
            reconnect_tasks.append(self._try_reconnect(status, settings))

        if reconnect_tasks:
            await asyncio.gather(*reconnect_tasks, return_exceptions=True)

    async def _try_reconnect(self, status: DeviceStatus, settings: dict):
        """Try to reconnect a single device."""
        ip = status.ip
        logger.info(f"[AutoReconnect] Attempting reconnect {ip} (attempt {status.reconnect_attempts + 1})")

        try:
            result = await adb_manager.connect_device(ip)
            success = result and ("connected" in str(result).lower() or "already" in str(result).lower())
        except Exception as e:
            logger.debug(f"[AutoReconnect] {ip} connect error: {e}")
            success = False

        async with self._cache_lock:
            s = self._status_cache.get(ip)
            if not s:
                return

            if success:
                s.online = True
                s.status = "online"
                s.consecutive_failures = 0
                s.last_online = datetime.now()
                s.reconnect_attempts = 0
                s.reconnect_next_at = None
                s.reconnect_circuit_open = False
                logger.info(f"[AutoReconnect] {ip} reconnected successfully")
                await ws_manager.broadcast_json({
                    "type": "reconnect_success",
                    "ip": ip,
                    "message": f"Device {ip} auto-reconnected",
                })
            else:
                s.reconnect_attempts += 1
                if s.reconnect_attempts >= settings["max_retries"]:
                    s.reconnect_circuit_open = True
                    logger.warning(f"[AutoReconnect] {ip} circuit breaker OPEN after {s.reconnect_attempts} attempts")
                    await ws_manager.broadcast_json({
                        "type": "reconnect_circuit_open",
                        "ip": ip,
                        "message": f"Auto-reconnect stopped for {ip} after {s.reconnect_attempts} failed attempts",
                    })
                else:
                    delay = min(settings["initial_delay"] * (2 ** s.reconnect_attempts), settings["max_delay"])
                    s.reconnect_next_at = time.time() + delay
                    logger.debug(f"[AutoReconnect] {ip} failed, next retry in {delay:.0f}s")

    async def reset_reconnect_state(self, ip: str):
        """Reset circuit breaker for a device."""
        async with self._cache_lock:
            s = self._status_cache.get(ip)
            if s:
                s.reconnect_attempts = 0
                s.reconnect_next_at = None
                s.reconnect_circuit_open = False
                logger.info(f"[AutoReconnect] Reset reconnect state for {ip}")

    def get_reconnect_status(self) -> List[dict]:
        """Get reconnect state for all devices (non-async, for API)."""
        result = []
        for s in self._status_cache.values():
            if s.reconnect_attempts > 0 or s.reconnect_circuit_open:
                result.append({
                    "ip": s.ip,
                    "online": s.online,
                    "reconnect_attempts": s.reconnect_attempts,
                    "reconnect_circuit_open": s.reconnect_circuit_open,
                    "reconnect_next_at": s.reconnect_next_at,
                })
        return result


# Global instance
connection_checker = ConnectionChecker()
