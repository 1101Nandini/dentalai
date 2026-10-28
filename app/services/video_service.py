from datetime import datetime, timezone
from typing import Optional, List

from bson import ObjectId
from fastapi import HTTPException

from app.core.database import get_database
from app.core.constants import COLLECTION_TEACHING_VIDEOS


def _serialize_video(doc: dict) -> dict:
    doc["id"] = str(doc.pop("_id"))
    for field in ("created_at", "updated_at"):
        if isinstance(doc.get(field), datetime):
            doc[field] = doc[field].isoformat()
    return doc


class VideoService:

    @staticmethod
    async def create_video(data: dict, created_by: str) -> dict:
        db = await get_database()
        now = datetime.now(timezone.utc)
        doc = {
            "title": data["title"],
            "description": data.get("description"),
            "youtube_url": data["youtube_url"],
            "is_active": True,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
        result = await db[COLLECTION_TEACHING_VIDEOS].insert_one(doc)
        doc["id"] = str(result.inserted_id)
        doc.pop("_id", None)
        for field in ("created_at", "updated_at"):
            if isinstance(doc.get(field), datetime):
                doc[field] = doc[field].isoformat()
        return doc

    @staticmethod
    async def list_videos(active_only: bool = True, skip: int = 0, limit: int = 50) -> dict:
        db = await get_database()
        query = {}
        if active_only:
            query["is_active"] = True
        total = await db[COLLECTION_TEACHING_VIDEOS].count_documents(query)
        cursor = (
            db[COLLECTION_TEACHING_VIDEOS]
            .find(query)
            .sort("created_at", -1)
            .skip(skip)
            .limit(limit)
        )
        videos = []
        async for doc in cursor:
            videos.append(_serialize_video(doc))
        return {"total": total, "skip": skip, "limit": limit, "videos": videos}

    @staticmethod
    async def get_video(video_id: str) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(video_id):
            raise HTTPException(status_code=400, detail="Invalid video ID format")
        doc = await db[COLLECTION_TEACHING_VIDEOS].find_one({"_id": ObjectId(video_id)})
        if not doc:
            raise HTTPException(status_code=404, detail="Video not found")
        return _serialize_video(doc)

    @staticmethod
    async def update_video(video_id: str, data: dict) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(video_id):
            raise HTTPException(status_code=400, detail="Invalid video ID format")
        update_fields = {k: v for k, v in data.items() if v is not None}
        if not update_fields:
            raise HTTPException(status_code=400, detail="No valid fields to update")
        update_fields["updated_at"] = datetime.now(timezone.utc)
        doc = await db[COLLECTION_TEACHING_VIDEOS].find_one_and_update(
            {"_id": ObjectId(video_id)},
            {"$set": update_fields},
            return_document=True,
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Video not found")
        return _serialize_video(doc)

    @staticmethod
    async def delete_video(video_id: str) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(video_id):
            raise HTTPException(status_code=400, detail="Invalid video ID format")
        doc = await db[COLLECTION_TEACHING_VIDEOS].find_one_and_delete(
            {"_id": ObjectId(video_id)}
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Video not found")
        return _serialize_video(doc)
