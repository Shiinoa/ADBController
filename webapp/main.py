"""
FastAPI Web Application for ADB Control Center
Main entry point - Application initialization and router registration
"""
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncio
import os
import signal
import sys

from config import CURRENT_DIR, print_config
from websocket_manager import ws_manager
from connection_checker import connection_checker
from database import migrate_templates_from_json
from auth import require_ws_auth
from ntp_service import ntp_service

# Import all routers
from routes import (
    auth_router,
    pages_router,
    devices_router,
    app_router,
    reports_router,
    templates_router,
    remote_router,
    settings_router,
    dashboard_router,
    users_router,
    screenshots_router,
    scrcpy_router,
    automation_router,
    documents_router,
    plants_router,
    backup_router,
    health_history_router,
)

# Print configuration on startup
print_config()

# Global shutdown flag
shutdown_event = asyncio.Event()


# Lifespan context manager for startup/shutdown
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Handle startup and shutdown events"""
    print("[Server] Starting ADB Control Center...")

    # Migrate templates from JSON to database (if exists)
    json_templates_path = os.path.join(CURRENT_DIR, "report_templates.json")
    if os.path.exists(json_templates_path):
        migrated = migrate_templates_from_json(json_templates_path)
        if migrated > 0:
            print(f"[Server] Migrated {migrated} templates from JSON to database")

    # Start background connection checker (30s interval)
    await connection_checker.start(check_interval=30)
    print("[Server] Background connection checker started")

    # Start NTP time sync service
    await ntp_service.start()
    print("[Server] NTP time service started")

    # Start automation engine
    from automation_engine import automation_engine
    await automation_engine.start()
    print("[Server] Automation engine started")

    yield

    # Shutdown
    print("[Server] Shutting down gracefully...")
    shutdown_event.set()

    # Stop automation engine
    await automation_engine.stop()
    print("[Server] Automation engine stopped")

    # Stop NTP time service
    await ntp_service.stop()
    print("[Server] NTP time service stopped")

    # Stop connection checker
    await connection_checker.stop()
    print("[Server] Connection checker stopped")

    # Close all scrcpy sessions
    try:
        from scrcpy_manager import scrcpy_window, scrcpy_manager
        await scrcpy_window.close_all()
        await scrcpy_manager.stop_all()
    except Exception as e:
        print(f"[Server] Cleanup error: {e}")
    print("[Server] Shutdown complete.")


# Initialize FastAPI app
app = FastAPI(
    title="ADB Control Center",
    description="Web-based Android TV Device Management",
    version="1.0.0",
    lifespan=lifespan
)

# Mount static files
app.mount("/static", StaticFiles(directory=os.path.join(CURRENT_DIR, "static")), name="static")


# ============================================
# Register all routers
# ============================================

# Auth routes (/api/auth/*)
app.include_router(auth_router)

# HTML pages (/, /login, /dashboard, etc.)
app.include_router(pages_router)

# Device routes (/api/devices/*)
app.include_router(devices_router)

# App routes (/api/app/*)
app.include_router(app_router)

# Reports routes (/api/reports/*, /api/report/*)
app.include_router(reports_router)

# Report templates routes (/api/report-templates/*)
app.include_router(templates_router)

# Remote control routes (/remote/*, /ws/remote/*, /api/remote/*)
app.include_router(remote_router)

# Settings routes (/api/settings/*)
app.include_router(settings_router)

# Dashboard routes (/api/dashboard/*)
app.include_router(dashboard_router)

# User management routes (/api/users/*)
app.include_router(users_router)

# Screenshots routes (/api/screenshots/*, /api/screenshot/*)
app.include_router(screenshots_router)

# Scrcpy routes (/api/scrcpy/*, /ws/scrcpy/*)
app.include_router(scrcpy_router)

# Automation routes (/api/automation/*)
app.include_router(automation_router)

# Documents routes (/api/documents/*)
app.include_router(documents_router)

# Plant management routes (/api/plants/*)
app.include_router(plants_router)

# Backup & Restore routes (/api/backup/*)
app.include_router(backup_router)

# Health History routes (/api/health-history/*)
app.include_router(health_history_router)


# ============================================
# WebSocket Endpoint
# ============================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time logs"""
    user = await require_ws_auth(websocket)
    if not user:
        return
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            # Handle incoming messages if needed
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)


# ============================================
# Run with uvicorn
# ============================================

if __name__ == "__main__":
    import uvicorn

    # Custom signal handler for graceful shutdown
    def handle_signal(signum, frame):
        print(f"\n[Server] Received signal {signum}, initiating shutdown...")
        sys.exit(0)

    # Register signal handlers (Windows compatible)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    print("=" * 50)
    print("  ADB Control Center - Web Application")
    print("  Press CTRL+C to stop the server")
    print("=" * 50)

    server_host = os.environ.get('HOST', '0.0.0.0')
    server_port = int(os.environ.get('PORT', 8000))

    uvicorn.run(
        app,
        host=server_host,
        port=server_port,
        reload=False,
        log_level="info"
    )
