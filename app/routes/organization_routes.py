"""
Organization routes.

Super Admin endpoints (prefix: /api/v1/organizations):
  POST   /                     — Create organization (name, email, org_name, logo)
  GET    /                     — List all organizations
  GET    /{org_id}             — Get organization details
  PUT    /{org_id}             — Update organization
  DELETE /{org_id}             — Permanently delete organization + all data
  POST   /{org_id}/invite      — Generate invitation link
  POST   /{org_id}/logo        — Upload organization logo
  GET    /audit-logs           — System-wide audit logs (HIPAA)
  GET    /user-activities      — User activation/deactivation activities across all orgs
  GET    /{org_id}/audit-logs  — Org-specific audit logs

Org Admin endpoints (prefix: /api/v1/org):
  GET    /dashboard            — Organization dashboard
  GET    /members              — List org members (?is_active=true/false filter)
  GET    /invite-link          — Get/regenerate the registration link
  PUT    /members/{user_id}/role    — Change member role (+ sends email)
  PUT    /members/{user_id}/status  — Activate/deactivate member (+ sends email on activate)
  GET    /user-activities      — Login/status change activities within the org
  GET    /audit-logs           — Org-scoped audit logs
"""
from fastapi import APIRouter, Depends, Query, UploadFile, File, Form, HTTPException, status, Body
from typing import Optional
from bson import ObjectId
from datetime import datetime, timezone

from app.core.response import api_response
from app.core.database import get_database
from app.core.constants import UserRole, COLLECTION_AUDIT_LOGS
from app.dependencies import get_super_admin, get_org_admin, get_org_admin_or_super
from app.schemas.organization_schema import (
    UpdateOrganizationRequest,
    UpdateOrgMemberRequest,
    InviteMemberRequest,
)
from app.services.organization_service import OrganizationService
from app.services.invitation_service import InvitationService, send_role_change_email, send_activation_email, send_deactivation_email, send_org_status_email
from app.background.report_processor import log_audit_event

# ═══════════════════════════════════════════════════════════════════════════
# SUPER ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════════════════

super_router = APIRouter()


@super_router.post("/")
async def create_organization(
    name: str = Form(..., description="Organization name"),
    email: str = Form(..., description="Org Admin email"),
    logo: Optional[UploadFile] = File(None, description="Organization logo image"),
    current_user=Depends(get_super_admin),
):
    """
    Super Admin: Create a new organization.
    Accepts name, email (for org admin), and optional logo image via multipart form.
    """
    # Validate email format
    from pydantic import EmailStr, ValidationError
    try:
        from pydantic import validate_email
        validate_email(email)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid email format")

    org = await OrganizationService.create_organization(
        name=name,
        email=email,
        created_by=str(current_user["_id"]),
    )

    # Upload logo if provided
    if logo and logo.content_type and logo.content_type.startswith("image/"):
        from app.utils.file_utils import upload_blob
        content = await logo.read()
        ext = logo.filename.rsplit(".", 1)[-1] if logo.filename and "." in logo.filename else "png"
        blob_name = f"organisation_logos/{org['id']}/logo.{ext}"
        await upload_blob(blob_name, content, logo.content_type)
        org = await OrganizationService.update_logo(org["id"], blob_name)

    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="ORG_CREATE",
        resource="organizations",
        resource_id=org["id"],
        metadata={
            "actor_email": current_user.get("email"),
            "org_name": name,
            "org_email": email,
        },
        organization_id=org["id"],
    )
    return api_response(
        success=True,
        message=f"Organization '{name}' created successfully.",
        data=org,
        status_code=201,
    )


@super_router.get("/")
async def list_organizations(
    skip: int = 0,
    limit: int = 50,
    search: Optional[str] = None,
    _=Depends(get_super_admin),
):
    """Super Admin: List all organizations."""
    result = await OrganizationService.list_organizations(skip, limit, search)
    return api_response(success=True, message="Organizations retrieved.", data=result)


