from fastapi import APIRouter, Depends, status
from bson import ObjectId

from app.core.response import api_response
from app.core.database import get_database
from app.schemas.user_schema import UserUpdateRequest, RoleUpdateRequest, BulkActionRequest
from app.services.user_service import UserService
from app.dependencies import get_current_user, get_current_admin, require_permission, get_super_admin
from app.core.constants import UserRole
from app.background.report_processor import log_audit_event

router = APIRouter()

@router.get("/me")
async def get_profile(current_user=Depends(get_current_user)):
    """Return the authenticated user's profile."""
    # current_user is already the user document from the DB
    user_data = await UserService.get_user_by_id(str(current_user["_id"]))
    return api_response(
        success=True,
        message="Profile retrieved successfully.",
        data=user_data
    )

@router.put("/me")
async def update_profile(
    payload: UserUpdateRequest, current_user=Depends(get_current_user)
):
    """Update the authenticated user's profile (full_name only)."""
    user_data = await UserService.update_user(str(current_user["_id"]), payload)
    return api_response(
        success=True,
        message="Profile updated successfully.",
        data=user_data
    )

@router.post("/me/agree-disclaimer")
async def agree_disclaimer(current_user=Depends(get_current_user)):
    """Mark that the authenticated user has accepted the disclaimer (called once)."""
    db = await get_database()
    await db["users"].update_one(
        {"_id": current_user["_id"]},
        {"$set": {"has_agreed": True}}
    )
    return api_response(
        success=True,
        message="Disclaimer accepted.",
        data={"has_agreed": True}
    )

# ── Admin-only endpoints ────────────────────────────────────────────────────

@router.get("/")
async def list_users(
    skip: int = 0,
    limit: int = 50,
    plan: str | None = None,
    is_active: bool | None = None,
    account_type: str | None = None,
    role: str | None = None,
    search: str | None = None,
    sort_by: str = "created_at",
    sort_order: int = -1,
    current_user: dict = Depends(get_current_admin)
):
    """
    List registered users with pagination and filters.
    Super Admins can see all users. Org Admins can only see users in their organization.
    """
    org_id = current_user.get("organization_id") if current_user.get("role") == UserRole.ORG_ADMIN else None
    
    result = await UserService.list_users(skip, limit, plan, is_active, account_type, role, search, sort_by, sort_order, org_id)
    # result already contains {"total": ..., "users": [...]}
    return api_response(
        success=True,
        message="Users retrieved successfully.",
        data=result
    )

@router.get("/{user_id}")
async def get_user(user_id: str, current_user=Depends(require_permission("users:list"))):
    """Fetch a single user by ID (admin only). Includes deep insights."""
    from app.dependencies import verify_same_org
    await verify_same_org(current_user, user_id)
    user_data = await UserService.get_user_by_id(user_id, admin_mode=True)
    return api_response(
        success=True,
        message="User retrieved successfully.",
        data=user_data
    )

@router.get("/{user_id}/subscriptions", dependencies=[Depends(require_permission("users:list"))])
async def get_user_subscriptions_admin(user_id: str, skip: int = 0, limit: int = 50):
    """Fetch a user's subscription history (admin only)."""
    from app.services.subscription_service import SubscriptionService
    result = await SubscriptionService.list_user_subscriptions(user_id, skip, limit)
    return api_response(
        success=True,
        message="User subscriptions retrieved successfully.",
        data=result
    )

@router.put("/{user_id}/role")
async def update_user_role(user_id: str, payload: RoleUpdateRequest, current_user=Depends(require_permission("users:manage"))):
    """Promote or demote a user's role (admin only)."""
    result = await UserService.update_role(user_id, payload.role)
    # Fetch target user info for human-readable audit record
    db = await get_database()
    target = await db["users"].find_one({"_id": ObjectId(user_id)}, {"email": 1, "full_name": 1, "organization_id": 1})
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="ROLE_CHANGE",
        resource="users",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "target_email": target.get("email") if target else "unknown",
            "target_name": target.get("full_name") if target else "unknown",
            "new_role": payload.role,
        },
        organization_id=target.get("organization_id") if target else current_user.get("organization_id"),
    )
    return api_response(
        success=True, 
        message=result["message"], 
        data={"user_id": result["user_id"]}
    )

