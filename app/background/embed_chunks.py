"""
Embedder — Phase 3: Vectorize chunks and push to Azure AI Search.

Strategies:
  - Text/table chunks → text-embedding-3-large via embedding_text (structured format)
    Processed in concurrent sub-batches for speed.
  - Image/figure chunks → DUAL embedding:
      1. Azure Vision vectorizeImage (1024-dim) for visual similarity
      2. text-embedding-3-large via embedding_text (caption + section + context) for text search
    Processed concurrently with semaphore to maximize throughput.
  - Figures always retain blob_name for frontend image retrieval

Pushes embedded chunks to the appropriate Azure AI Search index.
"""
import asyncio
import logging
import httpx
from datetime import datetime, timezone

from bson import ObjectId
import openai
from openai import AsyncAzureOpenAI
import tiktoken

from app.config import settings
from app.core.database import get_database
from app.core.constants import DocumentStatus
from app.utils.search_utils import ensure_search_index_exists
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

def _log_retry_attempt(retry_state):
    logger.warning(
        f"Azure OpenAI embedding call failed (attempt {retry_state.attempt_number}). "
        f"Retrying in {retry_state.next_action.sleep:.1f} seconds. Error: {retry_state.outcome.exception()}"
    )
    print(
        f"   ∟ [RETRY] Azure OpenAI rate limit/transient error (attempt {retry_state.attempt_number}). "
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

# ── Tokenizer for text-embedding-3-large (cl100k_base) ─────────────────────
_tiktoken_enc = tiktoken.get_encoding("cl100k_base")

# OpenAI text-embedding-3-large hard limit is 8192 tokens.
# Use a small safety margin to account for any overhead.
EMBEDDING_MAX_TOKENS = 8191

def _truncate_to_token_limit(text: str, max_tokens: int = EMBEDDING_MAX_TOKENS) -> str:
    """Truncate text to fit within the embedding model's token limit."""
    tokens = _tiktoken_enc.encode(text)
    if len(tokens) <= max_tokens:
        return text
    logger.warning(
        f"Truncating embedding text from {len(tokens)} to {max_tokens} tokens"
    )
    return _tiktoken_enc.decode(tokens[:max_tokens])

# Chunk types that get text embeddings
TEXT_CHUNK_TYPES = {"text", "table"}
# Chunk types that get image embeddings (+ text embedding for context)
IMAGE_CHUNK_TYPES = {"image", "figure", "xray"}

# Minimum words required to embed a chunk (skip weak/empty content)
MIN_EMBED_WORDS = 5
# Azure Vision retry config
VISION_MAX_RETRIES = 3
VISION_RETRY_BASE_DELAY = 1  # seconds

# Concurrency limits for embedding
MAX_CONCURRENT_TEXT_BATCHES = 1   # Slowed down from 3 to prevent Azure OpenAI TPM 429 errors
# Parallel Azure Vision + Gemini Vision + text embeddings
MAX_CONCURRENT_IMAGE_EMBEDS = 4   

# ── Deduplication: only check chunks from first N pages for PDFs ───────────
DEDUP_MAX_PAGE = 4







def _get_embed_text(chunk: dict) -> str:
    """Get the best text to embed: prefer structured embedding_text over raw content."""
    return chunk.get("embedding_text") or chunk.get("content") or ""


def _is_worth_embedding(text: str) -> bool:
    """Check if text has enough substance to produce a useful embedding."""
    return len(text.split()) >= MIN_EMBED_WORDS


async def _embed_text_chunks(chunks: list[dict]) -> list[dict]:
    """
    Batch embed text/table chunks using OpenAI text-embedding-3-large.
    Uses concurrent sub-batches for maximum throughput.
    """
    if not chunks:
        return chunks

    # Filter out weak/empty chunks
    embeddable = [c for c in chunks if _is_worth_embedding(_get_embed_text(c))]
    skipped = len(chunks) - len(embeddable)
    if skipped:
        print(f"[SKIPPED] Skipped {skipped} text chunks (< {MIN_EMBED_WORDS} words)", flush=True)

    if not embeddable:
        return chunks

    client = AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_EMBEDDING_ENDPOINT,
        api_key=settings.AZURE_OPENAI_EMBEDDING_KEY,
        api_version="2023-05-15"
    )

    # Reduced batch size from 2048 to 100 to stay within Azure OpenAI TPM limits
    BATCH_SIZE = 100
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_TEXT_BATCHES)

    async def _embed_sub_batch(sub_batch: list[dict]):
        """Embed a single sub-batch under the semaphore."""
        async with semaphore:
            texts = [_truncate_to_token_limit(_get_embed_text(c)) for c in sub_batch]
            response = await _create_embeddings_with_retry(
                client=client,
                texts=texts,
                model=settings.OPENAI_EMBEDDING_MODEL,
                dimensions=settings.OPENAI_EMBEDDING_DIMENSIONS,
            )
            for j, embedding_data in enumerate(response.data):
                sub_batch[j]["embedding"] = embedding_data.embedding
                sub_batch[j]["embedding_model"] = settings.OPENAI_EMBEDDING_MODEL
            # Small delay to spread tokens over time and avoid sharp TPM spikes
            await asyncio.sleep(1.5)

    # Build sub-batches and process concurrently
    sub_batches = [
        embeddable[i:i + BATCH_SIZE]
        for i in range(0, len(embeddable), BATCH_SIZE)
    ]

    if len(sub_batches) > 1:
        print(f"   ∟ [EMBED] Vectorizing {len(embeddable)} text chunks in {len(sub_batches)} batches...", flush=True)

    await asyncio.gather(*[_embed_sub_batch(sb) for sb in sub_batches])

    return chunks


