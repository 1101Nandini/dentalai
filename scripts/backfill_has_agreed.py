"""
Migration script: backfill has_agreed=True for all existing users.

Run once after deploying the disclaimer feature so that existing users are not
shown the disclaimer popup on their next login.

Usage:
    cd /home/synsoft/projects/petal_ai_backend
    .venv/bin/python scripts/backfill_has_agreed.py
"""

import asyncio
from app.core.database import connect_to_mongo, close_mongo_connection, get_database


async def main():
    await connect_to_mongo()
    db = await get_database()

    result = await db["users"].update_many(
        {"has_agreed": {"$ne": True}},
        {"$set": {"has_agreed": True}}
    )
    print(f"Backfill complete. Modified {result.modified_count} user document(s).")
    await close_mongo_connection()


if __name__ == "__main__":
    asyncio.run(main())
