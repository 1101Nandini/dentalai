"""
Azure AI Search hybrid retrieval for text and image indexes.

Each function returns (results, usage_metrics) so the caller can aggregate
usage data across the entire pipeline.
"""
import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from azure.search.documents.models import VectorizedQuery

from app.config import settings
from app.utils.search_utils import ensure_search_index_exists
from app.services.rag_service.clients import get_search_client
from app.services.rag_service.embeddings import embed_text, embed_text_vision, embed_image, slice_vector
from app.services.rag_service.results import format_results_async

logger = logging.getLogger(__name__)

SEMANTIC_CONFIG_NAME = "my-semantic-config"




async def hybrid_search_text_index(
    query: str | None,
    top_k: int,
    filter_query: Optional[str] = None,
    pre_embedded_vector: Optional[List[float]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Search the TEXT index (3072-dim content_vector) with hybrid + semantic reranking.
    Returns (results, usage_metrics).

    Args:
        pre_embedded_vector: Optional pre-computed 3072D vector. When provided,
            the embedding API call is skipped entirely.
    """
    if not query:
        return [], []

    usage_metrics: List[Dict[str, Any]] = []
    vector_queries = []
    index_name = settings.AZURE_SEARCH_INDEX_TEXT
    _fn_t0 = time.perf_counter()

    # Search all versions; ignore the old is_latest requirement
    effective_filter = filter_query

    try:
        if pre_embedded_vector:
            text_vector = pre_embedded_vector
            logger.info("[TIMING] embed_text(3072D): reused pre-computed vector (0.000s)")
        else:
            text_vector, usage = await embed_text(query, dimensions=3072)
            usage_metrics.append(usage)

        vector_queries.append(
            VectorizedQuery(
                vector=text_vector,
                k_nearest_neighbors=top_k,
                fields="content_vector",
                weight=1.0,
            )
        )

        client = get_search_client(index_name)

        try:
            _t0 = time.perf_counter()
            results = await client.search(
                search_text=query,
                vector_queries=vector_queries,
                filter=effective_filter,
                top=top_k,
                query_type="semantic",
                semantic_configuration_name=SEMANTIC_CONFIG_NAME,
                query_caption="extractive",
                query_answer="extractive",
            )
            formatted = await format_results_async(results, top_k, query=query)
            elapsed = time.perf_counter() - _t0
            logger.info(
                "[TIMING] text_index_search (semantic): %.3fs | results=%d",
                elapsed, len(formatted),
            )
            usage_metrics.append({
                "step": "azure_search_text",
                "index": index_name,
                "query_type": "semantic_hybrid",
                "results_returned": len(formatted),
                "latency_s": round(elapsed, 3),
            })

        except Exception as search_error:
            logger.warning(
                "Semantic search failed for text index, retrying simple hybrid: %s",
                search_error,
            )
            _t0 = time.perf_counter()
            results = await client.search(
                search_text=query,
                vector_queries=vector_queries,
                filter=effective_filter,
                top=top_k,
            )
            formatted = await format_results_async(results, top_k, query=query)
            elapsed = time.perf_counter() - _t0
            logger.info(
                "[TIMING] text_index_search (fallback hybrid): %.3fs | results=%d",
                elapsed, len(formatted),
            )
            usage_metrics.append({
                "step": "azure_search_text",
                "index": index_name,
                "query_type": "fallback_hybrid",
                "results_returned": len(formatted),
                "latency_s": round(elapsed, 3),
            })

        logger.info(
            "[TIMING] hybrid_search_text_index total: %.3fs",
            time.perf_counter() - _fn_t0,
        )
        return formatted, usage_metrics

    except Exception as exc:
        if "Not Found" in str(exc) or "404" in str(exc):
            logger.warning("[SEARCH] Index '%s' missing. Creating…", index_name)
            if await ensure_search_index_exists(index_name):
                try:
                    client = get_search_client(index_name)
                    it = await client.search(
                        search_text=query, vector_queries=vector_queries, top=top_k
                    )
                    return await format_results_async(it, top_k, query=query), usage_metrics
                except Exception as retry_exc:
                    logger.error("Retry search after creation failed: %s", retry_exc)
        logger.error("Text index search failed: %s", exc)
        return [], usage_metrics


async def hybrid_search_image_index(
    query: str | None,
    image_urls: List[str] | None,
    top_k: int,
    filter_query: Optional[str] = None,
    pre_embedded_1024: Optional[List[float]] = None,
    pre_embedded_vision: Optional[List[float]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Search the IMAGE index (1024-dim content_vector + 1024-dim image_vector).

    Multi-vector strategy:
      1. text → content_vector (1024D via Matryoshka slicing from 3072D)
      2. text → image_vector  (1024D Azure Vision vectorizeText)
      3. image → image_vector (1024D Azure Vision vectorizeImage, weight=3.0)

    Returns (results, usage_metrics).
    """
    _fn_t0 = time.perf_counter()
    usage_metrics: List[Dict[str, Any]] = []
    vector_queries = []

    # Search all versions; ignore the old is_latest requirement
    effective_filter = filter_query

    # ── Text vectors ───────────────────────────────────────────────────────────
    if query:
        # Both text embeddings in parallel when not pre-supplied
        if pre_embedded_1024 and pre_embedded_vision:
            text_embed_result   = pre_embedded_1024
            vision_embed_result = pre_embedded_vision
        elif pre_embedded_1024:
            vision_embed_result, usage = await embed_text_vision(query)
            text_embed_result = pre_embedded_1024
            usage_metrics.append(usage)
        elif pre_embedded_vision:
            vec_3072, usage = await embed_text(query, dimensions=3072)
            text_embed_result   = slice_vector(vec_3072, 1024)
            vision_embed_result = pre_embedded_vision
            usage_metrics.append(usage)
        else:
            # Neither pre-supplied — embed both in parallel
            (vec_3072, usage_t), (vision_embed_result, usage_v) = await asyncio.gather(
                embed_text(query, dimensions=3072),
                embed_text_vision(query),
            )
            text_embed_result = slice_vector(vec_3072, 1024)
            usage_metrics.extend([usage_t, usage_v])

        vector_queries.append(
            VectorizedQuery(
                vector=text_embed_result,
                k_nearest_neighbors=top_k,
                fields="content_vector",
                weight=0.9 if image_urls else 1.0,
            )
        )
        if not isinstance(vision_embed_result, Exception):
            vector_queries.append(
                VectorizedQuery(
                    vector=vision_embed_result,
                    k_nearest_neighbors=top_k,
                    fields="image_vector",
                    weight=0.7 if image_urls else 0.8,
                )
            )

    # ── Image vectors ──────────────────────────────────────────────────────────
    if image_urls:
        try:
            _t0 = time.perf_counter()
            # Pass blob_name (stable) as cache key so the embed cache survives
            # SAS URL rotation (SAS expires every 7 days; blob_name never changes)
            embed_tasks = [
                embed_image(url, blob_name=url.split("?")[0].split("/")[-1])
                for url in image_urls
            ]
            img_results = await asyncio.gather(*embed_tasks, return_exceptions=True)
            logger.info(
                "[TIMING] image_embeddings (batch %d): %.3fs",
                len(image_urls), time.perf_counter() - _t0,
            )
            for res in img_results:
                if isinstance(res, Exception):
                    logger.error("Single image embedding failed: %s", res)
                else:
                    vec, usage = res
                    usage_metrics.append(usage)
                    vector_queries.append(
                        VectorizedQuery(
                            vector=vec,
                            k_nearest_neighbors=top_k,
                            fields="image_vector",
                            weight=0.5,  # Reduced from 3.0: generic vision vectors cause clinical mismatches
                        )
                    )
        except Exception as exc:
            logger.error("Batch image embedding failed: %s", exc)


    if not vector_queries and not query:
        return [], usage_metrics

    client = get_search_client(settings.AZURE_SEARCH_INDEX_IMAGE)

    async def _semantic_search(filter_q: Optional[str]):
        return await client.search(
            search_text=query if query else "*",
            vector_queries=vector_queries or None,
            filter=filter_q,
            top=top_k,
            query_type="semantic",
            semantic_configuration_name=SEMANTIC_CONFIG_NAME,
            query_caption="extractive",
            query_answer="extractive",
        )

    async def _fallback_search(filter_q: Optional[str]):
        return await client.search(
            search_text=query if query else "*",
            vector_queries=vector_queries or None,
            filter=filter_q,
            top=top_k,
        )

    async def _run_with_filter_retry(search_fn, filter_q: Optional[str]) -> List[Dict[str, Any]]:
        """Run search; if 0 results with filter, retry without filter."""
        results = await search_fn(filter_q)
        formatted = await format_results_async(results, top_k)
        if len(formatted) == 0 and filter_q:
            logger.info("Image index got 0 results with filter, retrying without.")
            results = await search_fn(None)
            formatted = await format_results_async(results, top_k)
        return formatted

    try:
        _t0 = time.perf_counter()
        formatted = await _run_with_filter_retry(_semantic_search, effective_filter)
        elapsed = time.perf_counter() - _t0
        logger.info(
            "[TIMING] image_index_search (semantic): %.3fs | results=%d",
            elapsed, len(formatted),
        )
        usage_metrics.append({
            "step": "azure_search_image",
            "index": settings.AZURE_SEARCH_INDEX_IMAGE,
            "query_type": "semantic_hybrid",
            "results_returned": len(formatted),
            "latency_s": round(elapsed, 3),
        })

    except Exception as exc:
        logger.warning("Semantic search failed on image index, falling back: %s", exc)
        try:
            _t0 = time.perf_counter()
            formatted = await _run_with_filter_retry(_fallback_search, effective_filter)
            elapsed = time.perf_counter() - _t0
            logger.info(
                "[TIMING] image_index_search (fallback): %.3fs | results=%d",
                elapsed, len(formatted),
            )
            usage_metrics.append({
                "step": "azure_search_image",
                "index": settings.AZURE_SEARCH_INDEX_IMAGE,
                "query_type": "fallback_hybrid",
                "results_returned": len(formatted),
                "latency_s": round(elapsed, 3),
            })
        except Exception as fallback_exc:
            if "Not Found" in str(fallback_exc) or "404" in str(fallback_exc):
                logger.warning("[SEARCH] Image index missing. Creating…")
                if await ensure_search_index_exists(settings.AZURE_SEARCH_INDEX_IMAGE):
                    try:
                        client = get_search_client(settings.AZURE_SEARCH_INDEX_IMAGE)
                        res = await client.search(
                            search_text=query if query else "*",
                            vector_queries=vector_queries or None,
                            top=top_k,
                        )
                        return await format_results_async(res, top_k), usage_metrics
                    except Exception as retry_exc:
                        logger.error("Retry image search failed: %s", retry_exc)
            logger.error("Image index search failed entirely: %s", fallback_exc)
            formatted = []

    logger.info(
        "[TIMING] hybrid_search_image_index total: %.3fs",
        time.perf_counter() - _fn_t0,
    )
    return formatted, usage_metrics
