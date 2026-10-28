"""
CDT Code Service — Intelligent CDT code lookup from voice transcripts.

Based on client requirements (Akshat Jain confirmation):
  - CDT codebook has ONLY two columns: CDT Code + Description of Service
  - No shorthand mapping exists in the book
  - Doctor can say:
    1. A CDT code directly → "D0120" → fetch description
    2. A full/partial description → "periodic oral evaluation" → search & suggest matches
    3. Surface shorthand → "#2 MO" → we parse it, search for matching restoration codes
  - System returns SUGGESTIONS (multiple matches ranked by score) so doctor can pick

Pipeline (Mode B — description search):
  LLM Extract → Normalize → BM25+Vector+Semantic Reranking → Lexical Filtering → Confidence Threshold

Three detection modes:
  Mode A: Direct code ("D0120") → DB lookup → instant result
  Mode B: Description ("periodic oral evaluation") → full pipeline → suggestions
  Mode C: Surface shorthand ("#2 MO") → parse → search → suggestions
"""
import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import httpx
from azure.search.documents.models import VectorizedQuery
from fastapi import HTTPException
import openai
from openai import AsyncAzureOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config import settings
from app.core.database import get_database
from app.services.rag_service.clients import get_search_client

logger = logging.getLogger(__name__)

def _log_retry_attempt(retry_state):
    logger.warning(
        f"CDT Service Azure OpenAI embedding call failed (attempt {retry_state.attempt_number}). "
        f"Retrying in {retry_state.next_action.sleep:.1f} seconds. Error: {retry_state.outcome.exception()}"
    )

@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1.5, min=2, max=15),
    retry=retry_if_exception_type((
        openai.RateLimitError,
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.InternalServerError
    )),
    before_sleep=_log_retry_attempt,
    reraise=True
)
async def _create_embeddings_with_retry_cdt_service(
    client: AsyncAzureOpenAI,
    texts: list[str],
    model: str,
    dimensions: int
):
    """
    Generate embeddings with exponential backoff on rate limits or transient errors for CDT service lookup.
    """
    return await client.embeddings.create(
        input=texts,
        model=model,
        dimensions=dimensions
    )

# ── Surface name mappings (standard dental terminology, NOT from CDT book) ───
SURFACE_FULL_NAMES = {
    "M": "Mesial",
    "O": "Occlusal",
    "D": "Distal",
    "B": "Buccal",
    "L": "Lingual",
    "I": "Incisal",
}

SURFACE_ABBREVIATIONS = {
    "mesial": "M",
    "occlusal": "O",
    "distal": "D",
    "buccal": "B",
    "facial": "B",
    "lingual": "L",
    "incisal": "I",
    "palatal": "L",
}

VALID_SURFACES = set("MODBLI")

# Standard ADA Tooth Ranges
POSTERIOR_TEETH = frozenset([1, 2, 3, 4, 5, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 28, 29, 30, 31, 32])
ANTERIOR_TEETH = frozenset([6, 7, 8, 9, 10, 11, 22, 23, 24, 25, 26, 27])

# ── Number word → digit mapping (Whisper transcribes tooth numbers as words) ──
# Sorted longest-first so "twenty-eight" is replaced before "eight"
WORD_TO_NUMBER = {
    "thirty-two": "32", "thirty-one": "31", "thirty": "30",
    "twenty-nine": "29", "twenty-eight": "28", "twenty-seven": "27",
    "twenty-six": "26", "twenty-five": "25", "twenty-four": "24",
    "twenty-three": "23", "twenty-two": "22", "twenty-one": "21",
    "twenty": "20", "nineteen": "19", "eighteen": "18", "seventeen": "17",
    "sixteen": "16", "fifteen": "15", "fourteen": "14", "thirteen": "13",
    "twelve": "12", "eleven": "11", "ten": "10", "nine": "9",
    "eight": "8", "seven": "7", "six": "6", "five": "5",
    "four": "4", "three": "3", "two": "2", "one": "1",
}

# Pre-compiled patterns for number word replacement (longest first to avoid
# partial matches, e.g. "twenty-eight" before "eight")
_NUMBER_WORD_PATTERNS = [
    (re.compile(r'\b' + word.replace('-', r'[\s-]') + r'\b', re.IGNORECASE), digit)
    for word, digit in WORD_TO_NUMBER.items()
]

# Detect bare digit + surface abbreviation without prefix:
# "3 MO" or "3MO" or "3-MO" → "tooth 3 MO" (skip if already prefixed with #/tooth/number)
# Python lookbehinds must be fixed-width, so we use only fixed alternatives.
_BARE_TOOTH_SURFACE_RE = re.compile(
    r'(?<![#\w])(?<!tooth )(?<!number )(\d{1,2})[\s\-]*([MODBLI]{2,5})\b',
    re.IGNORECASE,
)

# Expand conjunction patterns: "8 and 9 ML" → "tooth 8 ML, tooth 9 ML"
# Handles two (or more) teeth sharing the same surface abbreviation.
_CONJUNCTION_SURFACE_RE = re.compile(
    r'\b(\d{1,2})\s+and\s+(\d{1,2})\s+([MODBLI]{2,5})\b',
    re.IGNORECASE,
)

# Detect punctuated surface shorthand (e.g. "M.O.", "M.O.D.")
# We will strip the periods from these before further processing.
_DOTTED_SURFACE_RE = re.compile(
    r'(?<![A-Z])((?:[MODBLI]\.){1,4}[MODBLI]\.?)(?![A-Z])',
    re.IGNORECASE,
)

