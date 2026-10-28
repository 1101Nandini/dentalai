"""
Case Study Embedder — Generate embeddings for case studies and push to Azure AI Search.

Embeds case studies from MongoDB (case_study collection) into the dedicated
dental-casestudy-index in Azure AI Search.

Embedding strategy:
  - Combines title + content into a single embedding text
  - Uses text-embedding-3-large at 3072 dimensions (same as text index for best accuracy)
  - Only processes case studies with is_embedded=False (delta-aware)
  - Can embed a single case study (for auto-embed on create) or all pending
"""
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from bson import ObjectId
import openai
import markdown
from bs4 import BeautifulSoup
from openai import AsyncAzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings
from app.core.database import get_database
from app.core.constants import COLLECTION_CASE_STUDY
from app.utils.search_utils import ensure_search_index_exists

logger = logging.getLogger(__name__)

CASE_STUDY_EMBED_DIMENSIONS = 3072
CASE_STUDY_BATCH_SIZE = 50


def _log_retry_attempt(retry_state):
    logger.warning(
        f"Case Study Azure OpenAI embedding call failed (attempt {retry_state.attempt_number}). "
        f"Retrying in {retry_state.next_action.sleep:.1f} seconds. Error: {retry_state.outcome.exception()}"
    )


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=2, min=4, max=180),
    retry=retry_if_exception_type((
        openai.RateLimitError,
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError,
    )),
    before_sleep=_log_retry_attempt,
    reraise=True,
)
async def _create_embeddings_with_retry(
    client: AsyncAzureOpenAI,
    texts: list[str],
    model: str,
    dimensions: int,
):
    """Generate embeddings with exponential backoff on rate limits or transient errors."""
    return await client.embeddings.create(
        input=texts,
        model=model,
        dimensions=dimensions,
    )


def strip_markdown(md_text: str) -> str:
    """Strip markdown formatting to return plain text for embeddings."""
    if not md_text:
        return ""
    html = markdown.markdown(md_text)
    return BeautifulSoup(html, "html.parser").get_text(separator=" ").strip()

def _build_embedding_text(case: dict) -> str:
    """Build the text to embed for a case study entry."""
    parts = [
        case.get("title", ""),
        strip_markdown(case.get("content", "")),
    ]
    return " ".join(p for p in parts if p).strip()


async def embed_single_case_study(case_id: str) -> bool:
    """
    Embed a single case study by its MongoDB _id and push to Azure AI Search.
    Used for auto-embed on create/update.
    
    Returns True if successful, False otherwise.
    """
    db = await get_database()
    doc = await db[COLLECTION_CASE_STUDY].find_one({"_id": ObjectId(case_id)})
    if not doc:
        logger.error(f"[CASE-EMBED] Case study {case_id} not found")
        return False

    embedding_text = _build_embedding_text(doc)

    oai_client = AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_EMBEDDING_ENDPOINT,
        api_key=settings.AZURE_OPENAI_EMBEDDING_KEY,
        api_version="2023-05-15",
    )

    try:
        response = await _create_embeddings_with_retry(
            client=oai_client,
            texts=[embedding_text],
            model=settings.OPENAI_EMBEDDING_MODEL,
            dimensions=CASE_STUDY_EMBED_DIMENSIONS,
        )
        embedding = response.data[0].embedding
    except Exception as e:
        logger.error(f"[CASE-EMBED] Embedding failed for case {case_id}: {e}")
        return False

    # Push to Azure AI Search
    index_name = settings.AZURE_SEARCH_INDEX_CASE_STUDY
    await ensure_search_index_exists(index_name)

    search_doc = {
        "@search.action": "mergeOrUpload",
        "case_id": doc.get("case_id", str(doc["_id"])),
        "title": doc.get("title", ""),
        "content": doc.get("content", ""),
        "embedding_text": embedding_text,
        "content_vector": embedding,
    }

    search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
    url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version=2024-07-01"

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        try:
            resp = await http_client.post(
                url,
                headers={"Content-Type": "application/json", "api-key": settings.AZURE_SEARCH_KEY},
                json={"value": [search_doc]},
            )
            if resp.status_code in (200, 207):
                resp_data = resp.json()
                for item in resp_data.get("value", []):
                    if item.get("status"):
                        await db[COLLECTION_CASE_STUDY].update_one(
                            {"_id": ObjectId(case_id)},
                            {"$set": {"is_embedded": True, "embedded_at": datetime.now(timezone.utc)}},
                        )
                        logger.info(f"[CASE-EMBED] Successfully embedded case study: {doc.get('case_id')}")
                        return True
                    else:
                        logger.error(f"[CASE-EMBED] Index error: {item.get('errorMessage')}")
            else:
                logger.error(f"[CASE-EMBED] Search push failed: {resp.status_code} - {resp.text}")
        except Exception as e:
            logger.error(f"[CASE-EMBED] Search push failed: {e}")

    return False


