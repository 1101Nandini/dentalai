"""
Result formatting helpers for the RAG pipeline.

Handles:
  - Async iteration of Azure Search's AsyncItemPaged
  - Soft caption keyword boost for figure/image chunks (stop-word filtered)
  - Parallel SAS URL hydration via asyncio.gather + to_thread
  - Figure match scoring for post-retrieval ranking

Performance improvements vs original:
  - Stop words filtered from query terms before caption boost matching
  - SAS URL hydration runs in parallel (asyncio.gather) instead of sequential loop in thread
"""
import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.rag_service.query_analysis import STOP_WORDS

logger = logging.getLogger(__name__)


async def format_results_async(
    results_paged,
    top_k: int,
    query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Asynchronously format results from AsyncItemPaged into normalised dicts.

    Improvements vs original:
    - Removed dead sync _format_results (AsyncItemPaged can't be iterated sync)
    - Stop words excluded from caption boost query terms (improves precision)
    - SAS URL generation runs in parallel via asyncio.gather + to_thread
    """
    from app.utils.file_utils import generate_sas_url_with_expiry

    formatted: List[Dict[str, Any]] = []
    count = 0
    sas_needed: List[tuple] = []  # (index_in_formatted, blob_name)

    # Filter stop words so "what", "is", "the" etc. don't skew the caption boost
    query_terms = (
        {w for w in query.lower().split() if len(w) > 2 and w not in STOP_WORDS}
        if query else set()
    )

    async for result in results_paged:
        if count >= top_k:
            break

        blob_name = result.get("blob_name", "")
        blob_url = result.get("blob_url", result.get("image_url", ""))

        chunk_type = str(result.get("chunk_type", "text")).lower()
        caption = result.get("caption", "")
        content = result.get("content", "")

        score = result.get("@search.score") or 0
        reranker_score = result.get("@search.reranker_score") or 0

        # Soft caption keyword boost for figure/image chunks
        if chunk_type in ["figure", "image"] and caption and query_terms:
            caption_lower = caption.lower()
            match_count = sum(1 for t in query_terms if t in caption_lower)
            if match_count > 0:
                boost = min(1.10, 1.0 + (match_count * 0.05))
                if reranker_score:
                    reranker_score *= boost
                score *= boost

        doc_name = (
            result.get("source_filename")
            or result.get("document_name")
            or result.get("title")
            or "Unknown Source"
        )

        doc: Dict[str, Any] = {
            "id": result.get("id"),
            "content": content,
            "document_name": doc_name,
            "page": result.get("page_number", result.get("page", None)),
            "score": score,
            "reranker_score": reranker_score,
            "blob_url": blob_url,
            "blob_name": blob_name,
            "chunk_type": chunk_type,
            "caption": caption,
            "chapter_name": result.get("chapter_name") or result.get("chapter"),
            "topic_name": result.get("topic_name") or result.get("topic"),
            "subheading_name": result.get("subheading_name") or result.get("subheading"),
            "clinical_description": (
                result.get("clinical_findings") or result.get("clinical_description")
            ),
            "modality": result.get("modality"),
            "anatomy": result.get("anatomy"),
            "visual_signs": result.get("signs") or result.get("visual_signs"),
            "candidate_conditions": (
                result.get("conditions") or result.get("candidate_conditions")
            ),
            "chunk_id": result.get("chunk_id"),
            "topic_id": result.get("topic_id"),
            "source_type": result.get("source_type"),
            "context_summary": result.get("context_summary"),
            "document_version": result.get("document_version"),
        }

        # Attach semantic caption when available
        captions = result.get("@search.captions")
        if captions:
            doc["_caption"] = captions[0].text

        formatted.append(doc)

        if blob_name and not blob_url:
            sas_needed.append((count, blob_name))

        count += 1

    # ── Parallel SAS URL generation (asyncio.gather + to_thread) ──────────────
    # Previously: sequential loop inside a single thread.
    # Now: each SAS call gets its own thread-pool slot → true parallelism.
    if sas_needed:
        try:
            sas_urls = await asyncio.gather(*[
                asyncio.to_thread(generate_sas_url_with_expiry, bn)
                for _, bn in sas_needed
            ])
            for (idx, _), url in zip(sas_needed, sas_urls):
                formatted[idx]["blob_url"] = url
        except Exception as exc:
            logger.error("Parallel SAS URL generation failed: %s", exc)

    return formatted


def score_figure_match(item: Dict[str, Any], query: str) -> float:
    """Score a figure chunk based on caption/content keyword overlap with the query."""
    content = (item.get("content") or "").lower()
    caption = (item.get("caption") or "").lower()
    query_lower = query.lower() if query else ""

    if not query_lower:
        return 0.0

    query_terms = [t for t in re.findall(r"\w+", query_lower) if len(t) > 2]
    caption_hits = sum(1 for t in query_terms if t in caption)
    content_hits = sum(1 for t in query_terms if t in content)
    return caption_hits * 5.0 + content_hits * 1.0
