from fastapi import APIRouter, Depends, HTTPException
from typing import List, Optional

from app.schemas.template_schema import (
    TemplateCreateRequest,
    TemplateUpdateRequest,
    TemplateListResponse,
    TemplateDetailResponse,
    OrgVariableCreate,
    OrgVariableUpdate,
    TemplateEnhanceRequest,
    TemplateEnhanceResponse
)
from app.services.template_service import TemplateService
from app.dependencies import get_current_user
from app.services.permission_service import PermissionService
from app.background.report_processor import log_audit_event

router = APIRouter()

def get_namespace_id(user: dict) -> Optional[str]:
    """Return user_id for solo dentists (individuals), and organization_id for clinics."""
    if user.get("organization_id"):
        return str(user["organization_id"])
    if user.get("role") == "super_admin":
        return None
    return str(user["_id"])

# ── Templates ─────────────────────────────────────────────────────────────

@router.get("", response_model=dict)
async def list_templates(
    active_only: bool = True,
    current_user: dict = Depends(get_current_user)
):
    """List all global templates and org-specific templates."""
    await PermissionService.check_permission(current_user, "templates:list")
    org_id = get_namespace_id(current_user)
    templates = await TemplateService.list_templates(org_id=org_id, active_only=active_only)
    return {"success": True, "data": templates}


@router.get("/{template_id}", response_model=dict)
async def get_template(
    template_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Get full details of a specific template."""
    await PermissionService.check_permission(current_user, "templates:list")
    org_id = get_namespace_id(current_user)
    is_super_admin = current_user.get("role") == "super_admin"
    template = await TemplateService.get_template(template_id, org_id, is_super_admin=is_super_admin)
    return {"success": True, "data": template}


@router.post("/enhance", response_model=dict)
async def enhance_template(
    request: TemplateEnhanceRequest,
    current_user: dict = Depends(get_current_user)
):
    """Enhance raw text into a fully structured template using AI."""
    await PermissionService.check_permission(current_user, "templates:manage")
    
    enhanced = await TemplateService.enhance_template(request.raw_text, request.procedure_name)
    return {"success": True, "data": enhanced}


@router.post("", response_model=dict)
async def create_template(
    request: TemplateCreateRequest,
    current_user: dict = Depends(get_current_user)
):
    """Create a new note template."""
    await PermissionService.check_permission(current_user, "templates:manage")
    
    # If the user submitted raw text, enhance it first
    if request.body_template and not request.cdt_refs:
        enhanced = await TemplateService.enhance_template(request.body_template, request.procedure_name)
        request.body_template = enhanced.get("body_template", request.body_template)
        request.cdt_refs = enhanced.get("cdt_refs", [])

    # Run the validation / compliance check on the final version
    val = await TemplateService.validate_template(request.model_dump())
    
    org_id = get_namespace_id(current_user)
    is_super_admin = current_user.get("role") == "super_admin"
    if not org_id and not is_super_admin:
        raise HTTPException(status_code=400, detail="User must have a namespace ID to create custom templates")
        
    template = await TemplateService.create_template(
        org_id=org_id, 
        data=request.model_dump(), 
        created_by=str(current_user["_id"]),
        is_super_admin=is_super_admin
    )
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="TEMPLATE_CREATED",
        resource="templates",
        resource_id=template["id"],
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "procedure_name": template.get("procedure_name"),
            "namespace_id": org_id
        },
    )
    
    return {"success": True, "data": template, "message": "Template created and validated successfully"}


@router.put("/{template_id}", response_model=dict)
async def update_template(
    template_id: str,
    request: TemplateUpdateRequest,
    current_user: dict = Depends(get_current_user)
):
    """Update a custom template (or disable it)."""
    await PermissionService.check_permission(current_user, "templates:manage")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    template = await TemplateService.update_template(
        template_id=template_id,
        org_id=org_id,
        data=request.model_dump(exclude_unset=True),
        updated_by=str(current_user["_id"]),
        is_super_admin=current_user.get("role") == "super_admin"
    )
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="TEMPLATE_UPDATED",
        resource="templates",
        resource_id=template["id"],
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "procedure_name": template.get("procedure_name"),
            "namespace_id": org_id
        },
    )
    
    return {"success": True, "data": template, "message": "Template updated successfully"}


@router.delete("/{template_id}", response_model=dict)
async def delete_template(
    template_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Soft delete a custom template."""
    await PermissionService.check_permission(current_user, "templates:manage")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    is_super_admin = current_user.get("role") == "super_admin"
    template = await TemplateService.delete_template(template_id, org_id, is_super_admin=is_super_admin)
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="TEMPLATE_DELETED",
        resource="templates",
        resource_id=template_id,
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "procedure_name": template.get("procedure_name"),
            "namespace_id": org_id
        },
    )
    
    return {"success": True, "data": template, "message": "Template deleted"}


