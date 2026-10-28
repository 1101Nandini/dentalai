"""
CDT Code Embedder — Generate embeddings for CDT codes and push to Azure AI Search.

Embeds CDT codes from MongoDB (cdt_codes collection) into the dedicated
dental-cdt-index in Azure AI Search.

Embedding strategy:
  - Combines code + description + category into a single embedding text
  - Uses text-embedding-3-large at 1024 dimensions (lean index for structured data)
  - Only processes codes with is_embedded=False (delta-aware)
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from bson import ObjectId
import openai
from openai import AsyncAzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings
from app.core.database import get_database
from app.core.constants import DocumentStatus, COLLECTION_CDT_DOCUMENTS
from app.utils.search_utils import ensure_search_index_exists

def _log_retry_attempt(retry_state):
    logger.warning(
        f"CDT Azure OpenAI embedding call failed (attempt {retry_state.attempt_number}). "
        f"Retrying in {retry_state.next_action.sleep:.1f} seconds. Error: {retry_state.outcome.exception()}"
    )
    print(
        f"   ∟ [RETRY] CDT Azure OpenAI rate limit/transient error (attempt {retry_state.attempt_number}). "
        f"Waiting {retry_state.next_action.sleep:.1f}s before retry...",
        flush=True
    )

@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=4, max=180),
    retry=retry_if_exception_type((
        openai.RateLimitError,
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError
    )),
    before_sleep=_log_retry_attempt,
    reraise=True
)
async def _create_embeddings_with_retry(
    client: AsyncAzureOpenAI,
    texts: list[str],
    model: str,
    dimensions: int
):
    """
    Generate embeddings with exponential backoff on rate limits or transient errors.
    """
    return await client.embeddings.create(
        input=texts,
        model=model,
        dimensions=dimensions
    )

logger = logging.getLogger(__name__)

# CDT embeddings use 1024 dimensions — sufficient for short structured descriptions
CDT_EMBED_DIMENSIONS = 1024
CDT_BATCH_SIZE = 100  # Codes per embedding batch


def _build_embedding_text(code: dict) -> str:
    """Build the text to embed for a CDT code entry."""
    parts = [
        code.get("code", ""),
        code.get("description", ""),
        code.get("category", ""),
    ]
    return " ".join(p for p in parts if p).strip()


async def embed_cdt_codes(document_id: str) -> None:
    """
    Embed all un-embedded CDT codes and push to the dental-cdt-index.

    Steps:
      1. Fetch all CDT codes with is_embedded=False from MongoDB
      2. Generate embeddings in batches using Azure OpenAI
      3. Push to Azure AI Search (dental-cdt-index)
      4. Mark codes as is_embedded=True
      5. Update document status to COMPLETED
    """
    db = await get_database()

    await db[COLLECTION_CDT_DOCUMENTS].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {"status": DocumentStatus.EMBEDDING, "updated_at": datetime.now(timezone.utc)}}
    )

    # ── 1. Fetch un-embedded codes ───────────────────────────────────────
    pending_codes = await db["cdt_codes"].find(
        {"is_embedded": {"$ne": True}, "is_active": True}
    ).to_list(length=None)

    if not pending_codes:
        print(f"   ∟ [CDT-EMBED] No pending CDT codes to embed. Marking complete.", flush=True)
        await db[COLLECTION_CDT_DOCUMENTS].update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {"status": DocumentStatus.COMPLETED, "updated_at": datetime.now(timezone.utc)}}
        )
        return

    print(f"   ∟ [CDT-EMBED] Embedding {len(pending_codes)} CDT codes...", flush=True)
    t_start = time.perf_counter()

    # ── 2. Generate embeddings in batches ─────────────────────────────────
    oai_client = AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_EMBEDDING_ENDPOINT,
        api_key=settings.AZURE_OPENAI_EMBEDDING_KEY,
        api_version="2023-05-15",
    )

    embedded_codes = []

    for i in range(0, len(pending_codes), CDT_BATCH_SIZE):
        batch = pending_codes[i:i + CDT_BATCH_SIZE]
        texts = [_build_embedding_text(c) for c in batch]

        try:
            response = await _create_embeddings_with_retry(
                client=oai_client,
                texts=texts,
                model=settings.OPENAI_EMBEDDING_MODEL,
                dimensions=CDT_EMBED_DIMENSIONS,
            )

            for j, embedding_data in enumerate(response.data):
                batch[j]["embedding"] = embedding_data.embedding
                batch[j]["embedding_text"] = texts[j]
                embedded_codes.append(batch[j])

            # Small delay to avoid TPM spikes
            await asyncio.sleep(1.0)

        except Exception as e:
            logger.error(f"[CDT-EMBED] Batch embedding failed at offset {i}: {e}")
            continue

    print(
        f"   ∟ [CDT-EMBED] Generated {len(embedded_codes)} embeddings "
        f"({time.perf_counter() - t_start:.1f}s)",
        flush=True,
    )

    if not embedded_codes:
        await db[COLLECTION_CDT_DOCUMENTS].update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {
                "status": DocumentStatus.FAILED,
                "error_message": "All embedding batches failed",
                "updated_at": datetime.now(timezone.utc),
            }}
        )
        return

    # ── 3. Push to Azure AI Search ────────────────────────────────────────
    index_name = settings.AZURE_SEARCH_INDEX_CDT
    await ensure_search_index_exists(index_name)

    search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
    search_key = settings.AZURE_SEARCH_KEY
    api_version = "2024-07-01"

    search_docs = []
    for code in embedded_codes:
        search_docs.append({
            "@search.action": "mergeOrUpload",
            "code": code["code"],
            "description": code.get("description", ""),
            "category": code.get("category", ""),
            "embedding_text": code.get("embedding_text", ""),
            "source_document_id": code.get("source_document_id", ""),
            "content_vector": code["embedding"],
        })

    url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version={api_version}"
    successful_codes = set()

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        # Push in sub-batches of 200
        for i in range(0, len(search_docs), 200):
            sub_batch = search_docs[i:i + 200]
            try:
                response = await http_client.post(
                    url,
                    headers={"Content-Type": "application/json", "api-key": search_key},
                    json={"value": sub_batch},
                )

                if response.status_code in (200, 207):
                    resp_data = response.json()
                    for item in resp_data.get("value", []):
                        if item.get("status"):
                            successful_codes.add(item.get("key"))
                        else:
                            logger.error(
                                f"[CDT-EMBED] Code {item.get('key')} failed to index: "
                                f"{item.get('errorMessage')}"
                            )
                else:
                    logger.error(
                        f"[CDT-EMBED] Search push failed: {response.status_code} - {response.text}"
                    )
            except Exception as e:
                logger.error(f"[CDT-EMBED] Search push failed for batch at {i}: {e}")

    print(
        f"   ∟ [CDT-EMBED] Pushed {len(successful_codes)} codes to Azure AI Search index '{index_name}'",
        flush=True,
    )

    # ── 4. Mark codes as embedded in MongoDB ─────────────────────────────
    now = datetime.now(timezone.utc)
    if successful_codes:
        from pymongo import UpdateOne
        bulk_ops = [
            UpdateOne(
                {"code": code_id},
                {"$set": {"is_embedded": True, "embedded_at": now}}
            )
            for code_id in successful_codes
        ]
        await db["cdt_codes"].bulk_write(bulk_ops)

    # ── 5. Update document status ────────────────────────────────────────
    embed_elapsed = time.perf_counter() - t_start
    await db[COLLECTION_CDT_DOCUMENTS].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {
            "status": DocumentStatus.COMPLETED,
            "embed_time_seconds": round(embed_elapsed, 1),
            "updated_at": now,
        }}
    )

    print(
        f"   ∟ [CDT-EMBED] Complete: {len(successful_codes)} codes embedded and indexed "
        f"({embed_elapsed:.1f}s total)",
        flush=True,
    )
