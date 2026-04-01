"""
User Management API routes
"""
from fastapi import APIRouter, Request, HTTPException

from models import UserRequest
from database import get_all_users, create_user, update_user, delete_user, get_plant_by_code
from auth import require_auth

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
async def list_users(request: Request):
    """Get all users"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return {"users": get_all_users()}


@router.post("")
async def add_user(request: Request, user_data: UserRequest):
    """Add new user"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    if not user_data.password:
        return {"success": False, "message": "Password required"}

    if user_data.role != "admin" and not user_data.plant_code:
        return {"success": False, "message": "Plant is required for non-admin users"}

    plant_code = None if user_data.role == "admin" else user_data.plant_code

    if plant_code and not get_plant_by_code(plant_code):
        return {"success": False, "message": "Selected plant does not exist"}

    success = create_user(user_data.username, user_data.password, user_data.role, plant_code)
    return {"success": success, "message": "User created" if success else "Username already exists"}


@router.put("/{user_id}")
async def edit_user(request: Request, user_id: int, user_data: UserRequest):
    """Update user"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    if user_data.role != "admin" and not user_data.plant_code:
        return {"success": False, "message": "Plant is required for non-admin users"}

    plant_code = None if user_data.role == "admin" else user_data.plant_code

    if plant_code and not get_plant_by_code(plant_code):
        return {"success": False, "message": "Selected plant does not exist"}

    success = update_user(
        user_id,
        user_data.username,
        user_data.password or None,
        user_data.role,
        plant_code,
    )
    return {"success": success}


@router.delete("/{user_id}")
async def remove_user(request: Request, user_id: int):
    """Delete user"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    success = delete_user(user_id)
    return {"success": success, "message": "User deleted" if success else "Cannot delete admin user"}