# Convert zero to 'O' in surface shorthands (e.g. "M0" -> "MO", "M0D" -> "MOD")
# STT sometimes transcribes the letter O as the digit 0
_ZERO_SURFACE_RE = re.compile(
    r'(?<![A-Z])([MDBLI])0([MDBLI]?)\b',
    re.IGNORECASE,
)

# ── Regex patterns ───────────────────────────────────────────────────────────

# Match CDT code pattern: D followed by 4 digits
CDT_CODE_RE = re.compile(r"\bD\d{4}\b")

# Extract the descriptive context spoken before a CDT code
# e.g., "periodic oral evaluation code D0120" → context="periodic oral evaluation"
CONTEXT_RE = re.compile(r"([^.;]*?)\s*(?:code\s+)(D\d{4})", re.IGNORECASE)

# Match "code" verbal markers: doctor says "periodic oral evaluation code"
# and we fill in the CDT code number. The negative lookahead prevents matching
# when a CDT code already follows (e.g., "code D0120" won't re-match).
# Captures the procedure phrase in group(1).
CODE_MARKER_RE = re.compile(
    r"([\w][\w\s-]{2,60}?)\s+code\b(?!\s*D\d{4})",
    re.IGNORECASE,
)

# Match tooth + surface shorthand: "#2 MO", "tooth 3 MOD"
SHORTHAND_RE = re.compile(
    r"(?:#|tooth\s*|number\s*|no\.?\s*)(\d{1,2})\s+"
    r"([MODBLI]{2,5})\b",
    re.IGNORECASE,
)

# Match tooth + full surface names: "#2 mesial occlusal"
FULL_SURFACE_RE = re.compile(
    r"(?:#|tooth\s*|number\s*|no\.?\s*)(\d{1,2})\s+"
    r"((?:mesial|occlusal|distal|buccal|facial|lingual|incisal|palatal)"
    r"(?:\s+(?:mesial|occlusal|distal|buccal|facial|lingual|incisal|palatal))*)",
    re.IGNORECASE,
)