@super_router.get("/audit-logs")
async def get_system_audit_logs(
    skip: int = 0,
    limit: int = 50,
    action: Optional[str] = Query(None, description="Filter by action type (e.g. LOGIN, ORG_CREATE, ROLE_CHANGE)"),
    _=Depends(get_super_admin),
):
    """Super Admin: View system-wide audit logs across ALL organizations."""
    db = await get_database()
    query = {}
    if action:
        query["action"] = action

    total = await db[COLLECTION_AUDIT_LOGS].count_documents(query)
    cursor = db[COLLECTION_AUDIT_LOGS].find(query).sort("timestamp", -1).skip(skip).limit(limit)
    logs = []

    actor_cache: dict[str, dict] = {}
    async for log_entry in cursor:
        log_entry["id"] = str(log_entry.pop("_id"))

        actor_id = log_entry.get("actor_id", "")
        if actor_id and actor_id != "system" and actor_id not in actor_cache:
            actor_user = await db["users"].find_one(
                {"_id": ObjectId(actor_id)}, {"email": 1, "full_name": 1, "role": 1}
            ) if ObjectId.is_valid(actor_id) else None
            if actor_user:
                actor_cache[actor_id] = {
                    "email": actor_user.get("email", "unknown"),
                    "name": actor_user.get("full_name", "unknown"),
                    "role": actor_user.get("role", "unknown"),
                }
            else:
                actor_cache[actor_id] = {"email": "unknown", "name": "unknown", "role": "unknown"}

        cached_actor = actor_cache.get(actor_id, {"email": actor_id, "name": actor_id, "role": "unknown"})
        log_entry["actor_email"] = cached_actor["email"]
        log_entry["actor_name"] = cached_actor["name"]
        log_entry["actor_role"] = cached_actor["role"]

        log_org_id = log_entry.get("organization_id")
        if log_org_id and ObjectId.is_valid(log_org_id):
            org_doc = await db["organizations"].find_one(
                {"_id": ObjectId(log_org_id)}, {"name": 1}
            )
            if org_doc:
                log_entry["organization_name"] = org_doc["name"]
            else:
                log_entry["organization_name"] = log_entry.get("metadata", {}).get("org_name", "Unknown")
        else:
            log_entry["organization_name"] = "Platform"

        # Enrich BULK_USER_APPROVE / BULK_USER_REJECT logs with user details if missing
        if log_entry.get("action") in ["BULK_USER_APPROVE", "BULK_USER_REJECT"]:
            meta = log_entry.get("metadata", {})
            if "successful_ids" in meta and "successful_users" not in meta:
                meta["successful_users"] = []
                for uid in meta["successful_ids"]:
                    try:
                        u = await db["users"].find_one({"_id": ObjectId(uid)})
                        if u:
                            meta["successful_users"].append({
                                "id": uid,
                                "email": u.get("email"),
                                "name": u.get("full_name", u.get("name")),
                                "account_type": u.get("account_type"),
                                "role": u.get("role")
                            })
                        else:
                            meta["successful_users"].append({"id": uid, "email": "unknown", "name": "unknown"})
                    except:
                        meta["successful_users"].append({"id": uid, "email": "unknown", "name": "unknown"})
            if "failed_ids" in meta and "failed_users" not in meta:
                meta["failed_users"] = [{"id": uid} for uid in meta["failed_ids"]]

        for k, v in log_entry.items():
            if isinstance(v, datetime):
                log_entry[k] = v.isoformat()
        logs.append(log_entry)

    return api_response(success=True, message="System-wide audit logs retrieved.", data={
        "total": total, "skip": skip, "limit": limit, "logs": logs
    })


