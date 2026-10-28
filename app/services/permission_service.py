"""
Centralized Permission and Subscription Service.

Checks:
1. Role-based permissions (stored in `role_permissions` MongoDB collection).
2. Active subscription plans (stored in `subscriptions` and `plans` collections).
   Supports individual subscriptions and organization-wide subscriptions.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from app.core.database import get_database
from app.core.constants import UserRole

logger = logging.getLogger(__name__)

# ── Collection Names ──────────────────────────────────────────────────────────
COLLECTION_ROLE_PERMISSIONS = "role_permissions"
COLLECTION_PLANS = "plans"
COLLECTION_SUBSCRIPTIONS = "subscriptions"

# ── Default Permission Matrix ────────────────────────────────────────────────
# This is used to seed the DB on first run. After that, the DB is the source
# of truth and this dict is only used as a fallback.

DEFAULT_PERMISSIONS: dict[str, dict[str, bool]] = {
    UserRole.SUPER_ADMIN: {
        "sessions:list":        True,
        "sessions:view":        True,
        "sessions:edit":        True,
        "sessions:delete":      True,
        "sessions:bulk_delete": True,
        "sessions:org_access":  True,
        "documents:upload":     True,
        "documents:list":       True,
        "documents:delete":     True,
        "chat:use":             True,
        "analytics:dashboard":  True,
        "org:manage_members":   True,
        "org:invite":           True,
        "org:settings":         True,
        "org:create":           True,
        "org:delete":           True,
        "cdt:manage":           True,
        "users:list":           True,
        "users:manage":         True,
        "users:delete":         True,
        "voice:stream":         False,
        "cdt:upload_pdf":       True,
        "cdt:documents:view":   True,
        "cdt:codes:view":       True,
        "templates:list":       True,
        "templates:manage":     True,
        "templates:variables":  True,
        "videos:list":          True,
        "videos:manage":        True,
        "case_study:manage":    True,
        "financial_bridge:access": True,
    },
    UserRole.ORG_ADMIN: {
        "sessions:list":        True,
        "sessions:view":        True,
        "sessions:edit":        True,
        "sessions:delete":      True,
        "sessions:bulk_delete": True,
        "sessions:org_access":  True,
        "documents:upload":     False,
        "documents:list":       False,
        "documents:delete":     False,
        "chat:use":             True,
        "analytics:dashboard":  True,
        "org:manage_members":   True,
        "org:invite":           True,
        "org:settings":         True,
        "org:create":           False,
        "org:delete":           False,
        "cdt:manage":           False,
        "users:list":           True,
        "users:manage":         True,
        "users:delete":         False,
        "voice:stream":         True,
        "cdt:upload_pdf":       False,
        "cdt:documents:view":   False,
        "cdt:codes:view":       True,
        "templates:list":       True,
        "templates:manage":     True,
        "templates:variables":  True,
        "videos:list":          False,
        "videos:manage":        False,
        "case_study:manage":    False,
        "financial_bridge:access": True,
    },
    UserRole.DENTIST: {
        "sessions:list":        True,
        "sessions:view":        True,
        "sessions:edit":        True,
        "sessions:delete":      True,
        "sessions:bulk_delete": False,
        "sessions:org_access":  False,
        "documents:upload":     False,
        "documents:list":       False,
        "documents:delete":     False,
        "chat:use":             True,
        "analytics:dashboard":  False,
        "org:manage_members":   False,
        "org:invite":           False,
        "org:settings":         False,
        "org:create":           False,
        "org:delete":           False,
        "cdt:manage":           False,
        "users:list":           False,
        "users:manage":         False,
        "users:delete":         False,
        "voice:stream":         True,
        "cdt:upload_pdf":       False,
        "cdt:documents:view":   False,
        "cdt:codes:view":       True,
        "templates:list":       True,
        "templates:manage":     False,
        "videos:list":          False,
        "videos:manage":        False,
        "case_study:manage":    False,
        "financial_bridge:access": True,
    },
    UserRole.STAFF: {
        "sessions:list":        True,
        "sessions:view":        True,
        "sessions:edit":        True,
        "sessions:delete":      False,
        "sessions:bulk_delete": False,
        "sessions:org_access":  True,
        "documents:upload":     False,
        "documents:list":       False,
        "documents:delete":     False,
        "chat:use":             True,
        "analytics:dashboard":  False,
        "org:manage_members":   False,
        "org:invite":           False,
        "org:settings":         False,
        "org:create":           False,
        "org:delete":           False,
        "cdt:manage":           False,
        "users:list":           False,
        "users:manage":         False,
        "users:delete":         False,
        "voice:stream":         True,
        "cdt:upload_pdf":       False,
        "cdt:documents:view":   False,
        "cdt:codes:view":       True,
        "templates:list":       True,
        "templates:manage":     False,
        "videos:list":          False,
        "videos:manage":        False,
        "case_study:manage":    False,
        "financial_bridge:access": True,
    },
    UserRole.FRONT_OFFICE: {
        "sessions:list":        True,
        "sessions:view":        True,
        "sessions:edit":        True,
        "sessions:delete":      False,
        "sessions:bulk_delete": False,
        "sessions:org_access":  True,
        "documents:upload":     False,
        "documents:list":       False,
        "documents:delete":     False,
        "chat:use":             False,
        "analytics:dashboard":  False,
        "org:manage_members":   False,
        "org:invite":           False,
        "org:settings":         False,
        "org:create":           False,
        "org:delete":           False,
        "cdt:manage":           False,
        "users:list":           False,
        "users:manage":         False,
        "users:delete":         False,
        "voice:stream":         True,
        "cdt:upload_pdf":       False,
        "cdt:documents:view":   False,
        "cdt:codes:view":       True,
        "templates:list":       True,
        "templates:manage":     False,
        "videos:list":          False,
        "videos:manage":        False,
        "case_study:manage":    False,
        "financial_bridge:access": True,
    },
    UserRole.STUDENT: {
        "sessions:list":        False,
        "sessions:view":        False,
        "sessions:edit":        False,
        "sessions:delete":      False,
        "sessions:bulk_delete": False,
        "sessions:org_access":  False,
        "documents:upload":     False,
        "documents:list":       False,
        "documents:delete":     False,
        "chat:use":             True,
        "analytics:dashboard":  False,
        "org:manage_members":   False,
        "org:invite":           False,
        "org:settings":         False,
        "org:create":           False,
        "org:delete":           False,
        "cdt:manage":           False,
        "users:list":           False,
        "users:manage":         False,
        "users:delete":         False,
        "voice:stream":         False,
        "cdt:upload_pdf":       False,
        "cdt:documents:view":   False,
        "cdt:codes:view":       False,
        "templates:list":       False,
        "templates:manage":     False,
        "videos:list":          True,
        "videos:manage":        False,
        "case_study:manage":    False,
        "financial_bridge:access": False,
    },
    UserRole.SOLO_DENTIST: {
        "sessions:list":        True,
        "sessions:view":        True,
        "sessions:edit":        True,
        "sessions:delete":      True,
        "sessions:bulk_delete": False,
        "documents:upload":     False,
        "documents:list":       False,
        "documents:delete":     False,
        "chat:use":             True,
        "analytics:dashboard":  False,
        "org:manage_members":   False,
        "org:invite":           False,
        "org:settings":         False,
        "org:create":           False,
        "org:delete":           False,
        "cdt:manage":           False,
        "users:list":           False,
        "users:manage":         False,
        "users:delete":         False,
        "voice:stream":         True,
        "cdt:upload_pdf":       False,
        "cdt:documents:view":   False,
        "cdt:codes:view":       True,
        "templates:list":       True,
        "templates:manage":     True,
        "templates:variables":  True,
        "videos:list":          False,
        "videos:manage":        False,
        "case_study:manage":    False,
        "financial_bridge:access": True,
    },
}


class PermissionService:
    """
    Rebuilt Permission & Subscription Service.
    Integrates role-based configuration checking and active plan validation.
    """

    # ── Public Permission APIs ───────────────────────────────────────────

    @staticmethod
    async def has_permission(user: dict | str, permission_key: str) -> bool:
        """
        Check if a user or role has a specific permission. Returns bool.
        If a user dictionary is passed, both role permissions and active subscription
        access are validated.
        """
        if isinstance(user, str):
            role = user
        else:
            role = user.get("role")

        if role == UserRole.SUPER_ADMIN:
            return True

        # 1. Check role-based permission
        perms = await PermissionService._get_role_permissions(role)
        if not perms.get(permission_key, False):
            return False

        # 2. Check subscription status (if full user dictionary is provided)
        if isinstance(user, dict):
            try:
                await PermissionService.check_user_subscription(user)
            except Exception:
                return False

        return True

    @staticmethod
    async def check_permission(user: dict | str, permission_key: str):
        """
        Enforce a permission check.
        Raises HTTP 403 if role-permission is denied.
        Raises HTTP 402 if subscription is expired/inactive.
        """
        if isinstance(user, str):
            role = user
            if not await PermissionService.has_permission(role, permission_key):
                raise HTTPException(
                    status_code=403,
                    detail=f"Permission denied: role '{role}' does not have '{permission_key}' access.",
                )
        else:
            role = user.get("role")
            if role == UserRole.SUPER_ADMIN:
                return

            # First enforce role permission (raises 403)
            perms = await PermissionService._get_role_permissions(role)
            if not perms.get(permission_key, False):
                raise HTTPException(
                    status_code=403,
                    detail=f"Permission denied: your role '{role}' does not have '{permission_key}' access.",
                )

            # Then enforce subscription check (raises 402)
            await PermissionService.check_user_subscription(user)

    @staticmethod
    async def check_user_subscription(user: dict):
        """
        Verify the user (or their organization) has active access.
        Raises HTTP 402 if subscription is expired/inactive.
        """
        user_id = str(user["_id"])
        role = user.get("role")

        # Super Admin bypass subscription checks
        if role == UserRole.SUPER_ADMIN:
            return

        from app.services.subscription_service import SubscriptionService

        # Check if user has an active subscription (resolves individual vs organization)
        active_sub = await SubscriptionService.get_active_subscription(user_id)
        if active_sub:
            return  # Has active subscription!

        # If no active subscription, verify if they have ever subscribed (meaning they are expired)
        db = await get_database()
        is_individual = (user.get("account_type") == "individual")

        has_history = None
        if is_individual:
            has_history = await db[COLLECTION_SUBSCRIPTIONS].find_one({"user_id": user_id})
        else:
            org_id = user.get("organization_id")
            if org_id:
                has_history = await db[COLLECTION_SUBSCRIPTIONS].find_one({"organization_id": org_id})

        if has_history:
            # Subscribed in the past, but no longer active (expired, cancelled, or unpaid)
            raise HTTPException(
                status_code=402,
                detail="Your subscription has expired or is inactive. Please update your billing details to continue.",
            )

        # If no subscription history exists, they default to "free_trial".
        # Access is allowed, but usage will be gated by message/query limits.
        return

    # ── Internal Seeding & DB Helpers ────────────────────────────────────

    @staticmethod
    async def _get_role_permissions(role: str) -> dict[str, bool]:
        """Load permissions directly from DB."""
        db = await get_database()
        doc = await db[COLLECTION_ROLE_PERMISSIONS].find_one({"role": role})

        if doc:
            return doc.get("permissions", {})

        # Fall back to defaults if not found in DB
        perms = DEFAULT_PERMISSIONS.get(role, {})
        if perms:
            logger.warning(f"[PERMS] Role '{role}' not in DB, seeding from defaults")
            await PermissionService._seed_single_role(role, perms)

        return perms

    @staticmethod
    async def _seed_single_role(role: str, perms: dict[str, bool]):
        """Insert a single role's default permissions into the DB."""
        db = await get_database()
        now = datetime.now(timezone.utc)
        await db[COLLECTION_ROLE_PERMISSIONS].update_one(
            {"role": role},
            {
                "$setOnInsert": {
                    "role": role,
                    "permissions": perms,
                    "created_at": now,
                    "updated_at": now,
                    "updated_by": "system_seed",
                }
            },
            upsert=True,
        )

    # ── CRUD Methods (for Super Admin API) ───────────────────────────────

    @staticmethod
    async def get_all_permissions() -> list[dict]:
        """Get the full permission matrix for all roles."""
        db = await get_database()
        cursor = db[COLLECTION_ROLE_PERMISSIONS].find(
            {}, {"_id": 0, "role": 1, "permissions": 1, "updated_at": 1, "updated_by": 1}
        )
        return await cursor.to_list(length=20)

    @staticmethod
    async def get_role_permissions(role: str) -> Optional[dict]:
        """Get permissions for a specific role."""
        db = await get_database()
        doc = await db[COLLECTION_ROLE_PERMISSIONS].find_one(
            {"role": role},
            {"_id": 0, "role": 1, "permissions": 1, "updated_at": 1, "updated_by": 1}
        )
        return doc

    @staticmethod
    async def update_role_permissions(
        role: str,
        permissions: dict[str, bool],
        updated_by: str,
    ) -> dict:
        """Update the permission matrix for a role."""
        db = await get_database()
        now = datetime.now(timezone.utc)

        set_fields = {f"permissions.{k}": v for k, v in permissions.items()}
        set_fields["updated_at"] = now
        set_fields["updated_by"] = updated_by

        result = await db[COLLECTION_ROLE_PERMISSIONS].update_one(
            {"role": role},
            {"$set": set_fields},
            upsert=True,
        )

        logger.info(f"[PERMS] Updated permissions for role '{role}' by {updated_by}: {list(permissions.keys())}")

        return {
            "role": role,
            "permissions_updated": list(permissions.keys()),
            "updated_at": now.isoformat(),
            "matched": result.matched_count,
            "modified": result.modified_count,
        }

    # ── Plan Seeding & Defaults Seeding ──────────────────────────────────

    @staticmethod
    async def seed_plans():
        """
        Seed default subscription plans in the plans collection if empty.
        Uses $setOnInsert so existing customizations/edited price IDs are preserved.
        """
        db = await get_database()
        now = datetime.now(timezone.utc)

        default_plans = [  
            {
                "plan_id": "student",
                "stripe_price_id": "price_1TtPAIQ6R156Y2ZYn7IL03q2",
                "name": "student",
                "display_name": "Student",
                "user_type": "student",
                "cycle": "monthly",
                "price": 20.00,
                "currency": "usd",
                "description": "Best plan for students to learn dentistry.",
                "features": ["AI Chat Assistant", "CDT Codes Search", "knowledge videos"],
                "allowed_roles": ["student"],
                "trial_period": 90,
                "is_active": True,
                "created_at": now,
                "updated_at": now
            },
            {
                "plan_id": "individual_monthly",
                "stripe_price_id": "price_1TtP4DQ6R156Y2ZYKGvYbLQz",
                "name": "individual_monthly",
                "display_name": "Individual Monthly",
                "user_type": "individual",
                "cycle": "monthly",
                "price": 99.00,
                "currency": "usd",
                "description": "Pro access for individual dentists.",
                "features": ["AI Chat Assistant", "Dental Voice Streaming", "CDT Codes Search", "Financial Bridge"],
                "allowed_roles": ["solo_dentist"],
                "trial_period": 14,
                "is_active": True,
                "created_at": now,
                "updated_at": now
            },
            {
                "plan_id": "organization_monthly",
                "stripe_price_id": "price_1TtP9MQ6R156Y2ZY6dqYpMJM",
                "name": "organization_monthly",
                "display_name": "Organization Monthly",
                "user_type": "organization",
                "cycle": "monthly",
                "price": 99.00,
                "currency": "usd",
                "description": "Enterprise access for entire dental clinic and staff.",
                "features": ["All Individual Features", "Multi-tenant Org Control", "Shared Dental Voice Sessions", "Unlimited Staff Members"],
                "allowed_roles": ["org_admin"],
                "trial_period": 14,
                "is_active": True,
                "created_at": now,
                "updated_at": now
            }
        ]

        for plan in default_plans:
            insert_data = plan.copy()
            insert_data.pop("allowed_roles", None)
            insert_data.pop("trial_period", None)
            await db[COLLECTION_PLANS].update_one(
                {"plan_id": plan["plan_id"]},
                {
                    "$setOnInsert": insert_data,
                    "$set": {
                        "allowed_roles": plan["allowed_roles"],
                        "trial_period": plan["trial_period"]
                    }
                },
                upsert=True
            )
        logger.info("[PERMS] Seeded default subscription plans in plans collection")

    @staticmethod
    async def seed_defaults():
        """
        Seed the DB with default permissions and default plans.
        Uses $setOnInsert so existing customizations are never overwritten.
        """
        db = await get_database()
        now = datetime.now(timezone.utc)
        seeded = 0

        # Seed role permissions
        for role, perms in DEFAULT_PERMISSIONS.items():
            existing = await db[COLLECTION_ROLE_PERMISSIONS].find_one({"role": role})
            if existing:
                await db[COLLECTION_ROLE_PERMISSIONS].update_one(
                    {"role": role},
                    {"$set": {"permissions": perms, "updated_at": now}}
                )
                logger.info(f"[PERMS] Force-updated permissions for role: {role}")
            else:
                result = await db[COLLECTION_ROLE_PERMISSIONS].update_one(
                    {"role": role},
                    {
                        "$setOnInsert": {
                            "role": role,
                            "permissions": perms,
                            "created_at": now,
                            "updated_at": now,
                            "updated_by": "system_seed",
                        }
                    },
                    upsert=True,
                )
                if result.upserted_id:
                    seeded += 1
                    logger.info(f"[PERMS] Seeded default permissions for role: {role}")

        # Seed default plans
        await PermissionService.seed_plans()

        return {
            "total_roles": len(DEFAULT_PERMISSIONS),
            "newly_seeded": seeded,
            "already_existed": len(DEFAULT_PERMISSIONS) - seeded,
        }
