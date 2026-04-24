"""
HTML Page routes
"""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
import os

from config import CURRENT_DIR
from auth import require_auth

router = APIRouter(tags=["pages"])

# Setup templates
templates = Jinja2Templates(directory=os.path.join(CURRENT_DIR, "templates"))


@router.get("/robots.txt", response_class=PlainTextResponse)
async def robots_txt():
    """Robots.txt - disallow all crawlers"""
    return "User-agent: *\nDisallow: /\n"


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Login page"""
    return templates.TemplateResponse("login.html", {"request": request})


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Dashboard page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    """Settings page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("settings.html", {"request": request})


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    """Reports page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("reports.html", {"request": request})


@router.get("/devices", response_class=HTMLResponse)
async def devices_page(request: Request):
    """TV Devices management page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("devices.html", {"request": request})


@router.get("/multiview", response_class=HTMLResponse)
async def multiview_page(request: Request):
    """Multi-View CCTV-style monitoring page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("multiview.html", {"request": request})


@router.get("/report-form", response_class=HTMLResponse)
async def report_form_page(request: Request):
    """Report Form Designer page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("report-form.html", {"request": request})


@router.get("/scrcpy-agent", response_class=HTMLResponse)
async def scrcpy_agent_page(request: Request):
    """Scrcpy Client Agent management page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("scrcpy-agent.html", {"request": request})


@router.get("/automate", response_class=HTMLResponse)
async def automate_page(request: Request):
    """Automation workflows page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("automate.html", {"request": request})


@router.get("/documents", response_class=HTMLResponse)
async def documents_page(request: Request):
    """Documents management page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("documents.html", {"request": request})


@router.get("/uptime", response_class=HTMLResponse)
async def uptime_page(request: Request):
    """Uptime History page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("uptime.html", {"request": request})


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Redirect to dashboard"""
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/control", response_class=HTMLResponse)
async def control_panel(request: Request):
    """Control Panel page"""
    user = require_auth(request)
    if not user:
        return RedirectResponse("/login")
    return templates.TemplateResponse("index.html", {"request": request})


@router.get("/favicon.ico")
async def favicon():
    """Return empty favicon to prevent 404 errors"""
    return Response(content=b"", media_type="image/x-icon")
