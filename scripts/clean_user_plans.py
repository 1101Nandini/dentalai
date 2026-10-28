import asyncio
import os
import sys

# Add the project root to sys.path so we can import app modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.core.constants import COLLECTION_USERS

async def clean_user_plans():
    print("Connecting to MongoDB...")
    await connect_to_mongo()
    db = await get_database()
    
    print("Unsetting plan and subscription_plan fields from all user documents...")
    result = await db[COLLECTION_USERS].update_many(
        {},
        {"$unset": {
            "plan": "",
            "subscription_plan": "",
            "plan_start_date": "",
            "plan_end_date": ""
        }}
    )
    print(f"Update completed. Matched {result.matched_count} documents, modified {result.modified_count} documents.")
    
    await close_mongo_connection()

if __name__ == "__main__":
    asyncio.run(clean_user_plans())