@super_router.get("/user-activities")
async def get_user_activities(
    skip: int = 0,
    limit: int = 50,
    organization_id: Optional[str] = Query(None, description="Filter by organization ID"),
    _=Depends(get_super_admin),
):
    """
    Super Admin: View user activation/deactivation activities across all organizations.
    Returns audit logs filtered to user status changes (activate, deactivate, delete, approve).
    Each entry includes: User, Organization, Timestamp, and Action details.
    """
    db = await get_database()
    
    # Filter for user status-related actions
    status_actions = ["MEMBER_APPROVE", "USER_DEACTIVATE", "USER_DELETE", "USER_UPDATE", "ORG_DEACTIVATE"]
    query = {"action": {"$in": status_actions}}
    
    if organization_id:
        query["organization_id"] = organization_id

    total = await db[COLLECTION_AUDIT_LOGS].count_documents(query)
    cursor = db[COLLECTION_AUDIT_LOGS].find(query).sort("timestamp", -1).skip(skip).limit(limit)
    
    activities = []
    actor_cache: dict[str, dict] = {}
    org_cache: dict[str, str] = {}
    
    async for entry in cursor:
        entry["id"] = str(entry.pop("_id"))
        
        # Resolve actor details
        actor_id = entry.get("actor_id", "")
        if actor_id and actor_id != "system" and actor_id not in actor_cache:
            actor_user = await db["users"].find_one(
                {"_id": ObjectId(actor_id)}, {"email": 1, "full_name": 1, "role": 1}
            ) if ObjectId.is_valid(actor_id) else None
            if actor_user:
                actor_cache[actor_id] = {
                    "email": actor_user.get("email", "unknown"),
                    "name": actor_user.get("full_name", "unknown"),
                    "role": actor_user.get("role", "unknown"),
                }
            else:
                actor_cache[actor_id] = {"email": "unknown", "name": "unknown", "role": "unknown"}
        
        cached_actor = actor_cache.get(actor_id, {"email": actor_id, "name": actor_id, "role": "unknown"})
        entry["actor_email"] = cached_actor["email"]
        entry["actor_name"] = cached_actor["name"]
        entry["actor_role"] = cached_actor["role"]
        
        # Resolve organization name
        log_org_id = entry.get("organization_id")
        if log_org_id and log_org_id not in org_cache:
            if ObjectId.is_valid(log_org_id):
                org_doc = await db["organizations"].find_one({"_id": ObjectId(log_org_id)}, {"name": 1})
                org_cache[log_org_id] = org_doc["name"] if org_doc else "Unknown"
            else:
                org_cache[log_org_id] = "Unknown"
        entry["organization_name"] = org_cache.get(log_org_id, "Platform")
        
        # Resolve target user from metadata
        meta = entry.get("metadata", {})
        entry["target_email"] = meta.get("target_email", meta.get("actor_email", "N/A"))
        
        for k, v in entry.items():
            if isinstance(v, datetime):
                entry[k] = v.isoformat()
        activities.append(entry)

    return api_response(success=True, message="User activities retrieved.", data={
        "total": total, "skip": skip, "limit": limit, "activities": activities
    })


@super_router.get("/{org_id}")
async def get_organization(org_id: str, _=Depends(get_super_admin)):
    """Super Admin: Get organization details."""
    org = await OrganizationService.get_organization(org_id)
    return api_response(success=True, message="Organization found.", data=org)


@super_router.put("/{org_id}")
async def update_organization(
    org_id: str,
    name: Optional[str] = Form(None, description="Organization name"),
    email: Optional[str] = Form(None, description="Org Admin email"),
    is_active: Optional[bool] = Form(None, description="Active status"),
    logo: Optional[UploadFile] = File(None, description="New logo image"),
    current_user=Depends(get_super_admin),
):
    """Super Admin: Update organization details and/or logo."""
    old_org = await OrganizationService.get_organization(org_id)

    update_data = {}
    if name is not None and name != old_org.get("name"):
        update_data["name"] = name
    if email is not None and email != old_org.get("email"):
        update_data["email"] = email
    if is_active is not None and is_active != old_org.get("is_active"):
        update_data["is_active"] = is_active

    # Update text fields if any
    if update_data:
        org = await OrganizationService.update_organization(org_id, update_data)
    else:
        org = old_org

    # Upload new logo if provided
    if logo and logo.content_type and logo.content_type.startswith("image/"):
        from app.utils.file_utils import upload_blob
        content = await logo.read()
        ext = logo.filename.rsplit(".", 1)[-1] if logo.filename and "." in logo.filename else "png"
        blob_name = f"organisation_logos/{org_id}/logo.{ext}"
        await upload_blob(blob_name, content, logo.content_type)
        org = await OrganizationService.update_logo(org_id, blob_name)

    # If is_active was updated, notify the org admin
    if "is_active" in update_data and org.get("email"):
        await send_org_status_email(
            to_email=org["email"],
            full_name="Organization Admin",
            org_name=org.get("name", "Unknown"),
            is_active=update_data["is_active"]
        )

    fields_dict = update_data.copy()
    if logo:
        fields_dict["logo"] = "updated"

    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="ORG_UPDATE",
        resource="organizations",
        resource_id=org_id,
        metadata={
            "actor_email": current_user.get("email"),
            "org_name": org.get("name", "Unknown"),
            "updated_fields": fields_dict,
        },
        organization_id=org_id,
    )
    return api_response(success=True, message="Organization updated.", data=org)


