"""
ChatService — CRUD operations for chat conversations.
"""
from datetime import datetime, timezone
from typing import List, Optional

from bson import ObjectId
from fastapi import HTTPException, status

from app.core.database import get_database
from app.core.constants import COLLECTION_CHATS, COLLECTION_MESSAGES


class ChatService:

    # ── Create ─────────────────────────────────────────────────────────────
    @staticmethod
    async def create_chat(user_id: str, title: Optional[str] = None) -> dict:
        """Create a new chat conversation for *user_id*."""
        db = await get_database()
        now = datetime.now(timezone.utc)

        chat = {
            "user_id": user_id,
            "title": title or "New Chat",
            "created_at": now,
            "updated_at": now,
        }
        result = await db[COLLECTION_CHATS].insert_one(chat)
        chat["id"] = str(result.inserted_id)
        chat.pop("_id", None)
        return chat

    # ── List ───────────────────────────────────────────────────────────────
    @staticmethod
    async def list_chats(
        user_id: str,
        limit: int = 50,
        cursor: Optional[str] = None,
        search: Optional[str] = None,
    ) -> dict:
        """Return chats for *user_id*, newest first, with optional cursor-based pagination."""
        db = await get_database()

        query: dict = {"user_id": user_id}
        if cursor and ObjectId.is_valid(cursor):
            query["_id"] = {"$lt": ObjectId(cursor)}

        if search:
            query["title"] = {"$regex": search, "$options": "i"}

        docs = (
            db[COLLECTION_CHATS]
            .find(query)
            .sort("updated_at", -1)
            .limit(limit + 1)
        )

        chats = []
        async for doc in docs:
            doc["id"] = str(doc.pop("_id"))
            chats.append(doc)
            
        has_more = len(chats) > limit
        if has_more:
            chats.pop()

        next_cursor = chats[-1]["id"] if chats else None

        return {
            "chats": chats,
            "has_more": has_more,
            "next_cursor": next_cursor
        }

    # ── Get single ─────────────────────────────────────────────────────────
    @staticmethod
    async def get_chat(chat_id: str) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(chat_id):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")

        doc = await db[COLLECTION_CHATS].find_one({"_id": ObjectId(chat_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Chat not found")

        doc["id"] = str(doc.pop("_id"))
        return doc

    # ── Update (rename) ───────────────────────────────────────────────────
    @staticmethod
    async def update_chat(chat_id: str, title: str) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(chat_id):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")

        result = await db[COLLECTION_CHATS].find_one_and_update(
            {"_id": ObjectId(chat_id)},
            {"$set": {"title": title, "updated_at": datetime.now(timezone.utc)}},
            return_document=True,
        )
        if not result:
            raise HTTPException(status_code=404, detail="Chat not found")

        result["id"] = str(result.pop("_id"))
        return result

    # ── Delete (cascade messages) ──────────────────────────────────────────
    @staticmethod
    async def delete_chat(chat_id: str) -> None:
        db = await get_database()
        if not ObjectId.is_valid(chat_id):
            raise HTTPException(status_code=400, detail="Invalid chat ID format")

        oid = ObjectId(chat_id)
        doc = await db[COLLECTION_CHATS].find_one({"_id": oid})
        if not doc:
            raise HTTPException(status_code=404, detail="Chat not found")

        # Collect all image blob names to delete
        from app.services.user_upload_service import UserUploadService
        messages = db[COLLECTION_MESSAGES].find({"chat_id": chat_id})
        blob_names = []
        async for m in messages:
            if m.get("image_urls"):
                blob_names.extend(m["image_urls"])
            if m.get("image_url"):
                blob_names.append(m["image_url"])
        
        if blob_names:
            import asyncio
            asyncio.create_task(UserUploadService.delete_images(list(set(blob_names))))

        # Cascade: remove all messages belonging to this chat
        await db[COLLECTION_MESSAGES].delete_many({"chat_id": chat_id})
        await db[COLLECTION_CHATS].delete_one({"_id": oid})

    # ── Touch (bump updated_at on new message) ─────────────────────────────
    @staticmethod
    async def touch_chat(chat_id: str) -> None:
        """Update the ``updated_at`` timestamp so the chat floats to the top."""
        db = await get_database()
        await db[COLLECTION_CHATS].update_one(
            {"_id": ObjectId(chat_id)},
            {"$set": {"updated_at": datetime.now(timezone.utc)}},
        )
