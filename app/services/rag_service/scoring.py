"""
Post-retrieval scoring, reranking, and result prioritization.

Performance improvements vs original:
  - Query-term regex patterns are pre-compiled ONCE outside the per-doc loop
    (was O(n×m×fields), now O(m) compile + O(n×fields) match)
  - Per-doc: hits against combined_text computed ONCE and cached as a set.
    The density boost and field-match boost both reuse this set — eliminates
    m redundant regex execs per document (was searched 2–3 times separately).
  - Pattern pre-filter for field-match loop: only patterns that hit combined_text
    can hit individual fields → skip guaranteed misses.
  - PROCEDURAL_TERMS is a module-level frozenset (was rebuilt each retrieve() call)
"""
import logging
import re
from typing import Any, Dict, List, Optional

import numpy as np

from app.services.rag_service.query_analysis import STOP_WORDS

logger = logging.getLogger(__name__)

# Module-level constant — was incorrectly defined inside retrieve() body
PROCEDURAL_TERMS = frozenset({
    "sectioning", "division", "dividing", "separating", "split", "splitting",
    "elevation", "elevating", "luxation", "suturing", "flap", "incising",
    "bone removal", "ostectomy", "root removal", "fragment removal",
})

# Pre-compiled procedural patterns (module-level, not rebuilt per request)
_PROCEDURAL_PATTERNS = {
    pt: re.compile(rf"\b{re.escape(pt)}\b")
    for pt in PROCEDURAL_TERMS
}


