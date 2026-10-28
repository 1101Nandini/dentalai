"""
FastAPI dependency functions for authentication and role-based access control.

Hierarchy (highest → lowest):
  super_admin  — Platform-wide control, no organization
  org_admin    — Manages one organization's members
  dentist      — AI chat, CDT reports within their org
  staff        — View/edit reports within their org
"""
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt, ExpiredSignatureError
from typing import Annotated, Optional

from app.config import settings
from app.core.database import get_database
from app.core.constants import UserRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


# ═══════════════════════════════════════════════════════════════════════════
# BASE: Resolve current user from JWT
# ═══════════════════════════════════════════════════════════════════════════

async def get_current_user(
    request: Request,
    bearer_token: Annotated[Optional[str], Depends(oauth2_scheme)] = None,
):
    """
    Resolve the current user from either:
      1. The `Authorization: Bearer <token>` header (primary)
      2. The `access_token` cookie (fallback for backward compatibility)

    Also verifies:
      - Account is active (not deactivated by admin)
      - Account is verified (not pending approval)
      - User's organization (if any) is active
    """
    token = bearer_token or request.cookies.get("access_token")

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Missing or invalid authentication token", "refresh_allowed": True},
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"message": "Invalid token format", "refresh_allowed": True},
                headers={"WWW-Authenticate": "Bearer"},
            )
    except ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"message": "Token has expired. Please refresh your token or log in again.", "refresh_allowed": True},
            headers={"WWW-Authenticate": "Bearer"},
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or malformed authentication token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    db = await get_database()
    user = await db["users"].find_one({"_id": ObjectId(user_id)})

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or account is inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if account is deactivated by admin
    if not user.get("is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "Your account has been deactivated. Please contact support.", "code": "ACCOUNT_DEACTIVATED"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check if account is pending admin approval
    if not user.get("is_verified", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"message": "Your account is pending admin approval. You will receive an email once approved.", "code": "ACCOUNT_PENDING_APPROVAL"},
            headers={"WWW-Authenticate": "Bearer"},
        )

    # If user belongs to an org, verify the org is active
    org_id = user.get("organization_id")
    if org_id:
        org = await db["organizations"].find_one({"_id": ObjectId(org_id)}) if ObjectId.is_valid(org_id) else None
        if org and not org.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": "Your organization has been deactivated. Please contact support.", "code": "ORGANIZATION_DEACTIVATED"},
            )

    # Students: enforce single-session — if session_id in JWT doesn't match DB, kick them out
    if user.get("role") == UserRole.STUDENT:
        token_session_id = payload.get("session_id")
        db_session_id = user.get("active_session_id")
        if db_session_id and token_session_id != db_session_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": "Your session has ended because your account was logged in on another device.", "code": "SESSION_REPLACED"},
                headers={"WWW-Authenticate": "Bearer"},
            )

    return user


# ═══════════════════════════════════════════════════════════════════════════
# ROLE-BASED GUARDS
# ═══════════════════════════════════════════════════════════════════════════

async def get_super_admin(current_user=Depends(get_current_user)):
    """Restrict to Super Admin only — platform-wide operations."""
    if current_user.get("role") != UserRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super Admin privileges required",
        )
    return current_user


async def get_org_admin(current_user=Depends(get_current_user)):
    """Restrict to Org Admin — organization-level management."""
    if current_user.get("role") != UserRole.ORG_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Organization Admin privileges required",
        )
    if not current_user.get("organization_id"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not assigned to any organization",
        )
    return current_user


async def get_org_admin_or_super(current_user=Depends(get_current_user)):
    """Allow Super Admin OR Org Admin — for management endpoints."""
    role = current_user.get("role")
    if role not in [UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required (Super Admin or Organization Admin)",
        )
    return current_user


# Legacy alias — existing code references get_current_admin
async def get_current_admin(current_user=Depends(get_current_user)):
    """Legacy: maps to org_admin_or_super for backward compatibility."""
    role = current_user.get("role")
    if role not in [UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN, UserRole.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required",
        )
    return current_user


async def get_staff_member(current_user=Depends(get_current_user)):
    """Allow access for Org Admins, Dentists, and Staff within their org."""
    allowed = [UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN, UserRole.ADMIN,
               UserRole.DENTIST, UserRole.STAFF]
    if current_user.get("role") not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to Admins, Dentists, and Staff only",
        )
    return current_user


async def get_current_dentist(current_user=Depends(get_current_user)):
    """Restrict endpoint access to Dentist users only."""
    if current_user.get("role") != UserRole.DENTIST:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dentist privileges required",
        )
    return current_user


async def get_admin_or_dentist(current_user=Depends(get_current_user)):
    """Allow access for Admins and Dentists only."""
    allowed = [UserRole.SUPER_ADMIN, UserRole.ORG_ADMIN, UserRole.ADMIN, UserRole.DENTIST]
    if current_user.get("role") not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access restricted to Admins and Dentists only",
        )
    return current_user





# ═══════════════════════════════════════════════════════════════════════════
# DB-DRIVEN PERMISSION GUARD
# ═══════════════════════════════════════════════════════════════════════════

def require_permission(permission_key: str):
    """
    Factory that returns a FastAPI dependency checking a DB-managed permission.

    Usage:
        @router.delete("/sessions/{id}")
        async def delete_session(current_user=Depends(require_permission("sessions:delete"))):
            ...

    Super Admin always bypasses the check.
    """
    async def _check(current_user=Depends(get_current_user)):
        from app.services.permission_service import PermissionService

        role = current_user.get("role")
        # Super Admin always has full access
        if role == UserRole.SUPER_ADMIN:
            return current_user

        await PermissionService.check_permission(current_user, permission_key)
        return current_user

    return _check


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def get_user_org_id(user: dict) -> str | None:
    """Extract organization_id from a user document, or None for Super Admin."""
    return user.get("organization_id")


async def verify_same_org(requester: dict, target_user_id: str) -> dict:
    """
    Ensure the requester and target user belong to the same organization.
    Super Admins bypass this check.
    """
    if requester.get("role") == UserRole.SUPER_ADMIN:
        db = await get_database()
        target = await db["users"].find_one({"_id": ObjectId(target_user_id)})
        if not target:
            raise HTTPException(status_code=404, detail="Target user not found")
        return target

    requester_org = requester.get("organization_id")
    if not requester_org:
        raise HTTPException(status_code=403, detail="You are not assigned to any organization")

    db = await get_database()
    target = await db["users"].find_one({"_id": ObjectId(target_user_id)})
    if not target:
        raise HTTPException(status_code=404, detail="Target user not found")

    if target.get("organization_id") != requester_org:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You cannot access users outside your organization",
        )
    return target