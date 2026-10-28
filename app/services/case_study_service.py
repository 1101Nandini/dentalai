"""
Case Study Search Service — Standalone search for related clinical case studies.

This is intentionally separate from the RAG pipeline. It searches the
dental-casestudy-index in Azure AI Search and returns matching case studies
that exceed the configured similarity threshold.
"""
import logging
import time
from typing import Any, Dict, List, Optional

from azure.search.documents.models import VectorizedQuery

from app.config import settings
from app.services.rag_service.clients import get_search_client
from app.services.rag_service.embeddings import embed_text
from app.utils.search_utils import ensure_search_index_exists
from app.core.database import get_database
from app.core.constants import COLLECTION_CASE_STUDY

logger = logging.getLogger(__name__)

CASE_STUDY_SEMANTIC_CONFIG = "casestudy-semantic-config"


async def search_related_cases(
    query: str,
    top_k: int = 3,
    pre_embedded_vector: Optional[List[float]] = None,
    threshold: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Search for case studies related to the user's query.

    This function is intentionally standalone (not part of the RAG pipeline).
    It is called in parallel with the RAG retrieval to add zero latency.

    Args:
        query: The user's question or the standalone query after analysis.
        top_k: Maximum number of results to return from the index.
        pre_embedded_vector: Optional pre-computed 3072D embedding vector.
            When provided, the embedding API call is skipped entirely.
        threshold: Minimum similarity score to include a result.
            Defaults to settings.CASE_STUDY_SIMILARITY_THRESHOLD (0.75).

    Returns:
        List of matching case study dicts with keys:
            case_id, title, content, score
        Only cases that meet the threshold are returned.
    """
    if not query:
        return []

    score_threshold = threshold or settings.CASE_STUDY_SIMILARITY_THRESHOLD
    index_name = settings.AZURE_SEARCH_INDEX_CASE_STUDY
    _t0 = time.perf_counter()

    try:
        # Embed the query (or reuse pre-computed vector)
        if pre_embedded_vector:
            query_vector = pre_embedded_vector
        else:
            query_vector, _ = await embed_text(query, dimensions=3072)

        vector_query = VectorizedQuery(
            vector=query_vector,
            k_nearest_neighbors=top_k,
            fields="content_vector",
            weight=1.0,
        )

        client = get_search_client(index_name)

        # Try pure vector search to get cosine similarity scores
        try:
            results = await client.search(
                search_text=None,
                vector_queries=[vector_query],
                top=top_k,
            )
        except Exception as err:
            logger.error(
                "[CASE-SEARCH] Vector search failed: %s",
                err,
            )
            return []

        # Collect and filter by threshold
        matching_cases = []
        case_ids_to_fetch = []
        async for result in results:
            score = result.get("@search.score", 0)
            if score >= score_threshold:
                case_id = result.get("case_id", "")
                case_ids_to_fetch.append(case_id)
                matching_cases.append({
                    "case_id": case_id,
                    "title": result.get("title", ""),
                    "content": result.get("content", ""),
                    "score": round(score, 4),
                })
                
        if case_ids_to_fetch:
            db = await get_database()
            # Fetch the actual MongoDB documents to map the string _id
            db_cases = await db[COLLECTION_CASE_STUDY].find(
                {"case_id": {"$in": case_ids_to_fetch}},
                {"_id": 1, "case_id": 1}
            ).to_list(None)
            id_map = {doc["case_id"]: str(doc["_id"]) for doc in db_cases}
            
            for case in matching_cases:
                case["id"] = id_map.get(case["case_id"], "")

        elapsed = time.perf_counter() - _t0
        logger.info(
            "[CASE-SEARCH] query=%r | results=%d (above threshold %.2f) | %.3fs",
            query[:80], len(matching_cases), score_threshold, elapsed,
        )
        return matching_cases

    except Exception as exc:
        if "Not Found" in str(exc) or "404" in str(exc):
            logger.warning(
                "[CASE-SEARCH] Index '%s' not found. Creating...", index_name
            )
            await ensure_search_index_exists(index_name)
        else:
            logger.error("[CASE-SEARCH] Search failed: %s", exc)
        return []