@super_router.delete("/{org_id}")
async def delete_organization(org_id: str, current_user=Depends(get_super_admin)):
    """Super Admin: Permanently delete an organization and all its data (users, chats, sessions, etc.)."""
    org = await OrganizationService.delete_organization(org_id)
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="ORG_DELETE",
        resource="organizations",
        resource_id=org_id,
        metadata={
            "actor_email": current_user.get("email"),
            "org_name": org.get("name"),
            "org_email": org.get("email"),
        },
        organization_id=org_id,
    )
    return api_response(success=True, message=f"Organization '{org.get('name')}' and all associated data permanently deleted.", data=org)


@super_router.post("/{org_id}/invite")
async def generate_invite_link(org_id: str, current_user=Depends(get_super_admin)):
    """
    Super Admin: Generate an invitation link for the organization.
    
    This single link serves both purposes:
      - Org Admin registers (matching email → org_admin role, is_active=True)
      - Members register (any email → staff role, is_active=False)
    
    The link is automatically emailed to the org admin email.
    """
    result = await InvitationService.generate_invite_link(
        org_id=org_id,
        invited_by=str(current_user["_id"]),
    )
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="INVITE_LINK_GENERATED",
        resource="invitations",
        resource_id=org_id,
        metadata={
            "actor_email": current_user.get("email"),
            "organization_name": result.get("organization_name"),
        },
    )
    return api_response(
        success=True,
        message=f"Invitation link generated and sent to org admin.",
        data=result,
    )



@super_router.get("/{org_id}/audit-logs")
async def get_org_audit_logs(
    org_id: str,
    skip: int = 0,
    limit: int = 50,
    _=Depends(get_super_admin),
):
    """Super Admin: View audit logs for a specific organization."""
    db = await get_database()
    query = {"organization_id": org_id}
    total = await db[COLLECTION_AUDIT_LOGS].count_documents(query)
    cursor = db[COLLECTION_AUDIT_LOGS].find(query).sort("timestamp", -1).skip(skip).limit(limit)
    logs = []
    actor_cache: dict[str, dict] = {}
    async for log in cursor:
        log["id"] = str(log.pop("_id"))

        # Resolve actor details (name + role)
        actor_id = log.get("actor_id", "")
        if actor_id and actor_id != "system" and actor_id not in actor_cache:
            actor_user = await db["users"].find_one(
                {"_id": ObjectId(actor_id)}, {"email": 1, "full_name": 1, "role": 1}
            ) if ObjectId.is_valid(actor_id) else None
            if actor_user:
                actor_cache[actor_id] = {
                    "email": actor_user.get("email", "unknown"),
                    "name": actor_user.get("full_name", "unknown"),
                    "role": actor_user.get("role", "unknown"),
                }
            else:
                actor_cache[actor_id] = {"email": "unknown", "name": "unknown", "role": "unknown"}

        cached_actor = actor_cache.get(actor_id, {"email": actor_id, "name": actor_id, "role": "unknown"})
        log["actor_email"] = cached_actor["email"]
        log["actor_name"] = cached_actor["name"]
        log["actor_role"] = cached_actor["role"]

        for k, v in log.items():
            if isinstance(v, datetime):
                log[k] = v.isoformat()
        logs.append(log)
    return api_response(success=True, message="Audit logs retrieved.", data={
        "total": total, "skip": skip, "limit": limit, "logs": logs
    })


# ═══════════════════════════════════════════════════════════════════════════
# PERMISSION MANAGEMENT (Super Admin)
# ═══════════════════════════════════════════════════════════════════════════

@super_router.get("/permissions")
async def list_all_permissions(_=Depends(get_super_admin)):
    """Super Admin: Get the full role-permission matrix for all roles."""
    from app.services.permission_service import PermissionService
    data = await PermissionService.get_all_permissions()
    return api_response(success=True, message="Permission matrix retrieved.", data=data)


