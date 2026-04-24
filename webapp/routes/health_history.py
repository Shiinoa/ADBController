"""
Device Health History API routes
"""
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, HTTPException

from auth import require_authenticated_user, require_admin_user
from database import (
    get_health_history, get_health_summary,
    get_all_devices_health_summary, cleanup_health_history,
    get_setting,
)

router = APIRouter(prefix="/api/health-history", tags=["health-history"])


def _parse_time_range(start: str = None, end: str = None, hours: int = 24):
    """Parse start/end ISO strings or default to last N hours."""
    now = datetime.now()
    if end:
        try:
            end_dt = datetime.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid end date format")
    else:
        end_dt = now

    if start:
        try:
            start_dt = datetime.fromisoformat(start)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid start date format")
    else:
        start_dt = end_dt - timedelta(hours=hours)

    return start_dt, end_dt


@router.get("/overview")
async def health_overview(request: Request, start: str = None, end: str = None, hours: int = 24):
    """Get uptime summary for all devices."""
    require_authenticated_user(request)
    start_dt, end_dt = _parse_time_range(start, end, hours)
    return {"summaries": get_all_devices_health_summary(start_dt, end_dt)}


@router.get("/{ip}")
async def device_health(request: Request, ip: str, start: str = None, end: str = None,
                        hours: int = 24, limit: int = 500):
    """Get health history for a single device."""
    require_authenticated_user(request)
    start_dt, end_dt = _parse_time_range(start, end, hours)
    records = get_health_history(ip, start_dt, end_dt, limit)
    return {"ip": ip, "records": records, "count": len(records)}


@router.get("/{ip}/summary")
async def device_health_summary(request: Request, ip: str, start: str = None, end: str = None,
                                hours: int = 24):
    """Get uptime summary for a single device."""
    require_authenticated_user(request)
    start_dt, end_dt = _parse_time_range(start, end, hours)
    return get_health_summary(ip, start_dt, end_dt)


@router.delete("/cleanup")
async def cleanup(request: Request, days: int = None):
    """Manually clean up old health history records."""
    require_admin_user(request)
    if days is None:
        days = int(get_setting("health_history_retention_days") or "30")
    if days < 1:
        days = 30
    deleted = cleanup_health_history(days)
    return {"success": True, "deleted": deleted, "retention_days": days}
