"""
Document routes.

Key endpoints:
  POST /check-duplicate  — batch pre-check before upload (JSON body)
  POST /upload           — single-file upload (multipart/form-data)
  GET  /status-stream    — SSE stream for real-time processing status
  GET  /                 — list all documents
  GET  /{id}             — get one document
  DELETE /{id}           — delete document
  POST /dev/storage-admin — dev tool
"""
import asyncio
import json
from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, status, HTTPException, Query
from fastapi.responses import StreamingResponse
from typing import List, Optional

from app.core.response import api_response
from app.schemas.document_schema import DuplicateCheckItem, DuplicateCheckResult
from app.services.document_service import DocumentService
from app.services.dedup_service import DedupService
from app.dependencies import get_super_admin, get_admin_or_dentist, require_permission
from app.background.report_processor import log_audit_event

router = APIRouter()


# ---------------------------------------------------------------------------
# POST /check-duplicate
# One batch JSON call — frontend sends all file hashes before uploading.
# Returns per-file duplicate status (uuid echoed back, never stored).
# ---------------------------------------------------------------------------
@router.post("/check-duplicate", response_model=List[DuplicateCheckResult])
async def check_duplicate(
    request: List[DuplicateCheckItem],
    _=Depends(require_permission("documents:upload")),
):
    """
    Pre-upload duplicate check.

    Frontend sends all file entries as a JSON array. Backend checks all hashes
    in a single DB round-trip and returns a result for each entry.

    - is_duplicate: true  + duplicate_type: "hash_match"    → skip upload
    - is_duplicate: false + duplicate_type: "filename_match" → warn admin (soft match, can still upload)
    - is_duplicate: false + duplicate_type: null             → no duplicate, proceed with upload
    """
    results = await DedupService.check_duplicates(request)

    return api_response(
        success=True,
        message=f"Duplicate check complete for {len(results)} file(s).",
        data=[r.model_dump() for r in results],
    )