@super_router.get("/permissions/{role}")
async def get_role_permissions(role: str, _=Depends(get_super_admin)):
    """Super Admin: Get permissions for a specific role."""
    from app.services.permission_service import PermissionService
    data = await PermissionService.get_role_permissions(role)
    if not data:
        raise HTTPException(status_code=404, detail=f"No permissions found for role: {role}")
    return api_response(success=True, message=f"Permissions for '{role}' retrieved.", data=data)


@super_router.put("/permissions/{role}")
async def update_role_permissions(
    role: str,
    permissions: dict = Body(..., description="Permission keys to update, e.g. {'sessions:delete': true}"),
    current_user=Depends(get_super_admin),
):
    """
    Super Admin: Update permissions for a specific role.

    Accepts a partial dict — only the keys you send are updated.
    The cache is invalidated immediately so the change takes effect instantly.

    Example body:
    ```json
    {"sessions:delete": true, "analytics:dashboard": false}
    ```
    """
    from app.services.permission_service import PermissionService

    # Validate that all values are booleans
    for key, value in permissions.items():
        if not isinstance(value, bool):
            raise HTTPException(
                status_code=400,
                detail=f"Permission value for '{key}' must be true or false, got: {type(value).__name__}"
            )

    result = await PermissionService.update_role_permissions(
        role=role,
        permissions=permissions,
        updated_by=str(current_user["_id"]),
    )

    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="PERMISSION_UPDATE",
        resource="role_permissions",
        resource_id=role,
        metadata={"permissions_changed": list(permissions.keys()), "actor_email": current_user.get("email")},
    )

    return api_response(success=True, message=f"Permissions for '{role}' updated.", data=result)


# ═══════════════════════════════════════════════════════════════════════════
# ORG ADMIN ROUTES
# ═══════════════════════════════════════════════════════════════════════════

org_router = APIRouter()


@org_router.get("/dashboard")
async def org_dashboard(current_user=Depends(get_org_admin)):
    """Org Admin: View organization dashboard with stats."""
    org_id = current_user["organization_id"]
    db = await get_database()

    from app.utils.file_utils import generate_permanent_sas_url
    
    org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
    
    logo_url = org.get("logo_url") if org else None
    if logo_url and not logo_url.startswith("http"):
        logo_url = generate_permanent_sas_url(logo_url)
        
    total_members = await db["users"].count_documents({"organization_id": org_id})
    active_members = await db["users"].count_documents({"organization_id": org_id, "is_active": True})
    pending_members = await db["users"].count_documents({"organization_id": org_id, "is_active": False})
    # total_sessions = await db["dental_voice_sessions"].count_documents({"organization_id": org_id})

    return api_response(success=True, message="Dashboard loaded.", data={
        "organization": {
            "id": org_id,
            "name": org["name"] if org else "Unknown",
            "email": org["email"] if org else None,
            "logo_url": logo_url,
            "auto_delete_sessions": org.get("auto_delete_sessions", True) if org else True,
            "xray_enabled": org.get("xray_enabled", True) if org else True
        },
        "total_members": total_members,
        "active_members": active_members,
        "pending_members": pending_members,
        # "total_sessions": total_sessions,
    })


@org_router.put("/settings")
async def update_org_settings(
    auto_delete_sessions: Optional[bool] = Body(None, description="Enable or disable 30-day auto deletion of voice sessions"),
    xray_enabled: Optional[bool] = Body(None, description="Enable or disable x-ray selection for sessions"),
    current_user=Depends(get_org_admin),
):
    """Org Admin: Update organization settings (e.g. auto deletion, x-ray toggle)."""
    org_id = current_user["organization_id"]

    update_data = {}
    updated_fields = []
    if auto_delete_sessions is not None:
        update_data["auto_delete_sessions"] = auto_delete_sessions
        updated_fields.append("auto_delete_sessions")
    if xray_enabled is not None:
        update_data["xray_enabled"] = xray_enabled
        updated_fields.append("xray_enabled")

    if not update_data:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Provide at least one setting to update")

    org = await OrganizationService.update_organization(org_id, update_data)
    
    from app.background.report_processor import log_audit_event
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="ORG_UPDATE",
        resource="organizations",
        resource_id=org_id,
        metadata={
            "actor_email": current_user.get("email"),
            "updated_fields": updated_fields,
            **update_data
        },
        organization_id=org_id
    )
    
    return api_response(success=True, message="Organization settings updated.", data=org)


