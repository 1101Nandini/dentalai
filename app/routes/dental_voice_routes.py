"""
Dental Voice Assistant API routes.

Endpoints:
  WS   /ws/voice-stream — Real-time voice streaming via WebSocket (audio → Whisper → transcript)
  POST /cdt-lookup       — CDT code lookup from transcript (Scenario 1)
  POST /generate-notes   — Generate SOAP note + patient summary (Scenario 2)
  POST /upload-cdt-pdf   — Upload CDT codebook PDF (separate storage from RAG)
  GET  /cdt-codes        — List all CDT codes in the system
  GET  /sessions         — List voice recording sessions
  GET  /sessions/{id}    — Get a specific session with audio URL
"""
import asyncio
import json
import logging
import uuid as _uuid_mod
from datetime import datetime, timezone, date

from bson import ObjectId
from fastapi import APIRouter, BackgroundTasks, Body, Depends, Query, UploadFile, File, Form, HTTPException, WebSocket, WebSocketDisconnect, status
from typing import Optional

from app.core.response import api_response
from app.core.database import get_database
from app.core.constants import COLLECTION_CDT_DOCUMENTS, DocumentStatus, UserRole
from app.dependencies import get_super_admin, get_current_dentist, get_staff_member, require_permission, get_current_user
from app.services.cdt_service import CDTService
from app.services.dental_voice_service import DentalVoiceService
from app.services.voice_stream_service import VoiceStreamService
from app.schemas.dental_voice_schema import SessionEditRequest, GenerateNotesRequest
from app.utils.file_utils import upload_blob, generate_sas_url, delete_blob
from app.templates.soap_template import XRAY_OPTIONS

router = APIRouter()
ws_router = APIRouter()  # Separate router for WebSocket (no rate limiting)
logger = logging.getLogger(__name__)

# Strong reference set to prevent Python GC from killing background tasks
_active_background_tasks = set()

def _track_background_task(coro):
    task = asyncio.create_task(coro)
    _active_background_tasks.add(task)
    task.add_done_callback(_active_background_tasks.discard)
    return task





# ── CDT Code Management (Admin/Dentist) ──────────────────────────────

@router.get("/cdt-documents")
async def list_cdt_documents(_=Depends(require_permission("cdt:documents:view"))):
    """List all uploaded CDT codebooks."""
    docs = await CDTService.list_cdt_documents()
    return api_response(success=True, message="CDT documents fetched.", data=docs)


@router.delete("/cdt-documents/{document_id}")
async def delete_cdt_document(document_id: str, current_user=Depends(require_permission("cdt:manage"))):
    """Completely remove a CDT book and all its associated data."""
    await CDTService.delete_cdt_document(document_id)
    
    from app.background.report_processor import log_audit_event
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="DOCUMENT_DELETE",
        resource="cdt_documents",
        resource_id=document_id,
        metadata={"actor_email": current_user.get("email"), "document_type": "CDT_CODEBOOK"},
        organization_id=current_user.get("organization_id"),
    )
    
    return api_response(success=True, message="CDT document and codes deleted successfully.")


# ── CDT Code Listing ────────────────────────────────────────────────────────

@router.get("/cdt-codes")
async def list_cdt_codes(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    category: Optional[str] = Query(None, description="Filter by category name"),
    search: Optional[str] = Query(None, description="Search by code or description"),
    _=Depends(require_permission("cdt:codes:view")),
):
    """
    List all CDT codes in the system.

    Used by staff (admins/dentists) to review what codes have been ingested from the CDT PDF.
    Supports pagination, category filtering, and search.
    """
    result = await CDTService.list_cdt_codes(
        skip=skip,
        limit=limit,
        category=category,
        search=search,
    )

    return api_response(
        success=True,
        message=f"Retrieved {len(result['codes'])} CDT codes (total: {result['total']}).",
        data=result,
    )


# ── X-Ray Options ────────────────────────────────────────────────────────────

@router.get("/xray-options")
async def get_xray_options(current_user=Depends(get_current_user)):
    """
    Return static x-ray options list and org-level xray_enabled flag.
    Frontend uses this to render the x-ray selector dropdown.
    """
    db = await get_database()
    org_id = current_user.get("organization_id")
    xray_enabled = True
    if org_id:
        org = await db["organizations"].find_one(
            {"_id": ObjectId(org_id)}, {"xray_enabled": 1}
        )
        if org:
            xray_enabled = org.get("xray_enabled", True)

    options = [
        {"key": key, "name": val["name"], "cdt_code": val["cdt_code"]}
        for key, val in XRAY_OPTIONS.items()
    ]

    return api_response(
        success=True,
        message="X-ray options retrieved.",
        data={"xray_enabled": xray_enabled, "options": options},
    )


# ── Regenerate Report ────────────────────────────────────────────────────────

