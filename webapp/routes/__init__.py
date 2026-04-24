"""
Routes module - API endpoints organized by feature
"""
from .auth import router as auth_router
from .pages import router as pages_router
from .devices import router as devices_router
from .app import router as app_router
from .reports import router as reports_router
from .templates import router as templates_router
from .remote import router as remote_router
from .settings import router as settings_router
from .dashboard import router as dashboard_router
from .users import router as users_router
from .screenshots import router as screenshots_router
from .scrcpy import router as scrcpy_router
from .automation import router as automation_router
from .documents import router as documents_router
from .plants import router as plants_router
from .backup import router as backup_router
from .health_history import router as health_history_router
from .network_scan import router as network_scan_router

__all__ = [
    'auth_router',
    'pages_router',
    'devices_router',
    'app_router',
    'reports_router',
    'templates_router',
    'remote_router',
    'settings_router',
    'dashboard_router',
    'users_router',
    'screenshots_router',
    'scrcpy_router',
    'automation_router',
    'documents_router',
    'plants_router',
    'backup_router',
    'health_history_router',
    'network_scan_router',
]
