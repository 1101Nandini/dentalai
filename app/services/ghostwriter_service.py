import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.templates.soap_template import GHOSTWRITER_SYSTEM_PROMPT, XRAY_OPTIONS
from app.services.template_service import TemplateService

logger = logging.getLogger(__name__)


class GhostwriterService:
    """Service for generating Ghostwriter notes using templates and org variables."""

    @staticmethod
    def _get_llm(timeout: float = 60.0) -> ChatGoogleGenerativeAI:
        return ChatGoogleGenerativeAI(
            model=settings.GEMINI_CHAT_MODEL,
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_LOCATION,
            credentials=settings.get_vertex_credentials(),
            vertexai=True,
            temperature=0.1,
            top_p=0.9,
            thinking_budget=0,
            timeout=timeout,
        )

    @staticmethod
    async def _get_cdt_descriptions(codes: List[str]) -> Dict[str, str]:
        """
        Fetch CDT code descriptions from MongoDB for a list of codes.
        Returns a dict mapping code -> description, e.g. {"D3310": "Endodontic therapy, anterior tooth"}.
        Falls back to an empty dict on any error.
        """
        if not codes:
            return {}
        try:
            from app.core.database import get_database
            db = await get_database()
            cursor = db["cdt_codes"].find(
                {"code": {"$in": codes}},
                {"_id": 0, "code": 1, "description": 1}
            )
            docs = await cursor.to_list(length=len(codes))
            return {doc["code"]: doc["description"] for doc in docs if "code" in doc and "description" in doc}
        except Exception as e:
            logger.warning(f"[GHOSTWRITER] CDT description lookup failed: {e}")
            return {}

    @staticmethod
    async def generate_ghostwriter_note(
        transcript: str,
        template_id: str,
        org_id: str,
        xray_selection: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate a flowing Ghostwriter note based on a specific template.
        """
        t0 = time.perf_counter()

        # 1. Fetch template and org variables
        try:
            template = await TemplateService.get_template(template_id, org_id)
            org_variables = await TemplateService.get_org_variables_dict(org_id)
        except Exception as e:
            logger.error(f"[GHOSTWRITER] Failed to fetch template {template_id} or variables for org {org_id}: {e}")
            raise

        t1 = time.perf_counter()
        logger.info(f"[GHOSTWRITER] Step 1 - Template + Variables fetch: {t1 - t0:.3f}s")

        # 2. Build context
        context_parts = [
            f"TRANSCRIPT:\n{transcript}\n",
            f"TEMPLATE BODY:\n{template.get('body_template', '')}\n",
        ]

        if template.get("fields"):
            context_parts.append(f"TEMPLATE FIELDS SCHEMA:\n{json.dumps(template['fields'], indent=2)}\n")

        if org_variables:
            context_parts.append(f"ORGANIZATION VARIABLES (Defaults):\n{json.dumps(org_variables, indent=2)}\n")

        # ── Inject template's pre-configured CDT codes ──────────────────────
        # These codes were set by the dentist/org when creating the template.
        # They represent procedures that are ALWAYS performed for this template
        # even if the dentist does not verbally mention them during the visit.
        cdt_refs = template.get("cdt_refs", [])
        cdt_desc_lookup: Dict[str, str] = {}
        if cdt_refs:
            all_codes = list(dict.fromkeys(cdt_refs))
            # Look up real descriptions from MongoDB so the LLM uses exact naming
            cdt_desc_lookup = await GhostwriterService._get_cdt_descriptions(all_codes)

            # Build a readable table: "D3310 — Endodontic therapy, anterior tooth"
            code_lines = [
                f"  {code} — {cdt_desc_lookup.get(code, 'see CDT codebook')}"
                for code in all_codes
            ]
            context_parts.append(
                f"TEMPLATE GUARANTEED CDT CODES:\n"
                f"The following CDT codes are pre-configured on this template by the clinic. "
                f"They represent procedures that are ALWAYS done for this visit type, even if "
                f"the dentist does not say them out loud.\n"
                + "\n".join(code_lines) + "\n\n"
                f"IMPORTANT: You MUST include ALL of the above codes in the `completed_today` "
                f"array of your JSON output. For each entry, set `procedure` to the exact "
                f"description shown above, `cdt_code` to the code, and `tooth` to null unless "
                f"the transcript or template specifies a tooth number. Do not skip any of them.\n"
            )

        # ── Inject user-selected x-ray types into prompt context ─────────
        if xray_selection:
            xray_lines = []
            for key in xray_selection:
                if key in XRAY_OPTIONS:
                    xr = XRAY_OPTIONS[key]
                    xray_lines.append(f"  {xr['cdt_code']} — {xr['name']}")
            if xray_lines:
                context_parts.append(
                    "X-RAY SELECTIONS (chosen by user for this visit):\n"
                    "Include these in `completed_today` and in the Radiographs section of the note. "
                    "Do NOT duplicate if the transcript already mentions the same x-ray.\n"
                    + "\n".join(xray_lines) + "\n"
                )

        user_message = "\n".join(context_parts)

        t2 = time.perf_counter()
        logger.info(f"[GHOSTWRITER] Step 2 - Context build: {t2 - t1:.3f}s")

        # 3. Call LLM
        try:
            llm = GhostwriterService._get_llm()
            response = await llm.ainvoke([
                SystemMessage(content=GHOSTWRITER_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ])

            t3 = time.perf_counter()
            logger.info(f"[GHOSTWRITER] Step 3 - LLM call: {t3 - t2:.3f}s")

            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )

            # Safely extract JSON block even if preceded by conversational text
            content = content.strip()
            match = re.search(r'```(?:json)?(.*?)```', content, re.DOTALL)
            if match:
                content = match.group(1).strip()
            else:
                start = content.find('{')
                end = content.rfind('}')
                if start != -1 and end != -1 and end > start:
                    content = content[start:end+1]

            result = json.loads(content)

            t4 = time.perf_counter()
            logger.info(f"[GHOSTWRITER] Step 4 - Parse response: {t4 - t3:.3f}s")
            logger.info(
                "[GHOSTWRITER] ✅ Total generation: %.3fs (fetch=%.3fs, build=%.3fs, llm=%.3fs, parse=%.3fs)",
                t4 - t0, t1 - t0, t2 - t1, t3 - t2, t4 - t3,
            )
            
            # Format note with dynamic date if present
            note_text = result.get("ghostwriter_note", "")
            from datetime import datetime
            today_str = datetime.now().strftime("%b %d, %Y")
            note_text = note_text.replace("[Date]", today_str)

            # ── Deduplicate completed_today and planned_future ────────────────
            # The LLM may emit a guaranteed template code AND also pick it up
            # from the transcript narrative, producing duplicates. We deduplicate
            # on (cdt_code, tooth) keeping only the first occurrence of each pair.
            def _dedup_procedures(items: list) -> list:
                seen = set()
                deduped = []
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    key = (
                        str(item.get("cdt_code", "")).upper().strip(),
                        str(item.get("tooth", "")).strip(),
                    )
                    if key not in seen:
                        seen.add(key)
                        deduped.append(item)
                return deduped

            completed_today = _dedup_procedures(result.get("completed_today", []))
            planned_future = _dedup_procedures(result.get("planned_future", []))

            # ── Enrich missing procedure descriptions ────────────────────────
            # If the LLM returned a completed_today entry without a `procedure`
            # description (or left it blank), fill it in from the DB lookup we
            # already did for the guaranteed codes. This ensures the frontend
            # always has a human-readable name to display next to the CDT code.
            if cdt_desc_lookup:
                for entry in completed_today:
                    if not entry.get("procedure"):
                        code = str(entry.get("cdt_code", "")).upper().strip()
                        if code in cdt_desc_lookup:
                            entry["procedure"] = cdt_desc_lookup[code]

            logger.info(
                "[GHOSTWRITER] completed_today=%d items, planned_future=%d items after dedup",
                len(completed_today), len(planned_future),
            )

            return {
                "soap_note": note_text,  # Mapping to soap_note for frontend compatibility initially
                "ghostwriter_note": note_text,
                "completed_today": completed_today,
                "planned_future": planned_future,
                "patient_summary": result.get("patient_summary", ""),
            }

        except Exception as e:
            logger.error(f"[GHOSTWRITER] Generation failed: {e}")
            raise
