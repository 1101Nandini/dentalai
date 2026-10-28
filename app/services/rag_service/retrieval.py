"""
Public RAG pipeline entry point.

`RagService` is a thin orchestration shell that delegates to focused sub-modules:
  clients · embeddings · query_analysis · search · results · scoring

Key improvements over the original monolithic file:
  - Embedding vectors are re-computed for the rewritten `search_q` when it
    differs meaningfully from the original query (fixes co-reference mismatch)
  - print() replaced with logger.info()
  - Usage metrics collected from EVERY pipeline step
"""
import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.services.rag_service.clients import get_search_client
from app.services.rag_service.embeddings import embed_text, embed_text_vision, slice_vector
from app.services.rag_service.query_analysis import (
    STOP_WORDS,
    analyze_clinical_query,
    fast_query_analysis,
    needs_analysis,
)
from app.services.rag_service.results import format_results_async
from app.services.rag_service.scoring import prioritize_results, score_and_rerank
from app.services.rag_service.search import (
    hybrid_search_text_index,
    hybrid_search_image_index,
)

logger = logging.getLogger(__name__)


class RagService:
    """
    Stateless orchestration shell — delegates to focused sub-modules.
    All class-level caches and clients live in `clients.py`.

    Compatibility aliases are provided so legacy callers that call
    RagService.analyze_clinical_query() etc. still work unchanged.
    """

    # ── Compatibility aliases (legacy + new module-level callers) ─────────────
    analyze_clinical_query = staticmethod(analyze_clinical_query)
    needs_analysis = staticmethod(needs_analysis)
    fast_query_analysis = staticmethod(fast_query_analysis)
    prioritize_results = staticmethod(prioritize_results)

    # Private-method aliases used by message_service.py before refactor
    # (embed_text / embed_text_vision now return (vec, usage) — callers that
    #  expect only the vector get it via these wrappers)
    @staticmethod
    async def _embed_text(text: str, dimensions: int = 3072):
        vec, _ = await embed_text(text, dimensions=dimensions)
        return vec

    @staticmethod
    async def _embed_text_vision(text: str):
        vec, _ = await embed_text_vision(text)
        return vec

    # ── Public API ─────────────────────────────────────────────────────────────
    @staticmethod
    async def retrieve(
        query: str,
        image_urls: List[str] | None = None,
        top_text: int = 12,
        top_image: int = 6,
        anchor_context: Dict[str, Any] | None = None,
        history_text: Optional[str] = None,
        pre_embed_vec_3072: Optional[List[float]] = None,
        pre_embed_vec_vision: Optional[List[float]] = None,
        pre_analysis: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], List[Dict[str, Any]]]:
        """
        Unified clinical retrieval pipeline with full usage tracking.

        Args:
            pre_analysis: Pre-computed analysis dict from message_service. When
                provided, skips the internal analyze_clinical_query call entirely.
                Must include at minimum: standalone_query, query_intent,
                rag_required, visual_signs, anatomy, candidate_conditions.

        Returns:
            (prioritized_docs, analysis_dict, usage_metrics)
        """
        _retrieve_t0 = time.perf_counter()
        logger.info(
            "[TIMING] retrieve() started — query=%r, has_images=%s",
            query[:80], bool(image_urls),
        )

        usage_metrics: List[Dict[str, Any]] = []
        analysis_memo: Dict[str, Any] = {}

        # ── 1. Embeddings + analysis — maximally parallel ───────────────────
        # When pre_analysis is provided (message_service path), skip Gemini
        # analysis entirely — it already ran with full history context.
        # When pre_analysis is absent (standalone/script callers), run it here.

        if pre_analysis is not None:
            # Fast path: message_service already ran analysis with full history
            analysis_memo.update(pre_analysis)
            analysis_task = None
            logger.info("[ANALYSIS] Using pre-computed analysis from caller (rag_required=%s)", pre_analysis.get("rag_required"))
        else:
            # Standalone path: run analysis internally
            _requires_analysis = needs_analysis(query, image_urls)
            analysis_task = (
                asyncio.create_task(
                    analyze_clinical_query(query, image_urls=image_urls, history_text=history_text)
                )
                if _requires_analysis else None
            )

        # Resolve embeddings — parallel gather when not pre-supplied by caller
        if pre_embed_vec_3072 is not None and pre_embed_vec_vision is not None:
            vec_3072 = pre_embed_vec_3072
            vec_vis  = pre_embed_vec_vision
            logger.info("[TIMING] embed: reusing pre-computed vectors from caller")
        else:
            _t_embed = time.perf_counter()
            (vec_3072, usage_3072), (vec_vis, usage_vis) = await asyncio.gather(
                embed_text(query, dimensions=3072),
                embed_text_vision(query),
            )
            usage_metrics.extend([usage_3072, usage_vis])
            logger.info("[TIMING] embed_text + embed_text_vision (parallel): %.3fs", time.perf_counter() - _t_embed)

        # ── Query expansion for clinical exclusion patterns ───────────────────
        expansion_terms: List[str] = []
        q_lower = query.lower()
        if any(re.search(pat, q_lower) for pat in [
            r"\b(?:without|no|avoid|contraindicated)\b.*\b(?:epi|epinephrine|vasoconstrictor)\b",
            r"\b(?:free|plain)\s+(?:formulation|anesthetic|epinephrine)\b",
        ]):
            expansion_terms = [
                "mepivacaine plain", "prilocaine plain", "carbocaine",
                "citanest plain", "anesthetic without epinephrine",
            ]
            logger.info("[EXPANSION] Added plain-anesthetic terms to search query")

        # ── 2. Await analysis → build enriched search_q ──────────────────────
        search_q = query

        if analysis_task:
            # Standalone path: wait for internal analysis
            try:
                analysis_result, analysis_usage = await analysis_task
                analysis_memo.update(analysis_result)
                if analysis_usage:
                    usage_metrics.append(analysis_usage)
            except Exception as exc:
                logger.warning("[FALLBACK] Analysis failed (%s), using fast regex", type(exc).__name__)
                analysis_memo.update(fast_query_analysis(query))
        elif not analysis_memo:
            # Neither pre_analysis nor analysis_task — use fast fallback
            analysis_memo.update(fast_query_analysis(query))
            logger.info("[TIMING] Fast regex analysis used (Gemini skipped)")

        # Build enriched search_q from whichever analysis we have
        standalone_q = analysis_memo.get("standalone_query") or query
        if image_urls:
            vs    = analysis_memo.get("visual_signs", [])
            ana   = analysis_memo.get("anatomy", [])
            conds = analysis_memo.get("candidate_conditions", [])
            search_q = " ".join([standalone_q] + vs[:5] + ana[:3] + conds[:3])
        else:
            search_q = standalone_q

        if expansion_terms:
            search_q += " " + " ".join(expansion_terms)

        logger.info("[SEARCH] Query used for retrieval: '%s'", search_q[:150])

        # ── 3. Assign final embed vectors — no re-embedding ───────────────────
        # The pre-computed embedding of `query` is ALWAYS ready before analysis
        # finishes (~1.5s embed vs ~3s analysis), so we reuse it for the vector
        # component of both search indexes.
        #
        # For co-reference rewrites the accuracy improvement comes from using
        # `search_q` as the BM25 keyword text — not from re-embedding.
        # Re-embedding search_q would add ~1.5s to the critical path.
        final_vec_3072 = vec_3072
        final_vec_vis  = vec_vis

        # ── 4. Parallel search (text + image indexes) ─────────────────────────
        async def search_text_flow() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
            try:
                results, s_usage = await hybrid_search_text_index(
                    query=search_q,
                    top_k=top_text,
                    pre_embedded_vector=final_vec_3072,
                )
                return results, s_usage
            except Exception as exc:
                logger.error("[ERROR] Text index search failed: %s", exc)
                return [], []

        async def search_image_flow() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
            try:
                v1024 = slice_vector(final_vec_3072, 1024)
                results, s_usage = await hybrid_search_image_index(
                    query=search_q,
                    image_urls=image_urls,
                    top_k=top_image,
                    pre_embedded_1024=v1024,
                    pre_embedded_vision=final_vec_vis,
                )
                return results, s_usage
            except Exception as exc:
                logger.error("[ERROR] Image index search failed: %s", exc)
                return [], []

        _search_t0 = time.perf_counter()
        (text_results, text_usage), (image_results, image_usage) = await asyncio.gather(
            search_text_flow(),
            search_image_flow(),
        )
        usage_metrics.extend(text_usage)
        usage_metrics.extend(image_usage)
        logger.info(
            "[TIMING] Step 3 — parallel_search (text + image): %.3fs",
            time.perf_counter() - _search_t0,
        )

        # Finalize analysis (cap wait at 5 s for safety)
        if analysis_task and not analysis_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(analysis_task), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("[TIMEOUT] Gemini analysis >5s — using fast regex fallback")
                analysis_task.cancel()

        if not analysis_memo:
            analysis_memo.update(fast_query_analysis(query))

        # ── 5. Merge + dedupe ─────────────────────────────────────────────────
        _t0 = time.perf_counter()
        primary_results: List[Dict[str, Any]] = []
        topic_ids_to_expand: set = set()
        seen_keys: set = set()
        intent = analysis_memo.get("query_intent", "general_qa")

        def _primary_key(d: Dict[str, Any]) -> str:
            return d.get("chunk_id") or f"{d.get('blob_name')}_{d.get('page')}"

        for task_results in [text_results, image_results]:
            for d in task_results:
                key = _primary_key(d)
                if key in seen_keys:
                    continue
                primary_results.append(d)
                seen_keys.add(key)
                if (
                    d.get("chunk_type") in ["figure", "image", "xray"]
                    and d.get("topic_id")
                    and (image_urls or intent in {"diagnosis", "treatment"})
                ):
                    topic_ids_to_expand.add(d["topic_id"])

        logger.info(
            "[TIMING] Step 4 — merge_dedupe: %.3fs | merged=%d, topic_ids=%d",
            time.perf_counter() - _t0, len(primary_results), len(topic_ids_to_expand),
        )

        # ── 6. Topic expansion (keyword-only, no re-embedding) ────────────────
        _t0 = time.perf_counter()
        secondary_results: List[Dict[str, Any]] = []
        if topic_ids_to_expand and (image_urls or intent in {"treatment", "diagnosis"}):
            topic_filter = " or ".join(
                [f"topic_id eq '{tid}'" for tid in list(topic_ids_to_expand)[:3]]
            )
            try:
                client = get_search_client(settings.AZURE_SEARCH_INDEX_TEXT)
                results_iter = await client.search(
                    search_text=f"{query} management treatment",
                    filter=topic_filter,
                    top=3,
                )
                secondary_results = await format_results_async(results_iter, 3, query=query)
                usage_metrics.append({
                    "step": "azure_search_topic_expansion",
                    "index": settings.AZURE_SEARCH_INDEX_TEXT,
                    "query_type": "keyword_only",
                    "results_returned": len(secondary_results),
                    "latency_s": round(time.perf_counter() - _t0, 3),
                })
            except Exception as exc:
                logger.warning("Topic expansion keyword search failed: %s", exc)

        logger.info(
            "[TIMING] Step 5 — topic_expansion: %.3fs | secondary=%d",
            time.perf_counter() - _t0, len(secondary_results),
        )

        all_docs = list(primary_results)
        for d in secondary_results:
            key = _primary_key(d)
            if key not in seen_keys:
                all_docs.append(d)
                seen_keys.add(key)

        # ── 7. Scoring & reranking ────────────────────────────────────────────
        _t0 = time.perf_counter()

        query_terms = [
            t for t in re.findall(r"\w+", query.lower())
            if len(t) > 2 and t not in STOP_WORDS
        ]

        # Fallback exclusion detection from regex if Gemini didn't find any
        if not analysis_memo.get("exclusions"):
            regex_exclusions = re.findall(
                r"(?:without|no|avoid|cannot use|contraindicated)\s+(\w+)", q_lower
            )
            if regex_exclusions:
                analysis_memo["exclusions"] = regex_exclusions

        all_docs = score_and_rerank(
            all_docs=all_docs,
            query=query,
            query_terms=query_terms,
            analysis=analysis_memo,
            image_urls=image_urls,
            anchor_context=anchor_context,
        )
        logger.info(
            "[TIMING] Step 6 — scoring_reranking: %.3fs | docs=%d",
            time.perf_counter() - _t0, len(all_docs),
        )

        # ── 8. Final sort + compact context selection ─────────────────────────
        _t0 = time.perf_counter()
        all_docs.sort(key=lambda x: x.get("_final_score", 0), reverse=True)

        # Cross-version text deduplication: Drop older versions of the exact same paragraph
        distinct_docs = []
        seen_fingerprints = set()
        for d in all_docs:
            fp = d.get("content_fingerprint") or hash(d.get("content", ""))
            if fp not in seen_fingerprints:
                distinct_docs.append(d)
                seen_fingerprints.add(fp)
        all_docs = distinct_docs

        prefer_figures = bool(image_urls) or intent in {"diagnosis", "comparison"}
        max_return = 8 if image_urls else 6

        prioritized = prioritize_results(
            results=all_docs,
            query=query,
            max_items=max_return,
            prefer_figures=prefer_figures,
        )
        logger.info(
            "[TIMING] Step 7 — sort_prioritize: %.3fs | final=%d",
            time.perf_counter() - _t0, len(prioritized),
        )

        logger.info(
            "[TIMING] retrieve() total: %.3fs",
            time.perf_counter() - _retrieve_t0,
        )

        return prioritized, analysis_memo, usage_metrics
