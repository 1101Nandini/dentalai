from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

from app.core.database import get_database
from app.core.constants import COLLECTION_USERS
from app.schemas.user_schema import UserUpdateRequest
from app.services.subscription_service import SubscriptionService

_SENSITIVE_FIELDS = {"hashed_password", "password", "refresh_tokens"}

def _sanitize_user(user: dict) -> dict:
    """Normalize a MongoDB user document for JSON responses."""
    if not user:
        return {}
    
    # 1. Map _id to id (string)
    if "_id" in user:
        user["id"] = str(user.pop("_id"))
        
    # 2. Strip sensitive fields
    for field in _SENSITIVE_FIELDS:
        user.pop(field, None)
    
    # 3. CONVERT DATETIMES TO STRINGS
    # This prevents the "not JSON serializable" error
    for key, value in user.items():
        if isinstance(value, datetime):
            user[key] = value.isoformat()

    # 4. Set safe defaults (both are plain booleans)
    user.setdefault("is_active", True)
    user.setdefault("is_verified", False)
    user.setdefault("auth_provider", "email")
    user.setdefault("updated_at", None)
    user.setdefault("has_agreed", False)
    
    return user

class UserService:

    @staticmethod
    def _validate_id(user_id: str) -> ObjectId:
        """Helper to validate MongoDB ObjectId format."""
        try:
            return ObjectId(user_id)
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, 
                detail="Invalid user ID format"
            )

    @staticmethod
    async def get_user_by_id(user_id: str, admin_mode: bool = False) -> dict:
        db = await get_database()
        user_oid = UserService._validate_id(user_id)
        
        user = await db[COLLECTION_USERS].find_one({"_id": user_oid})
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        
        sanitized = _sanitize_user(user)
        # Pull plan from dynamic subscription table
        active_sub = await SubscriptionService.get_active_subscription(user_id)
        sanitized["subscription_plan"] = active_sub.get("plan", "free_trial") if active_sub else "free_trial"
        # Also include dates if available
        if active_sub:
            sanitized["plan_start_date"] = active_sub.get("start_date").isoformat() if isinstance(active_sub.get("start_date"), datetime) else active_sub.get("start_date")
            sanitized["plan_end_date"] = active_sub.get("end_date").isoformat() if isinstance(active_sub.get("end_date"), datetime) else active_sub.get("end_date")
        else:
            sanitized["plan_start_date"] = None
            sanitized["plan_end_date"] = None

        stripe_sub = await db["stripe_subscriptions"].find_one({"user_id": user_id}, sort=[("updated_at", -1)])
        if stripe_sub:
            sanitized["billing_status"] = stripe_sub.get("status")
        else:
            sanitized["billing_status"] = None
            
        # Attach organization info if user belongs to one
        org_id = sanitized.get("organization_id")
        has_had_trial = user.get("has_had_trial", False)
        if org_id:
            try:
                org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
                if org:
                    sanitized["organization_name"] = org.get("name")
                    logo_url = org.get("logo_url")
                    if logo_url and not logo_url.startswith("http"):
                        from app.utils.file_utils import generate_permanent_sas_url
                        logo_url = generate_permanent_sas_url(logo_url)
                    sanitized["organization_logo_url"] = logo_url
                    has_had_trial = org.get("has_had_trial", False)
            except Exception:
                pass
        sanitized["has_had_trial"] = has_had_trial

        # Always surface the disclaimer agreement flag from the raw user document
        sanitized["has_agreed"] = user.get("has_agreed", False)

        if admin_mode:
            # 1. Fetch raw stripe subscription status overrides
            if stripe_sub:
                sanitized["stripe_details"] = {
                    "stripe_subscription_id": stripe_sub.get("stripe_subscription_id"),
                    "stripe_customer_id": stripe_sub.get("stripe_customer_id"),
                    "status": stripe_sub.get("status"),
                    "plan": stripe_sub.get("plan"),
                    "updated_at": stripe_sub.get("updated_at").isoformat() if isinstance(stripe_sub.get("updated_at"), datetime) else stripe_sub.get("updated_at")
                }
            
            # 2. Fetch resource insights using $lookup (since messages don't have user_id)
            from app.core.constants import COLLECTION_CHATS, COLLECTION_MESSAGES
            
            sanitized["total_chats"] = await db[COLLECTION_CHATS].count_documents({"user_id": user_id})
            
            # Aggregation to join chats -> messages and calculate stats
            pipeline = [
                {"$match": {"user_id": user_id}},
                {"$project": {"chat_id_str": {"$toString": "$_id"}}},
                {"$lookup": {
                    "from": COLLECTION_MESSAGES,
                    "localField": "chat_id_str",
                    "foreignField": "chat_id",
                    "as": "msgs"
                }},
                {"$unwind": "$msgs"},
                {"$group": {
                    "_id": None,
                    "total_messages": {"$sum": 1}
                }}
            ]
            
            insights = await db[COLLECTION_CHATS].aggregate(pipeline).to_list(1)
            sanitized["total_messages"] = insights[0].get("total_messages", 0) // 2 if insights else 0

            # Get tokens from usage_logs
            usage = await db["usage_logs"].aggregate([
                {"$match": {"user_id": user_id, "type": "message"}},
                {"$group": {"_id": None, "total_tokens": {"$sum": "$token_usage.total_tokens"}}}
            ]).to_list(1)
            
            sanitized["total_tokens_used"] = usage[0].get("total_tokens", 0) if usage else 0

            # 3. Fetch session and report stats
            sanitized["total_sessions"] = await db["dental_voice_sessions"].count_documents({"user_id": user_id})
            
            reports_pipeline = [
                {"$match": {"user_id": user_id}},
                {"$project": {"reports_count": {"$size": {"$ifNull": ["$reports", []]}}}},
                {"$group": {"_id": None, "total_reports": {"$sum": "$reports_count"}}}
            ]
            reports_res = await db["dental_voice_sessions"].aggregate(reports_pipeline).to_list(1)
            sanitized["total_reports_generated"] = reports_res[0].get("total_reports", 0) if reports_res else 0

            cdt_pipeline = [
                {"$match": {"user_id": user_id}},
                {"$project": {"cdt_count": {"$size": {"$ifNull": ["$completed_today", []]}}}},
                {"$group": {"_id": None, "total_cdt": {"$sum": "$cdt_count"}}}
            ]
            cdt_res = await db["dental_voice_sessions"].aggregate(cdt_pipeline).to_list(1)
            sanitized["total_cdt_codes_detected"] = cdt_res[0].get("total_cdt", 0) if cdt_res else 0

            # 4. Fetch recent reports details
            recent_sessions = await db["dental_voice_sessions"].find(
                {"user_id": user_id, "reports": {"$exists": True, "$not": {"$size": 0}}},
                {"session_name": 1, "created_at": 1, "reports": 1}
            ).sort("created_at", -1).limit(5).to_list(length=None)
            
            recent_reports = []
            for s in recent_sessions:
                s_id = str(s["_id"])
                s_name = s.get("session_name", "Unnamed Session")
                s_created = s.get("created_at").isoformat() if isinstance(s.get("created_at"), datetime) else s.get("created_at")
                for r in s.get("reports", []):
                    recent_reports.append({
                        "session_id": s_id,
                        "session_name": s_name,
                        "created_at": s_created,
                        "template_name": r.get("template_name"),
                        "template_id": str(r.get("template_id")) if r.get("template_id") else None,
                    })
            sanitized["recent_reports"] = recent_reports[:5]

            # 5. Fetch document count
            from app.core.constants import COLLECTION_DOCUMENTS
            sanitized["total_documents"] = await db[COLLECTION_DOCUMENTS].count_documents({"user_id": user_id})

        return sanitized

    @staticmethod
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
        org_id: str | None = None
    ) -> dict:
        db = await get_database()
        query = {}
        if org_id:
            query["organization_id"] = org_id
        if is_active is not None:
            query["is_active"] = is_active
        if account_type is not None:
            query["account_type"] = account_type
        if role is not None:
            query["role"] = role
        if search:
            query["$or"] = [
                {"full_name": {"$regex": search, "$options": "i"}},
                {"email": {"$regex": search, "$options": "i"}},
            ]
        if plan is not None:
            active_subs = await db["subscriptions"].find({"is_active": True, "plan": plan}).to_list(length=None)
            user_ids = []
            for s in active_subs:
                try:
                    user_ids.append(ObjectId(s["user_id"]))
                except:
                    pass
            query["_id"] = {"$in": user_ids}
        
        total = await db[COLLECTION_USERS].count_documents(query)
        cursor = db[COLLECTION_USERS].find(query).sort(sort_by, sort_order).skip(skip).limit(limit)
        
        users_raw = await cursor.to_list(length=None)
        
        user_ids = [str(u["_id"]) for u in users_raw if u.get("account_type") == "individual"]
        org_ids = list(set([u.get("organization_id") for u in users_raw if u.get("account_type") == "organization" and u.get("organization_id")]))
        
        sub_query_or = []
        if user_ids:
            sub_query_or.append({"user_id": {"$in": user_ids}, "is_active": True})
        if org_ids:
            sub_query_or.append({"organization_id": {"$in": org_ids}, "is_active": True})
            
        subs_map = {}
        if sub_query_or:
            active_subs = await db["subscriptions"].find({"$or": sub_query_or}).to_list(length=None)
            for sub in active_subs:
                if sub.get("user_id"):
                    subs_map[sub["user_id"]] = sub
                elif sub.get("organization_id"):
                    subs_map[sub["organization_id"]] = sub

        stripe_subs_map = {}
        stripe_query_or = []
        if user_ids:
            stripe_query_or.append({"user_id": {"$in": user_ids}})
        if org_ids:
            stripe_query_or.append({"organization_id": {"$in": org_ids}})
        if stripe_query_or:
            # Sort by updated_at ascending so latest overrides earlier
            stripe_subs = await db["stripe_subscriptions"].find({"$or": stripe_query_or}).sort("updated_at", 1).to_list(length=None)
            for ssub in stripe_subs:
                if ssub.get("user_id"):
                    stripe_subs_map[ssub["user_id"]] = ssub
                elif ssub.get("organization_id"):
                    stripe_subs_map[ssub["organization_id"]] = ssub

        users = []
        for u in users_raw:
            user_id = str(u["_id"])
            sanitized = _sanitize_user(u)
            
            if u.get("account_type") == "individual":
                active_sub = subs_map.get(user_id)
                stripe_sub = stripe_subs_map.get(user_id)
            else:
                active_sub = subs_map.get(u.get("organization_id"))
                stripe_sub = stripe_subs_map.get(u.get("organization_id"))
                
            sanitized["subscription_plan"] = active_sub.get("plan", "free_trial") if active_sub else "free_trial"
            
            # Additional subscription details for get user list
            if active_sub:
                sanitized["plan_start_date"] = active_sub.get("start_date").isoformat() if isinstance(active_sub.get("start_date"), datetime) else active_sub.get("start_date")
                sanitized["plan_end_date"] = active_sub.get("end_date").isoformat() if isinstance(active_sub.get("end_date"), datetime) else active_sub.get("end_date")
            else:
                sanitized["plan_start_date"] = None
                sanitized["plan_end_date"] = None
                
            sanitized["billing_status"] = stripe_sub.get("status") if stripe_sub else None
            
            users.append(sanitized)
            
        return {
            "total": total, 
            "skip": skip, 
            "limit": limit, 
            "users": users
        }

    @staticmethod
    async def update_user(user_id: str, payload: UserUpdateRequest) -> dict:
        db = await get_database()
        user_oid = UserService._validate_id(user_id)
        
        update_data = payload.model_dump(exclude_none=True)
        if not update_data:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid fields to update")
        
        update_data["updated_at"] = datetime.now(timezone.utc)
        
        # If user is being deactivated, clear their refresh tokens to log them out
        if update_data.get("is_active") is False:
            update_data["refresh_tokens"] = []
        
        result = await db[COLLECTION_USERS].find_one_and_update(
            {"_id": user_oid},
            {"$set": update_data},
            return_document=True,
        )
        
        if not result:
            raise HTTPException(status_code=404, detail="User not found")

        return _sanitize_user(result)

    @staticmethod
    async def update_role(user_id: str, new_role: str) -> dict:
        db = await get_database()
        user_oid = UserService._validate_id(user_id)
        
        result = await db[COLLECTION_USERS].find_one_and_update(
            {"_id": user_oid},
            {"$set": {
                "role": new_role, 
                "updated_at": datetime.now(timezone.utc)
            }},
            return_document=True,
        )
        
        if not result:
            raise HTTPException(status_code=404, detail="User not found")
            
        return {
            "message": f"Role updated to '{new_role}'", 
            "user_id": user_id,
            "role": new_role
        }

    @staticmethod
    async def delete_user(user_id: str) -> None:
        db = await get_database()
        user_oid = UserService._validate_id(user_id)
        
        # Soft-delete: setting is_active to False
        result = await db[COLLECTION_USERS].update_one(
            {"_id": user_oid},
            {"$set": {
                "is_active": False, 
                "updated_at": datetime.now(timezone.utc)
            }},
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    @staticmethod
    async def approve_user(user_id: str) -> dict:
        """Approve a pending registration request by activating the user (and org)."""
        db = await get_database()
        user_oid = UserService._validate_id(user_id)

        user = await db[COLLECTION_USERS].find_one({"_id": user_oid})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.get("is_active"):
            raise HTTPException(status_code=400, detail="User is already active.")

        now = datetime.now(timezone.utc)
        
        # 1. Activate User
        await db[COLLECTION_USERS].update_one(
            {"_id": user_oid},
            {"$set": {
                "is_active": True, 
                "requires_super_admin_approval": False,
                "updated_at": now
            }}
        )

        # 2. Activate Organization if user is org_admin
        from app.core.constants import UserRole
        if user.get("role") == UserRole.ORG_ADMIN and user.get("organization_id"):
            try:
                org_oid = ObjectId(user["organization_id"])
                await db["organizations"].update_one(
                    {"_id": org_oid},
                    {"$set": {"is_active": True, "updated_at": now}}
                )
            except Exception:
                pass

        # 3. Send Approval Email
        from app.services.invitation_service import send_activation_email
        org_name = "Smile Guide AI"
        if user.get("organization_id"):
            try:
                org = await db["organizations"].find_one({"_id": ObjectId(user["organization_id"])})
                if org:
                    org_name = org.get("name", "Smile Guide AI")
            except Exception:
                pass
        
        try:
            await send_activation_email(user["email"], user.get("full_name", ""), org_name)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Could not send approval email to {user['email']}: {e}")

        user["is_active"] = True
        user["requires_super_admin_approval"] = False
        user["updated_at"] = now
        return _sanitize_user(user)

    @staticmethod
    async def list_pending_approvals(skip: int = 0, limit: int = 50) -> dict:
        """List all users pending Super Admin approval."""
        db = await get_database()
        
        query = {
            "is_active": False,
            "is_verified": True,
            "requires_super_admin_approval": True
        }
        
        total = await db[COLLECTION_USERS].count_documents(query)
        cursor = db[COLLECTION_USERS].find(query).sort("created_at", -1).skip(skip).limit(limit)
        
        users = []
        async for u in cursor:
            sanitized = _sanitize_user(u)
            org_id = sanitized.get("organization_id")
            if org_id:
                try:
                    org = await db["organizations"].find_one({"_id": ObjectId(org_id)})
                    if org:
                        sanitized["organization_name"] = org.get("name")
                except:
                    pass
            users.append(sanitized)
            
        return {
            "total": total, 
            "skip": skip, 
            "limit": limit, 
            "users": users
        }

    @staticmethod
    async def reject_user(user_id: str) -> dict:
        """Reject a pending registration request by hard-deleting the user and their pending org."""
        db = await get_database()
        user_oid = UserService._validate_id(user_id)

        user = await db[COLLECTION_USERS].find_one({"_id": user_oid})
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        if user.get("is_active"):
            raise HTTPException(status_code=400, detail="Cannot reject an active user.")

        # Send Rejection Email
        from app.background.email_tasks import send_rejection_email
        import asyncio
        asyncio.create_task(send_rejection_email(user["email"], user.get("full_name", "")))

        # Soft-reject: leave is_active as False, but remove from pending queue
        from datetime import datetime, timezone
        await db[COLLECTION_USERS].update_one(
            {"_id": user_oid},
            {"$set": {
                "requires_super_admin_approval": False,
                "is_active": False,
                "updated_at": datetime.now(timezone.utc)
            }}
        )

        return {
            "user_id": user_id, 
            "email": user.get("email"), 
            "full_name": user.get("full_name"),
            "account_type": user.get("account_type"),
            "role": user.get("role"),
            "message": "User rejected and marked inactive."
        }

    @staticmethod
    async def bulk_approve_users(user_ids: list[str]) -> dict:
        """Approve multiple users at once."""
        results = {"successful": [], "failed": []}
        for uid in user_ids:
            try:
                res = await UserService.approve_user(uid)
                results["successful"].append({
                    "id": uid, 
                    "email": res.get("email"),
                    "name": res.get("full_name"),
                    "account_type": res.get("account_type"),
                    "role": res.get("role")
                })
            except Exception as e:
                results["failed"].append({"id": uid, "error": str(e)})
        return results

    @staticmethod
    async def bulk_reject_users(user_ids: list[str]) -> dict:
        """Reject multiple users at once."""
        results = {"successful": [], "failed": []}
        for uid in user_ids:
            try:
                res = await UserService.reject_user(uid)
                results["successful"].append({
                    "id": uid, 
                    "email": res.get("email"),
                    "name": res.get("full_name", res.get("name")),
                    "account_type": res.get("account_type"),
                    "role": res.get("role")
                })
            except Exception as e:
                results["failed"].append({"id": uid, "error": str(e)})
        return results