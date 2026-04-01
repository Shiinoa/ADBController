"""
Report Templates API routes
"""
from fastapi import APIRouter, Request, HTTPException
from typing import List, Dict
import time

from models import ReportTemplateRequest
from auth import require_auth
from database import (
    get_all_templates,
    get_template_by_id,
    create_template,
    update_template,
    delete_template
)

router = APIRouter(prefix="/api/report-templates", tags=["templates"])


def load_report_templates() -> List[Dict]:
    """Load report templates from database (for backward compatibility)"""
    return get_all_templates()


@router.get("")
async def get_report_templates(request: Request):
    """Get all saved report templates"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    templates = get_all_templates()
    return {"templates": templates}


@router.post("")
async def create_report_template(request: Request, template: ReportTemplateRequest):
    """Create a new report template"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    template_id = f"template_{int(time.time() * 1000)}"

    new_template = create_template(
        template_id=template_id,
        name=template.name,
        description=template.description or "",
        elements=template.elements,
        settings=template.settings if template.settings else {},
        created_by=user.get("username", "unknown")
    )

    if new_template:
        return {"success": True, "template": new_template, "message": "Template saved successfully"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save template")


@router.get("/{template_id}")
async def get_report_template(request: Request, template_id: str):
    """Get a specific report template by ID"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    template = get_template_by_id(template_id)

    if template:
        return {"template": template}

    raise HTTPException(status_code=404, detail="Template not found")


@router.put("/{template_id}")
async def update_report_template_route(request: Request, template_id: str, template: ReportTemplateRequest):
    """Update an existing report template"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    updated = update_template(
        template_id=template_id,
        name=template.name,
        description=template.description or "",
        elements=template.elements,
        settings=template.settings if template.settings else {},
        updated_by=user.get("username", "unknown")
    )

    if updated:
        return {"success": True, "template": updated, "message": "Template updated successfully"}

    raise HTTPException(status_code=404, detail="Template not found")


@router.delete("/{template_id}")
async def delete_report_template(request: Request, template_id: str):
    """Delete a report template"""
    user = require_auth(request)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    template_name = delete_template(template_id)

    if template_name:
        return {"success": True, "message": f"Template '{template_name}' deleted successfully"}

    raise HTTPException(status_code=404, detail="Template not found")
