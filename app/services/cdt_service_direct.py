import re
import json
import logging
import asyncio
from typing import List, Dict, Any, Optional
from app.core.database import get_database
from app.services.dental_voice_service import DentalVoiceService
from app.services.cdt_service import CDTService
from langchain_core.messages import SystemMessage, HumanMessage

logger = logging.getLogger(__name__)

# In-memory cache for the CDT table
_CDT_TABLE_CACHE: Optional[List[Dict[str, Any]]] = None
_CDT_TABLE_LAST_FETCH: float = 0
CACHE_TTL = 3600  # 1 hour

# ── Deterministic Composite Code Lookup ──────────────────────────────────────
POSTERIOR_TEETH = frozenset([1,2,3,4,5,12,13,14,15,16,17,18,19,20,21,28,29,30,31,32])
ANTERIOR_TEETH = frozenset([6,7,8,9,10,11,22,23,24,25,26,27])

POSTERIOR_COMPOSITE = {1: "D2391", 2: "D2392", 3: "D2393", 4: "D2394"}
ANTERIOR_COMPOSITE = {1: "D2330", 2: "D2331", 3: "D2332", 4: "D2335"}

# Gap scanner regexes — catch patterns the LLM may have missed
_GAP_SURFACE_RE = re.compile(
    r'(?:tooth|number|#)?\s*(\d{1,2})\s*[,-]?\s*([MODBLI]{2,5})\b',
    re.IGNORECASE
)
_GAP_SURFACE_DASH_RE = re.compile(
    r'\b(\d{1,2})-([MODBLI]{2,5})\b',
    re.IGNORECASE
)
_GAP_EXTRACT_RE = re.compile(
    r'\bextract(?:ion)?\s+(\d{1,2})\b',
    re.IGNORECASE
)

CDT_DIRECT_EXTRACTION_PROMPT = """You are a senior dental clinical auditor. Your goal is 100% precision.

## RULES
1. Tooth numbers 1–5, 12–16, 17–21, 28–32 → POSTERIOR (premolars/molars)
2. Tooth numbers 6–11, 22–27 → ANTERIOR (incisors/canines)
3. If surfaces include BOTH "O" and "M" → immediately suggest D2392
4. Count distinct surfaces to determine 1/2/3/4+ surface code
5. Always confirm tooth number + surfaces before giving a code

## POSTERIOR COMPOSITE CODES (Premolars & Molars)
Teeth: 1,2,3,4,5,12,13,14,15,16,17,18,19,20,21,28,29,30,31,32

| Surfaces | CDT Code | Common Combinations        |
|----------|----------|----------------------------|
| 1        | D2391    | O, B, L, F                 |
| 2        | D2392    | MO, DO, OB, OL, MB, DL     |
| 3        | D2393    | MOD, MOB, MOL, DOB, DOL    |
| 4+       | D2394    | MODB, MODL, MODBL, OBL     |

## ANTERIOR COMPOSITE CODES (Incisors & Canines)
Teeth: 6,7,8,9,10,11,22,23,24,25,26,27

| Surfaces | CDT Code | Common Combinations        |
|----------|----------|----------------------------|
| 1        | D2330    | F, L, M, D, I              |
| 2        | D2331    | MF, ML, DF, DL, MI, DI, FL |
| 3        | D2332    | MFL, DFL, MIF, MIL, DIF    |
| 4+       | D2335    | MIFL, DIFL, MIDFL, MIDL    |

### ANTI-HALLUCINATION RULES (STRICT):
1. **NO BRIDGE LEAKAGE:** D6750 and D6240 apply **ONLY** to bridge units (abutments/pontics). 
   - NEVER add D6750 to a simple restoration like "tooth 8 ML" or "10 ML". 
2. **NO STRAY CHARACTERS:** Do not add letters (like a trailing "D") to the "input" field.
3. **CONFLICTS (EXTRACTION PRIORITY):** If a tooth is being extracted, DO NOT output any restoration entries (composites/crowns) for that tooth.

### CRITICAL CODING INSTRUCTIONS:
1. **SURGICAL MERGE RULE:** If "extract [tooth]" is followed by "surgical extraction", you MUST merge them. The final entry MUST have the tooth number and code **D7210**.
2. **BRIDGE RULE:** For bridge units, create a separate entry for EVERY tooth number mentioned. 
   - Abutments → **D6750**
   - Pontics → **D6240**
3. **DE-DUPLICATION:** Only output ONE entry per unique code per tooth number. If the same tooth or procedure is mentioned twice, output it only once.
4. **COMPLETENESS (CRITICAL):** You MUST create an entry for EVERY procedure in the transcript. Do NOT skip any tooth+surface pattern (e.g., 4-MOD, 13-MOD), any extraction, any prophy, any bridge unit, any crown, any buildup, or any root canal. Missing even one entry is a critical failure.
5. **SINGLE CODE:** Each entry MUST have exactly ONE correct CDT code in suggestions (except compound procedures like "buildup and crown" which need D2950 + D2740).

### REFERENCE CDT CODEBOOK:
{cdt_table}

TRANSCRIPT:
{transcript}

### OUTPUT FORMAT:
Return ONLY JSON.
{{
  "verbatim_phrases": [
    {{
      "input": ["extract 16", "surgical extraction"],
      "suggestions": [{{ "code": "D7210", "description": "Surgical removal..." }}],
      "merged": true
    }},
    {{
      "input": "28",
      "suggestions": [{{ "code": "D6750", "description": "Abutment..." }}]
    }},
    {{
      "input": "29",
      "suggestions": [{{ "code": "D6240", "description": "Pontic..." }}]
    }}
  ]
}}
"""

