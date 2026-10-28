"""
XLSX Image Parser + Chunker — Combined Phase 1+2.

No AI parsing needed — clinical data is already in metadata.
Creates one enriched chunk per image with full lineage.
"""
import logging
from datetime import datetime, timezone

from bson import ObjectId

from app.core.database import get_database
from app.core.constants import DocumentStatus

logger = logging.getLogger(__name__)


async def parse_and_chunk_image(
    document_id: str,
    metadata: dict,
    blob_name: str,
) -> None:
    """
    Parse + chunk an XLSX-backed image in one step.

    metadata contains: Image Name, Description, Symptoms, Treatment, xlsx_filename
    """
    db = await get_database()

    # ── Build enriched content from metadata ───────────────────────────────
    image_name = metadata.get("Image Name", "")
    description = metadata.get("Description", "")
    symptoms = metadata.get("Symptoms", "")
    treatment = metadata.get("Treatment", "")
    xlsx_filename = metadata.get("xlsx_filename", "")

    content_parts = []
    if image_name:
        content_parts.append(f"Image Name: {image_name}")
    if description:
        content_parts.append(f"Description: {description}")
    if symptoms:
        content_parts.append(f"Symptoms: {symptoms}")
    if treatment:
        content_parts.append(f"Treatment: {treatment}")

    content = "\n".join(content_parts)
    nearby_text = ""
    if symptoms:
        nearby_text += f"Symptoms: {symptoms}"
    if treatment:
        nearby_text += f"\nTreatment: {treatment}" if nearby_text else f"Treatment: {treatment}"

    # Build structured embedding_text for vector search
    embedding_text_parts = [f"Chunk type: image"]
    if image_name:
        embedding_text_parts.append(f"Image name: {image_name}")
    embedding_text_parts.append("Content:")
    embedding_text_parts.append(content)
    embedding_text = "\n".join(embedding_text_parts)

    # ── Create chunk ───────────────────────────────────────────────────────
    chunk = {
        "chunk_id": f"{document_id}_chunk_0",
        "document_id": document_id,
        "sequence": 0,
        "source_type": "xlsx",
        "source_filename": xlsx_filename,  # lineage → the XLSX spreadsheet
        "chunk_type": "image",
        "content": content,
        "embedding_text": embedding_text,
        "page_number": None,
        "section_heading": image_name,  # e.g. "2b"
        "figure_id": None,
        "table_id": None,
        "sheet_name": None,
        "row_number": None,
        "blob_name": blob_name,  # the image's blob path
        "caption": description,
        "nearby_text": nearby_text,
        "embedding_model": None,
        "embedding": None,
        "created_at": datetime.now(timezone.utc),
    }

    await db["chunks"].insert_one(chunk)

    # ── Update document status ─────────────────────────────────────────────
    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {
            "status": DocumentStatus.CHUNKED,
            "chunk_count": 1,
            "updated_at": datetime.now(timezone.utc),
        }}
    )

    logger.info(f"✅ Image chunked: {image_name} → 1 chunk for doc {document_id}")