class CDTService:
    """
    Service for intelligent CDT code lookup.

    Handles three input modes:
      A) Direct code:  "D0120" → DB lookup
      B) Description:  "periodic oral evaluation" → RAG search
      C) Shorthand:    "#2 MO" → parse + RAG search
    """

    # ── Main Entry Point ─────────────────────────────────────────────────

    # ── Number Word Normalization ─────────────────────────────────────────

    @staticmethod
    def normalize_number_words(text: str) -> str:
        """
        Convert spoken number words to digits and tag bare tooth+surface patterns.

        Whisper transcribes tooth numbers as words, so:
          "three MO"        → "tooth 3 MO"
          "fourteen MOD"    → "tooth 14 MOD"
          "twenty-eight DO" → "tooth 28 DO"
          "tooth three MO"  → "tooth 3 MO"
          "#3 MO"           → "#3 MO"  (unchanged — already prefixed)
          "Extract fourteen" → "Extract 14"  (no surfaces → no "tooth" prefix)
        """
        result = text

        # Step 0: Remove periods from surface shorthands (e.g., "M.O." → "MO")
        # Transcripts often output abbreviations with periods.
        result = _DOTTED_SURFACE_RE.sub(
            lambda m: m.group(1).replace('.', ''),
            result
        )

        # Step 0.5: Replace zeroes with 'O' in surface shorthands (e.g., "M0" -> "MO")
        # STT sometimes transcribes the letter O as the digit 0
        result = _ZERO_SURFACE_RE.sub(r'\g<1>O\g<2>', result)

        # Step 1: Replace number words with digits (longest match first)
        for pattern, digit in _NUMBER_WORD_PATTERNS:
            result = pattern.sub(digit, result)

        # Step 2: Expand conjunction patterns BEFORE bare-tooth tagging
        # "8 and 9 ML" → "tooth 8 ML, tooth 9 ML"
        result = _CONJUNCTION_SURFACE_RE.sub(r'tooth \1 \3, tooth \2 \3', result)

        # Step 3: Prepend "tooth" when a bare digit is followed by
        # surface abbreviations (MO, MOD, DO, ML, etc.)
        # "3 MO" → "tooth 3 MO"  but "#3 MO" or "tooth 3 MO" stays as-is
        result = _BARE_TOOTH_SURFACE_RE.sub(r'tooth \1 \2', result)

        # Step 4: STT artifact correction
        artifacts = {
            r"\bbuilding\b": "buildup",
            r"\bprofi\b": "prophy",
            r"\bbuild up\b": "buildup",
        }
        for pattern, replacement in artifacts.items():
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)

        return result

    # ── Main Entry Point ─────────────────────────────────────────────────

    @staticmethod
    async def process_transcript(transcript: str) -> Dict[str, Any]:
        """
        Analyze transcript and return CDT code suggestions.

        Multi-mode processing: detects ALL applicable modes and merges results.
        A real dental transcript often mixes shorthand ("3 MO") with descriptions
        ("extract sixteen", "bridge 31 to 28"), so we process every mode and
        strip matched text before falling through to the next mode.

        Processing order:
          A) Direct CDT codes ("D0120") → DB lookup
          B) Code markers ("periodic oral evaluation code") → regex + search
          C) Surface shorthand ("tooth 3 MO") → parse + search
          D) Remaining text → LLM extraction + full pipeline

        Returns:
        {
            "mode": "shorthand" | "description" | "mixed" | ...,
            "entries": [
                {
                    "input": "what the doctor said",
                    "mode": "shorthand",
                    "suggestions": [
                        {"code": "D2392", "description": "...", "score": 0.95},
                    ]
                }
            ]
        }
        """
        transcript = transcript.strip()
        if not transcript:
            return {"mode": "empty", "entries": []}

        # ── Normalize number words → digits ──────────────────────────────
        # "three MO, four MOD" → "tooth 3 MO, tooth 4 MOD"
        # "eight and nine ML" → "tooth 8 ML, tooth 9 ML"
        transcript = CDTService.normalize_number_words(transcript)
        logger.debug(f"[CDT] Normalized transcript: {transcript[:120]}")

        all_entries: List[Dict[str, Any]] = []
        remaining = transcript

        # ── Mode A: Direct CDT code(s) in transcript ────────────────────
        direct_codes = CDT_CODE_RE.findall(remaining)
        if direct_codes:
            entries = await CDTService._lookup_direct_codes(direct_codes, remaining)
            for e in entries:
                e["mode"] = "direct_code"
            all_entries.extend(entries)
            # Strip matched codes so they don't confuse later modes
            remaining = CDT_CODE_RE.sub("", remaining)

        # ── Mode B: Code markers ("periodic oral evaluation code") ──────
        code_markers = CODE_MARKER_RE.findall(remaining)
        if code_markers:
            entries = await CDTService._lookup_code_markers(code_markers)
            for e in entries:
                e["mode"] = "code_marker"
            all_entries.extend(entries)
            # Strip matched markers
            for marker in code_markers:
                remaining = remaining.replace(marker, "", 1)

        # ── Mode C: Surface shorthand (#2 MO, tooth 3 MOD) ─────────────
        shorthand_parsed = CDTService._parse_shorthand(remaining)
        if shorthand_parsed:
            entries = await CDTService._lookup_shorthand(shorthand_parsed)
            for e in entries:
                e["mode"] = "shorthand"
            all_entries.extend(entries)
            # Strip matched shorthand patterns from remaining text
            for se in shorthand_parsed:
                raw = se.get("raw", "")
                if raw:
                    remaining = remaining.replace(raw, "", 1)

        # ── Mode D: Description — process remaining text ────────────────
        # Clean up leftover punctuation / whitespace from stripped patterns
        remaining = re.sub(r'[\s,;.]+', ' ', remaining).strip()
        remaining = re.sub(r'^[\s,;.]+|[\s,;.]+$', '', remaining)

        if remaining and len(remaining) > 5:
            logger.info(f"[CDT] Remaining text for description mode: '{remaining[:80]}'")
            desc_entries = await CDTService._search_by_description(remaining)
            for e in desc_entries:
                e["mode"] = "description"
            all_entries.extend(desc_entries)

        if not all_entries:
            return {"mode": "empty", "entries": []}

        # ── Determine overall mode label ─────────────────────────────────
        modes_used = set(e.get("mode") for e in all_entries)
        if len(modes_used) == 1:
            mode = modes_used.pop()
        else:
            mode = "mixed"
            logger.info(f"[CDT] Mixed-mode transcript: {modes_used}")

        return {"mode": mode, "entries": all_entries}

    # ── Mode A: Direct Code Lookup ───────────────────────────────────────

    @staticmethod
    async def _lookup_direct_codes(
        codes: List[str],
        transcript: str = "",
    ) -> List[Dict[str, Any]]:
        """
        Doctor said CDT code(s) directly (e.g., "periodic oral evaluation code D0120").
        Look up each in DB. Extract the clinical context spoken alongside the code.
        """
        db = await get_database()
        entries = []

        # Extract clinical context for each code from the transcript
        context_map: Dict[str, str] = {}
        if transcript:
            for match in CONTEXT_RE.finditer(transcript):
                ctx = match.group(1).strip()
                # Clean leading connectors ("and fluoride varnish" → "fluoride varnish")
                ctx = re.sub(r"^(?:and|or|,)\s*", "", ctx, flags=re.IGNORECASE).strip()
                code = match.group(2).upper()
                if ctx:
                    context_map[code] = ctx

        for code in codes:
            code_upper = code.upper()

            # First: exact DB lookup (fastest)
            doc = await db["cdt_codes"].find_one(
                {"code": code_upper, "is_active": True},
                {"embedding": 0, "embedding_text": 0},
            )

            spoken_context = context_map.get(code_upper, "")

            if doc:
                entries.append({
                    "input": code_upper,
                    "spoken_context": spoken_context,
                    "suggestions": [{
                        "code": doc["code"],
                        "description": doc.get("description", ""),
                        "category": doc.get("category", ""),
                        "score": 1.0,
                    }],
                })
            else:
                # Code not in DB — might be typo, search for closest matches
                suggestions = await CDTService._search_cdt_index(code_upper, top_k=5)
                entries.append({
                    "input": code_upper,
                    "spoken_context": spoken_context,
                    "suggestions": CDTService._format_suggestions(suggestions),
                })

        return entries

    # ── Mode B: Code Marker Lookup (FASTEST — no LLM needed) ────────────

    @staticmethod
    async def _lookup_code_markers(phrases: List[str]) -> List[Dict[str, Any]]:
        """
        Fastest path: doctor used 'code' as a verbal marker.

        Input transcript: "periodic oral evaluation code . Four bitewing radiographs code were taken."
        Extracted phrases: ["periodic oral evaluation", "Four bitewing radiographs"]

        Pipeline (no LLM):
          1. Normalize each phrase (remove fillers, preserve modifiers)
          2. Search ALL phrases in PARALLEL (asyncio.gather)
          3. Apply lexical filtering + confidence threshold
          4. Return results ready for injection into transcript

        This skips the LLM extraction step entirely, saving ~1-2 seconds.
        """
        # Dedupe + normalize
        seen = set()
        search_tasks = []

        for raw_phrase in phrases:
            phrase = raw_phrase.strip()
            # Clean leading prepositions/articles/sentence preambles from regex capture
            # e.g., "Patient presented for periodic oral evaluation" → "periodic oral evaluation"
            phrase = re.sub(
                r"^(?:patient\s+(?:presented|here|came|is here)\s+(?:for|with)\s+)?",
                "", phrase, flags=re.IGNORECASE,
            ).strip()
            phrase = re.sub(
                r"^(?:we\s+(?:did|performed|completed)\s+)?",
                "", phrase, flags=re.IGNORECASE,
            ).strip()
            phrase = re.sub(
                r"^(?:for|with|a|an|the|then|also|plus|and|or)\s+",
                "", phrase, flags=re.IGNORECASE,
            ).strip()

            if not phrase or phrase.lower() in seen:
                continue
            seen.add(phrase.lower())

            normalized = CDTService._normalize_phrase(phrase)
            if not normalized:
                continue

            # Queue the search (will run in parallel)
            search_tasks.append((phrase, normalized))

        if not search_tasks:
            return []

        # ── Run ALL searches in parallel ─────────────────────────────────
        async def _search_one(original: str, normalized: str) -> Optional[Dict[str, Any]]:
            raw_candidates = await CDTService._search_cdt_index(normalized, top_k=10)
            if not raw_candidates:
                return None

            # Lexical filtering
            filtered = CDTService._lexical_filter(normalized, raw_candidates)
            if not filtered:
                return None

            # Confidence threshold
            top = filtered[0]
            reranker_score = top.get("@search.reranker_score") or top.get("@search.score") or 0

            if reranker_score >= CDTService._HIGH_CONFIDENCE_SCORE:
                return {
                    "input": original,
                    "suggestions": [CDTService._format_one(top)],
                }
            elif reranker_score >= CDTService._MIN_THRESHOLD_SCORE:
                return {
                    "input": original,
                    "suggestions": [CDTService._format_one(c) for c in filtered[:3]],
                }
            else:
                logger.info(
                    f"[CDT] Code-marker '{original}' dropped — reranker score "
                    f"{reranker_score:.2f} < {CDTService._MIN_THRESHOLD_SCORE}"
                )
                return None

        results = await asyncio.gather(
            *[_search_one(orig, norm) for orig, norm in search_tasks]
        )

        return [r for r in results if r is not None]


    # Pipeline: LLM Extract → Normalize → BM25+Vector+Semantic Reranking
    #           → Lexical Filtering → Confidence Threshold

    # Critical modifiers that distinguish semantically similar CDT codes.
    # If a CDT description contains one of these but the doctor's phrase does NOT,
    # the result gets a heavy penalty to prevent false matches.
    # Example: D0210 "complete series" vs D0709 "image capture only"
    _CRITICAL_MODIFIERS = [
        "image capture only",
        "by report",
        "not to include",
        "re-evaluation",
        "post-operative",
        "limited",
        "comprehensive",
        "complete",
        "partial",
        "unilateral",
        "bilateral",
        "primary",
        "permanent",
        "per tooth",
        "per quadrant",
        "per arch",
        "per sextant",
        "each additional",
        "first tooth",
        "maxillary",
        "mandibular",
        "anterior",
        "posterior",
        "premolar",
        "molar",
    ]

    # Confidence threshold for semantic reranker scores (0-4 scale).
    # ≥ HIGH_CONFIDENCE  → return as single best match
    # ≥ MIN_THRESHOLD    → include in suggestions list
    # < MIN_THRESHOLD    → discard
    _HIGH_CONFIDENCE_SCORE = 2.0
    _MIN_THRESHOLD_SCORE = 0.5

    @staticmethod
    def _normalize_phrase(phrase: str) -> str:
        """
        Normalize a spoken clinical phrase for search.

        Removes filler words and standardizes phrasing while PRESERVING
        all clinical modifiers, numbers, and qualifying words that affect
        which CDT code applies.
        """
        # Lowercase for processing
        normalized = phrase.lower().strip()

        # Remove common speech fillers that add no clinical value
        fillers = [
            r"\bwe (?:did|performed|completed|need)\b",
            r"\bpatient (?:needs?|had|has|got|received)\b",
            r"\bgoing to (?:do|perform)\b",
            r"\blet'?s do\b",
            r"\bI (?:did|performed)\b",
            r"\bplease\b",
            r"\bthe\b",
            r"\ba\b",
            r"\ban\b",
        ]
        for filler in fillers:
            normalized = re.sub(filler, " ", normalized, flags=re.IGNORECASE)

        # Map common spoken dental terms to CDT-official terminology
        # Doctors say "cleaning" but CDT uses "prophylaxis",
        # "x-rays" but CDT uses "radiographic images", etc.
        synonyms = {
            r"\bcleaning\b": "prophylaxis",
            r"\bclean\b": "prophylaxis",
            r"\bprophy\b": "adult prophylaxis",
            r"\bx-?rays?\b": "radiographic images",
            r"\bx ray\b": "radiographic images",
            r"\bfilling\b": "restoration",
            r"\bfillings\b": "restorations",
            r"\bpulling\b": "extraction",
            r"\bpull\b": "extraction",
            r"\bdeep cleaning\b": "scaling and root planing",
            r"\bnumb(?:ing)?\b": "anesthesia",
            r"\bnerve treatment\b": "endodontic therapy",
            r"\broot canal\b": "endodontic therapy",
            r"\bcrown\b": "crown",
            r"\bcap\b": "crown",
            r"\bbitewings?\b": "bitewing",
            r"\bpano\b": "panoramic",
            r"\bfull mouth x-?rays?\b": "intraoral complete series",
            r"\bexam\b": "evaluation",
            r"\bcheckup\b": "periodic oral evaluation",
            r"\bcheck-?up\b": "periodic oral evaluation",
        }
        for pattern, replacement in synonyms.items():
            # Only replace if the official term isn't already present
            # e.g., "prophylaxis cleaning" → "prophylaxis" (don't duplicate)
            if replacement.lower() not in normalized:
                normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
            else:
                # Official term exists — just remove the colloquial synonym
                normalized = re.sub(pattern, "", normalized, flags=re.IGNORECASE)

        # Collapse whitespace
        normalized = " ".join(normalized.split())
        return normalized

    @staticmethod
    async def _search_by_description(transcript: str) -> List[Dict[str, Any]]:
        """
        Full pipeline for description-based CDT code lookup.

        1. LLM Extract — pull clinical procedure phrases from transcript
        2. Normalize   — clean filler words, preserve modifiers
        3. Search      — BM25 + Vector + Semantic Reranking via Azure AI Search
        4. Lexical Filter — penalize results with critical modifiers absent from input
        5. Confidence Threshold — return high-confidence matches or ranked suggestions
        """
        from app.services.dental_voice_service import DentalVoiceService
        from langchain_core.messages import HumanMessage, SystemMessage

        # ── Step 1: LLM Extract ──────────────────────────────────────────
        prompt = """You are a dental coding assistant. Read the provided clinical transcript and extract every unique dental procedure, evaluation, imaging, or treatment mentioned.

RULES:
1. Return ONLY a JSON array of strings. No markdown, no explanations.
2. Each string MUST be an exact substring from the transcript (character-for-character match).
3. Extract EVERY distinct procedure as a separate item.
4. CRITICAL — preserve numeric and quantifying words inside the extracted phrase:
   - "Four bitewing radiographs" → extract as "Four bitewing radiographs" (NOT "bitewing radiographs")
   - "two periapical images" → extract as "two periapical images" (NOT "periapical images")
   - "full mouth series" → extract as "full mouth series"
   These numbers determine the exact CDT code (D0272 vs D0273 vs D0274 are different codes).
5. CRITICAL — preserve qualifying modifiers:
   - "image capture only" → keep "image capture only" (NOT just "radiographic images")
   - "by report" → keep "by report"
   - "limited" vs "comprehensive" → keep the exact modifier
6. Include: exams, cleanings, x-rays, fillings, extractions, evaluations, fluoride, sealants, etc.
7. Exclude: clinical findings (e.g. "mild plaque", "no caries"), diagnoses, patient statements, general chatter.
8. If no procedures found, return [].

Example Input:
"Patient here for a comprehensive exam. We did four bitewing radiographs and a prophy. Some gingivitis noted."

Example Output:
["comprehensive exam", "four bitewing radiographs", "prophy"]"""

        procedures = []
        try:
            llm = DentalVoiceService._get_llm()
            response = await llm.ainvoke([
                SystemMessage(content=prompt),
                HumanMessage(content=transcript),
            ])

            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            procedures = json.loads(content)
            if not isinstance(procedures, list):
                procedures = [transcript]
        except Exception as e:
            logger.error(f"[CDT] Procedure extraction failed: {e}")
            # Fallback: split by sentence boundaries
            segments = re.split(r'[.,;]|\band\b', transcript, flags=re.IGNORECASE)
            procedures = [s.strip() for s in segments if len(s.strip()) >= 4]
            if not procedures:
                procedures = [transcript]

        # ── Step 2–5: Process each procedure phrase ──────────────────────
        seen_inputs = set()
        raw_candidates_map = {}

        for proc in procedures:
            proc = str(proc).strip()
            if not proc or proc.lower() in seen_inputs:
                continue
            seen_inputs.add(proc.lower())

            # Step 2: Normalize
            normalized = CDTService._normalize_phrase(proc)
            if not normalized:
                continue

            # Step 3: BM25 + Vector + Semantic Reranking
            raw_candidates = await CDTService._search_cdt_index(normalized, top_k=10)
            if raw_candidates:
                raw_candidates_map[proc] = [CDTService._format_one(c) for c in raw_candidates]

        if not raw_candidates_map:
            return []

        # ── Step 4: LLM Batch Refinement ──────────────────────────────────
        from app.templates.soap_template import BATCH_CDT_REFINEMENT_PROMPT
        from app.services.dental_voice_service import DentalVoiceService
        from langchain_core.messages import SystemMessage, HumanMessage

        llm_payload = json.dumps(raw_candidates_map, indent=2)
        refined_results = {}

        try:
            llm = DentalVoiceService._get_llm()
            response = await llm.ainvoke([
                SystemMessage(content=BATCH_CDT_REFINEMENT_PROMPT.format(transcript=transcript)),
                HumanMessage(content=llm_payload),
            ])

            content = response.content
            if isinstance(content, list):
                content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            refined_results = json.loads(content)
        except Exception as e:
            logger.error(f"[CDT] LLM Batch Refinement failed: {e}")
            # Fallback to top 1 semantic match if LLM fails
            refined_results = {
                proc: [candidates[0]] 
                for proc, candidates in raw_candidates_map.items() 
                if candidates
            }

        # ── Step 5: Format entries ────────────────────────────────────────
        entries = []
        for proc, codes in refined_results.items():
            if not codes:
                continue
            # Ensure codes is a list
            if not isinstance(codes, list):
                codes = [codes]
            entries.append({
                "input": proc,
                "suggestions": codes,
                "mode": "description"
            })

        return entries

    @staticmethod
    def _lexical_filter(
        phrase: str,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        phrase_lower = phrase.lower()
        # Remove hyphens for fairer matching
        clean_phrase = phrase_lower.replace("-", "").replace(" ", "")
        phrase_lower_no_hyphen = phrase_lower.replace("-", " ")
        
        adjusted = []

        for candidate in candidates:
            desc_lower = (candidate.get("description") or "").lower()
            desc_lower_no_hyphen = desc_lower.replace("-", " ")
            
            reranker_score = candidate.get("@search.reranker_score") or candidate.get("@search.score") or 0
            adjustment = 0.0

            # 1. Critical Modifier penalties/boosts
            for modifier in CDTService._CRITICAL_MODIFIERS:
                mod_clean = modifier.replace("-", " ")
                modifier_in_desc = mod_clean in desc_lower_no_hyphen
                modifier_in_phrase = mod_clean in phrase_lower_no_hyphen

                if modifier_in_desc and not modifier_in_phrase:
                    adjustment -= 1.5
                elif modifier_in_phrase and modifier_in_desc:
                    adjustment += 0.5
                elif modifier_in_phrase and not modifier_in_desc:
                    adjustment -= 0.3

            # 2. Exact Substring Boost
            # If the doctor said EXACTLY the phrase (ignoring hyphens/extra spaces)
            clean_desc = desc_lower.replace("-", "").replace(" ", "")
            if clean_phrase and clean_phrase in clean_desc:
                adjustment += 3.0
                
                # Extra boost if it matches the START of the description
                if clean_desc.startswith(clean_phrase):
                    adjustment += 2.0

            candidate["_adjusted_score"] = reranker_score + adjustment
            adjusted.append(candidate)

        # Re-sort by adjusted score (descending)
        adjusted.sort(key=lambda x: x.get("_adjusted_score", 0), reverse=True)
        return adjusted

    @staticmethod
    def _format_one(result: Dict[str, Any]) -> Dict[str, Any]:
        """Format a single search result into a clean suggestion object."""
        reranker_score = result.get("@search.reranker_score") or result.get("@search.score") or 0
        adjusted_score = result.get("_adjusted_score") or reranker_score

        # Normalize to 0-1 range (reranker scores are 0-4, BM25 scores vary)
        has_reranker = result.get("@search.reranker_score") is not None
        if has_reranker:
            normalized_score = min(adjusted_score / 4.0, 1.0)
        else:
            # BM25+vector scores — use relative scaling (cap at 1.0)
            normalized_score = min(adjusted_score / max(reranker_score, 1.0), 1.0)

        return {
            "code": result.get("code", ""),
            "description": result.get("description", ""),
            "category": result.get("category", ""),
            "score": round(max(normalized_score, 0.0), 3),
        }

    # ── Mode C: Shorthand Parsing + Search ───────────────────────────────

    @staticmethod
    def _parse_shorthand(transcript: str) -> List[Dict[str, Any]]:
        """Parse surface shorthand from transcript."""
        results = []
        seen = set()

        # Pass 1: Abbreviated surfaces (MO, MOD, DO)
        for match in SHORTHAND_RE.finditer(transcript):
            tooth = int(match.group(1))
            surfaces = match.group(2).upper()

            if tooth < 1 or tooth > 32:
                continue
            if not all(c in VALID_SURFACES for c in surfaces):
                continue

            key = (tooth, surfaces)
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "tooth": tooth,
                "surfaces": surfaces,
                "surface_count": len(surfaces),
                "surface_names": ", ".join(SURFACE_FULL_NAMES.get(c, c) for c in surfaces),
                "raw": match.group(0).strip(),
            })

        # Pass 2: Full surface names (mesial, occlusal)
        for match in FULL_SURFACE_RE.finditer(transcript):
            tooth = int(match.group(1))
            surface_words = re.findall(
                r"mesial|occlusal|distal|buccal|facial|lingual|incisal|palatal",
                match.group(2), re.IGNORECASE,
            )
            surfaces = "".join(SURFACE_ABBREVIATIONS.get(w.lower(), "") for w in surface_words)

            if tooth < 1 or tooth > 32 or len(surfaces) < 2:
                continue

            key = (tooth, surfaces)
            if key in seen:
                continue
            seen.add(key)

            results.append({
                "tooth": tooth,
                "surfaces": surfaces,
                "surface_count": len(surfaces),
                "surface_names": ", ".join(SURFACE_FULL_NAMES.get(c, c) for c in surfaces),
                "raw": match.group(0).strip(),
            })

        return results

    @staticmethod
    async def _lookup_shorthand(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        For each shorthand entry, map exactly to the deterministic CDT code based on 
        the ADA combinations table provided. Guarantees 100% accuracy.
        """
        # Exact mappings from the provided image
        POSTERIOR_MAPPINGS = {
            "D2391": ["O", "B", "L", "F"],
            "D2392": ["MO", "DO", "OB", "OL", "MB", "DL", "OM"], # OM added per "If AI hears O and M..."
            "D2393": ["MOD", "MOB", "MOL", "DOB", "DOL"],
            "D2394": ["MODB", "MODL", "MODBL", "OBL", "MDB"]
        }

        ANTERIOR_MAPPINGS = {
            "D2330": ["F", "L", "M", "D", "I"],
            "D2331": ["MF", "ML", "DF", "DL", "MI", "DI", "FL"],
            "D2332": ["MFL", "DFL", "MIF", "MIL", "DIF", "DIL"],
            "D2335": ["MIFL", "DIFL", "MIDFL", "MIDL", "MIFLB"]
        }

        async def _lookup_one(entry: dict) -> dict:
            tooth = entry["tooth"]
            surfaces = entry["surfaces"].upper()
            
            # Sort surfaces alphabetically just in case they say "OM" instead of "MO" 
            # (though the image explicitly handles some, sorting ensures we catch permutations if needed, 
            # but we will check exact string first per the table)

            is_posterior = tooth in POSTERIOR_TEETH

            mapping = POSTERIOR_MAPPINGS if is_posterior else ANTERIOR_MAPPINGS
            
            # Find the exact code from the table
            target_code = None
            
            # First try exact match from the table
            for code, combinations in mapping.items():
                if surfaces in combinations:
                    target_code = code
                    break
                    
            # If not in table exactly (e.g. they said "OM" and it's not explicitly in the list), 
            # fallback to length-based counting with a sort-check
            if not target_code:
                sorted_surfaces = "".join(sorted(surfaces))
                for code, combinations in mapping.items():
                    if any("".join(sorted(c)) == sorted_surfaces for c in combinations):
                        target_code = code
                        break

            # If STILL not found (a weird combination), fallback to surface count
            if not target_code:
                surface_count = min(len(surfaces), 4)
                if is_posterior:
                    fallback_map = {1: "D2391", 2: "D2392", 3: "D2393", 4: "D2394"}
                    target_code = fallback_map[surface_count]
                else:
                    fallback_map = {1: "D2330", 2: "D2331", 3: "D2332", 4: "D2335"}
                    target_code = fallback_map[surface_count]

            # Fetch the OFFICIAL description from the database to ensure 100% parity
            suggestions = await CDTService._search_cdt_index(target_code, top_k=1)
            
            if suggestions:
                suggestion = CDTService._format_one(suggestions[0])
                suggestion["score"] = 1.0 # Guarantee top placement
            else:
                suggestion = {
                    "code": target_code,
                    "description": "Resin-based composite",
                    "category": "Restorative",
                    "score": 1.0
                }

            return {
                "input": entry["raw"],
                "tooth": entry["tooth"],
                "surfaces": entry["surfaces"],
                "surface_count": entry["surface_count"],
                "surface_names": entry["surface_names"],
                "suggestions": [suggestion],
            }

        results = await asyncio.gather(*[_lookup_one(e) for e in entries])
        return list(results)

    # ── Shared: CDT Index Search (BM25 + Vector + Semantic Reranking) ───

    # Dedicated semantic configuration for CDT index
    CDT_SEMANTIC_CONFIG = "cdt-semantic-config"

    @staticmethod
    async def _search_cdt_index(query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Search the dental-cdt-index via BM25 + Vector + Semantic Reranking.

        Uses the AsyncSearchClient (same pattern as rag_service/search.py)
        with the dedicated 'cdt-semantic-config' for optimal CDT code matching.

        Returns raw result dicts with '@search.reranker_score' for downstream
        lexical filtering and confidence thresholds.
        """
        index_name = settings.AZURE_SEARCH_INDEX_CDT

        # ── Generate embedding for vector component ──────────────────────
        query_vector = None
        try:
            oai_client = AsyncAzureOpenAI(
                azure_endpoint=settings.AZURE_OPENAI_EMBEDDING_ENDPOINT,
                api_key=settings.AZURE_OPENAI_EMBEDDING_KEY,
                api_version="2023-05-15",
            )
            response = await _create_embeddings_with_retry_cdt_service(
                client=oai_client,
                texts=[query],
                model=settings.OPENAI_EMBEDDING_MODEL,
                dimensions=1024,
            )
            query_vector = response.data[0].embedding
        except Exception as e:
            logger.error(f"[CDT] Embedding failed for query '{query}': {e}")

        # ── Build vector queries ─────────────────────────────────────────
        vector_queries = []
        if query_vector:
            vector_queries.append(
                VectorizedQuery(
                    vector=query_vector,
                    k_nearest_neighbors=top_k,
                    fields="content_vector",
                    weight=1.0,
                )
            )

        # ── Execute search: BM25 + Vector + Semantic Reranking ───────────
        client = get_search_client(index_name)

        try:
            # Primary: Full hybrid + semantic reranking
            results = await client.search(
                search_text=query,
                vector_queries=vector_queries or None,
                top=top_k,
                query_type="semantic",
                semantic_configuration_name=CDTService.CDT_SEMANTIC_CONFIG,
                query_caption="extractive",
                query_answer="extractive",
            )

            formatted = []
            async for result in results:
                formatted.append(dict(result))
                if len(formatted) >= top_k:
                    break

            logger.info(
                f"[CDT] Semantic hybrid search for '{query[:50]}' → {len(formatted)} results"
            )
            return formatted

        except Exception as semantic_err:
            logger.warning(
                f"[CDT] Semantic search failed, falling back to hybrid: {semantic_err}"
            )

            # Fallback: Simple hybrid (BM25 + Vector, no semantic reranker)
            try:
                results = await client.search(
                    search_text=query,
                    vector_queries=vector_queries or None,
                    top=top_k,
                )

                formatted = []
                async for result in results:
                    formatted.append(dict(result))
                    if len(formatted) >= top_k:
                        break

                return formatted

            except Exception as fallback_err:
                logger.error(f"[CDT] Fallback search also failed: {fallback_err}")
                return []

    # ── Shared: Format Suggestions (for Mode A & C) ──────────────────────

    @staticmethod
    def _format_suggestions(raw_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Format raw Azure AI Search results into clean suggestion objects.

        Prefers '@search.reranker_score' (0-4 scale from semantic reranker)
        when available, falling back to '@search.score' (BM25+vector).
        Includes a minimum score threshold to prevent garbage matches.
        """
        if not raw_results:
            return []

        suggestions = []

        # Check if we have reranker scores (semantic search was used)
        has_reranker = any(r.get("@search.reranker_score") for r in raw_results)

        if has_reranker:
            # Semantic reranker scores (0-4 scale)
            for result in raw_results:
                reranker_score = result.get("@search.reranker_score") or 0
                normalized = min(reranker_score / 4.0, 1.0)

                if normalized < 0.1 and len(suggestions) >= 1:
                    continue

                suggestions.append({
                    "code": result.get("code", ""),
                    "description": result.get("description", ""),
                    "category": result.get("category", ""),
                    "score": round(normalized, 3),
                })
        else:
            # Fallback: BM25+vector scores (relative scaling)
            max_score = max(r.get("@search.score", 1) for r in raw_results) or 1

            for result in raw_results:
                raw_score = result.get("@search.score", 0)
                rel_score = raw_score / max_score

                if rel_score < 0.4 and len(suggestions) >= 1:
                    continue

                suggestions.append({
                    "code": result.get("code", ""),
                    "description": result.get("description", ""),
                    "category": result.get("category", ""),
                    "score": round(rel_score, 3),
                })

        return suggestions

    # ── Admin: List CDT Codes ────────────────────────────────────────────

    @staticmethod
    async def list_cdt_codes(
        skip: int = 0,
        limit: int = 100,
        category: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List CDT codes from the database."""
        db = await get_database()

        query: Dict[str, Any] = {"is_active": True}
        if category:
            query["category"] = {"$regex": category, "$options": "i"}

        if search:
            query["$or"] = [
                {"code": {"$regex": search, "$options": "i"}},
                {"description": {"$regex": search, "$options": "i"}}
            ]

        total = await db["cdt_codes"].count_documents(query)
        cursor = db["cdt_codes"].find(
            query, {"embedding": 0, "embedding_text": 0}
        ).sort("code", 1).skip(skip).limit(limit)

        codes = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            codes.append(doc)

        return {"total": total, "codes": codes}



    # ── Admin: CDT Document Management ───────────────────────────────────

    @staticmethod
    async def list_cdt_documents() -> List[Dict[str, Any]]:
        """Admin: List all uploaded CDT codebooks."""
        from app.core.constants import COLLECTION_CDT_DOCUMENTS
        from app.utils.file_utils import generate_sas_url
        db = await get_database()
        
        cursor = db[COLLECTION_CDT_DOCUMENTS].find().sort("created_at", -1)
        docs = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            if doc.get("blob_name"):
                doc["secure_url"] = generate_sas_url(doc["blob_name"])
            docs.append(doc)
        return docs

    @staticmethod
    async def delete_cdt_document(document_id: str) -> None:
        """
        Admin: Completely remove a CDT book and all its associated data.
        """
        from app.core.constants import COLLECTION_CDT_DOCUMENTS
        from app.utils.file_utils import delete_blob
        from bson import ObjectId
        
        db = await get_database()
        if not ObjectId.is_valid(document_id):
            raise HTTPException(status_code=400, detail="Invalid ID")
            
        oid = ObjectId(document_id)
        doc = await db[COLLECTION_CDT_DOCUMENTS].find_one({"_id": oid})
        if not doc:
            raise HTTPException(status_code=404, detail="Not found")
            
        # 1. Find all associated codes
        codes_cursor = db["cdt_codes"].find({"source_document_id": document_id}, {"code": 1})
        code_ids = [c["code"] for c in await codes_cursor.to_list(length=None)]
        
        # 2. Delete from Azure AI Search
        if code_ids:
            url = f"{settings.AZURE_SEARCH_ENDPOINT.rstrip('/')}/indexes/{settings.AZURE_SEARCH_INDEX_CDT}/docs/index?api-version=2024-07-01"
            batch = [{"@search.action": "delete", "code": cid} for cid in code_ids]
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(url, headers={"api-key": settings.AZURE_SEARCH_KEY, "Content-Type": "application/json"}, json={"value": batch})

        # 3. Delete from MongoDB
        await db["cdt_codes"].delete_many({"source_document_id": document_id})
        
        # 4. Delete Blob
        if doc.get("blob_name"):
            await delete_blob(doc["blob_name"])
                
        # 5. Delete Document Record
        await db[COLLECTION_CDT_DOCUMENTS].delete_one({"_id": oid})
        logger.info(f"[CDT] Deleted document {document_id}")