@router.put("/{user_id}")
async def admin_update_user(user_id: str, payload: UserUpdateRequest, current_user=Depends(require_permission("users:manage"))):
    """Admin can update any user field, including is_active (activation/deactivation)."""
    # Fetch target user info before update for audit record
    db = await get_database()
    target = await db["users"].find_one({"_id": ObjectId(user_id)}, {"email": 1, "full_name": 1, "organization_id": 1})
    user_data = await UserService.update_user(user_id, payload)
    action = "USER_DEACTIVATE" if payload.is_active is False else "USER_UPDATE"
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action=action,
        resource="users",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "target_email": target.get("email") if target else "unknown",
            "target_name": target.get("full_name") if target else "unknown",
            "updated_fields": payload.model_dump(exclude_unset=True),
        },
        organization_id=target.get("organization_id") if target else current_user.get("organization_id"),
    )
    return api_response(
        success=True,
        message="User updated successfully.",
        data=user_data
    )

@router.post("/{user_id}/approve", dependencies=[Depends(get_super_admin)])
async def approve_user(user_id: str, current_user=Depends(get_current_user)):
    """Approve a pending registration request (Super Admin only)."""
    result = await UserService.approve_user(user_id)
    
    # Audit log
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="USER_APPROVE",
        resource="users",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "target_email": result.get("email"),
        },
        organization_id=result.get("organization_id"),
    )
    
    return api_response(
        success=True,
        message="User approved successfully.",
        data=result
    )

@router.post("/{user_id}/reject", dependencies=[Depends(get_super_admin)])
async def reject_user(user_id: str, current_user=Depends(get_current_user)):
    """Reject a pending registration request (Super Admin only)."""
    result = await UserService.reject_user(user_id)
    
    # Audit log
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="USER_REJECT",
        resource="users",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "target_email": result.get("email"),
        },
        organization_id=None,
    )
    
    return api_response(
        success=True,
        message="User rejected successfully.",
        data=result
    )

@router.get("/pending-approvals/all", dependencies=[Depends(get_super_admin)])
async def list_pending_approvals(skip: int = 0, limit: int = 50):
    """List all users pending Super Admin approval."""
    result = await UserService.list_pending_approvals(skip, limit)
    return api_response(
        success=True,
        message="Pending approvals retrieved successfully.",
        data=result
    )

@router.post("/bulk-approve", dependencies=[Depends(get_super_admin)])
async def bulk_approve_users(payload: BulkActionRequest, current_user=Depends(get_current_user)):
    """Approve multiple pending registration requests at once."""
    result = await UserService.bulk_approve_users(payload.user_ids)
    
    # Audit log
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="BULK_USER_APPROVE",
        resource="users",
        resource_id="bulk",
        metadata={
            "actor_email": current_user.get("email"),
            "successful_ids": [r["id"] for r in result["successful"]],
            "failed_ids": [r["id"] for r in result["failed"]],
            "successful_users": result["successful"],
            "failed_users": result["failed"],
        },
        organization_id=None,
    )

    return api_response(
        success=True,
        message="Bulk approval processed.",
        data=result
    )

@router.post("/bulk-reject", dependencies=[Depends(get_super_admin)])
async def bulk_reject_users(payload: BulkActionRequest, current_user=Depends(get_current_user)):
    """Reject multiple pending registration requests at once."""
    result = await UserService.bulk_reject_users(payload.user_ids)
    
    # Audit log
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="BULK_USER_REJECT",
        resource="users",
        resource_id="bulk",
        metadata={
            "actor_email": current_user.get("email"),
            "successful_ids": [r["id"] for r in result["successful"]],
            "failed_ids": [r["id"] for r in result["failed"]],
            "successful_users": result["successful"],
            "failed_users": result["failed"],
        },
        organization_id=None,
    )

    return api_response(
        success=True,
        message="Bulk rejection processed.",
        data=result
    )

@router.delete("/{user_id}")
async def delete_user(user_id: str, current_user=Depends(require_permission("users:delete"))):
    """Soft-delete a user account (admin only)."""
    # Fetch target user info BEFORE deletion so we can record it
    db = await get_database()
    target = await db["users"].find_one({"_id": ObjectId(user_id)}, {"email": 1, "full_name": 1, "organization_id": 1})
    await UserService.delete_user(user_id)
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="USER_DELETE",
        resource="users",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "target_email": target.get("email") if target else "unknown",
            "target_name": target.get("full_name") if target else "unknown",
        },
        organization_id=target.get("organization_id") if target else current_user.get("organization_id"),
    )
    return api_response(
        success=True,
        message="User deleted successfully.",
        data=None,
        status_code=status.HTTP_200_OK 
    )