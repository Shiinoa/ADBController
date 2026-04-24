"""
Network Scan API routes - Scan subnets for ADB devices
"""
import logging
from typing import Optional, List
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel

from adb_manager import adb_manager
from auth import require_admin_user
from services.device_inventory import load_devices

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/network-scan", tags=["network-scan"])


class ScanRequest(BaseModel):
    subnet: str  # e.g. "10.10.210" or "10.10.210.0/24"
    port: int = 5555
    timeout: float = 1.0


@router.post("/scan")
async def scan_network(request: Request, body: ScanRequest):
    """Scan a subnet for ADB devices."""
    require_admin_user(request)

    if not body.subnet:
        raise HTTPException(status_code=400, detail="Subnet is required")

    # Get existing inventory IPs for comparison
    devices = load_devices()
    inventory_ips = {d.get("IP") for d in devices if d.get("IP")}
    inventory_map = {d.get("IP"): d for d in devices if d.get("IP")}

    found = await adb_manager.scan_network(body.subnet, body.port, body.timeout)

    # Enrich with inventory info
    for device in found:
        ip = device["ip"]
        device["in_inventory"] = ip in inventory_ips
        if ip in inventory_map:
            inv = inventory_map[ip]
            device["name"] = inv.get("Asset Name", "")
            device["location"] = inv.get("Default Location", "")
        else:
            device["name"] = ""
            device["location"] = ""

    return {
        "subnet": body.subnet,
        "scanned": True,
        "found": found,
        "total_found": len(found),
        "in_inventory": sum(1 for d in found if d["in_inventory"]),
        "new_devices": sum(1 for d in found if not d["in_inventory"]),
    }


@router.get("/subnets")
async def get_subnets(request: Request):
    """Auto-detect subnets from existing inventory."""
    require_admin_user(request)
    devices = load_devices()
    subnets = set()
    for d in devices:
        ip = d.get("IP", "")
        parts = ip.split(".")
        if len(parts) == 4:
            subnets.add(f"{parts[0]}.{parts[1]}.{parts[2]}")
    return {"subnets": sorted(subnets)}
