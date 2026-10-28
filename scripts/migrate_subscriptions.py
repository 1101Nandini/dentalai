import asyncio
import os
import sys
from datetime import datetime, timezone

# Add the project root to sys.path so we can import app modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.core.constants import COLLECTION_USERS

async def migrate_subscriptions():
    print("Starting subscription migration...")
    await connect_to_mongo()
    db = await get_database()
    
    users_cursor = db[COLLECTION_USERS].find({})
    count = 0
    updated = 0
    
    async for user in users_cursor:
        count += 1
        user_id = str(user["_id"])
        plan = user.get("subscription_plan")
        
        # 1. Check if they already have a subscription record
        existing_sub = await db["subscriptions"].find_one({"user_id": user_id})
        
        if not existing_sub:
            # Create a subscription record based on their current user data
            # If plan was 'pro', we might want to set a future end date or keep it perpetual
            new_sub = {
                "user_id": user_id,
                "plan": plan or "free_trial",
                "is_active": True,
                "start_date": user.get("created_at") or datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc)
            }
            await db["subscriptions"].insert_one(new_sub)
            print(f"Created subscription record for user {user_id} (plan: {plan})")
            updated += 1
        
        # 2. Remove subscription_plan from user document
        await db[COLLECTION_USERS].update_one(
            {"_id": user["_id"]},
            {"$unset": {"subscription_plan": ""}}
        )
        # Also remove other legacy plan fields if they exist
        await db[COLLECTION_USERS].update_one(
            {"_id": user["_id"]},
            {"$unset": {"plan_start_date": "", "plan_end_date": ""}}
        )

    print(f"Migration complete. Processed {count} users, created {updated} new subscription records.")
    await close_mongo_connection()

if __name__ == "__main__":
    asyncio.run(migrate_subscriptions())
