"""
DB-driven processing worker.

Polls MongoDB for documents with status="saved", claims them atomically,
and runs the appropriate parse → chunk → embed pipeline.

Optimized for Standard tier + multi-admin workloads:
  - Higher concurrency (10 simultaneous documents)
  - Adaptive polling (fast when busy, slow when idle)
  - Per-document timing and progress logging
  - On server restart, stuck documents are recovered automatically.
"""
import asyncio
import logging
import time
import traceback
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.core.database import get_database
from app.core.constants import DocumentStatus

logger = logging.getLogger(__name__)

# ── Concurrency & Polling ──────────────────────────────────────────────────────
# S0 Standard tier supports 15 concurrent DI requests.
# Each parse_pdf may use up to 4 concurrent DI calls internally,
# so 10 concurrent documents is safe (burst of large docs auto-throttled by DI semaphore).
MAX_CONCURRENT = 10

POLL_INTERVAL_IDLE = 5       # seconds between polls when no documents found
POLL_INTERVAL_ACTIVE = 1     # seconds between polls when documents are being processed

# ── Wake-up Event ──────────────────────────────────────────────────────────────
# Signalled by the upload route so the worker picks up new documents instantly
# instead of waiting for the next poll cycle.
_new_document_event: asyncio.Event | None = None


def get_new_document_event() -> asyncio.Event:
    """Get (or lazily create) the global wake-up event."""
    global _new_document_event
    if _new_document_event is None:
        _new_document_event = asyncio.Event()
    return _new_document_event


def notify_new_document():
    """Call from upload route to wake the worker immediately."""
    evt = get_new_document_event()
    evt.set()

# XLSX MIME types to skip
XLSX_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}

# Word MIME types to parse using Azure DI
WORD_TYPES = {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


# ── Status helpers ─────────────────────────────────────────────────────────────

async def update_status(document_id: str, status: str, **extra_fields):
    """Update a document's status and any extra fields."""
    db = await get_database()
    update = {
        "status": status,
        "updated_at": datetime.now(timezone.utc),
        **extra_fields,
    }
    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": update}
    )


async def set_failed(document_id: str, error_message: str):
    """Mark a document as failed with an error message."""
    logger.error(f"Document {document_id} failed: {error_message}")
    await update_status(document_id, DocumentStatus.FAILED, error_message=error_message)


# ── Crash Recovery ─────────────────────────────────────────────────────────────

async def recover_stuck_documents():
    """
    Re-queue documents stuck in intermediate states from a previous crash.
    Called once on server startup before the worker loop begins.
    """
    db = await get_database()
    stuck_statuses = [
        DocumentStatus.PARSING,
        DocumentStatus.PARSED,
        DocumentStatus.CHUNKING,
        DocumentStatus.CHUNKED,
        DocumentStatus.EMBEDDING,
    ]
    result = await db["documents"].update_many(
        {"status": {"$in": stuck_statuses}},
        {"$set": {
            "status": DocumentStatus.SAVED,
            "updated_at": datetime.now(timezone.utc),
        }}
    )
    if result.modified_count > 0:
        logger.info(f"[RECOVERED] Recovered {result.modified_count} stuck document(s) back to 'saved'")
        # Also print to terminal for visibility
        print(f"[RECOVERED] Reset {result.modified_count} documents to 'saved' to resume processing.")


# ── Claim next document ───────────────────────────────────────────────────────

