"""
Background worker: Report processor
Handles post-query logging, usage analytics, and audit trail updates.
This module is intended to be used with FastAPI BackgroundTasks
or a task queue (e.g., Celery / Azure Functions).
"""
from datetime import datetime, timezone
import logging

from app.core.database import get_database
from app.core.constants import COLLECTION_AUDIT_LOGS

logger = logging.getLogger(__name__)


async def log_audit_event(
    actor_id: str,
    action: str,
    resource: str,
    resource_id: str,
    metadata: dict | None = None,
    organization_id: str | None = None,
) -> None:
    """
    Write an immutable audit log entry (SOC 2 / HIPAA requirement).
    actor_id        – user or system performing the action
    action          – e.g. 'LOGIN', 'DOCUMENT_DELETE', 'ROLE_CHANGE', 'USER_DELETE'
    resource        – collection name or resource type
    resource_id     – MongoDB ObjectId string of the affected document
    organization_id – org this event belongs to (None for platform-level events)

    Failures are swallowed so audit errors never break the main request.
    """
    try:
        db = await get_database()
        await db[COLLECTION_AUDIT_LOGS].insert_one(
            {
                "actor_id": actor_id,
                "action": action,
                "resource": resource,
                "resource_id": resource_id,
                "metadata": metadata or {},
                "organization_id": organization_id,
                "timestamp": datetime.now(timezone.utc),
            }
        )
    except Exception as e:
        logger.warning(f"[AUDIT] Failed to write audit log ({action}): {e}")


async def update_usage_stats(user_id: str) -> None:
    """Increment per-user query counter (called as a background task)."""
    db = await get_database()
    await db["users"].update_one(
        {"_id": user_id}, {"$inc": {"query_count": 1}}
    )
