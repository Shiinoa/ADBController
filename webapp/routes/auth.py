"""
Authentication API routes
"""
import os
import asyncio
import logging
from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import JSONResponse
from typing import Optional

from models import LoginRequest, ChangePasswordRequest
from database import verify_user, create_session, delete_session, update_user
from auth import get_current_user, require_auth, invalidate_session_cache

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

# Cookie security settings
# Set SECURE_COOKIES=true in production with HTTPS
SECURE_COOKIES = os.environ.get('SECURE_COOKIES', 'false').lower() == 'true'


@router.post("/login")
async def login(request: LoginRequest, req: Request):
    """Login endpoint"""
    # Get client IP for logging
    client_ip = req.client.host if req.client else "unknown"

    # Run bcrypt in thread pool to avoid blocking event loop (~250ms)
    user = await asyncio.to_thread(verify_user, request.username, request.password)
    if not user:
        # Log failed login attempt for security monitoring
        logger.warning(f"Failed login attempt for user '{request.username}' from {client_ip}")
        return {"success": False, "message": "Invalid username or password"}

    # Log successful login
    logger.info(f"User '{request.username}' logged in from {client_ip}")

    token = await asyncio.to_thread(create_session, user["id"])
    response = JSONResponse({"success": True, "message": "Login successful"})
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=SECURE_COOKIES,  # True in production with HTTPS
        samesite="lax",  # Prevent CSRF
        max_age=14400 if request.remember else None  # 4 hours max
    )
    return response


@router.post("/logout")
async def logout(session_token: Optional[str] = Cookie(None)):
    """Logout endpoint"""
    if session_token:
        invalidate_session_cache(session_token)
        delete_session(session_token)
    response = JSONResponse({"success": True})
    response.delete_cookie(
        key="session_token",
        secure=SECURE_COOKIES,
        samesite="lax"
    )
    return response


@router.get("/check")
async def check_auth(session_token: Optional[str] = Cookie(None)):
    """Check if user is authenticated"""
    user = await get_current_user(session_token)
    if user:
        return {"authenticated": True, "user": user}
    return {"authenticated": False}


@router.get("/me")
async def get_me(session_token: Optional[str] = Cookie(None)):
    """Get current user info"""
    user = await get_current_user(session_token)
    if user:
        return user
    raise HTTPException(status_code=401, detail="Not authenticated")


@router.put("/password")
async def change_password(request: Request, body: ChangePasswordRequest):
    """Change own password (any authenticated user)"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if len(body.new_password) < 4:
        return {"success": False, "message": "Password must be at least 4 characters"}

    # Verify current password
    verified = await asyncio.to_thread(verify_user, user["username"], body.current_password)
    if not verified:
        return {"success": False, "message": "Current password is incorrect"}

    # Update password
    success = await asyncio.to_thread(update_user, user["id"], None, body.new_password, None)
    if success:
        logger.info(f"User '{user['username']}' changed their password")
    return {"success": success, "message": "Password changed" if success else "Failed to change password"}