# ---------------------------------------------------------------------------
# POST /upload
# One HTTP request per file — frontend fires these concurrently.
# Accepts: file (binary), file_hash (form field), metadata (JSON string form field).
# ---------------------------------------------------------------------------
@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    file_hash: str = Form(...),
    metadata: str = Form(...),
    current_user=Depends(require_permission("documents:upload")),
):
    """
    Upload a single document with deduplication.

    Frontend fires one request per file concurrently.

    Form fields:
      - file:      binary file content
      - file_hash: SHA-256 hex string (computed by frontend)
      - metadata:  JSON string — must include 'file_type'; for XLSX-backed images
                   also includes 'Image Name', 'Description', 'Symptoms', 'Treatment'

    Returns the saved document with MongoDB _id as 'id'.
    The frontend uuid is never sent to this endpoint and never stored.
    """
    try:
        parsed_metadata = json.loads(metadata)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON in metadata field.")

    if not file_hash or len(file_hash) < 10:
        raise HTTPException(status_code=400, detail="file_hash is required and must be a valid hash string.")

    import time
    t_start = time.perf_counter()

    try:
        doc = await DocumentService.upload_document(
            file=file,
            file_hash=file_hash,
            metadata=parsed_metadata,
        )
    except Exception as e:
        await log_audit_event(
            actor_id=str(current_user["_id"]),
            action="DOCUMENT_UPLOAD_FAILED",
            resource="documents",
            resource_id="none",
            metadata={
                "actor_email": current_user.get("email"),
                "actor_role": current_user.get("role"),
                "filename": file.filename,
                "file_type": parsed_metadata.get("file_type", ""),
                "file_hash": file_hash,
                "error": str(e)
            },
        )
        raise

    save_elapsed = time.perf_counter() - t_start
    m, s = divmod(int(save_elapsed), 60)
    save_time_str = f"{m} min {s} sec" if m > 0 else f"{s} sec"
    print(f"📥 [SAVING] Document '{file.filename}' saved in {save_time_str}")

    # If this is an XLSX spreadsheet file itself (not an image), mark as skipped
    # — the content is already mapped onto the image files via xlsx_filename
    file_type = parsed_metadata.get("file_type", "")
    xlsx_types = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel",
    }
    if file_type in xlsx_types:
        from app.core.database import get_database
        from bson import ObjectId
        db = await get_database()
        await db["documents"].update_one(
            {"_id": ObjectId(doc["id"])},
            {"$set": {"status": "completed"}}
        )
    else:
        # Wake the processing worker immediately — no waiting for the next poll
        from app.background.processing_worker import notify_new_document
        notify_new_document()
        print(f"⚡ [NOTIFY] Worker signalled — processing will start immediately")

    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="DOCUMENT_UPLOAD",
        resource="documents",
        resource_id=doc["id"],
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "filename": file.filename,
            "file_type": parsed_metadata.get("file_type", ""),
            "file_hash": file_hash,
        },
    )

    return api_response(
        success=True,
        message="Document uploaded successfully.",
        data=doc,
        status_code=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# GET /status-stream — SSE real-time processing status
# ---------------------------------------------------------------------------
@router.get("/status-stream")
async def status_stream(
    request: Request,
    _=Depends(require_permission("documents:upload")),
):
    """
    Server-Sent Events stream for real-time document processing status.

    Pushes status updates every 3 seconds for:
      - Documents currently being processed (not completed/skipped/failed)
      - Documents that completed/failed in the last 30 seconds (so frontend catches the transition)

    Frontend usage:
        const source = new EventSource("/api/v1/documents/status-stream");
        source.onmessage = (e) => updateUI(JSON.parse(e.data));
    """
    from app.core.database import get_database

    async def event_generator():
        while True:
            # Check if client disconnected
            if await request.is_disconnected():
                break

            db = await get_database()
            now = datetime.now(timezone.utc)
            thirty_seconds_ago = now - timedelta(seconds=30)

            # Active docs: anything not in a terminal state
            active_docs = await db["documents"].find(
                {"status": {"$nin": ["completed", "skipped", "failed"]}},
                {"_id": 1, "original_filename": 1, "status": 1, "file_type": 1, "updated_at": 1}
            ).to_list(200)

            # Recently finished: completed/failed in last 30s so frontend sees the transition
            recent_docs = await db["documents"].find(
                {
                    "status": {"$in": ["completed", "failed"]},
                    "updated_at": {"$gte": thirty_seconds_ago}
                },
                {"_id": 1, "original_filename": 1, "status": 1, "file_type": 1,
                 "error_message": 1, "chunk_count": 1, "updated_at": 1}
            ).to_list(200)

            # Serialize ObjectId to string
            all_docs = []
            for doc in active_docs + recent_docs:
                all_docs.append({
                    "id": str(doc["_id"]),
                    "filename": doc.get("original_filename", ""),
                    "status": doc.get("status", ""),
                    "file_type": doc.get("file_type", ""),
                    "error_message": doc.get("error_message"),
                    "chunk_count": doc.get("chunk_count"),
                    "updated_at": doc.get("updated_at", "").isoformat() if doc.get("updated_at") else None,
                })

            data = json.dumps(all_docs, default=str)
            yield f"data: {data}\n\n"

            await asyncio.sleep(3)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering if behind proxy
        },
    )


