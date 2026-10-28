from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.config import settings
import logging

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect_to_mongo() -> None:
    global _client, _db

    # Skip if still placeholder
    if "YOUR_PASSWORD_HERE" in settings.MONGODB_URI or "<db_password>" in settings.MONGODB_URI:
        logger.warning("[WARNING] MONGODB_URI password not set — skipping DB connection. Replace YOUR_PASSWORD_HERE in .env")
        return

    try:
        _client = AsyncIOMotorClient(
            settings.MONGODB_URI, 
            serverSelectionTimeoutMS=20000,
            maxIdleTimeMS=60000,
            connectTimeoutMS=20000,
        )
        _db = _client[settings.MONGODB_DB_NAME]
        await _db.command("ping")  # Verify connectivity
        # Create indexes
        await _db["users"].create_index("email", unique=True)
        await _db["documents"].create_index("blob_name", unique=True)
        await _db["documents"].create_index("status")
        # Dedup: sparse unique so only docs that have file_hash are constrained
        await _db["documents"].create_index("file_hash", unique=True, sparse=True)
        # Soft-match: for PDF filename lookup
        await _db["documents"].create_index("original_filename")
        await _db["chunks"].create_index("document_id")
        # OTP indexes: TTL auto-deletes expired docs; compound index for fast lookup
        await _db["otps"].create_index("expires_at", expireAfterSeconds=0)
        await _db["otps"].create_index([("email", 1), ("purpose", 1)])
        # Chat & message indexes
        await _db["chats"].create_index([("user_id", 1), ("updated_at", -1)])
        await _db["messages"].create_index([("chat_id", 1), ("created_at", 1)])
        # CDT code indexes
        await _db["cdt_codes"].create_index("code", unique=True)
        await _db["cdt_codes"].create_index("source_document_id")
        # CDT document (PDF metadata) — separate from main documents collection
        await _db["cdt_documents"].create_index("status")
        await _db["cdt_documents"].create_index("created_at")
        # Dental voice session indexes
        await _db["dental_voice_sessions"].create_index([("user_id", 1), ("created_at", -1)])
        await _db["dental_voice_sessions"].create_index("session_type")
        await _db["dental_voice_sessions"].create_index("organization_id")
        await _db["dental_voice_sessions"].create_index([("organization_id", 1), ("created_at", -1), ("is_verified", 1), ("session_type", 1)])
        # ── Multi-tenant: Organizations ──────────────────────────────────
        await _db["organizations"].create_index("name", unique=True)
        await _db["organizations"].create_index("email", unique=True)
        # ── Multi-tenant: Invitations ────────────────────────────────────
        await _db["invitations"].create_index("token", unique=True)
        await _db["invitations"].create_index("expires_at", expireAfterSeconds=0)  # TTL
        # ── Multi-tenant: User → Org lookup ──────────────────────────────
        await _db["users"].create_index("organization_id")
        # ── Audit logs: org-scoped queries ───────────────────────────────
        await _db["audit_logs"].create_index("organization_id")
        await _db["audit_logs"].create_index([("timestamp", -1)])
        # ── Performance: Dashboard Filters ───────────────────────────────
        await _db["messages"].create_index("created_at")
        await _db["users"].create_index("created_at")
        await _db["transaction_logs"].create_index("created_at")
        await _db["transaction_logs"].create_index("user_id")

        logger.info("[SUCCESS] Connected to MongoDB Atlas")
    except Exception as e:
        logger.error(f"[ERROR] MongoDB connection failed: {e}")
        logger.warning("Server started without DB. Fix MONGODB_URI in .env and restart.")


async def close_mongo_connection() -> None:
    global _client
    if _client:
        _client.close()
        print("[DISCONNECTED] MongoDB connection closed")


async def get_database() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised. Call connect_to_mongo() first.")
    return _db