class CDTServiceDirect:

    @staticmethod
    def _get_composite_code(tooth_num: int, surface_count: int) -> Optional[str]:
        """Deterministically get the correct composite CDT code based on tooth number and surface count."""
        surface_count = min(max(surface_count, 1), 4)
        if tooth_num in POSTERIOR_TEETH:
            return POSTERIOR_COMPOSITE[surface_count]
        elif tooth_num in ANTERIOR_TEETH:
            return ANTERIOR_COMPOSITE[surface_count]
        return None

    @staticmethod
    async def _get_latest_cdt_table_raw() -> List[Dict[str, Any]]:
        """Fetch all codes from MongoDB as raw dicts."""
        global _CDT_TABLE_CACHE, _CDT_TABLE_LAST_FETCH
        import time
        current_time = time.time()
        if _CDT_TABLE_CACHE and (current_time - _CDT_TABLE_LAST_FETCH) < CACHE_TTL:
            return _CDT_TABLE_CACHE
        try:
            db = await get_database()
            cursor = db["cdt_codes"].find({}, {"_id": 0, "code": 1, "description": 1, "category": 1})
            raw_codes = await cursor.to_list(length=2000)
            raw_codes.sort(key=lambda x: x.get('code', ''))
            _CDT_TABLE_CACHE = raw_codes
            _CDT_TABLE_LAST_FETCH = current_time
            return raw_codes
        except Exception as e:
            logger.error(f"[CDT-DIRECT] MongoDB fetch failed: {e}")
            return []

    @staticmethod
    async def _get_latest_cdt_table_str(raw_codes: List[Dict[str, Any]]) -> str:
        lines = [f"{item.get('code', '')}: {item.get('description', '')}" for item in raw_codes]
        return "\n".join(lines)

    @staticmethod
    def _extract_tooth_nums(text: str) -> List[str]:
        """Robustly extract tooth numbers from text."""
        # Finds standalone 1-32 or tooth numbers in strings like "14-MOD"
        nums = re.findall(r'(?<!D)(?<![0-9])(?:[1-9]|[12][0-9]|3[0-2])(?![0-9])', text)
        return list(set(nums))

    @staticmethod
    async def process_transcript(transcript: str) -> Dict[str, Any]:
        """Direct LLM approach with Python-side clinical auditing and deterministic gap filling."""
        try:
            transcript = CDTService.normalize_number_words(transcript)
            raw_codes = await CDTServiceDirect._get_latest_cdt_table_raw()
            code_map = {c["code"]: c for c in raw_codes}
            verbatim_phrases = []

            try:
                llm = DentalVoiceService._get_llm(timeout=45.0)
                cdt_table_content = await CDTServiceDirect._get_latest_cdt_table_str(raw_codes)

                response = await asyncio.wait_for(
                    llm.ainvoke([
                        SystemMessage(content=CDT_DIRECT_EXTRACTION_PROMPT.format(
                            cdt_table=cdt_table_content,
                            transcript=transcript
                        )),
                        HumanMessage(content="Audit and code this transcript. Ensure surgical extractions are merged with their tooth number. Do NOT skip any procedure."),
                    ]),
                    timeout=45.0,
                )

                content = response.content
                if isinstance(content, list):
                    content = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
                content = content.replace("```json", "").replace("```", "").strip()
                
                try:
                    raw_data = json.loads(content)
                except json.JSONDecodeError:
                    match = re.search(r'\{.*\}', content, re.DOTALL)
                    raw_data = json.loads(match.group()) if match else {"verbatim_phrases": []}

                verbatim_phrases = raw_data.get("verbatim_phrases", [])
            except (asyncio.TimeoutError, Exception) as llm_err:
                logger.warning(
                    f"[CDT-DIRECT] LLM extraction timed out or failed ({llm_err}). "
                    f"Falling back to deterministic gap scanner.",
                    exc_info=True
                )
            
            # ─── PYTHON SIDE AUDITOR ─────────────────────────────────────
            entries = []
            extraction_teeth = set()
            seen_tooth_codes = set()  # (tooth_num, code)
            
            # Pass 1: Identify all extractions and handle surgical merging
            last_extraction_tooth = None
            for item in verbatim_phrases:
                inputs = item.get("input")
                txt = " ".join(inputs) if isinstance(inputs, list) else str(inputs)
                nums = CDTServiceDirect._extract_tooth_nums(txt)
                
                sugs = item.get("suggestions", [])
                codes = [s if isinstance(s, str) else s.get("code") for s in sugs]
                
                is_extraction = any(c in ["D7140", "D7210"] for c in codes) or "extract" in txt.lower() or "surgical" in txt.lower()
                
                if is_extraction:
                    if nums:
                        for n in nums: extraction_teeth.add(n)
                        last_extraction_tooth = nums[0]
                        # If "surgical" is mentioned, upgrade to D7210
                        if "surgical" in txt.lower():
                            item["suggestions"] = [{"code": "D7210"}]
                    elif "surgical" in txt.lower() and last_extraction_tooth:
                        # Standalone "surgical extraction" — upgrade the PREVIOUS extraction entry to D7210
                        for prev_item in verbatim_phrases:
                            prev_txt = " ".join(prev_item.get("input")) if isinstance(prev_item.get("input"), list) else str(prev_item.get("input", ""))
                            prev_nums = CDTServiceDirect._extract_tooth_nums(prev_txt)
                            if last_extraction_tooth in prev_nums:
                                prev_item["suggestions"] = [{"code": "D7210"}]
                                break
                        # Also give this entry D7210 so it gets coded in the transcript
                        item["suggestions"] = [{"code": "D7210"}]

            # Pass 2: Filter, Clean, and Fallbacks
            for item in verbatim_phrases:
                inputs = item.get("input")
                if not isinstance(inputs, list): inputs = [inputs]
                
                # Cleanup: Strip stray characters and hallucinations
                inputs = [re.sub(r'[^a-zA-Z0-9\s-]', '', str(i)).strip() for i in inputs]
                # Specifically strip trailing "D" that isn't part of a code
                inputs = [re.sub(r'(?<!D)D$', '', i) for i in inputs]
                
                input_text = " ".join(inputs).lower()
                nums = CDTServiceDirect._extract_tooth_nums(input_text)
                
                sugs = item.get("suggestions", [])
                final_suggestions = []
                for s in sugs:
                    c_val = s if isinstance(s, str) else s.get("code")
                    if c_val in code_map:
                        final_suggestions.append(code_map[c_val])

                # DYNAMIC FALLBACKS
                if not final_suggestions:
                    # 1. Prophy Fallback
                    if "prophy" in input_text:
                        final_suggestions = [code_map.get("D1110")]
                    # 2. Extraction Fallback
                    elif "extract" in input_text and "surgical" in input_text:
                        final_suggestions = [code_map.get("D7210")]
                    elif "extract" in input_text:
                        final_suggestions = [code_map.get("D7140")]
                    # 3. Composite Shorthand Fallback (e.g. "14-MOD")
                    elif nums:
                        tooth_num = int(nums[0])
                        surfaces = len(re.findall(r'[modblif]', input_text))
                        if surfaces > 0:
                            code = CDTServiceDirect._get_composite_code(tooth_num, surfaces)
                            if code and code in code_map:
                                final_suggestions = [code_map[code]]

                if not final_suggestions or not all(final_suggestions):
                    continue

                # Rule: De-duplication
                key_teeth = nums if nums else ["0"]
                valid_suggestions = []
                for s in final_suggestions:
                    s_code = s.get("code")
                    duplicate_found = False
                    for n in key_teeth:
                        if (n, s_code) in seen_tooth_codes:
                            duplicate_found = True
                            break
                    if not duplicate_found:
                        valid_suggestions.append(s)
                        for n in key_teeth: seen_tooth_codes.add((n, s_code))
                
                if not valid_suggestions: continue

                entries.append({
                    "input": inputs,
                    "suggestions": valid_suggestions,
                    "conflict_note": item.get("conflict_note"),
                    "merged": item.get("merged", False)
                })

            # ─── Pass 2.5: FIX COMPOSITE INPUTS — match actual transcript ─
            # The LLM sometimes hallucinates surface counts or codes. Verify against transcript.
            for entry in entries:
                entry_codes = [s.get("code", "") for s in entry.get("suggestions", [])]
                if not any(c.startswith("D23") for c in entry_codes):
                    continue  # Only fix composite entries
                
                input_text = " ".join(entry["input"])
                nums = CDTServiceDirect._extract_tooth_nums(input_text)
                if not nums:
                    continue
                tooth = nums[0]
                
                # Look for the tooth in the transcript
                tooth_idx = transcript.find(tooth)
                if tooth_idx != -1:
                    # Look at a window of text after the tooth number (e.g. 50 chars)
                    window = transcript[tooth_idx:tooth_idx+50].lower()
                    
                    # We only want to search until the next sentence or another tooth number
                    match_window = window
                    next_tooth_match = re.search(r'\b([1-9]|[12][0-9]|3[0-2])\b', window[len(tooth):])
                    if next_tooth_match:
                        match_window = window[:len(tooth) + next_tooth_match.start()]
                        
                    surface_words = ["mesial", "occlusal", "distal", "buccal", "lingual", "incisal", "facial"]
                    found_surfaces = set()
                    
                    for w in surface_words:
                        if w in match_window:
                            found_surfaces.add(w)
                            match_window = match_window.replace(w, "")
                            
                    # Clean the window of words that might contain surface letters
                    for w in ["number", "tooth", "extract", "and", "the", "a", "of"]:
                        match_window = re.sub(rf'\b{w}\b', '', match_window)
                        
                    # Find surface acronyms
                    acronyms = re.findall(r'\b[modblif]{1,5}\b', match_window)
                    for acr in acronyms:
                        for char in acr:
                            found_surfaces.add(char)
                            
                    surface_count = len(found_surfaces)
                    if surface_count > 0:
                        correct_code = CDTServiceDirect._get_composite_code(int(tooth), surface_count)
                        if correct_code and correct_code in code_map:
                            entry["suggestions"] = [code_map[correct_code]]
                            logger.info(f"[CDT-DIRECT] Pass 2.5 Override composite: tooth {tooth} -> {correct_code} ({surface_count} surfaces)")

            # ─── Pass 3: GAP SCANNER — deterministic safety net ──────────
            logger.info(f"[CDT-DIRECT] === PASS 3 START === LLM entries after Pass2: {len(entries)}")
            logger.info(f"[CDT-DIRECT] extraction_teeth: {extraction_teeth}")
            for i, e in enumerate(entries):
                ecodes = [s.get('code','') for s in e.get('suggestions',[])]
                logger.info(f"[CDT-DIRECT]   entry[{i}]: input={e['input']} codes={ecodes}")

            # 3a. Build set of teeth already covered by composite codes
            composite_covered = set()
            extraction_covered = set()
            has_prophy = False
            for entry in entries:
                entry_text = " ".join(entry["input"]).lower()
                entry_nums = CDTServiceDirect._extract_tooth_nums(entry_text)
                entry_codes = [s.get("code", "") for s in entry.get("suggestions", [])]
                if any(c.startswith("D23") for c in entry_codes):
                    for n in entry_nums:
                        composite_covered.add(n)
                if any(c in ("D7140", "D7210") for c in entry_codes):
                    for n in entry_nums:
                        extraction_covered.add(n)
                if any(c == "D1110" for c in entry_codes):
                    has_prophy = True

            logger.info(f"[CDT-DIRECT] composite_covered: {composite_covered}")
            logger.info(f"[CDT-DIRECT] extraction_covered: {extraction_covered}")
            logger.info(f"[CDT-DIRECT] has_prophy: {has_prophy}")

            # 3b. Scan for missed tooth+surface composites
            all_surface_matches = list(_GAP_SURFACE_RE.finditer(transcript)) + list(_GAP_SURFACE_DASH_RE.finditer(transcript))
            logger.info(f"[CDT-DIRECT] Gap scanner found {len(all_surface_matches)} surface matches in transcript")
            seen_gap_teeth = set()
            for match in all_surface_matches:
                tooth_str = match.group(1)
                surfaces = match.group(2).upper()
                tooth_num = int(tooth_str)
                logger.info(f"[CDT-DIRECT] Gap check: tooth={tooth_str} surfaces={surfaces} match='{match.group(0)}'")

                if tooth_num < 1 or tooth_num > 32:
                    logger.info(f"[CDT-DIRECT]   SKIP: tooth_num {tooth_num} out of range")
                    continue
                if tooth_str in seen_gap_teeth:
                    logger.info(f"[CDT-DIRECT]   SKIP: already in seen_gap_teeth")
                    continue
                if tooth_str in composite_covered:
                    logger.info(f"[CDT-DIRECT]   SKIP: tooth {tooth_str} already in composite_covered")
                    continue  # Already covered by LLM

                code = CDTServiceDirect._get_composite_code(tooth_num, len(surfaces))
                if code and code in code_map:
                    key = (tooth_str, code)
                    if key not in seen_tooth_codes:
                        seen_tooth_codes.add(key)
                        composite_covered.add(tooth_str)
                        seen_gap_teeth.add(tooth_str)
                        # Use the actual matched text from transcript so injection can find it
                        matched_text = match.group(0).strip()
                        entries.append({
                            "input": [matched_text],
                            "suggestions": [code_map[code]],
                            "conflict_note": None,
                            "merged": False
                        })
                        logger.info(f"[CDT-DIRECT] Gap fill composite: tooth {tooth_str} {surfaces} → {code}")

            # 3c. Scan for missed extractions
            for match in _GAP_EXTRACT_RE.finditer(transcript):
                tooth_str = match.group(1)
                tooth_num = int(tooth_str)
                if tooth_num < 1 or tooth_num > 32:
                    continue
                if tooth_str in extraction_covered:
                    continue

                # Check if "surgical" appears near this extraction in the transcript
                match_start = match.start()
                context_window = transcript[match_start:match_start + 80].lower()
                if "surgical" in context_window:
                    ext_code = "D7210"
                else:
                    ext_code = "D7140"

                if ext_code in code_map:
                    key = (tooth_str, ext_code)
                    if key not in seen_tooth_codes:
                        seen_tooth_codes.add(key)
                        extraction_covered.add(tooth_str)
                        extraction_teeth.add(tooth_str)
                        entries.append({
                            "input": [f"extract {tooth_str}"],
                            "suggestions": [code_map[ext_code]],
                            "conflict_note": None,
                            "merged": "surgical" in context_window
                        })
                        logger.info(f"[CDT-DIRECT] Gap fill extraction: tooth {tooth_str} → {ext_code}")

            # 3d. Scan for missed prophy
            if not has_prophy and re.search(r'\bprophy\b', transcript, re.IGNORECASE):
                if "D1110" in code_map:
                    key = ("0", "D1110")
                    if key not in seen_tooth_codes:
                        seen_tooth_codes.add(key)
                        entries.append({
                            "input": ["prophy"],
                            "suggestions": [code_map["D1110"]],
                            "conflict_note": None,
                            "merged": False
                        })
                        logger.info("[CDT-DIRECT] Gap fill: prophy → D1110")

            # ─── Pass 4: SURGICAL OVERRIDE — D7210 trumps D7140 ─────────
            # If the same tooth has both D7140 and D7210 entries, remove D7140
            surgical_teeth = set()
            for entry in entries:
                entry_codes = [s.get("code") for s in entry.get("suggestions", [])]
                if "D7210" in entry_codes:
                    for n in CDTServiceDirect._extract_tooth_nums(" ".join(entry["input"])):
                        surgical_teeth.add(n)

            if surgical_teeth:
                entries = [
                    e for e in entries
                    if not (
                        any(s.get("code") == "D7140" for s in e.get("suggestions", []))
                        and any(
                            n in surgical_teeth
                            for n in CDTServiceDirect._extract_tooth_nums(" ".join(e["input"]))
                        )
                    )
                ]



            logger.info(f"[CDT-DIRECT] === FINAL === Returning {len(entries)} entries")
            for i, e in enumerate(entries):
                ecodes = [s.get('code','') for s in e.get('suggestions',[])]
                logger.info(f"[CDT-DIRECT]   final[{i}]: input={e['input']} codes={ecodes}")
            return {"mode": "direct_llm", "entries": entries}

        except Exception as e:
            logger.error(f"[CDT-DIRECT] Audit failed: {e}", exc_info=True)
            return {"mode": "failed", "entries": []}