def score_and_rerank(
    all_docs: List[Dict[str, Any]],
    query: str,
    query_terms: List[str],
    analysis: Dict[str, Any],
    image_urls: Optional[List] = None,
    anchor_context: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Apply all scoring boosts, penalties, and relevance gates to `all_docs`.

    Mutates `_final_score` in-place on each doc and returns the same list.

    Performance notes:
    - Per-term regex patterns compiled once before the document loop: O(m) vs O(n×m)
    - Per-doc: combined_text searched once; hit set reused for density + field boosts
    - Field-match loop uses pre-filter: only check fields for patterns already in hit set
    - Procedural term patterns compiled at module import (not per request)
    """
    intent = analysis.get("query_intent", "general_qa")
    modality = analysis.get("modality")
    confidence = float(analysis.get("modality_confidence") or 0.0)
    anatomy = [a.lower() for a in (analysis.get("anatomy") or [])]
    visual_signs = [s.lower() for s in (analysis.get("visual_signs") or [])]
    candidate_conditions = [c.lower() for c in (analysis.get("candidate_conditions") or [])]
    exclusions = [e.lower() for e in (analysis.get("exclusions") or [])]

    # ── Pre-compile per-term regex patterns once (O(m) vs O(n×m×fields)) ─────
    term_patterns = {t: re.compile(rf"\b{re.escape(t)}\b") for t in query_terms}
    anatomy_patterns = {a: re.compile(rf"\b{re.escape(a)}\b") for a in anatomy}
    sign_patterns = {s: re.compile(rf"\b{re.escape(s)}\b") for s in visual_signs}
    cond_patterns = {c: re.compile(rf"\b{re.escape(c)}\b") for c in candidate_conditions}

    for d in all_docs:
        score = float(d.get("reranker_score") or d.get("score") or 0)

        chunk_content    = (d.get("content") or "").lower()
        chunk_caption    = (d.get("caption") or "").lower()
        chunk_clinical   = (d.get("clinical_description") or "").lower()
        chunk_chapter    = (d.get("chapter_name") or "").lower()
        chunk_topic      = (d.get("topic_name") or "").lower()
        chunk_subheading = (d.get("subheading_name") or "").lower()

        chunk_anatomy    = [a.lower() for a in (d.get("anatomy") or [])]
        chunk_signs      = [s.lower() for s in (d.get("visual_signs") or [])]
        chunk_conditions = [c.lower() for c in (d.get("candidate_conditions") or [])]

        combined_text = " ".join([
            chunk_content, chunk_caption, chunk_clinical,
            chunk_chapter, chunk_topic, chunk_subheading,
        ])
        metadata_text = " ".join([chunk_chapter, chunk_topic, chunk_subheading])

        # ── Cache regex hits against combined_text ONCE per doc ───────────────
        # Reused for density boost, field-match boost, and metadata boost.
        # Patterns not hitting combined_text cannot hit any individual field —
        # skip them in the field-match loop (pre-filter eliminates misses).
        combined_hits = {
            t for t, pat in term_patterns.items()
            if pat.search(combined_text)
        }
        combined_hit_patterns = {t: term_patterns[t] for t in combined_hits}

        # ── Metadata-aware lexical boost ──────────────────────────────────────
        # Only check patterns that are already in combined_hits (pre-filter)
        metadata_hits = sum(
            1 for t, pat in combined_hit_patterns.items()
            if pat.search(metadata_text)
        )
        if metadata_hits > 0:
            score *= min(1.20, 1.0 + (metadata_hits * 0.05))

        # ── All-field lexical agreement boost ─────────────────────────────────
        # Only iterate patterns known to hit combined_text (pre-filter).
        # This reduces from O(m × 6 fields) to O(hits × 6 fields).
        field_texts = [
            chunk_chapter, chunk_topic, chunk_subheading,
            chunk_content, chunk_caption, chunk_clinical,
        ]
        field_match_score = 0.0
        for pat in combined_hit_patterns.values():
            for field_text in field_texts:
                if field_text and pat.search(field_text):
                    field_match_score += 1
        if field_match_score > 0:
            score *= min(1.30, 1.0 + (field_match_score * 0.03))

        # ── Query-term density boost (reuses combined_hits — no extra regex) ──
        if query_terms:
            density = len(combined_hits) / max(len(query_terms), 1)
            if density > 0.4:
                score *= 1.0 + (density * 0.5)

        # ── Modality boost ────────────────────────────────────────────────────
        if modality and modality != "unknown" and d.get("modality") == modality:
            score *= 1.0 + (confidence * 0.2)

        # ── Anatomy boost ─────────────────────────────────────────────────────
        if anatomy:
            anatomy_list = " ".join(chunk_anatomy)
            if any(pat.search(combined_text) or pat.search(anatomy_list) for pat in anatomy_patterns.values()):
                score *= 1.3

        # ── Visual signs boost ────────────────────────────────────────────────
        if visual_signs:
            signs_list = " ".join(chunk_signs)
            hits = sum(1 for pat in sign_patterns.values() if pat.search(combined_text) or pat.search(signs_list))
            if hits > 0:
                multiplier = min(2.0, 1.0 + (0.5 * hits))
                if d.get("chunk_type") in ["figure", "image", "xray"]:
                    score *= multiplier
                else:
                    score *= min(1.3, multiplier)

        # ── Procedural boost (pre-compiled module-level patterns) ─────────────
        if d.get("chunk_type") in ["figure", "image", "xray"] and (
            image_urls or intent in {"diagnosis", "treatment"}
        ):
            proc_hits = sum(
                1 for pat in _PROCEDURAL_PATTERNS.values()
                if pat.search(combined_text)
            )
            if proc_hits > 0:
                score *= 1.8
            if intent == "treatment" and any(
                pt in chunk_caption for pt in ["procedure", "step", "how", "technique", "management"]
            ):
                score *= 1.3
            elif intent == "diagnosis" and any(
                dt in chunk_caption for dt in ["pathology", "disease", "condition", "signs", "findings"]
            ):
                score *= 1.3

        # ── Contextual anchoring ──────────────────────────────────────────────
        if anchor_context:
            anchor_chunk = anchor_context.get("chunk_id")
            anchor_topic = anchor_context.get("topic_id")
            anchor_doc   = anchor_context.get("document_name")
            if anchor_chunk and d.get("chunk_id") == anchor_chunk:
                score *= 2.0
            elif anchor_topic and d.get("topic_id") == anchor_topic:
                score *= 1.5
            elif anchor_doc and d.get("document_name") == anchor_doc:
                score *= 1.1

        # ── Candidate condition boost ─────────────────────────────────────────
        if candidate_conditions:
            conds_list = " ".join(chunk_conditions)
            cond_hits = sum(1 for pat in cond_patterns.values() if pat.search(combined_text) or pat.search(conds_list))
            if cond_hits > 0:
                score *= 1.2 + (0.3 * confidence)

        # ── Intent weighting ──────────────────────────────────────────────────
        if intent == "treatment" and d.get("chunk_type") == "text":
            score *= 1.2
        elif intent == "diagnosis" and d.get("chunk_type") in ["figure", "image", "xray"]:
            score *= 1.1

        # ── XLSX clinical case boost ──────────────────────────────────────────
        if d.get("source_type") == "xlsx":
            score *= 5.0

        # ── Exclusion-signal penalty ──────────────────────────────────────────
        for exc_term in exclusions:
            if exc_term in chunk_topic or exc_term in chunk_subheading or exc_term in chunk_chapter:
                has_alternative = any(
                    w in chunk_content for w in
                    ["without", "plain", "alternative", "substitute", "instead", "no vasoconstrictor"]
                )
                if not has_alternative:
                    score *= 0.2
                    logger.debug("[PENALTY] Exclusion '%s' matched chunk %s", exc_term, d.get("chunk_id"))

        # ── Version boost — newer document versions rank slightly higher ──────
        doc_version = d.get("document_version")
        if doc_version and isinstance(doc_version, (int, float)) and doc_version > 1:
            version_boost = min(1.15, 1.0 + (0.05 * (doc_version - 1)))
            score *= version_boost
            
        if d.get("is_latest") is True:
            score *= 1.2

        d["_final_score"] = score

    # ── Off-topic relevance gate ──────────────────────────────────────────────
    if all_docs and query_terms:
        scores = [d["_final_score"] for d in all_docs]
        median_score = float(np.median(scores))
        for d in all_docs:
            mtxt = " ".join([
                d.get("chapter_name") or "",
                d.get("topic_name") or "",
                d.get("subheading_name") or "",
            ]).lower()
            if (
                not any(pat.search(mtxt) for pat in term_patterns.values())
                and d["_final_score"] < median_score
            ):
                d["_final_score"] *= 0.5
                logger.debug("[GATE] Demoting off-topic chunk: %s", d.get("chunk_id"))

    # ── Image relevance gate (text-only queries) ──────────────────────────────
    if not image_urls:
        best_text_reranker = max(
            (float(d.get("reranker_score") or d.get("score") or 0)
             for d in all_docs if d.get("chunk_type") == "text"),
            default=0.0,
        )
        if best_text_reranker > 0:
            for d in all_docs:
                if d.get("chunk_type") not in ["figure", "image", "xray"]:
                    continue
                img_base = float(d.get("reranker_score") or d.get("score") or 0)
                ratio = img_base / best_text_reranker
                if ratio < 0.4:
                    d["_final_score"] *= 0.1
                    logger.debug("[GATE] Image %s: ratio=%.2f (<0.4) → 0.1x", d.get("chunk_id"), ratio)
                elif ratio < 0.7:
                    d["_final_score"] *= 0.4
                    logger.debug("[GATE] Image %s: ratio=%.2f (<0.7) → 0.4x", d.get("chunk_id"), ratio)

    return all_docs


def prioritize_results(
    results: List[Dict[str, Any]],
    query: str,
    max_items: int,
    prefer_figures: bool = False,
) -> List[Dict[str, Any]]:
    """
    Adaptive post-retrieval prioritization.

    Rules:
    1. Sort by _final_score descending
    2. Optionally force a figure near the top for image-driven queries
    3. Deduplicate near-identical chunks (same doc + page + content prefix)
    4. Diversity cap: max 3 chunks per unique document (relaxed if needed)
    """
    if not results:
        return results

    ranked = sorted(
        results,
        key=lambda x: (x.get("_final_score", 0), x.get("score", 0)),
        reverse=True,
    )
    figures = [r for r in ranked if str(r.get("chunk_type", "")).lower() in ("figure", "image", "xray")]

    selected: List[Dict[str, Any]] = []
    seen_keys: set = set()
    doc_counts: Dict[str, int] = {}

    def _content_key(item: Dict[str, Any]) -> str:
        content = (item.get("content") or "").strip().lower()
        page = item.get("page")
        doc = (item.get("document_name") or "").strip().lower()
        return f"{doc}|{page}|{content[:220]}"

    # 1. Force best figure near top when explicitly preferred
    if prefer_figures and figures:
        best_fig = figures[0]
        key = _content_key(best_fig)
        doc_name = (best_fig.get("document_name") or "Unknown").strip().lower()
        selected.append(best_fig)
        seen_keys.add(key)
        doc_counts[doc_name] = 1

    # 2. Fill with diverse chunks (max 3 per document)
    for item in ranked:
        if len(selected) >= max_items:
            break
        key = _content_key(item)
        if not key.strip("|") or key in seen_keys:
            continue
        doc_name = (item.get("document_name") or "Unknown").strip().lower()
        if doc_counts.get(doc_name, 0) >= 3:
            continue
        selected.append(item)
        seen_keys.add(key)
        doc_counts[doc_name] = doc_counts.get(doc_name, 0) + 1

    # 3. Relax diversity rule if we still need more items
    if len(selected) < max_items:
        for item in ranked:
            if len(selected) >= max_items:
                break
            key = _content_key(item)
            if key not in seen_keys:
                selected.append(item)
                seen_keys.add(key)

    return selected[:max_items]
