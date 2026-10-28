"""
Migration script: Create "SmileGuide" org and move specific users.

What this script does:
  1. Creates the "SmileGuide" organization
  2. Moves ONLY users whose name contains "nandini", "ai", or "yogesh"
  3. Promotes nandini.synsoft to super_admin (platform-level, no org)
  4. Tags existing sessions + audit logs for moved users

Run:
    python -m scripts.migrate_to_orgs
"""
import asyncio
import sys
import os
import re

# Allow imports from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorClient
from bson import ObjectId

from app.config import settings
from app.core.constants import UserRole, UserStatus


SUPER_ADMIN_EMAIL_PATTERN = "nandini.synsoft"   # partial match
ORG_NAME = "SmileGuide"
ORG_EMAIL = "admin@smileguide.com"

# Users whose full_name matches any of these (case-insensitive) get moved to org
NAME_PATTERNS = ["nandini", "ai", "yogesh"]


async def run_migration():
    print("=" * 60)
    print("  PetalAI Multi-Tenant Migration")
    print("=" * 60)

    client = AsyncIOMotorClient(settings.MONGODB_URI, serverSelectionTimeoutMS=20000)
    db = client[settings.MONGODB_DB_NAME]

    # Verify connectivity
    await db.command("ping")
    print("[OK] Connected to MongoDB Atlas\n")

    now = datetime.now(timezone.utc)

    # ── Step 1: Create "SmileGuide" organization ────────────────────────────
    existing_org = await db["organizations"].find_one({"name": ORG_NAME})
    if existing_org:
        org_id = existing_org["_id"]
        print(f"[INFO] Organization '{ORG_NAME}' already exists (ID: {org_id})")
    else:
        org_doc = {
            "name": ORG_NAME,
            "email": ORG_EMAIL,
            "logo_url": None,
            "is_active": True,
            "created_by": "migration_script",
            "created_at": now,
            "updated_at": now,
        }
        result = await db["organizations"].insert_one(org_doc)
        org_id = result.inserted_id
        print(f"[OK] Created organization '{ORG_NAME}' (ID: {org_id})")

    org_id_str = str(org_id)

    # ── Step 2: Find and promote Super Admin ────────────────────────────────
    # Find user whose email contains "nandini.synsoft"
    super_admin = await db["users"].find_one({
        "email": {"$regex": SUPER_ADMIN_EMAIL_PATTERN, "$options": "i"}
    })
    if not super_admin:
        print(f"[WARN] No user found with email matching '{SUPER_ADMIN_EMAIL_PATTERN}'")
        print("       Listing all users so you can identify the correct one:")
        all_users = db["users"].find({}, {"email": 1, "full_name": 1})
        async for u in all_users:
            print(f"         - {u.get('email')} ({u.get('full_name')})")
    else:
        await db["users"].update_one(
            {"_id": super_admin["_id"]},
            {"$set": {
                "role": UserRole.SUPER_ADMIN,
                "organization_id": None,      # Super Admin is platform-level
                "status": UserStatus.ACTIVE,
                "is_active": True,
                "is_verified": True,
                "updated_at": now,
            }}
        )
        print(f"[OK] Promoted '{super_admin['email']}' ({super_admin.get('full_name')}) to super_admin")

    # ── Step 3: Move matching users to SmileGuide ───────────────────────────
    # Build regex: matches if full_name contains "nandini" OR "ai" OR "yogesh"
    name_regex = "|".join(NAME_PATTERNS)
    name_filter = {
        "full_name": {"$regex": name_regex, "$options": "i"},
    }

    # Exclude the super admin — they don't belong to any org
    if super_admin:
        name_filter["_id"] = {"$ne": super_admin["_id"]}

    matching_users = []
    cursor = db["users"].find(name_filter)
    async for user in cursor:
        is_active = user.get("is_active", True)
        is_verified = user.get("is_verified", False)

        if not is_active:
            new_status = UserStatus.INACTIVE
        elif not is_verified:
            new_status = UserStatus.PENDING
        else:
            new_status = UserStatus.ACTIVE

        # Convert legacy "admin" role to "org_admin"
        current_role = user.get("role", "intern")
        if current_role == "admin":
            current_role = UserRole.ORG_ADMIN

        await db["users"].update_one(
            {"_id": user["_id"]},
            {"$set": {
                "organization_id": org_id_str,
                "status": new_status,
                "role": current_role,
                "updated_at": now,
            }}
        )
        matching_users.append(user)
        print(f"  -> Moved: {user.get('email')} ({user.get('full_name')}) | role={current_role} | status={new_status}")

    print(f"\n[OK] Moved {len(matching_users)} users to org '{ORG_NAME}'")

    # ── Step 4: List users NOT moved (for your reference) ───────────────────
    print("\n--- Users NOT moved to any org ---")
    not_moved = db["users"].find({
        "organization_id": {"$exists": False},
        "_id": {"$ne": super_admin["_id"]} if super_admin else {"$exists": True},
    }, {"email": 1, "full_name": 1, "role": 1})
    not_moved_count = 0
    async for u in not_moved:
        print(f"  - {u.get('email')} ({u.get('full_name')}) [role={u.get('role')}]")
        not_moved_count += 1

    # Also check for users with organization_id = None (explicitly set, like super admin)
    none_org_users = db["users"].find({
        "organization_id": None,
    }, {"email": 1, "full_name": 1, "role": 1})
    async for u in none_org_users:
        if super_admin and u["_id"] == super_admin["_id"]:
            continue  # Already reported as super admin
        print(f"  - {u.get('email')} ({u.get('full_name')}) [role={u.get('role')}]")
        not_moved_count += 1

    if not_moved_count == 0:
        print("  (none — all users were either moved or are super_admin)")

    # ── Step 5: Tag sessions for moved users ────────────────────────────────
    moved_user_ids = [str(u["_id"]) for u in matching_users]
    if moved_user_ids:
        session_result = await db["dental_voice_sessions"].update_many(
            {"user_id": {"$in": moved_user_ids}, "organization_id": {"$exists": False}},
            {"$set": {"organization_id": org_id_str}}
        )
        print(f"\n[OK] Tagged {session_result.modified_count} dental_voice_sessions with org_id")

    # ── Step 6: Tag existing audit_logs ─────────────────────────────────────
    audit_result = await db["audit_logs"].update_many(
        {"organization_id": {"$exists": False}},
        {"$set": {"organization_id": org_id_str}}
    )
    print(f"[OK] Tagged {audit_result.modified_count} audit_logs with org_id")

    # ── Summary ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  Migration complete!")
    print(f"  Organization: {ORG_NAME} ({org_id})")
    if super_admin:
        print(f"  Super Admin:  {super_admin['email']}")
    print(f"  Users moved:  {len(matching_users)}")
    print(f"  Users left:   {not_moved_count}")
    print("=" * 60)

    client.close()


if __name__ == "__main__":
    asyncio.run(run_migration())
