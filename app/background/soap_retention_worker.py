"""
SOAP Report Retention Worker

Runs daily to:
  1. Find dental_voice_sessions older than 30 days
  2. Log SOAP_AUTO_DELETE audit entry for each (preserving metadata before deletion)
  3. Delete the records and associated audio blobs

This is a worker-based approach (rather than MongoDB TTL) so we can
write audit logs BEFORE the records disappear.
"""
import asyncio
import logging
from datetime import datetime, timezone, timedelta

from bson import ObjectId

from app.core.database import get_database
from app.background.report_processor import log_audit_event

logger = logging.getLogger(__name__)

RETENTION_DAYS = 30
CHECK_INTERVAL_HOURS = 24  # Run once a day


async def soap_retention_worker():
    """Background worker that runs daily to clean up expired SOAP reports."""
    logger.info("[SOAP-RETENTION] Worker started — checking every %d hours", CHECK_INTERVAL_HOURS)

    while True:
        try:
            await _cleanup_expired_sessions()
        except Exception as e:
            logger.error(f"[SOAP-RETENTION] Error during cleanup: {e}")

        # Sleep until next check
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 3600)


async def _cleanup_expired_sessions():
    """Find and delete sessions older than RETENTION_DAYS."""
    db = await get_database()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    # 1. Find organizations that have disabled auto-deletion
    excluded_org_cursor = db["organizations"].find(
        {"auto_delete_sessions": False}, {"_id": 1}
    )
    excluded_org_ids = [str(org["_id"]) async for org in excluded_org_cursor]

    # 2. Find all users belonging to those organizations
    excluded_user_ids = []
    if excluded_org_ids:
        user_cursor = db["users"].find(
            {"organization_id": {"$in": excluded_org_ids}}, {"_id": 1}
        )
        excluded_user_ids = [str(u["_id"]) async for u in user_cursor]

    # 3. Build deletion query
    query = {"created_at": {"$lt": cutoff}}
    if excluded_user_ids:
        query["user_id"] = {"$nin": excluded_user_ids}

    # Find all sessions created before the cutoff
    expired_cursor = db["dental_voice_sessions"].find(
        query,
        {
            "_id": 1,
            "user_id": 1,
            "session_name": 1,
            "session_type": 1,
            "audio_blob_name": 1,
            "organization_id": 1,
            "created_at": 1,
        }
    )

    deleted_count = 0
    async for session in expired_cursor:
        session_id = str(session["_id"])

        # Log audit entry BEFORE deletion
        await log_audit_event(
            actor_id="system",
            action="SOAP_AUTO_DELETE",
            resource="dental_voice_sessions",
            resource_id=session_id,
            metadata={
                "session_name": session.get("session_name", ""),
                "session_type": session.get("session_type", ""),
                "user_id": session.get("user_id", ""),
                "created_at": session["created_at"].isoformat() if session.get("created_at") else None,
                "retention_days": RETENTION_DAYS,
                "reason": f"Auto-deleted after {RETENTION_DAYS} days per retention policy",
            },
            organization_id=session.get("organization_id"),
        )

        # Delete audio blob if it exists
        blob_name = session.get("audio_blob_name")
        if blob_name:
            try:
                from app.utils.file_utils import delete_blob
                await delete_blob(blob_name)
                logger.info(f"[SOAP-RETENTION] Deleted audio blob: {blob_name}")
            except Exception as e:
                logger.warning(f"[SOAP-RETENTION] Failed to delete blob {blob_name}: {e}")

        # Delete the session record
        await db["dental_voice_sessions"].delete_one({"_id": session["_id"]})
        deleted_count += 1

    if deleted_count > 0:
        logger.info(f"[SOAP-RETENTION] Deleted {deleted_count} expired session(s) (>{RETENTION_DAYS} days)")
    else:
        logger.info("[SOAP-RETENTION] No expired sessions found")