# ---------------------------------------------------------------------------
# Legacy batch upload — kept for backwards compatibility
# ---------------------------------------------------------------------------
@router.post("/upload-batch")
async def upload_documents_batch(
    files: List[UploadFile] = File(...),
    metadata: List[str] = Form(...),
    current_user=Depends(require_permission("documents:upload")),
):
    """
    Legacy batch upload without dedup hashes.
    Prefer POST /upload (single-file with hash) for new integrations.
    """
    if len(files) != len(metadata):
        raise HTTPException(
            status_code=400,
            detail="Number of files and metadata entries must match."
        )

    parsed_metadata = []
    try:
        for m in metadata:
            parsed_metadata.append(json.loads(m))
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format in metadata")

    try:
        result = await DocumentService.upload_documents(files, parsed_metadata)
    except Exception as e:
        await log_audit_event(
            actor_id=str(current_user["_id"]),
            action="DOCUMENT_UPLOAD_BATCH_FAILED",
            resource="documents",
            resource_id="batch",
            metadata={
                "actor_email": current_user.get("email"),
                "actor_role": current_user.get("role"),
                "file_count": len(files),
                "filenames": [f.filename for f in files],
                "error": str(e)
            },
        )
        raise

    # Wake the processing worker for the new batch
    if result["count"] > 0:
        from app.background.processing_worker import notify_new_document
        notify_new_document()
        print(f"⚡ [NOTIFY] Worker signalled — {result['count']} docs from batch upload")

    # Audit log for batch upload
    if result["count"] > 0:
        await log_audit_event(
            actor_id=str(current_user["_id"]),
            action="DOCUMENT_UPLOAD_BATCH",
            resource="documents",
            resource_id="batch",
            metadata={
                "actor_email": current_user.get("email"),
                "actor_role": current_user.get("role"),
                "file_count": result["count"],
                "filenames": [f.filename for f in files],
            },
        )

    return api_response(
        success=True,
        message=f"Successfully saved {result['count']} documents.",
        data=result["documents"],
        status_code=status.HTTP_201_CREATED,
    )


# ---------------------------------------------------------------------------
# POST /{document_id}/merge-version
# ---------------------------------------------------------------------------
@router.post("/{document_id}/merge-version")
async def merge_document_version(
    document_id: str,
    _=Depends(require_permission("documents:list")),
):
    """
    Admin: Approve and merge semantic overlaps.
    The old chunks will be recursively marked is_latest=False in the database and AI Search,
    officially deprecating them while leaving the new chunks active.
    """
    result = await DocumentService.merge_document_versions(document_id)
    return api_response(
        success=True,
        message=result["message"],
        data={"merged_chunk_count": result["merged_count"]},
    )


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------
@router.get("/")
async def list_documents(skip: int = 0, limit: int = 50, search: Optional[str] = None, _=Depends(require_permission("documents:list"))):
    """Admin: Retrieve all knowledge base documents with total count."""
    result = await DocumentService.list_documents(skip, limit, search)
    return api_response(
        success=True,
        message="Documents fetched successfully.",
        data=result,
    )


# ---------------------------------------------------------------------------
# GET /{document_id}
# ---------------------------------------------------------------------------
@router.get("/{document_id}")
async def get_document(document_id: str, _=Depends(require_permission("documents:list"))):
    """Admin: Get metadata for a specific document."""
    doc = await DocumentService.get_document(document_id)
    return api_response(success=True, message="Document found.", data=doc)


# ---------------------------------------------------------------------------
# DELETE /{document_id}
# ---------------------------------------------------------------------------
@router.delete("/{document_id}")
async def delete_document(document_id: str, current_user=Depends(require_permission("documents:delete"))):
    """Admin/Dentist: Remove document from storage and database."""
    # Fetch document name BEFORE deletion for audit record
    from app.core.database import get_database
    from bson import ObjectId
    db = await get_database()
    doc = await db["documents"].find_one({"_id": ObjectId(document_id)}, {"original_filename": 1})
    await DocumentService.delete_document(document_id)
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="DOCUMENT_DELETE",
        resource="documents",
        resource_id=document_id,
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "filename": doc.get("original_filename", "unknown") if doc else "unknown",
        },
    )
    return api_response(success=True, message="Document deleted successfully.", data=None)


# ---------------------------------------------------------------------------
# POST /dev/storage-admin
# ---------------------------------------------------------------------------
@router.post("/dev/storage-admin")
async def storage_admin(
    action: str = Query(..., pattern="^(fetch|delete)$"),
    _=Depends(require_permission("documents:upload")),
):
    """
    Direct Azure Storage Management.
    - action=fetch: Returns all files in admin_doc/
    - action=delete: Deletes all files in admin_doc/
    """
    result = await DocumentService.manage_storage_directly(action)
    return api_response(
        success=True,
        message=f"Storage {action} operation successful.",
        data=result,
    )