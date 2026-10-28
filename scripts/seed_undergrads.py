import asyncio
from datetime import datetime, timezone
import os
import sys

# Ensure app can be imported
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.core.constants import COLLECTION_ORGANIZATIONS
from app.config import settings

async def seed_undergrads():
    print("Connecting to MongoDB...")
    await connect_to_mongo()
    
    try:
        db = await get_database()
        print("Checking for existing 'Under grads' organization...")
        undergrads_org = await db[COLLECTION_ORGANIZATIONS].find_one({"name": "Under grads"})
        
        if not undergrads_org:
            print("Creating 'Under grads' organization...")
            now = datetime.now(timezone.utc)
            await db[COLLECTION_ORGANIZATIONS].insert_one({
                "name": "Under grads",
                "email": None,
                "logo_url": None,
                "is_active": True,
                "auto_delete_sessions": True,
                "created_by": "system",
                "is_undergrad_organization": True, # special flag as requested
                "created_at": now,
                "updated_at": now,
            })
            print("Successfully seeded 'Under grads' organization.")
        else:
            print("'Under grads' organization already exists. Checking for flag and email...")
            updates = {}
            if not undergrads_org.get("is_undergrad_organization"):
                updates["is_undergrad_organization"] = True
                
            if "email" in undergrads_org and undergrads_org["email"] is not None:
                updates["email"] = None

            if updates:
                await db[COLLECTION_ORGANIZATIONS].update_one(
                    {"_id": undergrads_org["_id"]},
                    {"$set": updates}
                )
                print(f"Updated existing organization with: {updates}")
            else:
                print("Organization is already fully seeded.")
                
    except Exception as e:
        print(f"Error seeding organization: {e}")
    finally:
        print("Closing MongoDB connection...")
        await close_mongo_connection()

if __name__ == "__main__":
    asyncio.run(seed_undergrads())