async def embed_all_case_studies() -> dict:
    """
    Embed all un-embedded case studies and push to the dental-casestudy-index.

    Steps:
      1. Fetch all case studies with is_embedded!=True from MongoDB
      2. Generate embeddings in batches using Azure OpenAI
      3. Push to Azure AI Search (dental-casestudy-index)
      4. Mark case studies as is_embedded=True

    Returns a summary dict with counts.
    """
    db = await get_database()
    t_start = time.perf_counter()

    # 1. Fetch un-embedded case studies
    pending = await db[COLLECTION_CASE_STUDY].find(
        {"is_embedded": {"$ne": True}}
    ).to_list(length=None)

    if not pending:
        logger.info("[CASE-EMBED] No pending case studies to embed.")
        return {"total": 0, "embedded": 0, "failed": 0}

    logger.info(f"[CASE-EMBED] Embedding {len(pending)} case studies...")

    # 2. Generate embeddings in batches
    oai_client = AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_EMBEDDING_ENDPOINT,
        api_key=settings.AZURE_OPENAI_EMBEDDING_KEY,
        api_version="2023-05-15",
    )

    embedded_cases = []

    for i in range(0, len(pending), CASE_STUDY_BATCH_SIZE):
        batch = pending[i:i + CASE_STUDY_BATCH_SIZE]
        texts = [_build_embedding_text(c) for c in batch]

        try:
            response = await _create_embeddings_with_retry(
                client=oai_client,
                texts=texts,
                model=settings.OPENAI_EMBEDDING_MODEL,
                dimensions=CASE_STUDY_EMBED_DIMENSIONS,
            )

            for j, embedding_data in enumerate(response.data):
                batch[j]["embedding"] = embedding_data.embedding
                batch[j]["embedding_text"] = texts[j]
                embedded_cases.append(batch[j])

            await asyncio.sleep(1.0)  # Avoid TPM spikes

        except Exception as e:
            logger.error(f"[CASE-EMBED] Batch embedding failed at offset {i}: {e}")
            continue

    logger.info(
        f"[CASE-EMBED] Generated {len(embedded_cases)} embeddings "
        f"({time.perf_counter() - t_start:.1f}s)"
    )

    if not embedded_cases:
        return {"total": len(pending), "embedded": 0, "failed": len(pending)}

    # 3. Push to Azure AI Search
    index_name = settings.AZURE_SEARCH_INDEX_CASE_STUDY
    await ensure_search_index_exists(index_name)

    search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
    url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version=2024-07-01"

    search_docs = []
    for case in embedded_cases:
        search_docs.append({
            "@search.action": "mergeOrUpload",
            "case_id": case.get("case_id", str(case["_id"])),
            "title": case.get("title", ""),
            "content": case.get("content", ""),
            "embedding_text": case.get("embedding_text", ""),
            "content_vector": case["embedding"],
        })

    successful_ids = set()
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        for i in range(0, len(search_docs), 200):
            sub_batch = search_docs[i:i + 200]
            try:
                resp = await http_client.post(
                    url,
                    headers={"Content-Type": "application/json", "api-key": settings.AZURE_SEARCH_KEY},
                    json={"value": sub_batch},
                )
                if resp.status_code in (200, 207):
                    resp_data = resp.json()
                    for item in resp_data.get("value", []):
                        if item.get("status"):
                            successful_ids.add(item.get("key"))
                        else:
                            logger.error(f"[CASE-EMBED] Case {item.get('key')} failed: {item.get('errorMessage')}")
                else:
                    logger.error(f"[CASE-EMBED] Search push failed: {resp.status_code} - {resp.text}")
            except Exception as e:
                logger.error(f"[CASE-EMBED] Search push batch at {i} failed: {e}")

    logger.info(f"[CASE-EMBED] Pushed {len(successful_ids)} cases to index '{index_name}'")

    # 4. Mark as embedded in MongoDB
    now = datetime.now(timezone.utc)
    if successful_ids:
        from pymongo import UpdateOne
        bulk_ops = [
            UpdateOne(
                {"case_id": cid},
                {"$set": {"is_embedded": True, "embedded_at": now}},
            )
            for cid in successful_ids
        ]
        await db[COLLECTION_CASE_STUDY].bulk_write(bulk_ops)

    elapsed = time.perf_counter() - t_start
    summary = {
        "total": len(pending),
        "embedded": len(successful_ids),
        "failed": len(pending) - len(successful_ids),
        "elapsed_seconds": round(elapsed, 1),
    }
    logger.info(f"[CASE-EMBED] Complete: {summary}")
    return summary
