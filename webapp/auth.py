"""
Authentication helpers — with in-memory session cache for fast auth checks
"""
import asyncio
import os
import time
from typing import Optional, Dict, Tuple
from urllib.parse import urlparse
from fastapi import Cookie, Request, WebSocket, HTTPException, status
from database import verify_session

# In-memory session cache: token -> (user_dict, expire_timestamp)
# Avoids hitting SQLite on every single request
_session_cache: Dict[str, Tuple[dict, float]] = {}
_CACHE_TTL = 120  # Cache session for 2 minutes
_ALLOWED_ORIGINS = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]


def _cached_verify_session(token: str) -> Optional[dict]:
    """Verify session with in-memory cache layer"""
    now = time.time()

    # Check cache first
    cached = _session_cache.get(token)
    if cached:
        user, expires = cached
        if now < expires:
            return user
        else:
            del _session_cache[token]

    # Cache miss — hit database
    user = verify_session(token)
    if user:
        _session_cache[token] = (user, now + _CACHE_TTL)
    return user


def invalidate_session_cache(token: str):
    """Remove a session from cache (call on logout)"""
    _session_cache.pop(token, None)


def _is_allowed_origin(origin: Optional[str], host: Optional[str]) -> bool:
    """Allow same-host WebSocket origins by default, or explicit allowlist via env."""
    if not origin:
        return True

    normalized_origin = origin.strip().rstrip("/")
    if _ALLOWED_ORIGINS:
        return normalized_origin in _ALLOWED_ORIGINS

    try:
        parsed = urlparse(normalized_origin)
    except ValueError:
        return False

    return bool(parsed.netloc) and parsed.netloc.lower() == (host or "").lower()


async def get_current_user(session_token: Optional[str] = Cookie(None)):
    """Get current user from session cookie"""
    if not session_token:
        return None
    return await asyncio.to_thread(_cached_verify_session, session_token)


def require_auth(request: Request):
    """Check if user is authenticated (sync, used by page routes)"""
    token = request.cookies.get("session_token")
    if not token:
        return None
    return _cached_verify_session(token)


def require_authenticated_user(request: Request) -> dict:
    """Require an authenticated HTTP user."""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin_user(request: Request) -> dict:
    """Require an authenticated admin user."""
    user = require_authenticated_user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


async def require_ws_auth(websocket: WebSocket) -> Optional[dict]:
    """Authenticate WebSocket clients before accepting the connection."""
    if not _is_allowed_origin(websocket.headers.get("origin"), websocket.headers.get("host")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid origin")
        return None

    token = websocket.cookies.get("session_token")
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication required")
        return None

    user = await asyncio.to_thread(_cached_verify_session, token)
    if not user:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Authentication required")
        return None

    return user