async def _embed_image_chunks(chunks: list[dict]) -> list[dict]:
    """
    Embed image/figure/xray chunks with DUAL vectors concurrently:
      1. image_vector: Azure Vision vectorizeImage (1024-dim) from blob image
      2. text_vector: text-embedding-3-large from embedding_text (caption + context)

    Uses a semaphore to process multiple image chunks in parallel.
    Falls back to text-only embedding if image blob is unavailable.
    """
    if not chunks:
        return chunks

    import httpx
    from app.utils.blob_utils import download_blob

    vision_endpoint = settings.AZURE_VISION_ENDPOINT.rstrip("/")
    vision_key = settings.AZURE_VISION_KEY
    api_version = "2024-02-01"
    model_version = "2023-04-15"

    oai_client = AsyncAzureOpenAI(
        azure_endpoint=settings.AZURE_OPENAI_EMBEDDING_ENDPOINT,
        api_key=settings.AZURE_OPENAI_EMBEDDING_KEY,
        api_version="2023-05-15"
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_IMAGE_EMBEDS)

    async def _embed_single_image(chunk: dict, http_client: httpx.AsyncClient):
        """Process a single image chunk: vision embedding + text embedding."""
        async with semaphore:
            blob_name = chunk.get("blob_name")

            # ── 1. Image Vector (Azure Vision) with retry ────────────────────
            if blob_name:
                image_bytes = None
                try:
                    image_bytes = await download_blob(blob_name)
                except Exception as e:
                    logger.warning(f"Blob download failed for {blob_name}: {e}")

                if image_bytes:
                    url = (
                        f"{vision_endpoint}/computervision/retrieval:vectorizeImage"
                        f"?api-version={api_version}&model-version={model_version}"
                    )
                    for attempt in range(1, VISION_MAX_RETRIES + 1):
                        try:
                            response = await http_client.post(
                                url,
                                headers={
                                    "Ocp-Apim-Subscription-Key": vision_key,
                                    "Content-Type": "application/octet-stream",
                                },
                                content=image_bytes,
                            )
                            response.raise_for_status()
                            image_vector = response.json()["vector"]

                            chunk["image_embedding"] = image_vector
                            chunk["embedding_model"] = "azure-vision-multimodal"
                            logger.debug(f"[IMAGE] Image vector: {chunk['chunk_id']} ({len(image_vector)}d)")
                            
                            break  # success

                        except Exception as e:
                            if attempt < VISION_MAX_RETRIES:
                                delay = VISION_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                                await asyncio.sleep(delay)
                            else:
                                logger.error(
                                    f"Azure Vision failed after {VISION_MAX_RETRIES} attempts for "
                                    f"{chunk['chunk_id']}: {e}"
                                )

            # ── 2. Text Vector (if text is substantial enough) ───────────────
            embed_text = _get_embed_text(chunk)
            if embed_text and _is_worth_embedding(embed_text):
                embed_text = _truncate_to_token_limit(embed_text)
                try:
                    text_response = await _create_embeddings_with_retry(
                        client=oai_client,
                        texts=[embed_text],  # already truncated above
                        model=settings.OPENAI_EMBEDDING_MODEL,
                        dimensions=1024,  # Match image index dimensions
                    )
                    chunk["embedding"] = text_response.data[0].embedding

                    if "image_embedding" not in chunk:
                        chunk["embedding_model"] = f"{settings.OPENAI_EMBEDDING_MODEL}-fallback"
                except Exception as e:
                    logger.warning(f"Text embedding failed for figure {chunk['chunk_id']}: {e}")

            # If we got neither vector, set a fallback model marker
            if "embedding" not in chunk and "image_embedding" not in chunk:
                chunk["embedding_model"] = "none"

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        await asyncio.gather(*[
            _embed_single_image(chunk, http_client)
            for chunk in chunks
        ])

    return chunks