async def claim_next_document():
    """
    Atomically find and claim one document for processing.
    Uses find_one_and_update to prevent double-processing across concurrent workers.
    FIFO order (oldest first).
    """
    db = await get_database()
    return await db["documents"].find_one_and_update(
        {
            "status": DocumentStatus.SAVED,
            "file_type": {"$nin": list(XLSX_TYPES)},  # skip XLSX files
        },
        {"$set": {
            "status": DocumentStatus.PARSING,
            "processing_started_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }},
        sort=[("created_at", 1)],  # FIFO
        return_document=ReturnDocument.AFTER,
    )


def _fmt_time(seconds: float) -> str:
    """Format seconds into readable 'X min Y sec' or 'Z sec' string."""
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m} min {s} sec"
    return f"{s} sec"


def _print(msg: str):
    """Print with immediate flush so output appears in real-time during long async ops."""
    print(msg, flush=True)


# ── Pipeline router ───────────────────────────────────────────────────────────

async def run_pipeline(doc: dict):
    """Route document to the correct parse → chunk → embed pipeline."""
    from app.background.parse_pdf import parse_pdf
    from app.background.chunk_pdf import chunk_pdf
    from app.background.parse_image import parse_and_chunk_image
    from app.background.parse_audio import parse_audio
    from app.background.chunk_audio import chunk_audio
    from app.background.embed_chunks import embed_chunks

    doc_id = str(doc["_id"])
    file_type = doc.get("file_type", "")
    metadata = doc.get("metadata", {})
    blob_name = doc.get("blob_name", "")
    filename = doc.get("original_filename", "unknown")

    t_start = time.perf_counter()
    db = await get_database()
    safe_filename = filename.encode('ascii', 'ignore').decode()
    sep = "=" * 60
    _print(sep)
    _print(f"🚀 [STARTED] [{doc_id[:8]}] '{safe_filename}' ({file_type})")
    _print(sep)

    try:
        if file_type == "application/pdf" or file_type in WORD_TYPES:
            # Phase 1: Parse (Selective Resume)
            if not doc.get("parsed_elements"):
                t_phase = time.perf_counter()
                _print(f"🔹 [PHASE 1/3] Parsing document layout...")
                await parse_pdf(doc_id, blob_name)
                parse_elapsed = time.perf_counter() - t_phase
                _print(f"   ∟ ✅ COMPLETED in {_fmt_time(parse_elapsed)}")
            else:
                p_count = len(doc["parsed_elements"].get("paragraphs", []))
                _print(f"   ∟ [RESUME] Skipping Phase 1: Found {p_count} paragraphs in DB.")

            # Phase 2: Chunk (Selective Resume)
            doc_status = doc.get("status", "")
            if doc_status not in [DocumentStatus.CHUNKED.value, DocumentStatus.EMBEDDING.value, DocumentStatus.COMPLETED.value]:
                t_phase = time.perf_counter()
                _print(f"🔹 [PHASE 2/3] Chunking text and figures...")
                await chunk_pdf(doc_id)
                chunk_elapsed = time.perf_counter() - t_phase
                _print(f"   ∟ ✅ COMPLETED in {_fmt_time(chunk_elapsed)}")
            else:
                chunk_count = await db["chunks"].count_documents({"document_id": doc_id})
                _print(f"   ∟ [RESUME] Skipping Phase 2: Document already chunked ({chunk_count} chunks in DB).")

        elif file_type.startswith("image/"):
            t_phase = time.perf_counter()
            _print(f"🔹 [PHASE 1/2] Processing image...")
            await parse_and_chunk_image(doc_id, metadata, blob_name)
            parse_elapsed = time.perf_counter() - t_phase
            _print(f"   ∟ ✅ COMPLETED in {_fmt_time(parse_elapsed)}")

        elif file_type.startswith("audio/"):
            t_phase = time.perf_counter()
            _print(f"🔹 [PHASE 1/3] Transcribing audio...")
            await parse_audio(doc_id, blob_name)
            parse_elapsed = time.perf_counter() - t_phase
            _print(f"   ∟ ✅ COMPLETED in {_fmt_time(parse_elapsed)}")

            t_phase = time.perf_counter()
            _print(f"🔹 [PHASE 2/3] Chunking transcript...")
            await chunk_audio(doc_id)
            chunk_elapsed = time.perf_counter() - t_phase
            _print(f"   ∟ ✅ COMPLETED in {_fmt_time(chunk_elapsed)}")

        elif file_type in XLSX_TYPES or "excel" in file_type:
            _print(f"🔹 [PHASE 1/1] Excel sheet detected...")
            _print(f"   ∟ ✅ COMPLETED in 0s (Data stored in DB, logically marked as completed)")
            await update_status(doc_id, DocumentStatus.COMPLETED)
            return

        else:
            logger.warning(f"Unknown file_type '{file_type}' for doc {doc_id}, skipping.")
            await update_status(doc_id, DocumentStatus.SKIPPED)
            return

        # Convergence Phase: Embed
        t_phase = time.perf_counter()
        _print(f"🔹 [PHASE 3/3] Generating embeddings & pushing to Azure Search...")
        await embed_chunks(doc_id)
        embed_elapsed = time.perf_counter() - t_phase
        _print(f"   ∟ ✅ COMPLETED in {_fmt_time(embed_elapsed)}")

        elapsed_total = time.perf_counter() - t_start
        _print(sep)
        _print(f"✨ [SUCCESS] [{doc_id[:8]}] Finished '{safe_filename}' in {_fmt_time(elapsed_total)} total.")
        _print(sep)

    except Exception as e:
        elapsed_total = time.perf_counter() - t_start
        safe_filename = filename.encode('ascii', 'ignore').decode()
        _print(sep)
        _print(f"❌ [FAILED] [{doc_id[:8]}] Error in '{safe_filename}' after {elapsed_total:.1f}s")
        _print(f"   Message: {str(e)}")
        _print(traceback.format_exc())
        _print(sep)
        try:
            await set_failed(doc_id, str(e)[:500])
        except Exception as db_err:
            _print(f"   ⚠️ Could not mark doc {doc_id[:8]} as failed in DB: {db_err}")


# ── Worker loop ────────────────────────────────────────────────────────────────

async def processing_worker():
    """
    Main worker loop. Runs forever, polling for documents to process.

    Adaptive polling:
      - When documents are found → polls every 1s (fast pickup for batch uploads)
      - When idle → polls every 5s (saves resources)

    Concurrency is capped by the semaphore (10 simultaneous documents).
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    active_tasks: set = set()
    poll_count = 0
    _print(f"[WORKER] Processing worker started (max_concurrent={MAX_CONCURRENT}, S0 Standard tier)")

    async def _run(d, tsk_ref):
        """Execute pipeline under semaphore, then clean up task reference."""
        doc_id = str(d.get("_id", "unknown"))
        try:
            async with semaphore:
                await run_pipeline(d)
        except Exception as pipeline_error:
            _print(f"Pipeline crashed entirely for {doc_id[:8]}: {pipeline_error}")
            _print(traceback.format_exc())
            # Last-resort: ensure document is marked as failed
            try:
                await set_failed(doc_id, f"Outer crash: {str(pipeline_error)[:400]}")
            except Exception:
                _print(f"   ⚠️ CRITICAL: Could not mark {doc_id[:8]} as failed even in outer handler")
        finally:
            active_tasks.discard(tsk_ref[0])

    event = get_new_document_event()

    while True:
        try:
            doc = await claim_next_document()
            if doc:
                doc_id = str(doc["_id"])
                filename = doc.get("original_filename", "unknown")

                # Start processing immediately
                safe_filename = filename.encode('ascii', 'ignore').decode()
                _print(
                    f"[CLAIMED] '{safe_filename}' ({doc_id[:8]}) | "
                    f"Active: {len(active_tasks)}/{MAX_CONCURRENT}"
                )

                ref = [None]  # mutable reference for the task
                task = asyncio.create_task(_run(doc, ref))
                ref[0] = task
                active_tasks.add(task)
                poll_count = 0

                # Use fast polling when documents are being processed
                await asyncio.sleep(POLL_INTERVAL_ACTIVE)
            else:
                # Clean up completed tasks
                active_tasks = {t for t in active_tasks if not t.done()}

                # Wait for either the wake-up signal or the idle timeout
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=POLL_INTERVAL_IDLE)
                    _print("[WORKER] ⚡ Woke up — new document signalled")
                except asyncio.TimeoutError:
                    pass  # normal idle timeout, just poll again

        except PyMongoError as e:
            _print(f"[WORKER] Database connection hiccup, retrying... ({type(e).__name__})")
            await asyncio.sleep(POLL_INTERVAL_IDLE)
        except Exception as e:
            _print(f"[ERROR] Worker loop error: {e}")
            traceback.print_exc()
            logger.exception(f"Worker loop error: {e}")
            await asyncio.sleep(POLL_INTERVAL_IDLE)

