"""
Settings API routes
"""
import re
from fastapi import APIRouter, Request, HTTPException
from typing import Dict

from alert_manager import alert_manager
from auth import require_auth, require_admin_user
from database import get_all_settings, get_setting, set_setting
from models import validate_ntp_server
from ntp_service import ntp_service

router = APIRouter(prefix="/api/settings", tags=["settings"])


SENSITIVE_KEYS = {
    "smtp_password", "smtp_user",
    "interchat_token", "interchat_url",
    "syno_chat_token", "syno_chat_url",
}
BOOLEAN_SETTING_KEYS = {
    "alert_enabled",
    "smtp_auth_enabled",
    "ntp_sync_enabled",
    "interchat_skip_ssl_verification",
}
TIME_VALUE_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def normalize_setting_value(key: str, value) -> str:
    """Normalize and validate setting values before saving."""
    if value is None:
        return ""

    if isinstance(value, bool):
        value = "true" if value else "false"
    elif not isinstance(value, str):
        value = str(value)

    normalized = value.strip()

    if key == "ntp_server":
        if not normalized:
            return ""
        try:
            return validate_ntp_server(normalized)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if key in BOOLEAN_SETTING_KEYS:
        lowered = normalized.lower()
        if lowered not in {"true", "false"}:
            raise HTTPException(status_code=400, detail=f"Invalid value for {key}")
        return lowered

    if key == "ping_interval":
        if not normalized.isdigit():
            raise HTTPException(status_code=400, detail="Ping interval must be a number")
        interval = int(normalized)
        if interval < 30 or interval > 3600:
            raise HTTPException(status_code=400, detail="Ping interval must be between 30 and 3600 seconds")
        return str(interval)

    if key == "report_time":
        if not TIME_VALUE_RE.fullmatch(normalized):
            raise HTTPException(status_code=400, detail="Report time must be in HH:MM format")
        return normalized

    if key == "smtp_port" and normalized:
        if not normalized.isdigit():
            raise HTTPException(status_code=400, detail="SMTP port must be a number")
        port = int(normalized)
        if port < 1 or port > 65535:
            raise HTTPException(status_code=400, detail="SMTP port must be between 1 and 65535")
        return str(port)

    if key == "ntp_sync_interval" and normalized:
        if not normalized.isdigit():
            raise HTTPException(status_code=400, detail="NTP sync interval must be a number")
        interval = int(normalized)
        if interval < 60 or interval > 86400:
            raise HTTPException(status_code=400, detail="NTP sync interval must be between 60 and 86400 seconds")
        return str(interval)

    return normalized


@router.get("")
async def get_settings(request: Request):
    """Get all settings (sensitive fields hidden for non-admin)"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    settings = get_all_settings()
    if user.get("role") != "admin":
        for key in SENSITIVE_KEYS:
            if key in settings and settings[key]:
                settings[key] = "••••••••"
    return settings


@router.post("")
async def save_settings(request: Request, settings: Dict[str, str]):
    """Save settings"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    touched_ntp_settings = False
    for key, value in settings.items():
        set_setting(key, normalize_setting_value(key, value))
        if key.startswith("ntp_"):
            touched_ntp_settings = True
    alert_manager.reload_settings()

    ntp_sync_result = None
    if touched_ntp_settings:
        if get_setting("ntp_sync_enabled") == "true" and (get_setting("ntp_server") or "").strip():
            ntp_sync_result = await ntp_service.sync_now()
        else:
            ntp_sync_result = ntp_service.get_status()

    return {"success": True, "ntp_sync": ntp_sync_result}


@router.post("/test-smtp")
async def test_smtp(request: Request):
    """Test SMTP connection"""
    require_admin_user(request)
    return await alert_manager.test_smtp()


@router.post("/test-interchat")
async def test_interchat(request: Request):
    """Test Interchat webhook"""
    require_admin_user(request)
    return await alert_manager.test_interchat()


@router.post("/test-syno")
async def test_syno(request: Request):
    """Backward-compatible alias for Interchat test."""
    require_admin_user(request)
    return await alert_manager.test_interchat()


@router.get("/ntp/status")
async def get_ntp_status(request: Request):
    """Get current webapp NTP synchronization status."""
    require_admin_user(request)
    return ntp_service.get_status()


@router.post("/ntp/sync-now")
async def sync_ntp_now(request: Request):
    """Force the webapp to sync time from the configured NTP server."""
    require_admin_user(request)

    try:
        body = await request.json()
    except Exception:
        body = {}

    requested_server = (body.get("ntp_server", "") or "").strip()
    ntp_server = requested_server or get_setting("ntp_server") or ""
    if not ntp_server.strip():
        raise HTTPException(status_code=400, detail="NTP server is not configured")

    try:
        validated_server = validate_ntp_server(ntp_server)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if requested_server:
        set_setting("ntp_server", validated_server)

    result = await ntp_service.sync_now(validated_server)
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("message", "NTP sync failed"))
    return result
