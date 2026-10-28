"""
Query analysis for the RAG pipeline.

Provides two analysis paths:
  - Fast regex path (fast_query_analysis): zero API calls, for simple queries
  - Gemini path (analyze_clinical_query): full pre-retrieval analysis including
    rag_required determination — called from message_service AFTER history is
    available so the decision has full conversational context.

Model selection:
  - Image queries  → GEMINI_CHAT_MODEL  (better multimodal clinical understanding)
  - Text queries   → GEMINI_ANALYSIS_MODEL (fast/cheap, sufficient for rewriting)

Performance:
  - LLM clients cached as module-level singletons (no per-request construction)
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI

from app.config import settings

logger = logging.getLogger(__name__)

CANONICAL_MODALITIES = [
    "periapical_xray",
    "panoramic_xray",
    "intraoral_photo",
    "bitewing_xray",
    "cbct",
    "histology",
    "clinical_illustration",
    "diagram",
]

# Stop words for query-term filtering (domain-agnostic)
STOP_WORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would",
    "can", "could", "should", "may", "might", "and", "but", "or",
    "not", "for", "with", "from", "into", "that", "this", "these",
    "those", "they", "them", "their", "its", "she", "her", "his",
    "who", "which", "what", "when", "where", "how", "why",
    "all", "each", "any", "some", "also", "about", "very", "just",
    "than", "too", "only", "so", "yet", "such", "own", "same",
    "need", "needs", "use", "using", "used", "states", "taking",
    "several", "many", "most", "get", "got", "like",
})


# ── LLM singletons — created once, reused on every request ────────────────────
_analysis_llm_text: Optional[ChatGoogleGenerativeAI] = None   # fast model, text-only
_analysis_llm_image: Optional[ChatGoogleGenerativeAI] = None  # main model, image queries


def _get_analysis_llm(has_images: bool) -> Tuple[ChatGoogleGenerativeAI, str]:
    """Return a cached LLM client and model name for query analysis."""
    global _analysis_llm_text, _analysis_llm_image

    if has_images:
        if _analysis_llm_image is None:
            _analysis_llm_image = ChatGoogleGenerativeAI(
                model=settings.GEMINI_CHAT_MODEL,
                project=settings.GOOGLE_CLOUD_PROJECT,
                location=settings.GOOGLE_CLOUD_LOCATION,
                credentials=settings.get_vertex_credentials(),
                vertexai=True,
                temperature=0,
                thinking_budget=0,
            )
        return _analysis_llm_image, settings.GEMINI_CHAT_MODEL
    else:
        if _analysis_llm_text is None:
            _analysis_llm_text = ChatGoogleGenerativeAI(
                model=settings.GEMINI_ANALYSIS_MODEL,
                project=settings.GOOGLE_CLOUD_PROJECT,
                location=settings.GOOGLE_CLOUD_LOCATION,
                credentials=settings.get_vertex_credentials(),
                vertexai=True,
                temperature=0,
                thinking_budget=0,
            )
        return _analysis_llm_text, settings.GEMINI_ANALYSIS_MODEL


def needs_analysis(query: str, image_urls: Optional[List[str]] = None) -> bool:
    """
    Heuristic guard for retrieve()-only callers (scripts, background jobs).
    message_service always runs analysis, so this is only used internally.
    """
    if image_urls:
        return True
    pronouns = {"it", "this", "that", "those", "they", "them", "these", "it's"}
    query_words = set(re.findall(r"\w+", query.lower()))
    if query_words.intersection(pronouns):
        return True
    ambiguous = ["show me", "how", "why", "tell me more", "explain", "steps", "more"]
    clean_q = query.lower().strip()
    if any(clean_q.startswith(a) for a in ambiguous) or len(clean_q.split()) < 3:
        return True
    clinical_signals = {
        "patient", "medication", "medications", "drug", "dosage",
        "contraindic", "allergy", "allergic", "medical history",
        "best", "preferred", "recommend", "first choice",
    }
    if any(s in query.lower() for s in clinical_signals):
        return True
    return False


def fast_query_analysis(query: str) -> Dict[str, Any]:
    """
    Zero-cost query structure analysis via regex.
    Used as a fallback when Gemini analysis fails or is skipped.
    Always sets rag_required=True (conservative default).
    """
    q_lower = query.lower()
    intent = "general_qa"
    if any(p in q_lower for p in [
        "best", "preferred", "recommend", "which should",
        "what can i use", "what should i use", "ideal",
        "drug of choice", "first choice", "first line",
    ]):
        intent = "recommendation"
    elif any(p in q_lower for p in [
        "treat", "management", "manage", "procedure",
        "how to", "technique", "protocol",
    ]):
        intent = "treatment"
    elif any(p in q_lower for p in ["diagnos", "identify", "differentiat", "classify"]):
        intent = "diagnosis"
    elif any(p in q_lower for p in ["explain", "what is", "define", "describe", "mechanism"]):
        intent = "explanation"
    elif any(p in q_lower for p in ["compare", "difference", "versus", "vs"]):
        intent = "comparison"

    exclusions = re.findall(
        r"(?:without|no|avoid|cannot use|contraindicated)\s+(\w+)", q_lower
    )
    query_terms = [t for t in re.findall(r"\w+", q_lower) if len(t) > 2]

    return {
        "standalone_query": query,
        "is_dental_query": True,    # conservative — let the LLM gate it
        "modality": "unknown",
        "modality_confidence": 0.0,
        "anatomy": [],
        "visual_signs": [],
        "candidate_conditions": [],
        "clinical_entities": [],
        "query_intent": intent,
        "query_terms": query_terms,
        "exclusions": exclusions,
        "rag_required": True,       # conservative default
        "rag_rationale": "fast-path fallback — defaulting to retrieval",
    }


async def analyze_clinical_query(
    query: str,
    image_urls: Optional[List[str]] = None,
    history_text: Optional[str] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Pre-retrieval analysis with RAG gating.

    Called from message_service AFTER history is available so rag_required
    has full conversational context. Returns (analysis_dict, usage_metric).

    New field — rag_required (bool):
      False ONLY when ALL of these hold:
        • No images in the current message
        • The last 1-3 assistant turns in history contain a COMPLETE, SPECIFIC
          answer to this exact question (not just related information)
        • The query is cosmetic: summarize, repeat, rephrase, bullet-point
          the previous answer — no new clinical entity or drug introduced
      True for everything else (conservative — better to over-retrieve).

    Model selection:
      • Images present → GEMINI_CHAT_MODEL (multimodal clinical understanding)
      • Text only      → GEMINI_ANALYSIS_MODEL (faster, cheaper)
    """
    try:
        model, model_name = _get_analysis_llm(has_images=bool(image_urls))

        prompt_text = f"""You are a dental clinical AI assistant pre-processing a user query before textbook retrieval.

USER QUERY: '{query}'

RECENT CHAT HISTORY (last 3 turns):
{history_text or "None"}

TASK: Analyze the query and return a JSON object with the fields below.

--- FIELDS ---

1. "is_dental_query" (boolean)
   Set to TRUE if the query is related to dentistry, oral health, dental procedures,
   dental anatomy, dental radiology, oral pathology, dental pharmacology, dental materials,
   clinical dentistry, or any topic a dental professional would look up in a dental textbook.
   Set to FALSE if the query is clearly unrelated to dentistry — e.g. general trivia,
   weather, geography, animal facts, cooking, sports, history, etc.
   When in doubt (e.g. a general medical question that could relate to dentistry), set TRUE.

2. "standalone_query"
   Rewrite the query to be fully self-contained, replacing any pronouns (it, this, that)
   or vague references with the actual drug/condition/topic from history.
   If the query is already self-contained, return it unchanged.

3. "rag_required" (boolean — DEFAULT TRUE — be conservative)
   Set to FALSE only when ALL of the following are true:
     a) No images are attached to this message
     b) The chat history above contains a COMPLETE and SPECIFIC answer to this exact question
        (not just loosely related information)
     c) The query is purely cosmetic: e.g. "summarize that", "give me bullet points",
        "say that again", "make it shorter" — adds NO new clinical topic, drug, or anatomy
   Set to TRUE for:
     - Any clinical question (drugs, dosages, procedures, diagnoses, anatomy)
     - Any image analysis request
     - Any question introducing a new topic not in history
     - When uncertain — default to TRUE

4. "rag_rationale" (string, brief)
   One sentence explaining why rag_required is true or false.

5. "modality" (one of: {', '.join(CANONICAL_MODALITIES)}, or "unknown")

6. "modality_confidence" (0.0 to 1.0)

7. "anatomy" (list of specific anatomical locations, teeth, or quadrants visible/relevant)

8. "visual_signs" (list of observable clinical or radiographic signs — for image queries)

9. "candidate_conditions" (list of likely diagnoses — for scoring only, not shown to user)

10. "clinical_entities" (list of drugs, medications, or specific substances mentioned)

11. "query_intent" (one of: 'diagnosis', 'treatment', 'recommendation', 'explanation', 'comparison', 'general_qa')

12. "exclusions" (list of substances/treatments to exclude, e.g. if user says "without epinephrine")

Return ONLY a JSON object. No markdown fences, no explanation."""

        content_parts: List[Any] = [{"type": "text", "text": prompt_text}]
        if image_urls:
            for url in image_urls:
                content_parts.append({"type": "image_url", "image_url": {"url": url}})

        _t0 = time.perf_counter()
        resp = await model.ainvoke([HumanMessage(content=content_parts)])
        elapsed = time.perf_counter() - _t0
        logger.info("[TIMING] analyze_clinical_query (%s): %.3fs", model_name, elapsed)

        raw_content = resp.content
        if isinstance(raw_content, list):
            raw_content = "".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in raw_content
            )
        content = raw_content.strip()
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].strip()

        result = json.loads(content)

        # Ensure is_dental_query defaults to True when missing or ambiguous
        if "is_dental_query" not in result:
            result["is_dental_query"] = True
        result["is_dental_query"] = bool(result["is_dental_query"])

        # Force is_dental_query=True when images are attached (assume dental image)
        if image_urls:
            result["is_dental_query"] = True

        # Ensure rag_required defaults to True when missing or ambiguous
        if "rag_required" not in result:
            result["rag_required"] = True
        result["rag_required"] = bool(result["rag_required"])

        # Force rag_required=True when images are present (never skip)
        if image_urls:
            result["rag_required"] = True

        usage: Dict[str, Any] = {}
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            meta = resp.usage_metadata
            if isinstance(meta, dict):
                input_tokens = (
                    meta.get("input_tokens")
                    or meta.get("prompt_token_count")
                    or 0
                )
                output_tokens = (
                    meta.get("output_tokens")
                    or meta.get("candidates_token_count")
                    or 0
                )
            else:
                input_tokens = (
                    getattr(meta, "input_tokens", None)
                    or getattr(meta, "prompt_token_count", None)
                    or 0
                )
                output_tokens = (
                    getattr(meta, "output_tokens", None)
                    or getattr(meta, "candidates_token_count", None)
                    or 0
                )
            usage = {
                "step": "query_analysis",
                "model": model_name,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "latency_s": round(elapsed, 3),
            }

        rag_str = "SKIP" if not result["rag_required"] else "REQUIRED"
        logger.info(
            "[ANALYSIS] is_dental=%s | rag_required=%s | intent=%s | standalone_query=%r | rationale=%s",
            result.get("is_dental_query", True),
            rag_str,
            result.get("query_intent", "?"),
            result.get("standalone_query", query)[:80],
            result.get("rag_rationale", ""),
        )

        return result, usage

    except Exception as exc:
        logger.warning("Query analysis failed (%s) — defaulting rag_required=True: %s", type(exc).__name__, exc)
        return {
            "standalone_query": query,
            "is_dental_query": True,  # conservative — don't block on analysis failure
            "modality": "unknown",
            "modality_confidence": 0,
            "query_intent": "general_qa",
            "rag_required": True,
            "rag_rationale": "analysis failed — defaulting to retrieval",
            "anatomy": [],
            "visual_signs": [],
            "candidate_conditions": [],
            "clinical_entities": [],
            "exclusions": [],
        }, {}
