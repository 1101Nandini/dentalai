"""
Dental Voice Service — SOAP Note + Patient Summary generation.

Scenario 2 pipeline:
  1. Receive full visit transcript from voice recording
  2. Extract clinical entities using LLM
  3. Generate SOAP note using structured template
  4. Generate patient-friendly summary
  5. Return both outputs

Uses Gemini LLM (GEMINI_CHAT_MODEL) for generation.
"""
import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.templates.soap_template import (
    SOAP_SYSTEM_PROMPT,
    ENTITY_EXTRACTION_PROMPT,
    XRAY_OPTIONS,
)

logger = logging.getLogger(__name__)


class DentalVoiceService:
    """Service for generating clinical SOAP notes and patient summaries."""

    @staticmethod
    def _get_llm(timeout: float = 60.0) -> ChatGoogleGenerativeAI:
        """Get a Gemini LLM client for dental voice processing."""
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
    async def extract_clinical_entities(transcript: str) -> Dict[str, Any]:
        """
        Pre-processing step: Extract structured clinical entities from transcript.

        Uses Gemini to identify:
          - Chief complaint, findings, procedures, tooth numbers
          - Medical history changes, recommendations
          - Diagnostic test results, materials used

        Returns structured entity dict.
        """
        t0 = time.perf_counter()

        try:
            llm = DentalVoiceService._get_llm()
            response = await llm.ainvoke([
                SystemMessage(content=ENTITY_EXTRACTION_PROMPT),
                HumanMessage(content=f"TRANSCRIPT:\n{transcript}"),
            ])

            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )

            # Strip markdown code fences if present
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            entities = json.loads(content)

            logger.info(
                "[DENTAL-VOICE] Entity extraction: %.3fs | teeth=%d, findings=%d, procedures=%d",
                time.perf_counter() - t0,
                len(entities.get("teeth_mentioned", [])),
                len(entities.get("findings", [])),
                len(entities.get("procedures_performed", [])),
            )

            return entities

        except Exception as e:
            logger.error(f"[DENTAL-VOICE] Entity extraction failed: {e}")
            return {
                "chief_complaint": None,
                "medical_history_changes": None,
                "teeth_mentioned": [],
                "findings": [],
                "procedures_performed": [],
                "materials_used": [],
                "recommendations": [],
                "next_visit": None,
            }

    @staticmethod
    async def generate_soap_and_summary(
        transcript: str,
        template_ids: Optional[list[str]] = None,
        org_id: Optional[str] = None,
        xray_selection: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """
        Generate both SOAP note and patient-friendly summary from transcript.

        Pipeline:
          1. Extract clinical entities (structured data from freeform speech)
          2. Generate SOAP note + patient summary in a single LLM call
          3. Return structured output

        Args:
            transcript: Full voice transcript from dental visit
            template_id: Optional ID of the Ghostwriter template to use
            org_id: Optional Organization ID (required if template_id is provided)

        Returns:
            {
                "soap_note": {"header": "...", "subjective": "...", "objective": "...", ...},
                "patient_summary": "...",
                "entities_extracted": {...}
            }
        """
        t0 = time.perf_counter()

        if not transcript or not transcript.strip():
            logger.info("[DENTAL-VOICE] Empty transcript provided. Skipping LLM note generation.")
            return {
                "soap_note": "No speech detected in audio recording.",
                "patient_summary": "No speech detected in audio recording.",
                "entities_extracted": {},
                "completed_today": [],
                "planned_future": [],
                "reports": []
            }

        # Step 1: Extract clinical entities
        entities = await DentalVoiceService.extract_clinical_entities(transcript)

        # If templates are provided, route to Ghostwriter Engine
        if template_ids and org_id:
            from app.services.ghostwriter_service import GhostwriterService
            from app.core.database import get_database
            from bson import ObjectId
            
            logger.info(f"[DENTAL-VOICE] Routing to Ghostwriter engine with templates {template_ids}")
            
            reports = []
            combined_completed_today = []
            combined_planned_future = []
            db = await get_database()
            
            for tid in template_ids:
                try:
                    template_doc = await db["note_templates"].find_one({"_id": ObjectId(tid)}, {"procedure_name": 1})
                    template_name = template_doc.get("procedure_name", "Unknown Template") if template_doc else "Unknown Template"
                    
                    gw_result = await GhostwriterService.generate_ghostwriter_note(transcript, tid, org_id, xray_selection=xray_selection)
                    
                    reports.append({
                        "template_id": tid,
                        "template_name": template_name,
                        "report": gw_result.get("ghostwriter_note", "")
                    })
                    combined_completed_today.extend(gw_result.get("completed_today", []))
                    combined_planned_future.extend(gw_result.get("planned_future", []))
                    # Only inject x-ray on the first template to avoid duplicates
                    xray_selection = None
                except Exception as e:
                    logger.error(f"[DENTAL-VOICE] Failed to generate report for template {tid}: {e}")
            
            # ── Deduplicate across all templates ─────────────────────────
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

            combined_completed_today = _dedup_procedures(combined_completed_today)
            combined_planned_future = _dedup_procedures(combined_planned_future)
            logger.info(
                "[DENTAL-VOICE] After cross-template dedup: completed_today=%d, planned_future=%d",
                len(combined_completed_today), len(combined_planned_future),
            )

            # Generate ONE unified patient summary from the transcript (not per-template)
            patient_summary = ""
            try:
                llm = DentalVoiceService._get_llm()
                summary_prompt = (
                    "You are a dental clinical documentation assistant. "
                    "Generate a single, warm, professional patient summary in simple language from the following dental visit transcript. "
                    "Use exactly these section headers in Markdown:\n"
                    "**VISIT TYPE**\n**WHY THE PATIENT CAME IN**\n**WHAT WE FOUND**\n"
                    "**WHAT IT MEANS FOR YOU**\n**NEXT STEPS**\n\n"
                    "CRITICAL: Always write in THIRD PERSON referring to the patient "
                    "(e.g. \"The patient reported...\"). NEVER use \"you\" or \"your\".\n\n"
                    "Return ONLY the summary text in Markdown. No JSON wrapping."
                )
                summary_response = await llm.ainvoke([
                    SystemMessage(content=summary_prompt),
                    HumanMessage(content=f"TRANSCRIPT:\n{transcript}"),
                ])
                summary_content = summary_response.content
                if isinstance(summary_content, list):
                    summary_content = "".join(
                        p.get("text", "") if isinstance(p, dict) else str(p)
                        for p in summary_content
                    )
                patient_summary = summary_content.strip()
                logger.info("[DENTAL-VOICE] Generated unified patient summary from transcript")
            except Exception as e:
                logger.error(f"[DENTAL-VOICE] Failed to generate patient summary: {e}")
            
            return {
                "reports": reports,
                "entities_extracted": entities,
                "completed_today": combined_completed_today,
                "planned_future": combined_planned_future,
                "patient_summary": patient_summary
            }

        # Step 2: Build context-enriched prompt
        context_parts = [f"TRANSCRIPT:\n{transcript}"]

        if entities:
            context_parts.append(
                f"\nEXTRACTED ENTITIES (for reference):\n{json.dumps(entities, indent=2)}"
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
                    "Include these in `completed_today` and in the Radiographs line of the SOAP note. "
                    "Do NOT duplicate if the transcript already mentions the same x-ray.\n"
                    + "\n".join(xray_lines) + "\n"
                )

        user_message = "\n".join(context_parts)

        # Step 3: Generate SOAP + Summary
        try:
            llm = DentalVoiceService._get_llm()
            response = await llm.ainvoke([
                SystemMessage(content=SOAP_SYSTEM_PROMPT),
                HumanMessage(content=user_message),
            ])

            content = response.content
            if isinstance(content, list):
                content = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )

            # Strip markdown code fences if present
            content = content.strip()
            if content.startswith("```"):
                content = re.sub(r"^```(?:json)?\s*", "", content)
                content = re.sub(r"\s*```$", "", content)

            result = json.loads(content)

            elapsed = time.perf_counter() - t0
            logger.info(
                "[DENTAL-VOICE] SOAP + Summary generation: %.3fs total",
                elapsed,
            )

            # Normalize structure
            soap_md = result.get("soap_markdown", "Not documented in this visit.")
            
            # Dynamically inject today's date into the header
            from datetime import datetime
            today_str = datetime.now().strftime("%b %d, %Y")
            soap_md = soap_md.replace("[Date]", today_str)

            completed_today = result.get("completed_today", [])
            planned_future = result.get("planned_future", [])

            # ── Deduplicate completed_today as safety net ─────────────
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

            completed_today = _dedup_procedures(completed_today)
            planned_future = _dedup_procedures(planned_future)

            return {
                "soap_note": soap_md,
                "patient_summary": result.get("patient_summary", ""),
                "entities_extracted": result.get("entities_extracted", entities),
                "completed_today": completed_today,
                "planned_future": planned_future,
            }

        except json.JSONDecodeError as e:
            logger.error(f"[DENTAL-VOICE] JSON parse error: {e}. Raw content: {content[:500]}")
            raise

        except Exception as e:
            logger.error(f"[DENTAL-VOICE] Generation failed: {e}")
            raise