@router.post("/sessions/{session_id}/regenerate-report")
async def regenerate_report(
    session_id: str,
    background_tasks: BackgroundTasks,
    payload: Optional[dict] = Body(None),
    current_user=Depends(require_permission("sessions:edit")),
):
    """
    Intelligently retries/regenerates a session or report based on its failure step:
    - If audio remuxing failed (stage: pending_remux / remux_failed), remuxes audio first, then runs STT & SOAP.
    - If STT failed (stage: remux_completed / ai_failed), skips remuxing and resumes at Gemini STT.
    - If STT succeeded, regenerates the Ghostwriter SOAP note & summary reports.
    """
    from app.services.voice_stream_service import VoiceStreamService
    from app.services.permission_service import PermissionService
    from bson.errors import InvalidId
    from bson import ObjectId
    from datetime import datetime, timezone

    try:
        obj_id = ObjectId(session_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid session ID format.")

    db = await get_database()
    user_id = str(current_user.get("_id"))
    role = current_user.get("role")
    org_id = current_user.get("organization_id")
    
    # Access check: can user see this session?
    has_org_access = await PermissionService.has_permission(current_user, "sessions:org_access")
    query = {"_id": obj_id}
    if has_org_access and org_id:
        query["organization_id"] = org_id
    else:
        query["user_id"] = user_id

    session = await db["dental_voice_sessions"].find_one(query)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or access denied.")

    # Extract optional xray_selection from payload if provided
    xray_selection = None
    if payload and "xray_selection" in payload:
        xray_selection = payload["xray_selection"]
        if isinstance(xray_selection, str):
            xray_selection = [xray_selection]

    transcript = session.get("transcript")
    processing_stage = session.get("processing_stage", "pending_remux")
    status = session.get("status")

    # If transcript is missing, or status is failed, or processing_stage indicates incomplete/failed state:
    if not transcript or status == "failed" or processing_stage in ("pending_remux", "remux_failed", "ai_failed", "ai_processing"):
        logger.info(f"[REGENERATE-REPORT] Session {session_id} is in stage '{processing_stage}' (status: '{status}'). Reprocessing from failure step.")
        
        # If xray_selection was provided in payload, update it on the DB document
        if xray_selection is not None:
            await db["dental_voice_sessions"].update_one(
                {"_id": obj_id},
                {"$set": {"xray_selection": xray_selection, "updated_at": datetime.now(timezone.utc)}}
            )

        # Reprocess intelligently based on processing_stage
        result = await VoiceStreamService.reprocess_session(session_id)
        return api_response(
            message="Session reprocessed successfully from failure step.",
            data={"session_id": session_id, "status": result.get("status", "transcripted"), **result}
        )

    # Otherwise, transcript exists and stage is complete — regenerate SOAP report
    await db["dental_voice_sessions"].update_one(
        {"_id": obj_id},
        {"$set": {
            "status": "processing",
            "updated_at": datetime.now(timezone.utc),
            **({"xray_selection": xray_selection} if xray_selection is not None else {})
        }}
    )

    # Queue the background generation task
    background_tasks.add_task(
        VoiceStreamService.run_soap_background,
        session_id=str(session["_id"]),
        transcript=transcript,
        user_id=session.get("user_id", user_id),
        template_ids=session.get("template_ids"),
        org_id=session.get("organization_id"),
        xray_selection=xray_selection or session.get("xray_selection"),
    )

    return api_response(
        message="Report regeneration started successfully. Please wait a moment.",
        data={"session_id": session_id, "status": "processing"}
    )

@router.post("/sessions/{session_id}/reprocess")
@router.post("/sessions/{session_id}/regenerate-full")
async def regenerate_full_session(
    session_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Regenerates/reprocesses a session while preserving stage state:
    - If ffmpeg remuxing failed or was incomplete (processing_stage != 'remux_completed'),
      starts from the beginning: remuxing audio with ffmpeg, then moving to AI stage.
    - If remuxing was already completed (processing_stage == 'remux_completed'),
      skips remuxing and starts directly from the AI stage (Gemini STT + CDT + SOAP).
    """
    from app.services.voice_stream_service import VoiceStreamService
    from app.services.permission_service import PermissionService
    from bson.errors import InvalidId
    from bson import ObjectId

    try:
        obj_id = ObjectId(session_id)
    except InvalidId:
        raise HTTPException(status_code=400, detail="Invalid session ID format.")

    db = await get_database()
    user_id = str(current_user.get("_id"))
    org_id = current_user.get("organization_id")
    
    # Access check
    has_org_access = await PermissionService.has_permission(current_user, "sessions:org_access")
    query = {"_id": obj_id}
    if has_org_access and org_id:
        query["organization_id"] = org_id
    else:
        query["user_id"] = user_id

    session = await db["dental_voice_sessions"].find_one(query)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or access denied.")

    audio_blob_name = session.get("audio_blob_name")
    if not audio_blob_name:
        raise HTTPException(status_code=400, detail="Session has no audio attached to regenerate from.")

    try:
        result = await VoiceStreamService.reprocess_session(session_id)
        return api_response(
            success=True,
            message="Session reprocessing started/completed successfully.",
            data=result
        )
    except Exception as e:
        logger.error(f"[REGENERATE-FULL] Failed to reprocess session {session_id}: {e}")
        return api_response(success=False, message=str(e), status_code=500)

@router.post("/test/generate")
async def test_generate(
    audio: UploadFile = File(...),
    template_id: Optional[str] = Form(None)
):
    """
    Test endpoint: Give an audio file and optional template_id via form data.
    No auth required. Generates transcript, CDT codes, and SOAP note using existing logic.
    """
    import uuid
    from app.services.cdt_service_direct import CDTServiceDirect
    from app.services.dental_voice_service import DentalVoiceService
    from app.services.voice_stream_service import VoiceStreamService
    
    try:
        user_id = "test_dev_user"
        org_id = None

        # 1. Generate Transcript from Audio
        session = VoiceStreamService(user_id=user_id, stream_id=f"test_{uuid.uuid4()}", organization_id=org_id)
        await session.initialize()
        
        audio_bytes = await audio.read()
        await session.add_audio_chunk(audio_bytes)
        
        # finalize() transcribes the audio and returns the transcript
        transcript = await session.finalize()
        
        # 2. Generate CDT Codes
        cdt_result = await CDTServiceDirect.process_transcript(transcript)
        
        # 3. Generate SOAP Note
        template_ids = [template_id] if template_id else None
        soap_result = await DentalVoiceService.generate_soap_and_summary(
            transcript=transcript,
            template_ids=template_ids,
            org_id=org_id
        )
        
        return api_response(
            success=True,
            message="Test generation successful.",
            data={
                "transcript": transcript,
                "cdt_result": cdt_result,
                **soap_result
            }
        )
    except Exception as e:
        logger.error(f"[TEST-GENERATE] Error: {e}")
        return api_response(success=False, message=str(e), status_code=500)


@router.post("/generate-notes")
async def generate_notes(
    payload: GenerateNotesRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    On-demand endpoint to generate multiple Ghostwriter notes from a report ID and template IDs.
    Finds the transcript using the report ID, generates notes, and pushes them into the report's reports array.
    """
    try:
        if not ObjectId.is_valid(payload.report_id):
            raise HTTPException(status_code=400, detail="Invalid report ID format")
        session_obj_id = ObjectId(payload.report_id)

        db = await get_database()
        user_id = str(current_user.get("_id"))
        org_id = current_user.get("organization_id")

        # Access check
        from app.services.permission_service import PermissionService
        has_org_access = await PermissionService.has_permission(current_user, "sessions:org_access")

        query = {"_id": session_obj_id}
        if has_org_access and org_id:
            query["organization_id"] = org_id
        else:
            query["user_id"] = user_id

        session = await db["dental_voice_sessions"].find_one(query)
        if not session:
            raise HTTPException(status_code=404, detail="Session not found or access denied.")

        transcript = session.get("transcript")
        if not transcript:
            raise HTTPException(status_code=400, detail="Session has no transcript to generate notes from.")

        from app.services.dental_voice_service import DentalVoiceService
        result = await DentalVoiceService.generate_soap_and_summary(
            transcript=transcript,
            template_ids=payload.template_ids,
            org_id=org_id,
            xray_selection=payload.xray_selection,
        )

        new_reports = result.get("reports", [])

        if new_reports:
            existing_reports = session.get("reports", []) or []
            existing_template_ids = session.get("template_ids", []) or []

            # Map existing reports by template_id to overwrite duplicates or append new ones
            reports_map = {str(r["template_id"]): r for r in existing_reports}
            for r in new_reports:
                reports_map[str(r["template_id"])] = r
            updated_reports = list(reports_map.values())

            # Update template_ids list maintaining uniqueness
            updated_template_ids = list(existing_template_ids)
            for tid in payload.template_ids:
                if tid not in updated_template_ids:
                    updated_template_ids.append(tid)

            # Reconstruct combined soap_note, preserving existing SOAP if present
            combined_reports = []
            
            # Preserve the original SOAP note if it exists (generated before templates were added)
            existing_soap = session.get("soap_note", "")
            has_existing_soap_without_templates = existing_soap and not existing_reports
            if has_existing_soap_without_templates:
                combined_reports.append(f"# SOAP Note\n{existing_soap}")
            
            for r in updated_reports:
                name = r.get("template_name") or "Report"
                report_body = r.get("report") or ""
                combined_reports.append(f"# {name}\n{report_body}")
            new_soap_note = "\n\n---\n\n".join(combined_reports)

            # Save to database — include all fields from the result
            from datetime import datetime, timezone
            update_fields = {
                "reports": updated_reports,
                "template_ids": updated_template_ids,
                "soap_note": new_soap_note,
                "updated_at": datetime.now(timezone.utc)
            }
            
            # Update patient_summary, completed_today, planned_future if returned
            patient_summary = result.get("patient_summary")
            if patient_summary:
                update_fields["patient_summary"] = patient_summary
            
            completed_today = result.get("completed_today", [])
            if completed_today:
                # Merge with existing completed_today, deduplicating on (cdt_code, tooth)
                existing_completed = session.get("completed_today", []) or []
                merged = existing_completed + completed_today
                seen = set()
                deduped = []
                for item in merged:
                    if not isinstance(item, dict):
                        continue
                    key = (
                        str(item.get("cdt_code", "")).upper().strip(),
                        str(item.get("tooth", "")).strip(),
                    )
                    if key not in seen:
                        seen.add(key)
                        deduped.append(item)
                update_fields["completed_today"] = deduped
            
            planned_future = result.get("planned_future", [])
            if planned_future:
                existing_planned = session.get("planned_future", []) or []
                merged = existing_planned + planned_future
                seen = set()
                deduped = []
                for item in merged:
                    if not isinstance(item, dict):
                        continue
                    key = (
                        str(item.get("cdt_code", "")).upper().strip(),
                        str(item.get("tooth", "")).strip(),
                    )
                    if key not in seen:
                        seen.add(key)
                        deduped.append(item)
                update_fields["planned_future"] = deduped
            
            entities = result.get("entities_extracted")
            if entities:
                update_fields["entities_extracted"] = entities

            # Store xray_selection on the session document
            if payload.xray_selection is not None:
                update_fields["xray_selection"] = payload.xray_selection

            await db["dental_voice_sessions"].update_one(
                {"_id": session_obj_id},
                {"$set": update_fields}
            )

            # Log audit event
            from app.background.report_processor import log_audit_event
            await log_audit_event(
                actor_id=user_id,
                action="REPORT_EDIT",
                resource="dental_voice_sessions",
                resource_id=payload.report_id,
                metadata={
                    "actor_email": current_user.get("email"),
                    "action": "added_reports_on_demand",
                    "template_ids": payload.template_ids
                },
                organization_id=org_id
            )

        return api_response(
            message="Notes generated successfully.",
            data={"reports": new_reports}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[DENTAL-VOICE] On-demand generation failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# ── Financial Dashboard ──────────────────────────────────────────────────────

@router.get("/financial-dashboard")
async def financial_dashboard(
    date_filter: str = Query("month", description="Filter by date: day, week, month, all"),
    is_verified: str = Query("all", description="Filter by verification status: true, false, all"),
    is_billed: str = Query("all", description="Filter by billing status: true, false, all"),
    session_type: Optional[str] = Query(None, description="Filter by session_type: cdt_lookup, generate_notes, all"),
    search_term: Optional[str] = Query(None, description="Search by creator name or session name"),
    skip: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=1000),
    current_user=Depends(require_permission("financial_bridge:access")),
):
    """
    Financial Bridge Dashboard API for Org Admins and Front Office.
    Returns stats and filtered sessions.
    """
    from datetime import datetime, timedelta, timezone
    
    db = await get_database()
    role = current_user.get("role")
    org_id = current_user.get("organization_id")
    user_id = str(current_user.get("_id"))

    if role == UserRole.SOLO_DENTIST:
        match_stage = {"user_id": user_id}
    else:
        match_stage = {"organization_id": org_id}
    
    # Date filter
    now = datetime.now(timezone.utc)
    if date_filter == "day":
        match_stage["created_at"] = {"$gte": now - timedelta(days=1)}
    elif date_filter == "week":
        match_stage["created_at"] = {"$gte": now - timedelta(weeks=1)}
    elif date_filter == "month":
        match_stage["created_at"] = {"$gte": now - timedelta(days=30)}
    elif date_filter and date_filter != "all":
        try:
            target_date = datetime.strptime(date_filter, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            end_of_day = target_date + timedelta(days=1)
            match_stage["created_at"] = {"$gte": target_date, "$lt": end_of_day}
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date_filter. Use 'day', 'week', 'month', 'all', or 'YYYY-MM-DD'.")
        
    # is_verified filter
    if is_verified == "true":
        match_stage["is_verified"] = True
    elif is_verified == "false":
        match_stage["is_verified"] = {"$ne": True}
        
    # is_billed filter
    if is_billed == "true":
        match_stage["is_billed"] = True
    elif is_billed == "false":
        match_stage["is_billed"] = {"$ne": True}
        
    # session_type filter
    if session_type and session_type != "all":
        match_stage["session_type"] = session_type

    # search filter
    if search_term:
        match_stage["$or"] = [
            {"creator_name": {"$regex": search_term, "$options": "i"}},
            {"session_name": {"$regex": search_term, "$options": "i"}},
        ]

    # For stats, we ONLY want organization-wide all-time totals, irrespective of ANY filter (even date_filter)
    if role == UserRole.SOLO_DENTIST:
        stats_match = {"user_id": user_id}
    else:
        stats_match = {"organization_id": org_id}
    
    stats_pipeline = [
        {"$match": stats_match},
        {
            "$group": {
                "_id": None,
                "needs_review": {
                    "$sum": {"$cond": [{"$ne": ["$is_verified", True]}, 1, 0]}
                },
                "total_cdt_codes": {
                    "$sum": {"$size": {"$ifNull": ["$completed_today", []]}}
                },
                "patients_ready": {
                    "$sum": {"$cond": [{"$and": [{"$eq": ["$is_verified", True]}, {"$ne": ["$is_billed", True]}]}, 1, 0]}
                },
                "total_billed": {
                    "$sum": {"$cond": [{"$eq": ["$is_billed", True]}, 1, 0]}
                }
            }
        }
    ]

    sessions_pipeline = [
        {"$match": match_stage},
        {"$sort": {"created_at": -1}},
        {"$skip": skip},
        {"$limit": limit}
    ]

    import asyncio
    stats_cursor = db["dental_voice_sessions"].aggregate(stats_pipeline)
    sessions_cursor = db["dental_voice_sessions"].aggregate(sessions_pipeline)

    stats_result, sessions_result = await asyncio.gather(
        stats_cursor.to_list(1),
        sessions_cursor.to_list(None)
    )
    
    if not stats_result and not sessions_result:
        return api_response(success=True, message="No data found", data={"stats": {}, "sessions": []})
        
    # Process sessions
    sessions = sessions_result if sessions_result else []
    for doc in sessions:
        doc["id"] = str(doc.pop("_id"))
        if doc.get("audio_blob_name"):
            doc["audio_url"] = generate_sas_url(doc["audio_blob_name"])
            
    # Process stats
    raw_stats = stats_result[0] if stats_result else {}
    stats_obj = {
        "needs_review_sessions": raw_stats.get("needs_review", 0),
        "total_cdt_codes_detected": raw_stats.get("total_cdt_codes", 0),
        "patients_ready_to_bill": raw_stats.get("patients_ready", 0),
        "total_billed": raw_stats.get("total_billed", 0)
    }

    return api_response(
        success=True,
        message="Financial dashboard data retrieved.",
        data={
            "stats": stats_obj,
            "sessions": sessions,
            "skip": skip,
            "limit": limit
        }
    )


# ── Session History ──────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    session_type: Optional[str] = Query(None, description="Filter: cdt_lookup or generate_notes"),
    search_term: Optional[str] = Query(None, description="Search by creator name or session name"),
    from_date: Optional[date] = Query(None, description="Filter sessions from this date (inclusive)"),
    to_date: Optional[date] = Query(None, description="Filter sessions up to this date (inclusive)"),
    current_user=Depends(require_permission("sessions:list")),
):
    """
    List voice recording sessions.

    Visibility rules:
      - Staff: sees ALL sessions across all users (with creator info).
      - Dentist / Admin: sees only their OWN sessions.

    Supports filtering by session_type, search_term, and date range.
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")

    # ── Build aggregation pipeline ───────────────────────────────────────
    pipeline = []

    # Stage 1: Match base filters
    match_stage = {}

    # Visibility rules:
    # Check if role has org-wide session access
    from app.services.permission_service import PermissionService
    has_org_access = await PermissionService.has_permission(current_user, "sessions:org_access")
    
    if has_org_access:
        org_id = current_user.get("organization_id")
        if org_id:
            match_stage["organization_id"] = org_id
    else:
        match_stage["user_id"] = user_id

    if session_type:
        match_stage["session_type"] = session_type

    # Date range filter
    if from_date or to_date:
        date_filter = {}
        if from_date:
            date_filter["$gte"] = datetime.combine(from_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        if to_date:
            date_filter["$lte"] = datetime.combine(to_date, datetime.max.time()).replace(tzinfo=timezone.utc)
        match_stage["created_at"] = date_filter

    if match_stage:
        pipeline.append({"$match": match_stage})

    # Stage 2: Lookup creator info from users collection
    pipeline.extend([
        {
            "$addFields": {
                "user_id_obj": {
                    "$convert": {
                        "input": "$user_id",
                        "to": "objectId",
                        "onError": None,
                        "onNull": None
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "users",
                "localField": "user_id_obj",
                "foreignField": "_id",
                "as": "creator_info"
            }
        },
        {
            "$addFields": {
                "creator_name": {"$arrayElemAt": ["$creator_info.full_name", 0]},
                "creator_role": {"$arrayElemAt": ["$creator_info.role", 0]},
            }
        },
        {"$unset": ["user_id_obj", "creator_info"]},
        {
            "$addFields": {
                "template_ids_obj": {
                    "$map": {
                        "input": {
                            "$filter": {
                                "input": { "$ifNull": ["$template_ids", [{ "$ifNull": ["$template_id", None] }]] },
                                "as": "tid",
                                "cond": { "$and": [
                                    { "$eq": [{ "$type": "$$tid" }, "string"] },
                                    { "$eq": [{ "$strLenCP": "$$tid" }, 24] }
                                ]}
                            }
                        },
                        "as": "tid",
                        "in": { "$toObjectId": "$$tid" }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "note_templates",
                "localField": "template_ids_obj",
                "foreignField": "_id",
                "as": "template_info"
            }
        },
        {
            "$addFields": {
                "template_names": {
                    "$map": {
                        "input": "$template_info",
                        "as": "tinfo",
                        "in": "$$tinfo.procedure_name"
                    }
                }
            }
        },
        {"$unset": ["template_ids_obj", "template_info"]},
    ])

    # Stage 3: Filter by search term (post-lookup)
    if search_term:
        pipeline.append({
            "$match": {
                "$or": [
                    {"creator_name": {"$regex": search_term, "$options": "i"}},
                    {"session_name": {"$regex": search_term, "$options": "i"}}
                ]
            }
        })

    # Stage 4: Sort
    pipeline.append({"$sort": {"created_at": -1}})

    # Count total before pagination
    count_pipeline = pipeline + [{"$count": "total"}]
    count_result = await db["dental_voice_sessions"].aggregate(count_pipeline).to_list(1)
    total = count_result[0]["total"] if count_result else 0

    # Stage 5: Pagination + projection
    pipeline.extend([
        {"$skip": skip},
        {"$limit": limit},
        {"$project": {
            "user_id": 1,
            "session_name": 1,
            "status": 1,
            "processing_stage": 1,
            "transcript": 1,
            "created_at": 1,
            "session_duration_seconds": 1,
            "creator_name": 1,
            "creator_role": 1,
            "template_ids": 1,
            "template_id": 1,
            "template_names": 1,
            "xray_selection": 1,
            "is_verified": 1,
            "is_billed": 1,
            "error_message": 1,
            "session_type": 1,
        }},
    ])

    cursor = db["dental_voice_sessions"].aggregate(pipeline)

    sessions = []
    async for doc in cursor:
        doc["id"] = str(doc.pop("_id"))

        # Truncate transcript to first 100 words
        transcript = doc.get("transcript", "")
        if transcript:
            words = transcript.split()
            if len(words) > 100:
                doc["transcript"] = " ".join(words[:100]) + "..."

        sessions.append(doc)

    return api_response(
        success=True,
        message=f"Retrieved {len(sessions)} session(s).",
        data={
            "total": total,
            "skip": skip,
            "limit": limit,
            "sessions": sessions
        },
    )


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    current_user=Depends(require_permission("sessions:view")),
):
    """
    Get a specific voice recording session with fresh audio URL.

    Visibility: Staff can view any session. Dentist/Admin can only view their own.
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")

    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    query = {"_id": ObjectId(session_id)}

    # Visibility check
    from app.services.permission_service import PermissionService
    has_org_access = await PermissionService.has_permission(current_user, "sessions:org_access")
    
    if has_org_access:
        # User can view any session within their org
        query["organization_id"] = current_user.get("organization_id")
    else:
        # User can only view their own sessions
        query["user_id"] = user_id

    doc = await db["dental_voice_sessions"].find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found")

    doc["id"] = str(doc.pop("_id"))
    if doc.get("audio_blob_name"):
        doc["audio_url"] = generate_sas_url(doc["audio_blob_name"])

    # Enrich with creator info
    try:
        creator = await db["users"].find_one({"_id": ObjectId(doc.get("user_id"))}, {"full_name": 1, "role": 1})
        if creator:
            doc["creator_name"] = creator.get("full_name", "Unknown")
            doc["creator_role"] = creator.get("role", "unknown")
    except Exception:
        pass

    # Enrich with template info
    template_ids = doc.get("template_ids", [])
    if not template_ids and doc.get("template_id"):
        template_ids = [doc.get("template_id")]
    
    if template_ids:
        try:
            valid_ids = [ObjectId(tid) for tid in template_ids if ObjectId.is_valid(tid)]
            if valid_ids:
                templates = await db["note_templates"].find({"_id": {"$in": valid_ids}}, {"procedure_name": 1}).to_list(length=None)
                doc["template_names"] = [t.get("procedure_name") for t in templates if t.get("procedure_name")]
        except Exception:
            pass

    return api_response(
        success=True,
        message="Session retrieved.",
        data=doc,
    )


@router.delete("/sessions")
async def delete_sessions_bulk(
    ids: list[str] = Query(..., description="List of session IDs to delete"),
    current_user=Depends(require_permission("sessions:bulk_delete")),
):
    """
    Delete multiple voice recording sessions and their associated audio blobs.
    Dentist/Admin can delete only their own sessions. Staff cannot delete sessions.
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")

    # Permission check is handled by require_permission("sessions:bulk_delete") dependency
    
    valid_ids = [ObjectId(sid) for sid in ids if ObjectId.is_valid(sid)]
    if not valid_ids:
        raise HTTPException(status_code=400, detail="No valid session IDs provided")

    query = {"_id": {"$in": valid_ids}}
    if role == UserRole.ORG_ADMIN:
        query["organization_id"] = current_user.get("organization_id")
    else:
        query["user_id"] = user_id
    
    # Fetch sessions to get their blob names for deletion
    cursor = db["dental_voice_sessions"].find(query, {"_id": 1, "audio_blob_name": 1})
    sessions_to_delete = await cursor.to_list(length=len(valid_ids))
    
    if not sessions_to_delete:
        raise HTTPException(status_code=404, detail="No sessions found or permission denied")
        
    from app.utils.file_utils import delete_blob
    from app.background.report_processor import log_audit_event
    
    deleted_count = 0
    for doc in sessions_to_delete:
        session_id = str(doc["_id"])
        blob_name = doc.get("audio_blob_name")
        
        # Delete audio blob
        if blob_name:
            try:
                await delete_blob(blob_name)
                logger.info(f"[VOICE] Deleted audio blob: {blob_name}")
            except Exception as e:
                logger.error(f"[VOICE] Failed to delete audio blob {blob_name}: {e}")
                
        # Delete from db
        await db["dental_voice_sessions"].delete_one({"_id": doc["_id"]})
        deleted_count += 1
        
        # Log audit event
        await log_audit_event(
            actor_id=user_id,
            action="REPORT_DELETE",
            resource="dental_voice_sessions",
            resource_id=session_id,
            metadata={"actor_email": current_user.get("email"), "bulk_delete": True},
            organization_id=current_user.get("organization_id")
        )

    return api_response(
        success=True,
        message=f"{deleted_count} session(s) and audio blob(s) deleted successfully."
    )


@router.delete("/sessions/{session_id}")
async def delete_session(
    session_id: str,
    current_user=Depends(require_permission("sessions:delete")),
):
    """
    Delete a specific voice recording session and its audio blob.

    Dentist/Admin can delete only their own sessions. Staff cannot delete sessions.
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")

    # Permission check is handled by require_permission("sessions:delete") dependency

    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    # Org Admin can delete any session in their org, Dentist can only delete their own
    query = {"_id": ObjectId(session_id)}
    if role == UserRole.ORG_ADMIN:
        query["organization_id"] = current_user.get("organization_id")
    else:
        query["user_id"] = user_id

    doc = await db["dental_voice_sessions"].find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found or permission denied")

    # Delete audio blob if it exists
    blob_name = doc.get("audio_blob_name")
    if blob_name:
        try:
            await delete_blob(blob_name)
            logger.info(f"[VOICE] Deleted audio blob: {blob_name}")
        except Exception as e:
            logger.error(f"[VOICE] Failed to delete audio blob {blob_name}: {e}")

    # Delete from database
    await db["dental_voice_sessions"].delete_one({"_id": ObjectId(session_id)})
    logger.info(f"[VOICE] Deleted session record: {session_id}")

    from app.background.report_processor import log_audit_event
    await log_audit_event(
        actor_id=user_id,
        action="REPORT_DELETE",
        resource="dental_voice_sessions",
        resource_id=session_id,
        metadata={"actor_email": current_user.get("email")},
        organization_id=current_user.get("organization_id")
    )

    return api_response(
        success=True,
        message="Session and audio blob deleted successfully."
    )


@router.delete("/sessions/{session_id}/reports/{template_id}")
async def delete_session_report(
    session_id: str,
    template_id: str,
    current_user=Depends(require_permission("sessions:edit")),
):
    """
    Delete a specific report (by template_id) from a session with multiple generated reports.
    Updates the session's template_ids list and recalculates the combined soap_note.
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")

    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    query = {"_id": ObjectId(session_id)}
    if role == UserRole.DENTIST:
        query["user_id"] = user_id
    elif role == UserRole.ORG_ADMIN:
        query["organization_id"] = current_user.get("organization_id")
    
    session = await db["dental_voice_sessions"].find_one(query)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or permission denied")

    reports = session.get("reports", [])
    template_ids = session.get("template_ids", [])

    # Filter out the deleted report
    new_reports = [r for r in reports if str(r.get("template_id")) != template_id]
    
    if len(new_reports) == len(reports):
        raise HTTPException(status_code=404, detail="Report not found in this session.")

    new_template_ids = [tid for tid in template_ids if str(tid) != template_id]

    # Recalculate soap_note
    if new_reports:
        combined_reports = []
        for r in new_reports:
            name = r.get("template_name", "Report")
            body = r.get("report", "")
            combined_reports.append(f"# {name}\n{body}")
        new_soap_note = "\n\n---\n\n".join(combined_reports)
    else:
        new_soap_note = ""

    from datetime import datetime, timezone
    update_data = {
        "reports": new_reports,
        "template_ids": new_template_ids,
        "soap_note": new_soap_note,
        "updated_at": datetime.now(timezone.utc)
    }

    await db["dental_voice_sessions"].update_one(
        {"_id": ObjectId(session_id)},
        {"$set": update_data}
    )
    
    from app.background.report_processor import log_audit_event
    await log_audit_event(
        actor_id=user_id,
        action="REPORT_EDIT",
        resource="dental_voice_sessions",
        resource_id=session_id,
        metadata={"actor_email": current_user.get("email"), "action": "deleted_specific_report", "template_id": template_id},
        organization_id=current_user.get("organization_id")
    )

    return api_response(
        success=True,
        message="Specific report deleted successfully."
    )


# ── Session Editing (SOAP Note / Summary) ────────────────────────────────────

@router.patch("/sessions/{session_id}")
async def edit_session(
    session_id: str,
    body: SessionEditRequest,
    current_user=Depends(require_permission("sessions:edit")),
):
    """
    Edit a session's SOAP note, patient summary, and/or staff note.

    - Dentist/Admin: can edit only their own sessions.
    - Staff: can edit any session.

    SOAP note / summary edits are recorded in `edit_history` with original value,
    new value, editor info, and timestamp.

    Staff note behavior:
      - `staff_note` only → adds a new note to `staff_notes` array.
      - `staff_note` + `note_id` → edits the existing note (only by original author).
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")
    editor_name = current_user.get("full_name", "Unknown")

    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    if (
        body.soap_note is None
        and body.patient_summary is None
        and body.staff_note is None
        and body.ghostwriter_note is None
        and body.completed_today is None
        and body.planned_future is None
        and body.is_verified is None
        and body.is_billed is None
        and body.reports is None
        and body.xray_selection is None
    ):
        raise HTTPException(
            status_code=400,
            detail="Provide at least one field to update (soap_note, patient_summary, staff_note, ghostwriter_note, completed_today, planned_future, is_verified, is_billed, reports, or xray_selection)"
        )

    # Build access query
    query = {"_id": ObjectId(session_id)}
    if role in (UserRole.STAFF, UserRole.ORG_ADMIN, UserRole.FRONT_OFFICE):
        query["organization_id"] = current_user.get("organization_id")
    elif role != UserRole.ADMIN:
        query["user_id"] = user_id

    doc = await db["dental_voice_sessions"].find_one(query)
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found or permission denied")

    now = datetime.now(timezone.utc)
    update_set = {"updated_at": now}
    edit_entries = []
    fields_updated = []

    # Track reports edit
    if body.reports is not None:
        original_reports = doc.get("reports", [])
        import json as _json
        update_set["reports"] = [r.dict() for r in body.reports]
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "reports",
            "original_value": _json.dumps(original_reports),
            "new_value": _json.dumps([r.dict() for r in body.reports]),
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("reports")

        # Reconstruct the combined soap_note from individual reports
        combined_reports = []
        for r in body.reports:
            name = r.template_name or "Report"
            report_body = r.report or ""
            combined_reports.append(f"# {name}\n{report_body}")

        new_soap_note = "\n\n---\n\n".join(combined_reports)
        original_soap = doc.get("soap_note", "")
        if isinstance(original_soap, dict):
            original_soap = _json.dumps(original_soap)

        update_set["soap_note"] = new_soap_note
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "soap_note",
            "original_value": original_soap,
            "new_value": new_soap_note,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("soap_note")

    # Track SOAP note edit
    if body.soap_note is not None and body.reports is None:
        original_soap = doc.get("soap_note", "")
        if isinstance(original_soap, dict):
            import json as _json
            original_soap = _json.dumps(original_soap)
        update_set["soap_note"] = body.soap_note
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "soap_note",
            "original_value": original_soap,
            "new_value": body.soap_note,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("soap_note")

    # Track patient summary edit
    if body.patient_summary is not None:
        original_summary = doc.get("patient_summary", "")
        if isinstance(original_summary, dict):
            import json as _json
            original_summary = _json.dumps(original_summary)
        update_set["patient_summary"] = body.patient_summary
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "patient_summary",
            "original_value": original_summary,
            "new_value": body.patient_summary,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("patient_summary")

    # Handle staff note edit
    if body.staff_note is not None:
        original_staff_note = doc.get("staff_note", "")
        if isinstance(original_staff_note, dict):
            import json as _json
            original_staff_note = _json.dumps(original_staff_note)
        update_set["staff_note"] = body.staff_note
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "staff_note",
            "original_value": original_staff_note,
            "new_value": body.staff_note,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("staff_note")

    # Track Ghostwriter note edit
    if body.ghostwriter_note is not None:
        original_gw = doc.get("ghostwriter_note", "")
        if isinstance(original_gw, dict):
            import json as _json
            original_gw = _json.dumps(original_gw)
        update_set["ghostwriter_note"] = body.ghostwriter_note
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "ghostwriter_note",
            "original_value": original_gw,
            "new_value": body.ghostwriter_note,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("ghostwriter_note")

    # Track completed today procedures edit
    if body.completed_today is not None:
        original_completed = doc.get("completed_today", [])
        update_set["completed_today"] = body.completed_today
        import json as _json
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "completed_today",
            "original_value": _json.dumps(original_completed),
            "new_value": _json.dumps(body.completed_today),
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("completed_today")
        if "cdt_codes" not in fields_updated:
            fields_updated.append("cdt_codes")

    # Track planned future procedures edit
    if body.planned_future is not None:
        original_planned = doc.get("planned_future", [])
        update_set["planned_future"] = body.planned_future
        import json as _json
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "planned_future",
            "original_value": _json.dumps(original_planned),
            "new_value": _json.dumps(body.planned_future),
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("planned_future")
        if "cdt_codes" not in fields_updated:
            fields_updated.append("cdt_codes")

    # Track x-ray selection edit
    if body.xray_selection is not None:
        original_xray = doc.get("xray_selection")
        update_set["xray_selection"] = body.xray_selection
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "xray_selection",
            "original_value": original_xray,
            "new_value": body.xray_selection,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("xray_selection")

    # Handle verification edit
    if body.is_verified is not None:
        original_verification = doc.get("is_verified")
        update_set["is_verified"] = body.is_verified
        edit_entries.append({
            "edit_id": str(_uuid_mod.uuid4()),
            "field": "is_verified",
            "original_value": original_verification,
            "new_value": body.is_verified,
            "edited_by_id": user_id,
            "edited_by_name": editor_name,
            "edited_by_role": role,
            "edited_at": now,
        })
        fields_updated.append("is_verified")
        
        # Unverifying a session automatically unbills it
        if body.is_verified is False and doc.get("is_billed") is True:
            update_set["is_billed"] = False
            edit_entries.append({
                "edit_id": str(_uuid_mod.uuid4()),
                "field": "is_billed",
                "original_value": True,
                "new_value": False,
                "edited_by_id": user_id,
                "edited_by_name": editor_name,
                "edited_by_role": role,
                "edited_at": now,
            })
            fields_updated.append("is_billed")

    # Handle billing edit
    if body.is_billed is not None:
        if role not in (UserRole.FRONT_OFFICE, UserRole.STAFF, UserRole.ORG_ADMIN, UserRole.SUPER_ADMIN):
            raise HTTPException(status_code=403, detail="Permission denied to update billing status")
            
        is_verified_now = update_set.get("is_verified", doc.get("is_verified"))
        if body.is_billed is True and not is_verified_now:
            raise HTTPException(status_code=400, detail="Cannot mark as billed because the session is not verified")
            
        original_billing = doc.get("is_billed")
        if original_billing != body.is_billed:
            update_set["is_billed"] = body.is_billed
            edit_entries.append({
                "edit_id": str(_uuid_mod.uuid4()),
                "field": "is_billed",
                "original_value": original_billing,
                "new_value": body.is_billed,
                "edited_by_id": user_id,
                "edited_by_name": editor_name,
                "edited_by_role": role,
                "edited_at": now,
            })
            if "is_billed" not in fields_updated:
                fields_updated.append("is_billed")

    # Build atomic update operations
    update_ops = {"$set": update_set}
    if edit_entries:
        update_ops.setdefault("$push", {})["edit_history"] = {"$each": edit_entries}

    await db["dental_voice_sessions"].update_one({"_id": ObjectId(session_id)}, update_ops)
    logger.info(f"[VOICE] Session {session_id} updated by {editor_name} ({role}): {fields_updated}")

    from app.background.report_processor import log_audit_event
    await log_audit_event(
        actor_id=user_id,
        action="REPORT_EDIT",
        resource="dental_voice_sessions",
        resource_id=session_id,
        metadata={"actor_email": current_user.get("email"), "fields_updated": fields_updated},
        organization_id=current_user.get("organization_id")
    )
    
    if "soap_note" in fields_updated:
        await log_audit_event(
            actor_id=user_id,
            action="SOAP_NOTE_MODIFIED",
            resource="dental_voice_sessions",
            resource_id=session_id,
            metadata={"actor_email": current_user.get("email")},
            organization_id=current_user.get("organization_id")
        )
        
    if "cdt_codes" in fields_updated:
        await log_audit_event(
            actor_id=user_id,
            action="CDT_REPORT_MODIFIED",
            resource="dental_voice_sessions",
            resource_id=session_id,
            metadata={"actor_email": current_user.get("email")},
            organization_id=current_user.get("organization_id")
        )

    if "ghostwriter_note" in fields_updated:
        await log_audit_event(
            actor_id=user_id,
            action="GHOSTWRITER_NOTE_MODIFIED",
            resource="dental_voice_sessions",
            resource_id=session_id,
            metadata={"actor_email": current_user.get("email")},
            organization_id=current_user.get("organization_id")
        )

    response_data = {
        "session_id": session_id,
        "fields_updated": fields_updated,
        "edited_by": editor_name,
        "edited_at": now.isoformat(),
    }

    return api_response(
        success=True,
        message="Session updated successfully.",
        data=response_data,
    )


# ── Edit History / Audit Trail ───────────────────────────────────────────────

@router.get("/sessions/{session_id}/edit-history")
async def get_edit_history(
    session_id: str,
    current_user=Depends(require_permission("sessions:view")),
):
    """
    Get the full edit history (audit log) for a session.

    Returns all SOAP note and summary edits with original/new values,
    editor info, and timestamps.
    """
    db = await get_database()
    user_id = str(current_user["_id"])
    role = current_user.get("role")

    if not ObjectId.is_valid(session_id):
        raise HTTPException(status_code=400, detail="Invalid session ID format")

    query = {"_id": ObjectId(session_id)}
    if role in (UserRole.STAFF, UserRole.ORG_ADMIN, UserRole.FRONT_OFFICE):
        query["organization_id"] = current_user.get("organization_id")
    else:
        query["user_id"] = user_id

    doc = await db["dental_voice_sessions"].find_one(
        query,
        {"edit_history": 1},
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Session not found or permission denied")

    edit_history = doc.get("edit_history", [])
    # Sort the history so the most recent edits appear first
    edit_history.sort(key=lambda x: x.get("edited_at", datetime.min.replace(tzinfo=timezone.utc)), reverse=True)

    return api_response(
        success=True,
        message="Edit history retrieved.",
        data={
            "session_id": session_id,
            "edit_history": edit_history,
        },
    )


# ── CDT PDF Upload (Completely separate from main RAG pipeline) ──────────────

@router.post("/upload-cdt-pdf", status_code=status.HTTP_201_CREATED)
async def upload_cdt_pdf(
    file: UploadFile = File(...),
    current_user=Depends(require_permission("cdt:upload_pdf")),
):
    """
    Upload a CDT codebook PDF for processing.

    FULLY SEPARATE from the main document/RAG pipeline:
      - Stored in blob prefix: cdt_codebook/ (not admin_doc/)
      - Metadata saved in collection: cdt_documents (not documents)
      - Codes saved in collection: cdt_codes (not chunks)
      - Embeddings pushed to index: dental-cdt-index (not dental-text-index)

    Processing (parse → embed) runs as a background task.
    Admins and dentists can upload the CDT codebook.
    """
    # Validate file type
    if not file.content_type or "pdf" not in file.content_type.lower():
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are accepted for CDT codebook upload.",
        )

    db = await get_database()
    user_id = str(current_user["_id"])

    # ── Read and upload to separate blob prefix ──────────────────────────
    content = await file.read()
    unique_prefix = str(_uuid_mod.uuid4())[:8]
    blob_name = f"cdt_codebook/{unique_prefix}_{file.filename}"

    await upload_blob(blob_name, content, file.content_type)

    # ── Save to separate cdt_documents collection ────────────────────────
    now = datetime.now(timezone.utc)
    cdt_doc = {
        "original_filename": file.filename,
        "blob_name": blob_name,
        "file_type": file.content_type,
        "file_size_bytes": len(content),
        "uploaded_by": user_id,
        "status": DocumentStatus.SAVED,
        "created_at": now,
        "updated_at": now,
    }
    result = await db[COLLECTION_CDT_DOCUMENTS].insert_one(cdt_doc)
    doc_id = str(result.inserted_id)

    # ── Trigger CDT processing as background task ────────────────────────
    
    from app.background.report_processor import log_audit_event
    await log_audit_event(
        actor_id=str(current_user["_id"]),
        action="DOCUMENT_UPLOAD",
        resource="cdt_documents",
        resource_id=doc_id,
        metadata={
            "actor_email": current_user.get("email"),
            "filename": file.filename,
            "document_type": "CDT_CODEBOOK",
            "file_size_bytes": len(content)
        },
        organization_id=current_user.get("organization_id"),
    )

    async def _process_cdt_background(document_id: str, blob: str):
        """Run CDT parse → embed pipeline in background."""
        from app.background.parse_cdt_pdf import parse_cdt_pdf
        from app.background.embed_cdt_codes import embed_cdt_codes
        try:
            print(f"\n{'='*60}", flush=True)
            print(f"🦷 [CDT] Starting CDT codebook processing: {blob}", flush=True)
            print(f"{'='*60}", flush=True)

            await parse_cdt_pdf(document_id, blob)
            await embed_cdt_codes(document_id)

            print(f"{'='*60}", flush=True)
            print(f"✨ [CDT] CDT codebook processing complete!", flush=True)
            print(f"{'='*60}\n", flush=True)
        except Exception as e:
            import logging
            logging.getLogger(__name__).error(f"[CDT] Background processing failed: {e}")
            db_inner = await get_database()
            await db_inner[COLLECTION_CDT_DOCUMENTS].update_one(
                {"_id": ObjectId(document_id)},
                {"$set": {
                    "status": DocumentStatus.FAILED,
                    "error_message": str(e)[:500],
                    "updated_at": datetime.now(timezone.utc),
                }}
            )

    asyncio.create_task(_process_cdt_background(doc_id, blob_name))

    return api_response(
        success=True,
        message="CDT codebook PDF uploaded. Processing started in background.",
        data={
            "document_id": doc_id,
            "blob_name": blob_name,
            "filename": file.filename,
            "status": "processing",
        },
        status_code=status.HTTP_201_CREATED,
    )


# ── WebSocket: Real-time Voice Streaming ─────────────────────────────────────

async def _authenticate_ws(token: str) -> dict:
    """
    Authenticate a WebSocket connection using a JWT token.
    WebSocket can't use HTTP headers, so token comes via query param.
    """
    from jose import jwt as jose_jwt, JWTError, ExpiredSignatureError
    from app.config import settings

    if not token:
        return None

    try:
        payload = jose_jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id = payload.get("sub")
        if not user_id:
            return None

        db = await get_database()
        user = await db["users"].find_one({"_id": ObjectId(user_id)})
        if not user or not user.get("is_active", True) or not user.get("is_verified", False):
            return None
            
        from app.services.permission_service import PermissionService
        role = user.get("role", "")
        has_perm = await PermissionService.has_permission(user, "voice:stream")
        if not has_perm:
            return None

        return user
    except (JWTError, ExpiredSignatureError, Exception):
        return None


@ws_router.websocket("/ws/voice-stream")
async def voice_stream_ws(
    websocket: WebSocket,
    token: str = Query(...),
    stream_id: Optional[str] = Query(None, description="Unique ID to resume streaming"),
    session_name: Optional[str] = Query(None, description="Initial session name"),
    template_ids: Optional[str] = Query(None, description="Comma-separated template IDs"),
):
    """
    WebSocket endpoint for real-time voice streaming.

    Connection: ws://host/api/v1/dental-voice/ws/voice-stream?token=JWT_TOKEN

    Protocol:
      Client sends:
        - Binary frames: raw audio chunks from MediaRecorder
        - Text frames (JSON):
            {"action": "transcribe"}       — get interim transcript from buffered audio
            {"action": "stop"}             — finalize recording, get final transcript, save audio
            {"action": "lookup"}           — run CDT lookup on transcript
            {"action": "lookup_and_save"} — run CDT lookup + save session to DB

      Server sends:
        - Text frames (JSON):
            {"type": "connected",  "message": "Ready to receive audio"}
            {"type": "buffered",   "size": 12345}              — audio chunk received
            {"type": "interim",    "transcript": "..."}
            {"type": "final",      "transcript": "...", "audio_saved": true}
            {"type": "lookup",     "mode": "...", "entries": [...]}
            {"type": "saved",      "session_id": "..."}
            {"type": "error",      "message": "..."}
    """
    # ── Authenticate ─────────────────────────────────────────────────────
    user = await _authenticate_ws(token)
    if not user:
        await websocket.close(code=4001, reason="Authentication failed")
        return

    await websocket.accept()
    user_id = str(user["_id"])
    session = VoiceStreamService(user_id, stream_id, organization_id=user.get("organization_id"))
    if session_name:
        session.session_name = session_name
    if template_ids:
        session.template_ids = [t.strip() for t in template_ids.split(",") if t.strip()]

    await session.initialize()

    logger.info(
        f"[VOICE-WS] Connection opened for user {user_id[:8]} "
        f"(stream_id='{session.stream_id}', session_name='{session.session_name}')"
    )

    await websocket.send_json({
        "type": "connected",
        "message": "Ready to receive audio. Send binary audio frames, then actions.",
    })

    try:
        while True:
            # Receive message (binary audio OR text command)
            message = await websocket.receive()

            if message.get("type") == "websocket.disconnect":
                break

            # ── Binary: audio chunk ──────────────────────────────────
            if "bytes" in message and message["bytes"]:
                await session.add_audio_chunk(message["bytes"])
                await websocket.send_json({
                    "type": "buffered",
                    "size": session.get_buffer_size(),
                })
                continue

            # ── Text: JSON command ───────────────────────────────────
            if "text" in message and message["text"]:
                try:
                    cmd = json.loads(message["text"])
                except json.JSONDecodeError:
                    await websocket.send_json({
                        "type": "error",
                        "message": "Invalid JSON command",
                    })
                    continue

                action = cmd.get("action", "")
                
                # Store session metadata sent in JSON command frame
                if "original_transcript" in cmd:
                    session.original_transcript = cmd["original_transcript"]
                if "session_name" in cmd and cmd["session_name"] is not None:
                    session.session_name = cmd["session_name"]
                if "template_ids" in cmd and cmd["template_ids"] is not None:
                    session.template_ids = cmd["template_ids"]
                elif "template_id" in cmd and cmd["template_id"] is not None:
                    session.template_ids = [cmd["template_id"]]
                if "xray_selection" in cmd and cmd["xray_selection"] is not None:
                    xray = cmd["xray_selection"]
                    session.xray_selection = [xray] if isinstance(xray, str) else xray

                # ── TRANSCRIBE: interim transcript ───────────────────
                if action == "transcribe":
                    try:
                        transcript = await session.transcribe_buffer()
                        await websocket.send_json({
                            "type": "interim",
                            "transcript": transcript,
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Transcription failed: {str(e)[:200]}",
                        })

                # ── STOP: finalize recording ─────────────────────────
                elif action == "stop":
                    try:
                        logger.info(
                            f"[VOICE-WS] Finalizing recording for session_name='{session.session_name}', "
                            f"stream_id='{session.stream_id}', user='{user_id[:8]}'"
                        )
                        transcript = await session.finalize()
                        await websocket.send_json({
                            "type": "final",
                            "transcript": transcript,
                            "audio_saved": session.audio_blob_name is not None,
                        })
                    except Exception as e:
                        logger.error(f"[VOICE-WS] Finalization failed for session_name='{session.session_name}': {e}")
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Finalization failed: {str(e)[:200]}",
                        })

                # ── LOOKUP: CDT code search ──────────────────────────
                elif action == "lookup":
                    try:
                        result = await session.lookup_cdt()
                        await websocket.send_json({
                            "type": "lookup",
                            "mode": result.get("mode", ""),
                            "entries": result.get("entries", []),
                            "transcript": session.transcript,
                        })
                    except Exception as e:
                        await websocket.send_json({
                            "type": "error",
                            "message": f"CDT lookup failed: {str(e)[:200]}",
                        })

                # ── LOOKUP_AND_SAVE: CDT lookup + save session ───────
                elif action == "lookup_and_save":
                    try:
                        session_name_val = cmd.get("session_name") or session.session_name
                        template_ids = cmd.get("template_ids") or session.template_ids
                        xray_selection = cmd.get("xray_selection") or session.xray_selection
                        # Backward compat: normalize string to list
                        if isinstance(xray_selection, str):
                            xray_selection = [xray_selection]
                        if template_ids is None and cmd.get("template_id") is not None:
                            template_ids = [cmd.get("template_id")]
                        
                        try:
                            result = await session.lookup_cdt()
                        except Exception as cdt_err:
                            logger.error(f"[VOICE-WS] CDT lookup failed during lookup_and_save: {cdt_err}", exc_info=True)
                            result = {"mode": "error_fallback", "entries": []}

                        session_id = await session.save_session(
                            result,
                            session_name=session_name_val,
                            template_ids=template_ids,
                            xray_selection=xray_selection
                        )
                        logger.info(
                            f"[VOICE-WS] lookup_and_save complete for session_id='{session_id}', "
                            f"session_name='{session_name_val}', user='{user_id[:8]}'"
                        )
                        
                        # Audit log: Report creation (using tracked task to avoid GC destruction)
                        from app.background.report_processor import log_audit_event
                        _track_background_task(log_audit_event(
                            actor_id=user_id,
                            action="REPORT_CREATE",
                            resource="dental_voice_sessions",
                            resource_id=session_id,
                            metadata={"actor_email": user.get("email"), "session_name": session_name_val},
                            organization_id=user.get("organization_id")
                        ))
                        
                        # Spawn background task to generate SOAP note and summary (tracked task)
                        _track_background_task(session.run_soap_background(
                            session_id=session_id, 
                            transcript=session.raw_transcript, 
                            user_id=user_id,
                            template_ids=template_ids,
                            org_id=user.get("organization_id"),
                            xray_selection=xray_selection,
                        ))
                        
                        # Generate SAS URL for the audio blob
                        audio_url = None
                        if session.audio_blob_name:
                            from app.utils.file_utils import generate_sas_url
                            audio_url = generate_sas_url(session.audio_blob_name)

                        await websocket.send_json({
                            "type": "saved",
                            "session_id": session_id,
                            "status": "transcripted",
                            "audio_url": audio_url,
                            "mode": result.get("mode", ""),
                            "entries": result.get("entries", []),
                            "transcript": session.cdt_transcript,
                        })
                    except Exception as e:
                        logger.error(f"[VOICE-WS] lookup_and_save failed: {e}", exc_info=True)
                        if getattr(session, "session_id", None):
                            try:
                                db = await get_database()
                                from bson import ObjectId
                                from datetime import datetime, timezone
                                await db["dental_voice_sessions"].update_one(
                                    {"_id": ObjectId(session.session_id)},
                                    {"$set": {
                                        "status": "failed",
                                        "processing_stage": "ai_failed",
                                        "error_message": f"Lookup/save failed: {e}",
                                        "updated_at": datetime.now(timezone.utc)
                                    }}
                                )
                            except Exception as db_err:
                                logger.error(f"[VOICE-WS] Failed to mark session {session.session_id} as failed in DB: {db_err}")

                        await websocket.send_json({
                            "type": "error",
                            "message": f"Lookup/save failed: {str(e)[:200]}",
                        })

                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": f"Unknown action: {action}",
                    })

    except WebSocketDisconnect:
        logger.info(f"[VOICE-WS] Connection closed for user {user_id[:8]}")
    except Exception as e:
        logger.error(f"[VOICE-WS] Unexpected error: {e}")
        try:
            await websocket.send_json({
                "type": "error",
                "message": f"Server error: {str(e)[:200]}",
            })
        except Exception:
            pass
    finally:
        logger.info(
            f"[VOICE-WS] Session ended for user {user_id[:8]}: "
            f"{session.get_buffer_size()} bytes audio, "
            f"{len(session.transcript)} chars transcript"
        )
