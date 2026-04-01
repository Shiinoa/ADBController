"""
Reports API routes
"""
from fastapi import APIRouter, Request, HTTPException
from typing import List
import os
import json

from models import ReportRequest, ReportEmailRequest, GenerateReportRequest
from config import REPORT_DIR
from adb_manager import adb_manager
from websocket_manager import ws_manager
from database import save_daily_report, get_daily_reports
from alert_manager import alert_manager
from auth import require_auth, require_authenticated_user
from .templates import load_report_templates

router = APIRouter(tags=["reports"])


@router.post("/api/report/generate")
async def generate_report(http_request: Request, request: GenerateReportRequest):
    """Generate CSV report for selected devices"""
    require_authenticated_user(http_request)
    # Convert validated Pydantic models to dicts for adb_manager
    devices = [d.model_dump() for d in request.devices]
    await ws_manager.log(f"Generating report for {len(devices)} devices...")

    async def log_callback(message: str):
        await ws_manager.log(message)

    report_path = await adb_manager.generate_report(devices, log_callback)

    if report_path.startswith("Error"):
        await ws_manager.task_complete("report", False, report_path)
        raise HTTPException(status_code=500, detail=report_path)

    await ws_manager.task_complete("report", True, f"Report saved: {os.path.basename(report_path)}")
    return {"success": True, "path": report_path, "filename": os.path.basename(report_path)}


@router.get("/api/report/download/{filename}")
async def download_report(request: Request, filename: str):
    """Download generated report"""
    require_authenticated_user(request)
    from fastapi.responses import FileResponse
    safe_name = os.path.basename(filename)
    file_path = os.path.join(REPORT_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(file_path, media_type="text/csv", filename=safe_name)


@router.get("/api/reports")
async def list_reports(request: Request):
    """List all generated reports"""
    require_authenticated_user(request)
    reports = []
    if os.path.exists(REPORT_DIR):
        for f in os.listdir(REPORT_DIR):
            if f.endswith(".csv"):
                path = os.path.join(REPORT_DIR, f)
                reports.append({
                    "filename": f,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path)
                })
    return {"reports": sorted(reports, key=lambda x: x["modified"], reverse=True)}


@router.post("/api/reports/save")
async def save_report(request: Request, report: ReportRequest):
    """Save daily report"""
    require_authenticated_user(request)
    save_daily_report(
        report.date,
        report.total,
        report.online,
        report.offline,
        json.dumps(report.devices)
    )
    return {"success": True}


@router.get("/api/reports/history")
async def get_report_history(request: Request):
    """Get report history"""
    require_authenticated_user(request)
    return {"reports": get_daily_reports(30)}


@router.post("/api/reports/send-email")
async def send_report_email(request: Request, report: ReportEmailRequest):
    """Send report via email"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    template = None
    if report.template_id:
        templates = load_report_templates()
        template = next((t for t in templates if t.get("id") == report.template_id), None)

    success = alert_manager.send_daily_report({
        "total": report.total,
        "online": report.online,
        "offline": report.offline,
        "devices": report.devices
    }, template=template)

    return {"success": success, "message": "Email sent" if success else "Failed to send email"}