@org_router.get("/members")
async def list_org_members(
    skip: int = 0,
    limit: int = 50,
    is_active: Optional[bool] = Query(None, description="Filter by active status: true=active, false=inactive"),
    current_user=Depends(get_org_admin),
):
    """Org Admin: List members in their organization. Supports ?is_active=true/false filter."""
    org_id = current_user["organization_id"]
    result = await OrganizationService.get_org_members(org_id, skip, limit, is_active=is_active)
    return api_response(success=True, message="Members retrieved.", data=result)


@org_router.get("/billing-preview")
async def get_org_billing_preview(
    target_user_id: Optional[str] = Query(None, description="ID of the user being modified"),
    action: Optional[str] = Query(None, description="Action: activate, deactivate, change_role, or invite"),
    new_role: Optional[str] = Query(None, description="Proposed new role"),
    current_user=Depends(get_org_admin),
):
    """
    Org Admin: Preview subscription cost impact before making a dentist seat change.
    Calculates current vs proposed monthly rate and estimated deferred proration charge/credit.
    """
    org_id = current_user["organization_id"]
    from app.services.subscription_service import SubscriptionService
    preview = await SubscriptionService.preview_org_member_change_impact(
        org_id=org_id,
        target_user_id=target_user_id,
        action=action,
        new_role=new_role
    )
    return api_response(
        success=True,
        message="Billing preview calculated successfully.",
        data=preview
    )


@org_router.post("/invite")
async def invite_member(
    payload: InviteMemberRequest,
    current_user=Depends(get_org_admin)
):
    """Org Admin: Send a targeted registration link to a specific email."""
    org_id = current_user["organization_id"]
    db = await get_database()
    org = await db["organizations"].find_one({"_id": ObjectId(org_id)})

    if payload.role in ["dentist", UserRole.DENTIST] and not (org and org.get("is_exempt")):
        stripe_sub_doc = await db["stripe_subscriptions"].find_one({"organization_id": org_id, "status": "pending_cancellation"})
        if stripe_sub_doc:
            raise HTTPException(
                status_code=400,
                detail="Your organization subscription is scheduled to cancel at period end. You cannot invite new dentists while cancellation is pending. Please resume your subscription first."
            )

    result = await InvitationService.invite_member(
        org_id=org_id,
        email=payload.email,
        invited_by=str(current_user["_id"]),
        role=payload.role,
    )
    
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="MEMBER_INVITED",
        resource="invitations",
        resource_id=org_id,
        metadata={
            "actor_email": current_user.get("email"),
            "invited_email": payload.email,
        },
        organization_id=org_id
    )
    
    return api_response(success=True, message="Invitation sent successfully.", data=result)


