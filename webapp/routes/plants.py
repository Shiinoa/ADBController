"""
Plant Management API routes
"""
from fastapi import APIRouter, Request, HTTPException

from auth import require_auth
from database import (
    create_plant,
    get_all_plants,
    get_plant_by_id,
    delete_plant,
    get_device_count_by_plant,
    get_all_users,
    update_plant,
)
from models import PlantRequest, PlantUpdateRequest

router = APIRouter(prefix="/api/plants", tags=["plants"])


def require_admin(request: Request):
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@router.get("")
async def list_plants(request: Request, active_only: bool = False):
    """Get all plants"""
    require_admin(request)
    plants = get_all_plants(active_only=active_only)
    return {"plants": plants, "total": len(plants)}


@router.get("/{plant_id}")
async def get_plant(request: Request, plant_id: int):
    """Get a single plant"""
    require_admin(request)
    plant = get_plant_by_id(plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")
    return plant


@router.post("")
async def add_plant(request: Request, plant_data: PlantRequest):
    """Create a new plant"""
    require_admin(request)
    plant = create_plant(
        name=plant_data.name,
        code=plant_data.code,
        location=plant_data.location or "",
        timezone=plant_data.timezone or "Asia/Bangkok",
        description=plant_data.description or "",
    )
    if not plant:
        raise HTTPException(status_code=400, detail="Failed to create plant")
    return {"success": True, "plant": plant}


@router.put("/{plant_id}")
async def edit_plant(request: Request, plant_id: int, plant_data: PlantUpdateRequest):
    """Update an existing plant"""
    require_admin(request)
    existing = get_plant_by_id(plant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Plant not found")

    success = update_plant(
        plant_id,
        name=plant_data.name,
        code=plant_data.code,
        location=plant_data.location,
        timezone=plant_data.timezone,
        description=plant_data.description,
        is_active=plant_data.is_active,
    )
    if not success:
        raise HTTPException(status_code=400, detail="Failed to update plant")

    return {"success": True, "plant": get_plant_by_id(plant_id)}


@router.delete("/{plant_id}")
async def remove_plant(request: Request, plant_id: int):
    """Delete a plant if not protected or in use"""
    require_admin(request)
    plant = get_plant_by_id(plant_id)
    if not plant:
        raise HTTPException(status_code=404, detail="Plant not found")

    if (plant.get("code") or "").lower() == "default":
        raise HTTPException(status_code=400, detail="Default plant cannot be deleted")

    if get_device_count_by_plant(plant.get("code") or "") > 0:
        raise HTTPException(status_code=400, detail="Cannot delete plant that still has devices assigned")

    user_count = sum(1 for user in get_all_users() if (user.get("plant_code") or "") == (plant.get("code") or ""))
    if user_count > 0:
        raise HTTPException(status_code=400, detail="Cannot delete plant that still has users assigned")

    if not delete_plant(plant_id):
        raise HTTPException(status_code=400, detail="Failed to delete plant")

    return {"success": True, "message": "Plant deleted"}
