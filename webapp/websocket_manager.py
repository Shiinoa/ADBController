"""
WebSocket Manager for real-time communication
Handles multiple client connections and broadcasts log messages
"""
from typing import List, Dict, Any, Optional
from fastapi import WebSocket
import asyncio
import datetime
import json
import logging

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Manages WebSocket connections and message broadcasting"""

    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Accept new WebSocket connection"""
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """Remove disconnected WebSocket"""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        """Send message to specific client"""
        try:
            await websocket.send_text(message)
        except (RuntimeError, ConnectionError) as e:
            logger.debug(f"WebSocket send failed, disconnecting: {e}")
            self.disconnect(websocket)

    async def broadcast(self, message: str):
        """Broadcast message to all connected clients"""
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except (RuntimeError, ConnectionError) as e:
                logger.debug(f"Broadcast failed to client: {e}")
                disconnected.append(connection)

        # Clean up disconnected clients
        for conn in disconnected:
            self.disconnect(conn)

    async def broadcast_json(self, data: Dict[str, Any]):
        """Broadcast JSON data to all connected clients"""
        message = json.dumps(data, ensure_ascii=False)
        await self.broadcast(message)

    async def log(self, message: str, level: str = "info"):
        """Send log message with timestamp"""
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        log_data = {
            "type": "log",
            "timestamp": ts,
            "level": level,
            "message": message
        }
        await self.broadcast_json(log_data)

    async def status_update(self, ip: str, status: str, details: Optional[Dict] = None):
        """Send device status update"""
        data = {
            "type": "status",
            "ip": ip,
            "status": status,
            "details": details or {}
        }
        await self.broadcast_json(data)

    async def progress_update(self, current: int, total: int, message: str = ""):
        """Send progress update"""
        data = {
            "type": "progress",
            "current": current,
            "total": total,
            "percent": int((current / total) * 100) if total > 0 else 0,
            "message": message
        }
        await self.broadcast_json(data)

    async def task_complete(self, task: str, success: bool, message: str = ""):
        """Send task completion notification"""
        data = {
            "type": "complete",
            "task": task,
            "success": success,
            "message": message
        }
        await self.broadcast_json(data)


# Global instance
ws_manager = ConnectionManager()