@org_router.put("/members/{user_id}")
async def update_member(
    user_id: str,
    payload: UpdateOrgMemberRequest,
    current_user=Depends(get_org_admin),
):
    """
    Org Admin: Update a member's role and/or activation status.
    Sends appropriate emails if status or role changes.
    """
    org_id = current_user["organization_id"]
    db = await get_database()

    target = await db["users"].find_one({"_id": ObjectId(user_id)})
    if not target or target.get("organization_id") != org_id:
        raise HTTPException(status_code=404, detail="User not found in your organization")

    # Check if subscription is pending cancellation and requested update increases dentist count
    org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
    if not (org and org.get("is_exempt")):
        stripe_sub_doc = await db["stripe_subscriptions"].find_one({"organization_id": org_id, "status": "pending_cancellation"})
        if stripe_sub_doc:
            dentist_roles = ["dentist", UserRole.DENTIST]
            target_role = payload.role or target.get("role")
            target_active = payload.is_active if payload.is_active is not None else target.get("is_active")
            
            is_increasing_dentists = False
            if target_role in dentist_roles and target_active:
                if not target.get("is_active") or target.get("role") not in dentist_roles:
                    is_increasing_dentists = True
                    
            if is_increasing_dentists:
                raise HTTPException(
                    status_code=400,
                    detail="Your organization subscription is scheduled to cancel at period end. You cannot add or activate dentist seats while cancellation is pending. Please resume your subscription first."
                )

    updates = {}
    old_role = target.get("role", "unknown")
    new_role = payload.role
    is_active_flag = payload.is_active

    # Handle role validation
    if new_role and new_role != old_role:
        allowed_roles = [UserRole.DENTIST, UserRole.STAFF, UserRole.FRONT_OFFICE]
        if new_role not in [r.value for r in allowed_roles]:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid role. Allowed: {', '.join(r.value for r in allowed_roles)}"
            )
        updates["role"] = new_role

    # Handle status
    if is_active_flag is not None and is_active_flag != target.get("is_active"):
        updates["is_active"] = is_active_flag
        if is_active_flag is False:
            updates["refresh_tokens"] = []

    if not updates:
        return api_response(success=True, message="No changes requested.", data={"user_id": user_id})

    updates["updated_at"] = datetime.now(timezone.utc)
    await db["users"].update_one({"_id": ObjectId(user_id)}, {"$set": updates})

    # Send emails and log audit events
    org = await db["organizations"].find_one({"_id": ObjectId(org_id)}, {"name": 1})
    org_name = org["name"] if org else "your organization"
    messages = []

    # If role changed
    if new_role and new_role != old_role:
        await send_role_change_email(
            to_email=target["email"],
            full_name=target.get("full_name", "User"),
            new_role=new_role,
            org_name=org_name,
        )
        await log_audit_event(
            actor_id=str(current_user["_id"]),
            action="ROLE_CHANGE",
            resource="users",
            resource_id=user_id,
            metadata={
                "actor_email": current_user.get("email"),
                "target_email": target.get("email"),
                "old_role": old_role,
                "new_role": new_role,
            },
            organization_id=org_id,
        )
        messages.append(f"Role updated to '{new_role}'")

    # If status changed
    if is_active_flag is not None and is_active_flag != target.get("is_active"):
        if is_active_flag:
            await send_activation_email(
                to_email=target["email"],
                full_name=target.get("full_name", "User"),
                org_name=org_name,
            )
        else:
            await send_deactivation_email(
                to_email=target["email"],
                full_name=target.get("full_name", "User"),
                org_name=org_name,
            )
        action = "MEMBER_APPROVE" if is_active_flag else "USER_DEACTIVATE"
        await log_audit_event(
            actor_id=str(current_user["_id"]),
            action=action,
            resource="users",
            resource_id=user_id,
            metadata={
                "actor_email": current_user.get("email"),
                "target_email": target.get("email"),
                "is_active": is_active_flag,
            },
            organization_id=org_id,
        )
        msg_status = "activated" if is_active_flag else "deactivated"
        messages.append(f"User {msg_status}")

    billing_impact = None
    try:
        from app.services.subscription_service import SubscriptionService
        await SubscriptionService.sync_org_dentist_subscription_quantity(org_id)
        billing_impact = await SubscriptionService.preview_org_member_change_impact(org_id=org_id)
    except Exception:
        pass

    return api_response(
        success=True,
        message=", ".join(messages) + f". Notification(s) sent to {target['email']}.",
        data={
            "user_id": user_id,
            "role": new_role or old_role,
            "is_active": is_active_flag if is_active_flag is not None else target.get("is_active"),
            "billing_impact": billing_impact
        }
    )


@org_router.get("/user-activities")
async def get_org_user_activities(
    skip: int = 0,
    limit: int = 50,
    current_user=Depends(get_org_admin),
):
    """
    Org Admin: View user login/logout and status change activities within their organization.
    Tracks: LOGIN, MEMBER_APPROVE, USER_DEACTIVATE, ROLE_CHANGE.
    """
    org_id = current_user["organization_id"]
    db = await get_database()

    activity_actions = ["LOGIN", "MEMBER_APPROVE", "USER_DEACTIVATE", "ROLE_CHANGE"]
    query = {"organization_id": org_id, "action": {"$in": activity_actions}}

    total = await db[COLLECTION_AUDIT_LOGS].count_documents(query)
    cursor = db[COLLECTION_AUDIT_LOGS].find(query).sort("timestamp", -1).skip(skip).limit(limit)

    activities = []
    actor_cache: dict[str, dict] = {}
    async for entry in cursor:
        entry["id"] = str(entry.pop("_id"))

        actor_id = entry.get("actor_id", "")
        if actor_id and actor_id != "system" and actor_id not in actor_cache:
            actor_user = await db["users"].find_one(
                {"_id": ObjectId(actor_id)}, {"email": 1, "full_name": 1, "role": 1}
            ) if ObjectId.is_valid(actor_id) else None
            if actor_user:
                actor_cache[actor_id] = {
                    "email": actor_user.get("email", "unknown"),
                    "name": actor_user.get("full_name", "unknown"),
                    "role": actor_user.get("role", "unknown"),
                }
            else:
                actor_cache[actor_id] = {"email": "unknown", "name": "unknown", "role": "unknown"}

        cached_actor = actor_cache.get(actor_id, {"email": actor_id, "name": actor_id, "role": "unknown"})
        entry["actor_email"] = cached_actor["email"]
        entry["actor_name"] = cached_actor["name"]
        entry["actor_role"] = cached_actor["role"]

        meta = entry.get("metadata", {})
        entry["target_email"] = meta.get("target_email", meta.get("email", "N/A"))

        for k, v in entry.items():
            if isinstance(v, datetime):
                entry[k] = v.isoformat()
        activities.append(entry)

    return api_response(success=True, message="User activities retrieved.", data={
        "total": total, "skip": skip, "limit": limit, "activities": activities
    })