async def embed_chunks(document_id: str) -> None:
    """
    Phase 3: Embed all chunks for a document and push to Azure AI Search.
    
    Hardened Production Logic:
    - Deterministic Batching: Sort by created_at (FIFO).
    - State Separation: Tracks 'is_embedded' vs 'is_indexed'.
    - Payload Dedup: Reuses vectors/analysis for identical chunk_hashes across docs.
    - Partial Resilience: Marks only successful chunks as 'is_indexed'.
    - Audit Trail: Tracks attempts, timestamps, and model versions.
    """
    db = await get_database()

    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {"status": DocumentStatus.EMBEDDING, "updated_at": datetime.now(timezone.utc)}}
    )

    # ── Check for pending work ───────────────────────────────────────────
    pending_count = await db["chunks"].count_documents({
        "document_id": document_id,
        "is_indexed": {"$ne": True}
    })
    
    if pending_count == 0:
        await db["documents"].update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {"status": DocumentStatus.COMPLETED, "updated_at": datetime.now(timezone.utc)}}
        )
        return

    print(f"   ∟ [EMBED] Found {pending_count} pending chunks. Processing in increments of 30...", flush=True)

    # ── Incremental State-Aware Loop ───────────────────────────────────────
    BATCH_SIZE = 30
    processed_count = 0

    while True:
        # Fetch the next segment of pending chunks (Deterministic FIFO)
        cursor = db["chunks"].find({
            "document_id": document_id,
            "is_indexed": {"$ne": True}
        }).sort("created_at", 1).limit(BATCH_SIZE)
        
        batch = await cursor.to_list(length=BATCH_SIZE)
        if not batch:
            break

        # Update attempts and activity
        batch_ids = [c["_id"] for c in batch]
        await db["chunks"].update_many(
            {"_id": {"$in": batch_ids}},
            {
                "$inc": {"indexing_attempts": 1},
                "$set": {"last_processed_at": datetime.now(timezone.utc)}
            }
        )

        # ── 1. Payload-Specific Deduplication ─────────────────────────────
        # Identify chunks that need vectors generated
        to_embed = [c for c in batch if c.get("is_embedded") is not True]
        if to_embed:
            # Check for existing vectors in Azure AI Search with same hash (Cross-Doc Reuse)
            hashes = [c.get("chunk_hash") for c in to_embed if c.get("chunk_hash")]
            existing_vectors = {}
            
            if hashes:
                async def _fetch_reusable_vectors(idx: str) -> dict:
                    search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
                    search_key = settings.AZURE_SEARCH_KEY
                    api_version = "2024-07-01"
                    url = f"{search_endpoint}/indexes/{idx}/docs/search?api-version={api_version}"
                    
                    found = {}
                    hash_str = ",".join(hashes)
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        try:
                            payload = {
                                "filter": f"search.in(chunk_hash, '{hash_str}', ',')",
                                "select": "chunk_hash,content_vector,image_vector,clinical_description,modality,anatomy,visual_signs,candidate_conditions",
                                "top": len(hashes)
                            }
                            resp = await client.post(url, headers={"api-key": search_key, "Content-Type": "application/json"}, json=payload)
                            if resp.status_code == 200:
                                for r in resp.json().get("value", []):
                                    if "chunk_hash" in r and r["chunk_hash"] not in found:
                                        found[r["chunk_hash"]] = {
                                            "embedding": r.get("content_vector"),
                                            "image_embedding": r.get("image_vector"),
                                            "clinical_description": r.get("clinical_description"),
                                            "modality": r.get("modality"),
                                            "anatomy": r.get("anatomy"),
                                            "visual_signs": r.get("visual_signs"),
                                            "candidate_conditions": r.get("candidate_conditions"),
                                            "embedding_model": "azure-search-cache"
                                        }
                        except Exception as e:
                            logger.error(f"[DEDUP] Azure Search reuse lookup failed on {idx}: {e}")
                    return found

                text_vectors = await _fetch_reusable_vectors(settings.AZURE_SEARCH_INDEX_TEXT)
                image_vectors = await _fetch_reusable_vectors(settings.AZURE_SEARCH_INDEX_IMAGE)
                existing_vectors.update(text_vectors)
                existing_vectors.update(image_vectors)

            # Apply reuse
            final_to_embed = []
            for c in to_embed:
                chunk_hash = c.get("chunk_hash")
                reuse = existing_vectors.get(chunk_hash) if chunk_hash else None
                if reuse:
                    c["embedding"] = reuse.get("embedding")
                    c["image_embedding"] = reuse.get("image_embedding")
                    c["clinical_description"] = reuse.get("clinical_description")
                    c["modality"] = reuse.get("modality")
                    c["anatomy"] = reuse.get("anatomy") or []
                    c["visual_signs"] = reuse.get("visual_signs") or []
                    c["candidate_conditions"] = reuse.get("candidate_conditions") or []
                    c["context_summary"] = reuse.get("context_summary")
                    c["embedding_model"] = reuse.get("embedding_model")
                    c["is_embedded"] = True
                    c["embedded_at"] = datetime.now(timezone.utc)
                    logger.info(f"   ∟ [REUSE] Payload matched for {c['chunk_id'][:12]} (Hash: {c['chunk_hash'][:8]})")
                else:
                    final_to_embed.append(c)

            # Generate fresh vectors for those that couldn't be reused
            if final_to_embed:
                await db["chunks"].update_many(
                    {"_id": {"$in": [c["_id"] for c in final_to_embed]}},
                    {"$inc": {"embedding_attempts": 1}}
                )

                text_to_embed = [c for c in final_to_embed if c.get("chunk_type") in TEXT_CHUNK_TYPES]
                image_to_embed = [c for c in final_to_embed if c.get("chunk_type") in IMAGE_CHUNK_TYPES]

                if text_to_embed:
                    text_to_embed = await _embed_text_chunks(text_to_embed)
                if image_to_embed:
                    image_to_embed = await _embed_image_chunks(image_to_embed)

                # Mark those successfully embedded
                now = datetime.now(timezone.utc)
                for c in final_to_embed:
                    if c.get("embedding") or c.get("image_embedding"):
                        c["is_embedded"] = True
                        c["embedded_at"] = now

        # ── 2. Check Semantic Duplicates (Only for newly embedded) ──────────
        # For PDFs, only check chunks from first DEDUP_MAX_PAGE pages.
        # Audio/image chunks (page_number is None) are always checked.
        text_batch = [c for c in batch if c.get("chunk_type") in TEXT_CHUNK_TYPES]
        image_batch = [c for c in batch if c.get("chunk_type") in IMAGE_CHUNK_TYPES]

        # Filter to early-page chunks for dedup (skip deep-document chunks)
        text_dedup_batch = [
            c for c in text_batch
            if c.get("page_number") is None or c.get("page_number", 999) <= DEDUP_MAX_PAGE
        ]
        image_dedup_batch = [
            c for c in image_batch
            if c.get("page_number") is None or c.get("page_number", 999) <= DEDUP_MAX_PAGE
        ]
        
        if text_dedup_batch:
            text_dedup_batch = await _check_semantic_duplicates(settings.AZURE_SEARCH_INDEX_TEXT, text_dedup_batch)
        if image_dedup_batch:
            image_dedup_batch = await _check_semantic_duplicates(settings.AZURE_SEARCH_INDEX_IMAGE, image_dedup_batch)

        # ── 3. Persist Vectors (MongoDB) ───────────────────────────────────
        from pymongo import UpdateOne
        bulk_ops = []
        for chunk in batch:
            if not chunk.get("is_embedded"):
                continue
                
            update_data = {
                "is_embedded": True,
                "embedded_at": chunk.get("embedded_at"),
                "embedding_model": chunk.get("embedding_model"),
                "is_latest": True,
                "updated_at": datetime.now(timezone.utc),
            }
            if chunk.get("embedding"):
                update_data["embedding"] = chunk["embedding"]
            if chunk.get("image_embedding"):
                update_data["image_embedding"] = chunk["image_embedding"]
            if chunk.get("clinical_description"):
                update_data["clinical_description"] = chunk["clinical_description"]
            if chunk.get("semantic_duplicate_of"):
                update_data["semantic_duplicate_of"] = chunk["semantic_duplicate_of"]
                update_data["duplicate_score"] = chunk.get("duplicate_score")

            bulk_ops.append(UpdateOne({"_id": chunk["_id"]}, {"$set": update_data}))

        if bulk_ops:
            await db["chunks"].bulk_write(bulk_ops)

        # ── 4. Push to Azure AI Search (Partial Persistence) ───────────────
        indexed_ids = set()
        
        if text_batch:
            success_ids = await _push_to_search_index(settings.AZURE_SEARCH_INDEX_TEXT, text_batch, is_image=False)
            indexed_ids.update(success_ids)
            
        if image_batch:
            success_ids = await _push_to_search_index(settings.AZURE_SEARCH_INDEX_IMAGE, image_batch, is_image=True)
            indexed_ids.update(success_ids)

        # Mark successful chunks as indexed
        if indexed_ids:
            await db["chunks"].update_many(
                {"chunk_id": {"$in": list(indexed_ids)}},
                {"$set": {
                    "is_indexed": True, 
                    "indexed_at": datetime.now(timezone.utc),
                    "indexing_error": None
                }}
            )

        processed_count += len(batch)
        print(f"   ∟ [EMBED] Segment complete: {processed_count}/{pending_count} chunks indexed.", flush=True)

    # ── Aggregate Semantic Overlaps to Document Level ───────────────────────
    # This enables the merge-version admin API to find and act on overlaps.
    overlap_cursor = db["chunks"].find(
        {
            "document_id": document_id,
            "semantic_duplicate_of": {"$exists": True, "$ne": None}
        },
        {"chunk_id": 1, "semantic_duplicate_of": 1, "duplicate_score": 1}
    )
    overlaps = await overlap_cursor.to_list(length=None)

    if overlaps:
        # Tally which parent document has the most overlapping chunks
        # `semantic_duplicate_of` format: "69d62066ddc26f7252c38f84_text_1"
        doc_counts = {}
        for o in overlaps:
            dup_id = o.get("semantic_duplicate_of", "")
            if "_" in dup_id:
                target_doc = dup_id.split("_")[0]
                doc_counts[target_doc] = doc_counts.get(target_doc, 0) + 1

        most_similar_doc_id = None
        similarity_percentage = 0
        if doc_counts:
            most_similar_doc_id = max(doc_counts, key=doc_counts.get)
            total_doc_chunks = await db["chunks"].count_documents({"document_id": document_id})
            if total_doc_chunks > 0:
                similarity_percentage = round((doc_counts[most_similar_doc_id] / total_doc_chunks) * 100)

        await db["documents"].update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {
                "similar_to_document_id": most_similar_doc_id,
                "similarity_percentage": similarity_percentage,
                "semantic_overlaps": [
                    {
                        "chunk_id": o["chunk_id"],
                        "duplicate_of": o["semantic_duplicate_of"],
                        "score": o.get("duplicate_score")
                    }
                    for o in overlaps
                ]
            }}
        )
        print(f"   ∟ [DEDUP] Found {len(overlaps)} semantic overlaps → saved to document", flush=True)

    # ── Finalize Document ──────────────────────────────────────────────────
    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {
            "$set": {"status": DocumentStatus.COMPLETED, "updated_at": datetime.now(timezone.utc)},
            "$unset": {"parsed_elements": ""}  # Remove massive raw parse data to save space
        }
    )
    
    # ── Cleanup MongoDB Chunks ─────────────────────────────────────────────
    # Since Azure AI Search handles vectors and deduplication, we can safely delete chunks from MongoDB
    await db["chunks"].delete_many({"document_id": document_id})
    # ── Cleanup Gemini Vision Cache ────────────────────────────────────────
    # Cache is only useful during chunking. Safe to clear after embedding.
    cache_result = await db["clinical_enrichment_cache"].delete_many({})
    if cache_result.deleted_count:
        print(f"   ∟ [CLEANUP] Cleared {cache_result.deleted_count} clinical enrichment cache entries.", flush=True)
    print(f"   ∟ [EMBED] Finalized document {document_id[:8]} and cleaned up MongoDB. Done.", flush=True)

    # ── Zero-Downtime Version Transition ──────────────────────────────────
    doc_info = await db["documents"].find_one({"_id": ObjectId(document_id)}, {"parent_document_id": 1})
    if doc_info and doc_info.get("parent_document_id"):
        parent_id = doc_info["parent_document_id"]
        
        # We query Azure Search directly to find the chunk IDs of the previous version
        async def _fetch_old_chunk_ids(idx: str, p_id: str) -> list[str]:
            search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
            search_key = settings.AZURE_SEARCH_KEY
            api_version = "2024-07-01"
            url = f"{search_endpoint}/indexes/{idx}/docs/search?api-version={api_version}"
            
            c_ids = []
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    payload = {
                        "filter": f"document_id eq '{p_id}' and is_latest eq true",
                        "select": "chunk_id",
                        "top": 10000 
                    }
                    resp = await client.post(url, headers={"api-key": search_key, "Content-Type": "application/json"}, json=payload)
                    if resp.status_code == 200:
                        c_ids = [r["chunk_id"] for r in resp.json().get("value", [])]
                except Exception as e:
                    print(f"   ∟ [ERROR] Failed to fetch old chunk IDs from {idx}: {e}")
            return c_ids

        t_ids = await _fetch_old_chunk_ids(settings.AZURE_SEARCH_INDEX_TEXT, parent_id)
        i_ids = await _fetch_old_chunk_ids(settings.AZURE_SEARCH_INDEX_IMAGE, parent_id)
        
        async def _demote_chunks(idx: str, c_ids: list[str]):
            if not c_ids: return
            
            search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
            search_key = settings.AZURE_SEARCH_KEY
            api_version = "2024-07-01"
            
            batch = [{"@search.action": "merge", "chunk_id": c, "is_latest": False} for c in c_ids]
            url = f"{search_endpoint}/indexes/{idx}/docs/index?api-version={api_version}"
            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    await client.post(url, headers={"api-key": search_key, "Content-Type": "application/json"}, json={"value": batch})
                except Exception as e:
                    print(f"   ∟ [ERROR] Failed to demote old version chunks in {idx}: {e}")

        if t_ids: await _demote_chunks(settings.AZURE_SEARCH_INDEX_TEXT, t_ids)
        if i_ids: await _demote_chunks(settings.AZURE_SEARCH_INDEX_IMAGE, i_ids)
        print(f"   ∟ [VERSION] Safely demoted {len(t_ids) + len(i_ids)} chunks from old version {parent_id[:8]}.", flush=True)



