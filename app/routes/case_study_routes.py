"""
Case Study API routes.

CRUD operations for clinical case studies:
  POST   /case-studies           — Create a new case study (super_admin only)
  GET    /case-studies           — List all case studies (any chat user)
  GET    /case-studies/{id}      — Get a single case study (any chat user)
  PATCH  /case-studies/{id}      — Update a case study (super_admin only)
  DELETE /case-studies/{id}      — Delete a case study (super_admin only)
  POST   /case-studies/embed     — Trigger embedding for all un-embedded cases (super_admin only)
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Union

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.database import get_database
from app.core.constants import COLLECTION_CASE_STUDY
from app.core.response import api_response
from app.dependencies import require_permission
from app.schemas.case_study_schema import CaseStudyCreateRequest, CaseStudyUpdateRequest

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_case_study(
    body: Union[CaseStudyCreateRequest, List[CaseStudyCreateRequest]],
    current_user=Depends(require_permission("case_study:manage")),
):
    """
    Create one or multiple new case study entries. Auto-embeds them immediately.
    Only super_admin can create case studies. Duplicates are skipped.
    """
    db = await get_database()

    items = body if isinstance(body, list) else [body]
    if not items:
        raise HTTPException(status_code=400, detail="No case studies provided.")

    now = datetime.now(timezone.utc)
    docs_to_insert = []
    skipped = 0

    for item in items:
        # Check for duplicate case_id
        existing = await db[COLLECTION_CASE_STUDY].find_one({"case_id": item.case_id})
        if existing:
            skipped += 1
            continue

        docs_to_insert.append({
            "case_id": item.case_id,
            "title": item.title,
            "content": item.content,
            "is_embedded": False,
            "created_by": str(current_user["_id"]),
            "created_at": now,
            "updated_at": now,
        })

    if docs_to_insert:
        await db[COLLECTION_CASE_STUDY].insert_many(docs_to_insert)

    # Auto-embed in background (non-blocking)
    # embed_all_case_studies automatically only embeds is_embedded=False documents
    async def _auto_embed():
        try:
            from app.background.embed_case_studies import embed_all_case_studies
            await embed_all_case_studies()
        except Exception as e:
            logger.error(f"[CASE-STUDY] Auto-embed failed: {e}")

    asyncio.create_task(_auto_embed())

    msg = f"{len(docs_to_insert)} case studies created. Embedding in progress."
    if skipped > 0:
        msg += f" (Skipped {skipped} duplicates)"

    return api_response(
        success=True,
        message=msg,
        data={"inserted_count": len(docs_to_insert), "skipped_count": skipped},
        status_code=status.HTTP_201_CREATED,
    )


@router.get("")
async def list_case_studies(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    search: str = Query(None, description="Search by title or content"),
    current_user=Depends(require_permission("chat:use")),
):
    """
    List all case studies (paginated). Any user with chat access can view.
    """
    db = await get_database()

    query = {}
    if search:
        query["$or"] = [
            {"title": {"$regex": search, "$options": "i"}},
            {"content": {"$regex": search, "$options": "i"}},
        ]

    total = await db[COLLECTION_CASE_STUDY].count_documents(query)
    cursor = (
        db[COLLECTION_CASE_STUDY]
        .find(query)
        .sort("created_at", -1)
        .skip(skip)
        .limit(limit)
    )

    cases = []
    async for doc in cursor:
        cases.append({
            "id": str(doc["_id"]),
            "case_id": doc.get("case_id", ""),
            "title": doc.get("title", ""),
            "content": doc.get("content", ""),
            "is_embedded": doc.get("is_embedded", False),
            "created_at": doc.get("created_at"),
        })

    return api_response(
        success=True,
        message=f"Retrieved {len(cases)} case study(ies).",
        data={"total": total, "skip": skip, "limit": limit, "cases": cases},
    )


@router.get("/{case_study_id}")
async def get_case_study(
    case_study_id: str,
    current_user=Depends(require_permission("chat:use")),
):
    """Get a single case study by its MongoDB _id."""
    db = await get_database()

    if not ObjectId.is_valid(case_study_id):
        raise HTTPException(status_code=400, detail="Invalid case study ID format")

    doc = await db[COLLECTION_CASE_STUDY].find_one({"_id": ObjectId(case_study_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Case study not found")

    doc["id"] = str(doc.pop("_id"))
    return api_response(success=True, message="Case study retrieved.", data=doc)


@router.patch("/{case_study_id}")
async def update_case_study(
    case_study_id: str,
    body: CaseStudyUpdateRequest,
    current_user=Depends(require_permission("case_study:manage")),
):
    """
    Update a case study's title and/or content.
    Re-embeds automatically if content changes.
    """
    db = await get_database()

    if not ObjectId.is_valid(case_study_id):
        raise HTTPException(status_code=400, detail="Invalid case study ID format")

    if body.title is None and body.content is None:
        raise HTTPException(status_code=400, detail="Provide at least one field to update (title or content)")

    doc = await db[COLLECTION_CASE_STUDY].find_one({"_id": ObjectId(case_study_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Case study not found")

    now = datetime.now(timezone.utc)
    update_set = {"updated_at": now}
    needs_reembed = False

    if body.title is not None:
        update_set["title"] = body.title
        needs_reembed = True
    if body.content is not None:
        update_set["content"] = body.content
        needs_reembed = True

    if needs_reembed:
        update_set["is_embedded"] = False  # Mark for re-embedding

    await db[COLLECTION_CASE_STUDY].update_one(
        {"_id": ObjectId(case_study_id)},
        {"$set": update_set},
    )

    # Auto re-embed if content changed
    if needs_reembed:
        async def _re_embed():
            try:
                from app.background.embed_case_studies import embed_single_case_study
                await embed_single_case_study(case_study_id)
            except Exception as e:
                logger.error(f"[CASE-STUDY] Re-embed failed for {case_study_id}: {e}")

        asyncio.create_task(_re_embed())

    return api_response(
        success=True,
        message="Case study updated. Re-embedding in progress." if needs_reembed else "Case study updated.",
        data={"id": case_study_id},
    )


@router.delete("/{case_study_id}")
async def delete_case_study(
    case_study_id: str,
    current_user=Depends(require_permission("case_study:manage")),
):
    """
    Delete a case study from MongoDB and remove it from the Azure Search index.
    """
    db = await get_database()

    if not ObjectId.is_valid(case_study_id):
        raise HTTPException(status_code=400, detail="Invalid case study ID format")

    doc = await db[COLLECTION_CASE_STUDY].find_one({"_id": ObjectId(case_study_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Case study not found")

    # Remove from Azure Search index
    case_id = doc.get("case_id", "")
    if case_id:
        try:
            import httpx
            from app.config import settings
            search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
            index_name = settings.AZURE_SEARCH_INDEX_CASE_STUDY
            url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version=2024-07-01"

            async with httpx.AsyncClient(timeout=30.0) as http_client:
                await http_client.post(
                    url,
                    headers={"Content-Type": "application/json", "api-key": settings.AZURE_SEARCH_KEY},
                    json={"value": [{"@search.action": "delete", "case_id": case_id}]},
                )
            logger.info(f"[CASE-STUDY] Removed case_id={case_id} from search index")
        except Exception as e:
            logger.warning(f"[CASE-STUDY] Failed to remove from search index: {e}")

    # Remove from MongoDB
    await db[COLLECTION_CASE_STUDY].delete_one({"_id": ObjectId(case_study_id)})

    return api_response(success=True, message="Case study deleted successfully.")