@org_router.get("/audit-logs")
async def get_my_org_audit_logs(
    skip: int = 0,
    limit: int = 50,
    action: Optional[str] = Query(None, description="Filter by action type (e.g. LOGIN, ROLE_CHANGE, MEMBER_APPROVE)"),
    current_user=Depends(get_org_admin),
):
    """Org Admin: View audit logs for their organization with actor details."""
    org_id = current_user["organization_id"]
    db = await get_database()
    query = {"organization_id": org_id}
    if action:
        query["action"] = action

    total = await db[COLLECTION_AUDIT_LOGS].count_documents(query)
    cursor = db[COLLECTION_AUDIT_LOGS].find(query).sort("timestamp", -1).skip(skip).limit(limit)
    logs = []

    actor_cache: dict[str, dict] = {}
    async for log_entry in cursor:
        log_entry["id"] = str(log_entry.pop("_id"))

        actor_id = log_entry.get("actor_id", "")
        if actor_id and actor_id != "system" and actor_id not in actor_cache:
            actor_user = await db["users"].find_one(
                {"_id": ObjectId(actor_id)}, {"email": 1, "full_name": 1, "role": 1}
            ) if ObjectId.is_valid(actor_id) else None
            if actor_user:
                actor_cache[actor_id] = {
                    "email": actor_user.get("email", "unknown"),
                    "name": actor_user.get("full_name", "unknown"),
                    "role": actor_user.get("role", "unknown"),
                }
            else:
                actor_cache[actor_id] = {"email": "unknown", "name": "unknown", "role": "unknown"}

        cached = actor_cache.get(actor_id, {"email": actor_id, "name": actor_id, "role": "unknown"})
        log_entry["actor_email"] = cached["email"]
        log_entry["actor_name"] = cached["name"]
        log_entry["actor_role"] = cached["role"]

        # Enrich BULK_USER_APPROVE / BULK_USER_REJECT logs with user details if missing
        if log_entry.get("action") in ["BULK_USER_APPROVE", "BULK_USER_REJECT"]:
            meta = log_entry.get("metadata", {})
            if "successful_ids" in meta and "successful_users" not in meta:
                meta["successful_users"] = []
                for uid in meta["successful_ids"]:
                    try:
                        u = await db["users"].find_one({"_id": ObjectId(uid)})
                        if u:
                            meta["successful_users"].append({
                                "id": uid,
                                "email": u.get("email"),
                                "name": u.get("full_name", u.get("name")),
                                "account_type": u.get("account_type"),
                                "role": u.get("role")
                            })
                        else:
                            meta["successful_users"].append({"id": uid, "email": "unknown", "name": "unknown"})
                    except:
                        meta["successful_users"].append({"id": uid, "email": "unknown", "name": "unknown"})
            if "failed_ids" in meta and "failed_users" not in meta:
                meta["failed_users"] = [{"id": uid} for uid in meta["failed_ids"]]

        for k, v in log_entry.items():
            if isinstance(v, datetime):
                log_entry[k] = v.isoformat()
        logs.append(log_entry)

    return api_response(success=True, message="Audit logs retrieved.", data={
        "total": total, "skip": skip, "limit": limit, "logs": logs
    })
