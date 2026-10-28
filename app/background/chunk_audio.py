"""
Audio Chunker — Phase 2: Split transcript into RAG-ready text chunks.

Uses sentence-aware token splitter (~350-600 tokens, 50 overlap).
"""
import logging
from datetime import datetime, timezone

from bson import ObjectId

from app.core.database import get_database
from app.core.constants import DocumentStatus
from app.utils.text_splitter import split_text

logger = logging.getLogger(__name__)


async def chunk_audio(document_id: str) -> None:
    """
    Phase 2: Chunk the audio transcript into ~350-600 token text chunks.

    - Loads extracted_text from the document
    - Splits using sentence-aware token splitter
    - Saves chunks to chunks collection
    - Status → "chunked"
    """
    db = await get_database()

    # ── Load transcript ────────────────────────────────────────────────────
    doc = await db["documents"].find_one({"_id": ObjectId(document_id)})
    if not doc:
        raise RuntimeError(f"Document {document_id} not found")

    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {"status": DocumentStatus.CHUNKING, "updated_at": datetime.now(timezone.utc)}}
    )

    transcript = doc.get("extracted_text", "")
    source_filename = doc.get("original_filename", "")

    if not transcript.strip():
        logger.warning(f"Empty transcript for audio doc {document_id}, marking chunked with 0 chunks")
        await db["documents"].update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {"status": DocumentStatus.CHUNKED, "chunk_count": 0, "updated_at": datetime.now(timezone.utc)}}
        )
        return

    # ── Split into chunks ──────────────────────────────────────────────────
    text_chunks = split_text(transcript, max_tokens=500, overlap_tokens=50)

    chunks = []
    for seq, chunk_text in enumerate(text_chunks):
        chunks.append({
            "chunk_id": f"{document_id}_chunk_{seq}",
            "document_id": document_id,
            "sequence": seq,
            "source_type": "audio",
            "source_filename": source_filename,
            "chunk_type": "text",
            "content": chunk_text,
            "page_number": None,
            "section_heading": None,
            "figure_id": None,
            "table_id": None,
            "sheet_name": None,
            "row_number": None,
            "blob_name": None,
            "caption": None,
            "nearby_text": None,
            "embedding_model": None,
            "embedding": None,
            "created_at": datetime.now(timezone.utc),
        })

    if chunks:
        await db["chunks"].insert_many(chunks)

    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {
            "status": DocumentStatus.CHUNKED,
            "chunk_count": len(chunks),
            "updated_at": datetime.now(timezone.utc),
        }}
    )

    logger.info(f"✅ Audio chunked: {len(chunks)} text chunks for doc {document_id}")
