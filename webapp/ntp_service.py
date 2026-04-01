"""
NTP time service for webapp-side time synchronization.
Uses a lightweight SNTP query and maintains a local offset from system time.
"""
import asyncio
import socket
import struct
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

from database import get_all_settings, set_setting
from models import validate_ntp_server


NTP_PORT = 123
NTP_DELTA = 2208988800


class NTPTimeService:
    def __init__(self):
        self._offset_seconds = 0.0
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._last_sync: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._last_server: str = ""
        self._last_persisted_state = None

    def now(self) -> datetime:
        """Return local server time adjusted by the latest NTP offset."""
        return datetime.now() + timedelta(seconds=self._offset_seconds)

    async def start(self):
        """Start periodic synchronization loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._sync_loop())

    async def stop(self):
        """Stop periodic synchronization loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _sync_loop(self):
        """Periodically refresh offset from configured NTP server."""
        while self._running:
            try:
                settings = get_all_settings()
                enabled = settings.get("ntp_sync_enabled", "true") == "true"
                server = (settings.get("ntp_server", "") or "").strip()
                interval = self._get_interval_seconds(settings)

                if enabled and server:
                    should_sync = not self._last_sync or (datetime.now() - self._last_sync).total_seconds() >= interval
                    if should_sync:
                        await self.sync_now(server)
                else:
                    self._persist_status(enabled, server, "disabled" if not enabled else "idle")

                await asyncio.sleep(min(interval, 60))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                self._persist_status(True, self._last_server, "error")
                await asyncio.sleep(30)

    def _get_interval_seconds(self, settings: Dict[str, str]) -> int:
        raw = (settings.get("ntp_sync_interval", "900") or "900").strip()
        if raw.isdigit():
            value = int(raw)
            if 60 <= value <= 86400:
                return value
        return 900

    async def sync_now(self, server: Optional[str] = None) -> Dict:
        """Force an immediate sync against the configured or provided server."""
        settings = get_all_settings()
        enabled = settings.get("ntp_sync_enabled", "true") == "true"
        raw_server = (server or settings.get("ntp_server", "") or "").strip()
        if not raw_server:
            self._last_error = "NTP server is not configured"
            self._persist_status(enabled, "", "idle")
            return {"success": False, "message": self._last_error}

        try:
            validated_server = validate_ntp_server(raw_server)
        except ValueError as exc:
            self._last_error = str(exc)
            self._persist_status(enabled, raw_server, "error")
            return {"success": False, "message": self._last_error}

        async with self._lock:
            try:
                result = await asyncio.to_thread(self._query_ntp_server, validated_server)
                self._offset_seconds = result["offset_seconds"]
                self._last_sync = datetime.now()
                self._last_error = None
                self._last_server = validated_server
                self._persist_status(enabled, validated_server, "ok")
                return {
                    "success": True,
                    "server": validated_server,
                    "offset_ms": result["offset_ms"],
                    "current_time": self.now().isoformat(),
                    "message": "NTP sync completed"
                }
            except Exception as exc:
                self._last_error = str(exc)
                self._last_server = validated_server
                self._persist_status(enabled, validated_server, "error")
                return {
                    "success": False,
                    "server": validated_server,
                    "message": self._last_error,
                    "current_time": self.now().isoformat()
                }

    def get_status(self) -> Dict:
        """Return current sync status for UI/API."""
        settings = get_all_settings()
        enabled = settings.get("ntp_sync_enabled", "true") == "true"
        server = (settings.get("ntp_server", "") or "").strip()
        interval = self._get_interval_seconds(settings)
        return {
            "enabled": enabled,
            "server": server,
            "sync_interval_seconds": interval,
            "offset_ms": int(round(self._offset_seconds * 1000)),
            "last_sync": self._last_sync.isoformat() if self._last_sync else settings.get("ntp_last_sync", ""),
            "last_error": self._last_error or settings.get("ntp_last_error", ""),
            "status": settings.get("ntp_sync_status", "idle"),
            "current_time": self.now().isoformat(),
            "using_ntp": bool(enabled and server and not self._last_error)
        }

    def _persist_status(self, enabled: bool, server: str, status: str):
        state = (
            enabled,
            server,
            status,
            int(round(self._offset_seconds * 1000)),
            self._last_sync.isoformat() if self._last_sync else "",
            self._last_error or ""
        )
        if state == self._last_persisted_state:
            return

        set_setting("ntp_sync_enabled", "true" if enabled else "false")
        set_setting("ntp_sync_status", status)
        set_setting("ntp_last_server", server)
        set_setting("ntp_offset_ms", str(int(round(self._offset_seconds * 1000))))
        set_setting("ntp_last_sync", self._last_sync.isoformat() if self._last_sync else "")
        set_setting("ntp_last_error", self._last_error or "")
        self._last_persisted_state = state

    def _query_ntp_server(self, server: str, timeout: float = 5.0) -> Dict:
        addresses = socket.getaddrinfo(server, NTP_PORT, type=socket.SOCK_DGRAM)
        if not addresses:
            raise RuntimeError("Unable to resolve NTP server")

        last_error = None
        for family, socktype, proto, _, sockaddr in addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(timeout)

                transmit_time = time.time()
                packet = bytearray(48)
                packet[0] = 0x1B
                seconds = int(transmit_time) + NTP_DELTA
                fraction = int((transmit_time - int(transmit_time)) * (2 ** 32))
                struct.pack_into("!II", packet, 40, seconds, fraction)

                sock.sendto(packet, sockaddr)
                data, _ = sock.recvfrom(48)
                receive_time = time.time()
                if len(data) < 48:
                    raise RuntimeError("Incomplete NTP response")

                unpacked = struct.unpack("!12I", data[:48])
                recv_seconds = unpacked[8] - NTP_DELTA
                recv_fraction = unpacked[9] / 2 ** 32
                tx_seconds = unpacked[10] - NTP_DELTA
                tx_fraction = unpacked[11] / 2 ** 32

                server_receive = recv_seconds + recv_fraction
                server_transmit = tx_seconds + tx_fraction
                offset_seconds = ((server_receive - transmit_time) + (server_transmit - receive_time)) / 2

                return {
                    "offset_seconds": offset_seconds,
                    "offset_ms": int(round(offset_seconds * 1000))
                }
            except Exception as exc:
                last_error = exc
            finally:
                if sock:
                    sock.close()

        raise RuntimeError(str(last_error) if last_error else "NTP query failed")


ntp_service = NTPTimeService()