@router.post("/seed", response_model=dict)
async def seed_templates(current_user: dict = Depends(get_current_user)):
    """Manually trigger seeding of default templates (Super Admin only)."""
    if current_user["role"] != "super_admin":
        raise HTTPException(status_code=403, detail="Only super admin can manually seed templates")
        
    result = await TemplateService.seed_default_templates()
    return {"success": True, "data": result}


# ── Org Variables ─────────────────────────────────────────────────────────

@router.post("/org/variables", response_model=dict)
async def create_org_variable(
    request: OrgVariableCreate,
    current_user: dict = Depends(get_current_user)
):
    """Create a new organization variable."""
    await PermissionService.check_permission(current_user, "templates:variables")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    variable = await TemplateService.create_org_variable(
        org_id=org_id,
        name=request.name,
        value=request.value,
        updated_by=str(current_user["_id"])
    )
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="TEMPLATE_VARIABLE_CREATED",
        resource="template_variables",
        resource_id=variable["id"],
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "variable_name": request.name,
            "namespace_id": org_id
        },
    )
    
    return {"success": True, "data": variable, "message": "Variable created successfully"}

@router.get("/org/variables", response_model=dict)
async def list_org_variables(
    skip: int = 0,
    limit: int = 10,
    search: Optional[str] = None,
    current_user: dict = Depends(get_current_user)
):
    """List organization custom variables with pagination and search."""
    await PermissionService.check_permission(current_user, "templates:variables")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    result = await TemplateService.list_org_variables(org_id, skip, limit, search)
    return {"success": True, "data": result}

@router.get("/org/variables/{var_id}", response_model=dict)
async def get_org_variable(var_id: str, current_user: dict = Depends(get_current_user)):
    """Get a specific organization variable by ID."""
    await PermissionService.check_permission(current_user, "templates:variables")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    variable = await TemplateService.get_org_variable(org_id, var_id)
    return {"success": True, "data": variable}

@router.put("/org/variables/{var_id}", response_model=dict)
async def update_org_variable(
    var_id: str,
    request: OrgVariableUpdate,
    current_user: dict = Depends(get_current_user)
):
    """Update a specific organization variable."""
    await PermissionService.check_permission(current_user, "templates:variables")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    variable = await TemplateService.update_org_variable(
        org_id=org_id,
        var_id=var_id,
        value=request.value,
        updated_by=str(current_user["_id"])
    )
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="TEMPLATE_VARIABLE_UPDATED",
        resource="template_variables",
        resource_id=variable["id"],
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "variable_name": variable.get("name"),
            "namespace_id": org_id
        },
    )
    
    return {"success": True, "data": variable, "message": "Variable updated successfully"}

@router.delete("/org/variables/{var_id}", response_model=dict)
async def delete_org_variable(var_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a specific organization variable."""
    await PermissionService.check_permission(current_user, "templates:variables")
    org_id = get_namespace_id(current_user)
    if not org_id and current_user.get("role") != "super_admin":
        raise HTTPException(status_code=400, detail="User must have a namespace ID")
        
    variable = await TemplateService.delete_org_variable(org_id, var_id)
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="TEMPLATE_VARIABLE_DELETED",
        resource="template_variables",
        resource_id=var_id,
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "variable_name": variable.get("name"),
            "namespace_id": org_id
        },
    )
    
    return {"success": True, "data": variable, "message": "Variable deleted"}


