"""
Organization service — CRUD operations for multi-tenant organizations.

Only Super Admin can create/update/deactivate organizations.
"""
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status

from app.core.database import get_database
from app.core.constants import (
    COLLECTION_ORGANIZATIONS,
    COLLECTION_USERS,
    COLLECTION_INVITATIONS,
    UserRole,
)


class OrganizationService:

    @staticmethod
    async def create_organization(name: str, email: str, created_by: str) -> dict:
        """Create a new organization. Does NOT send the invitation — that's a separate step."""
        db = await get_database()

        # Domain restriction
        allowed_domains = ["gmail.com", "petaldental.com"]
        email_domain = email.split("@")[-1].lower() if "@" in email else ""
        if email_domain not in allowed_domains:
            raise HTTPException(
                status_code=400, 
                detail=f"Invalid email domain. Allowed domains are: {', '.join(allowed_domains)}."
            )

        # Uniqueness checks
        if await db[COLLECTION_ORGANIZATIONS].find_one({"name": name}):
            raise HTTPException(status_code=409, detail=f"Organization '{name}' already exists.")
        if await db[COLLECTION_ORGANIZATIONS].find_one({"email": email}):
            raise HTTPException(status_code=409, detail=f"Email '{email}' is already linked to another organization.")

        existing_user = await db[COLLECTION_USERS].find_one({"email": email.lower()})
        if existing_user:
            org_name_err = "another organization"
            if existing_user.get("organization_id"):
                try:
                    existing_org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(existing_user["organization_id"])})
                    if existing_org:
                        org_name_err = existing_org.get("name", org_name_err)
                except Exception:
                    pass
            raise HTTPException(
                status_code=409, 
                detail=f"A user with email '{email}' already exists in '{org_name_err}'. Cannot create a new organization using this email."
            )

        now = datetime.now(timezone.utc)
        org_doc = {
            "name": name,
            "email": email,
            "logo_url": None,
            "is_active": True,
            "auto_delete_sessions": True,
            "xray_enabled": True,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        result = await db[COLLECTION_ORGANIZATIONS].insert_one(org_doc)
        org_doc["_id"] = result.inserted_id
        return _serialize_org(org_doc)

    @staticmethod
    async def list_organizations(skip: int = 0, limit: int = 50, search: Optional[str] = None) -> dict:
        db = await get_database()
        query = {"is_individual_organization": {"$ne": True}}
        if search:
            query["$or"] = [
                {"name": {"$regex": search, "$options": "i"}},
                {"email": {"$regex": search, "$options": "i"}},
            ]

        total = await db[COLLECTION_ORGANIZATIONS].count_documents(query)
        cursor = db[COLLECTION_ORGANIZATIONS].find(query).sort("created_at", -1).skip(skip).limit(limit)
        
        raw_orgs = await cursor.to_list(length=None)
        org_ids = [str(org["_id"]) for org in raw_orgs]
        
        # Bulk fetch member counts
        counts = await db[COLLECTION_USERS].aggregate([
            {"$match": {"organization_id": {"$in": org_ids}}},
            {"$group": {"_id": "$organization_id", "count": {"$sum": 1}}}
        ]).to_list(length=None)
        count_map = {item["_id"]: item["count"] for item in counts}
        
        # Bulk fetch active subscriptions
        now = datetime.now(timezone.utc)
        sub_query = {
            "organization_id": {"$in": org_ids},
            "is_active": True,
            "$or": [
                {"end_date": {"$exists": False}},
                {"end_date": None},
                {"end_date": {"$gt": now}}
            ]
        }
        subscriptions_cursor = db["subscriptions"].find(sub_query).sort("created_at", -1)
        org_subs = {}
        async for sub in subscriptions_cursor:
            org_id_key = sub.get("organization_id")
            if org_id_key not in org_subs:
                org_subs[org_id_key] = sub
        
        orgs = []
        for org in raw_orgs:
            serialized = _serialize_org(org)
            serialized["member_count"] = count_map.get(serialized["id"], 0)
            
            # Fetch active subscription details
            sub = org_subs.get(serialized["id"])
            if sub:
                end_date = sub.get("end_date")
                if isinstance(end_date, datetime) and end_date.tzinfo is None:
                    end_date = end_date.replace(tzinfo=timezone.utc)
                serialized["plan_detail"] = {
                    "plan": sub.get("plan"),
                    "expiry_date": end_date.isoformat() if isinstance(end_date, datetime) else None,
                    "is_trial": sub.get("is_trial", False),
                    "status": "ACTIVE"
                }
            else:
                serialized["plan_detail"] = None
                
            orgs.append(serialized)

        return {"total": total, "skip": skip, "limit": limit, "organizations": orgs}

    @staticmethod
    async def get_organization(org_id: str) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(org_id):
            raise HTTPException(status_code=400, detail="Invalid organization ID format")

        org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(org_id)})
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        serialized = _serialize_org(org)
        serialized["member_count"] = await db[COLLECTION_USERS].count_documents(
            {"organization_id": org_id}
        )
        
        # Fetch members list
        members_cursor = db[COLLECTION_USERS].find({"organization_id": org_id})
        members_list = []
        async for member in members_cursor:
            members_list.append({
                "id": str(member["_id"]),
                "email": member.get("email"),
                "role": member.get("role"),
                "full_name": member.get("full_name"),
                "is_active": member.get("is_active", True),
                "joined_date": member.get("created_at").isoformat() if isinstance(member.get("created_at"), datetime) else member.get("created_at"),
                "last_login_at": member.get("last_login_at").isoformat() if isinstance(member.get("last_login_at"), datetime) else member.get("last_login_at")
            })
            
        serialized["members"] = members_list
        
        # Fetch single organization subscription details
        now = datetime.now(timezone.utc)
        sub = await db["subscriptions"].find_one({
            "organization_id": org_id,
            "is_active": True,
            "$or": [
                {"end_date": {"$exists": False}},
                {"end_date": None},
                {"end_date": {"$gt": now}}
            ]
        }, sort=[("created_at", -1)])
        
        if sub:
            end_date = sub.get("end_date")
            if isinstance(end_date, datetime) and end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            serialized["plan_detail"] = {
                "plan": sub.get("plan"),
                "expiry_date": end_date.isoformat() if isinstance(end_date, datetime) else None,
                "is_trial": sub.get("is_trial", False),
                "status": "ACTIVE"
            }
        else:
            serialized["plan_detail"] = None
            
        return serialized

    @staticmethod
    async def update_organization(org_id: str, update_data: dict) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(org_id):
            raise HTTPException(status_code=400, detail="Invalid organization ID format")

        # Filter out None values
        update_fields = {k: v for k, v in update_data.items() if v is not None}
        if not update_fields:
            raise HTTPException(status_code=400, detail="No valid fields to update")

        update_fields["updated_at"] = datetime.now(timezone.utc)

        result = await db[COLLECTION_ORGANIZATIONS].find_one_and_update(
            {"_id": ObjectId(org_id)},
            {"$set": update_fields},
            return_document=True,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Organization not found")
            
        # If organization's active status changed, cascade to all its users
        if "is_active" in update_fields:
            update_stmt = {"$set": {"is_active": update_fields["is_active"]}}
            if update_fields["is_active"] is False:
                update_stmt["$set"]["refresh_tokens"] = []
                
            await db[COLLECTION_USERS].update_many(
                {"organization_id": org_id},
                update_stmt
            )
            
        return _serialize_org(result)

    @staticmethod
    async def delete_organization(org_id: str) -> dict:
        """
        Hard-delete an organization and ALL associated data:
        users, chats, messages, voice sessions, subscriptions, invitations, logo blob.
        Audit logs are retained for HIPAA compliance.
        Returns the org data (snapshot) before deletion for the audit record.
        """
        db = await get_database()
        if not ObjectId.is_valid(org_id):
            raise HTTPException(status_code=400, detail="Invalid organization ID format")

        org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(org_id)})
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Snapshot for audit record & response before we destroy everything
        org_snapshot = _serialize_org({**org})

        # ── 1. Collect all user IDs belonging to this organization ──────────
        user_cursor = db[COLLECTION_USERS].find(
            {"organization_id": org_id}, {"_id": 1}
        )
        user_ids = []
        user_id_strings = []
        async for u in user_cursor:
            user_ids.append(u["_id"])
            user_id_strings.append(str(u["_id"]))

        if user_id_strings:
            # ── 2. Delete all chats & messages owned by these users ──────────
            chat_cursor = db["chats"].find(
                {"user_id": {"$in": user_id_strings}}, {"_id": 1}
            )
            chat_ids = []
            async for c in chat_cursor:
                chat_ids.append(str(c["_id"]))

            if chat_ids:
                await db["messages"].delete_many({"chat_id": {"$in": chat_ids}})
                await db["chats"].delete_many({"user_id": {"$in": user_id_strings}})

            # ── 3. Delete subscriptions & stripe records ────────────────────
            await db["subscriptions"].delete_many({"user_id": {"$in": user_id_strings}})
            await db["stripe_subscriptions"].delete_many({"user_id": {"$in": user_id_strings}})

            # ── 4. Delete all users ─────────────────────────────────────────
            await db[COLLECTION_USERS].delete_many({"organization_id": org_id})

        # ── 5. Delete dental voice sessions for this organization ───────────
        await db["dental_voice_sessions"].delete_many({"organization_id": org_id})

        # ── 6. Delete pending invitations ───────────────────────────────────
        await db[COLLECTION_INVITATIONS].delete_many({"organization_id": org_id})

        # ── 7. Delete logo blob from Azure Storage ──────────────────────────
        if org.get("logo_url"):
            try:
                from app.utils.file_utils import delete_blob
                await delete_blob(org["logo_url"])
            except Exception:
                pass  # Best-effort; don't block deletion if blob is already gone

        # ── 8. Delete the organization document itself ──────────────────────
        await db[COLLECTION_ORGANIZATIONS].delete_one({"_id": ObjectId(org_id)})

        return org_snapshot

    @staticmethod
    async def update_logo(org_id: str, logo_url: str) -> dict:
        db = await get_database()
        result = await db[COLLECTION_ORGANIZATIONS].find_one_and_update(
            {"_id": ObjectId(org_id)},
            {"$set": {"logo_url": logo_url, "updated_at": datetime.now(timezone.utc)}},
            return_document=True,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Organization not found")
        return _serialize_org(result)

    @staticmethod
    async def get_org_members(org_id: str, skip: int = 0, limit: int = 50, is_active: Optional[bool] = None) -> dict:
        """List members of an organization. Optionally filter by active status."""
        db = await get_database()
        query = {"organization_id": org_id}
        if is_active is not None:
            query["is_active"] = is_active
        total = await db[COLLECTION_USERS].count_documents(query)
        cursor = db[COLLECTION_USERS].find(
            query,
            {"hashed_password": 0}  # exclude sensitive fields
        ).sort("created_at", -1).skip(skip).limit(limit)

        members = []
        async for user in cursor:
            user["id"] = str(user.pop("_id"))
            for key, value in user.items():
                if isinstance(value, datetime):
                    user[key] = value.isoformat()
            
            # # Explicitly ensure last_login_at is present for frontend consistency
            # if "last_login_at" not in user:
            #     user["last_login_at"] = None
                
            members.append(user)

        return {"total": total, "skip": skip, "limit": limit, "members": members}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _serialize_org(org: dict) -> dict:
    """Convert MongoDB org document to JSON-safe dict."""
    if "_id" in org:
        org["id"] = str(org.pop("_id"))
    for key, value in org.items():
        if isinstance(value, datetime):
            org[key] = value.isoformat()

    # Convert blob path to a permanent public URL for frontend
    if org.get("logo_url") and not org["logo_url"].startswith("http"):
        from app.utils.file_utils import generate_permanent_sas_url
        org["logo_url"] = generate_permanent_sas_url(org["logo_url"])

    return org

