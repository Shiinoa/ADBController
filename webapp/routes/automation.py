"""
Automation API routes
"""
import secrets
from fastapi import APIRouter, Request, HTTPException
from typing import Optional

from models import CreateWorkflowRequest, UpdateWorkflowRequest
from database import (
    get_all_workflows, get_workflow_by_id, create_workflow, update_workflow,
    delete_workflow, set_workflow_enabled, get_automation_logs, get_automation_stats,
    get_plant_by_code,
)
from auth import require_auth

router = APIRouter(prefix="/api/automation", tags=["automation"])


def _validate_plant_scope(device_scope) -> None:
    if device_scope.mode != "plant":
        return
    if not device_scope.plant_id or not get_plant_by_code(device_scope.plant_id):
        raise HTTPException(status_code=400, detail="Selected plant does not exist")


# ============================================
# Workflow CRUD
# ============================================

@router.get("/workflows")
async def list_workflows(request: Request):
    """Get all automation workflows"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return {"workflows": get_all_workflows()}


@router.get("/workflows/{workflow_id}")
async def get_workflow(request: Request, workflow_id: str):
    """Get a single workflow"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    wf = get_workflow_by_id(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return wf


@router.post("/workflows")
async def add_workflow(request: Request, body: CreateWorkflowRequest):
    """Create a new workflow"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    _validate_plant_scope(body.device_scope)

    workflow_id = secrets.token_hex(8)
    nodes = [n.model_dump() for n in body.nodes]
    result = create_workflow(
        workflow_id, body.name, body.description,
        body.device_scope.model_dump(), nodes, body.cooldown_minutes,
        user.get("username", "")
    )
    if not result:
        raise HTTPException(status_code=400, detail="Failed to create workflow")

    # Reload engine workflows
    from automation_engine import automation_engine
    await automation_engine.reload_workflows()

    return result


@router.put("/workflows/{workflow_id}")
async def edit_workflow(request: Request, workflow_id: str, body: UpdateWorkflowRequest):
    """Update an existing workflow"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    _validate_plant_scope(body.device_scope)

    nodes = [n.model_dump() for n in body.nodes]
    result = update_workflow(
        workflow_id, body.name, body.description,
        body.device_scope.model_dump(), nodes, body.cooldown_minutes
    )
    if not result:
        raise HTTPException(status_code=404, detail="Workflow not found")

    from automation_engine import automation_engine
    await automation_engine.reload_workflows()
    await automation_engine.reset_workflow_state(workflow_id)

    return result


@router.delete("/workflows/{workflow_id}")
async def remove_workflow(request: Request, workflow_id: str):
    """Delete a workflow"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    success = delete_workflow(workflow_id)
    if not success:
        raise HTTPException(status_code=404, detail="Workflow not found")

    from automation_engine import automation_engine
    await automation_engine.reset_workflow_state(workflow_id)
    await automation_engine.reload_workflows()

    return {"success": True, "message": "Workflow deleted"}


@router.post("/workflows/{workflow_id}/toggle")
async def toggle_workflow(request: Request, workflow_id: str):
    """Enable or disable a workflow"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    wf = get_workflow_by_id(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    new_state = not bool(wf.get('enabled'))
    set_workflow_enabled(workflow_id, new_state)

    from automation_engine import automation_engine
    await automation_engine.reload_workflows()
    if new_state:
        await automation_engine.reset_workflow_state(workflow_id)

    return {"success": True, "enabled": new_state}


@router.post("/workflows/{workflow_id}/test")
async def test_workflow(request: Request, workflow_id: str):
    """Dry-run test a workflow on the first matching device without side effects."""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    wf = get_workflow_by_id(workflow_id)
    if not wf:
        raise HTTPException(status_code=404, detail="Workflow not found")

    from automation_engine import automation_engine
    device_ips = await automation_engine._get_workflow_devices(wf)
    if not device_ips:
        return {"success": False, "message": "No devices in scope"}

    test_ip = device_ips[0]
    result = await automation_engine._execute_workflow(
        wf, test_ip, 'manual_test',
        {"ip": test_ip, "trigger_type": "manual_test", "online": True,
         "app_status": "unknown", "cache_mb": 0, "consecutive_failures": 0},
        dry_run=True,
    )

    return {
        "success": result.get("success", False),
        "message": f"Dry run completed for {test_ip}",
        "result": result,
    }


# ============================================
# Execution Logs
# ============================================

@router.get("/logs")
async def list_logs(request: Request, workflow_id: Optional[str] = None,
                    limit: int = 100, offset: int = 0):
    """Get automation execution logs"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    logs = get_automation_logs(workflow_id, min(limit, 500), offset)
    return {"logs": logs}


@router.get("/logs/stats")
async def log_stats(request: Request):
    """Get automation statistics"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")
    return get_automation_stats()


# ============================================
# Engine Status
# ============================================

@router.get("/engine/status")
async def engine_status(request: Request):
    """Get automation engine status"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    from automation_engine import automation_engine
    return automation_engine.get_status()


@router.post("/engine/reload")
async def engine_reload(request: Request):
    """Force reload workflows"""
    user = require_auth(request)
    if not user or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    from automation_engine import automation_engine
    await automation_engine.reload_workflows()
    return {"success": True, "message": "Workflows reloaded"}
