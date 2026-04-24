"""
Backup & Restore API routes
"""
import os
import tempfile
import logging
from datetime import datetime

from fastapi import APIRouter, Request, HTTPException, UploadFile, File
from fastapi.responses import FileResponse

from database import (
    DB_PATH, backup_database, restore_database, validate_backup_db,
    export_settings_json, import_settings_json,
)
from auth import require_admin_user
from services.device_inventory import invalidate_device_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/download")
async def download_backup(request: Request):
    """Download a full database backup."""
    require_admin_user(request)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"adb_control_backup_{timestamp}.db"
    dest = os.path.join(tempfile.gettempdir(), filename)

    if not backup_database(dest):
        raise HTTPException(status_code=500, detail="Failed to create backup")

    return FileResponse(
        path=dest,
        filename=filename,
        media_type="application/octet-stream",
    )


@router.post("/restore")
async def restore_backup(request: Request, file: UploadFile = File(...)):
    """Restore database from an uploaded .db file."""
    require_admin_user(request)

    if not file.filename.endswith(".db"):
        raise HTTPException(status_code=400, detail="File must be a .db file")

    # Save uploaded file to temp location
    tmp_path = os.path.join(tempfile.gettempdir(), f"restore_{file.filename}")
    try:
        content = await file.read()
        with open(tmp_path, "wb") as f:
            f.write(content)

        if not validate_backup_db(tmp_path):
            raise HTTPException(
                status_code=400,
                detail="Invalid database: missing required tables (users, settings, device_inventory)",
            )

        if not restore_database(tmp_path):
            raise HTTPException(status_code=500, detail="Restore failed")

        # Invalidate caches so the app picks up restored data
        invalidate_device_cache()

        return {"success": True, "message": "Database restored successfully. Please refresh the page."}
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