async def _check_semantic_duplicates(
    index_name: str,
    chunks: list[dict],
    threshold: float = 0.90
) -> list[dict]:
    """
    Query Azure AI Search for existing vectors >90% similar.
    Returns the chunks, with a "semantic_duplicate_of" field if found.
    """
    if not chunks:
        return chunks

    import httpx
    import asyncio

    search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
    search_key = settings.AZURE_SEARCH_KEY
    api_version = "2024-07-01"

    semaphore = asyncio.Semaphore(15)

    async def _query_single_chunk(http_client, chunk):
        vector = chunk.get("embedding")
        if not vector:
            return chunk

        url = f"{search_endpoint}/indexes/{index_name}/docs/search?api-version={api_version}"

        payload = {
            "vectorQueries": [
                {
                    "kind": "vector",
                    "vector": vector,
                    "exhaustive": True,
                    "fields": "content_vector",
                    "k": 1
                }
            ],
            "filter": f"chunk_id ne '{chunk['chunk_id']}'",
            "select": "chunk_id",
            "top": 1
        }

        @retry(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
            reraise=True
        )
        async def _do_query():
            resp = await http_client.post(
                url,
                headers={
                    "Content-Type": "application/json",
                    "api-key": search_key,
                },
                json=payload
            )
            # Index may not exist yet on first document — skip gracefully
            if resp.status_code == 404:
                return resp
            resp.raise_for_status()
            return resp

        async with semaphore:
            try:
                response = await _do_query()
                if response.status_code == 404:
                    return chunk
                results = response.json().get("value", [])

                if results:
                    top_match = results[0]
                    score = top_match.get("@search.score", 0)
                    if score >= threshold:
                        chunk["semantic_duplicate_of"] = top_match.get("chunk_id")
                        chunk["duplicate_score"] = score
            except Exception as e:
                logger.warning(f"Semantic dedupe query failed for chunk {chunk['chunk_id']}: {e}")

        return chunk

    async with httpx.AsyncClient(timeout=45.0) as http_client:
        tasks = [_query_single_chunk(http_client, c) for c in chunks]
        await asyncio.gather(*tasks)

    return chunks


import ast

def _ensure_list_of_strings(val) -> list[str]:
    """Strictly format a value as a list of strings for Azure AI Search Collection fields."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(v) for v in val]
    if isinstance(val, str):
        val = val.strip()
        if val.startswith('[') and val.endswith(']'):
            try:
                parsed = ast.literal_eval(val)
                if isinstance(parsed, list):
                    return [str(v) for v in parsed]
            except Exception:
                pass
        return [val]
    return [str(val)]

async def _push_to_search_index(
    index_name: str,
    chunks: list[dict],
    is_image: bool = False,
) -> list[str]:
    """
    Upload/merge chunk documents into an Azure AI Search index.
    Returns a list of successfully indexed chunk_ids.
    """
    if not chunks:
        return []

    import httpx
    from app.core.database import get_database
    from bson import ObjectId

    search_endpoint = settings.AZURE_SEARCH_ENDPOINT.rstrip("/")
    search_key = settings.AZURE_SEARCH_KEY
    api_version = "2024-07-01"

    search_docs = []
    db = await get_database()

    # ── Fetch document versions for version-aware indexing ─────────────────
    doc_ids = list(set(chunk["document_id"] for chunk in chunks))
    version_map: dict[str, int] = {}
    for did in doc_ids:
        try:
            doc_record = await db["documents"].find_one(
                {"_id": ObjectId(did)}, {"document_version": 1}
            )
            if doc_record:
                version_map[did] = doc_record.get("document_version", 1)
        except Exception:
            version_map[did] = 1

    for chunk in chunks:
        doc = {
            "@search.action": "mergeOrUpload",
            "chunk_id": chunk["chunk_id"],
            "chunk_hash": chunk.get("chunk_hash"),
            "document_id": chunk["document_id"],
            "source_type": chunk.get("source_type"),
            "source_filename": chunk.get("source_filename"),
            "chunk_type": chunk.get("chunk_type"),
            "content": chunk.get("content", ""),
            "embedding_text": chunk.get("embedding_text", ""),
            "page_number": chunk.get("page_number"),
            "section_heading": chunk.get("section_heading"),
            "chapter_name": chunk.get("chapter_name"),
            "topic_name": chunk.get("topic_name"),
            "subheading_name": chunk.get("subheading_name"),
            "clinical_description": chunk.get("clinical_description"),
            "caption": chunk.get("caption") or chunk.get("figure_caption") or "",
            "figure_caption": chunk.get("figure_caption"),
            "nearby_text": chunk.get("nearby_text"),
            "blob_name": chunk.get("blob_name"),
            "figure_id": chunk.get("figure_id"),
            
            # ── Clinical Intelligence Mapping ───────────────
            "topic_id": chunk.get("topic_id"),
            "modality": chunk.get("modality"),
            "anatomy": _ensure_list_of_strings(chunk.get("anatomy")),
            "visual_signs": _ensure_list_of_strings(chunk.get("visual_signs")),
            "candidate_conditions": _ensure_list_of_strings(chunk.get("candidate_conditions")),
            "linked_chunk_ids": _ensure_list_of_strings(chunk.get("linked_chunk_ids")),

            "is_latest": True,
            "document_version": version_map.get(chunk["document_id"], 1),
        }

        embedding = chunk.get("embedding")
        if embedding and isinstance(embedding, list) and len(embedding) > 0:
            doc["content_vector"] = embedding

        if is_image:
            img_embedding = chunk.get("image_embedding")
            if img_embedding and isinstance(img_embedding, list) and len(img_embedding) > 0:
                doc["image_vector"] = img_embedding

        search_docs.append(doc)

    successful_ids = []

    # Pre-flight: ensure the index exists before pushing any documents
    await ensure_search_index_exists(index_name)

    async with httpx.AsyncClient(timeout=60.0) as http_client:
        # Pushing in smaller sub-batches (200) to avoid payload limit
        SUB_BATCH_SIZE = 200
        for i in range(0, len(search_docs), SUB_BATCH_SIZE):
            sub_batch = search_docs[i:i + SUB_BATCH_SIZE]
            url = f"{search_endpoint}/indexes/{index_name}/docs/index?api-version={api_version}"

            @retry(
                stop=stop_after_attempt(4),
                wait=wait_exponential(multiplier=1, min=2, max=20),
                retry=retry_if_exception_type((httpx.RequestError, httpx.HTTPStatusError)),
                reraise=True
            )
            async def _do_post():
                return await http_client.post(
                    url,
                    headers={"Content-Type": "application/json", "api-key": search_key},
                    json={"value": sub_batch},
                )

            response = None
            for attempt in range(2): # Retry once on transient index creation failure
                try:
                    response = await _do_post()
                except Exception as e:
                    logger.error(f"[SEARCH] Tenacity retries exhausted for {index_name}. Post failed: {e}")
                    break

                if response.status_code == 404 and attempt == 0:
                    logger.warning(f"[SEARCH] Index '{index_name}' not found on push. Re-creating...")
                    if await ensure_search_index_exists(index_name):
                        import asyncio
                        await asyncio.sleep(2)  # Give Azure time to provision
                        continue # Retry
                break

            if response is None or response.status_code not in (200, 207):
                if response:
                    logger.error(f"Azure Search push failed for {index_name}: {response.status_code} - {response.text}")
                continue

            resp_data = response.json()
            for item in resp_data.get("value", []):
                cid = item.get("key")
                if item.get("status"):
                    successful_ids.append(cid)
                else:
                    error_msg = item.get("errorMessage", "Unknown error")
                    logger.error(f"Chunk {cid} failed to index: {error_msg}")
                    # Update specific chunk with error
                    await db["chunks"].update_one(
                        {"chunk_id": cid},
                        {"$set": {"indexing_error": error_msg}}
                    )

    return successful_ids
