"""
MessageService — Message CRUD + RAG orchestration + SSE streaming.

Orchestration flow for stream_response:
  1. Save the user message to MongoDB
  2. Retrieve context from RagService
  3. Load recent conversation history
  4. Build a LangChain prompt with context
  5. Stream Gemini response token-by-token (yielded as SSE events)
  6. Save the complete assistant message with full usage metrics to MongoDB
"""
import json
import logging
import asyncio
import time
from datetime import datetime, timezone
from typing import List, AsyncGenerator, Optional, Dict, Any

from bson import ObjectId
from fastapi import HTTPException, Request

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

from app.config import settings
from app.core.database import get_database
from app.core.constants import COLLECTION_MESSAGES, MessageRole, COLLECTION_CASE_STUDY
from app.services.rag_service import RagService
from app.services.rag_service.embeddings import embed_text, embed_text_vision
from app.services.rag_service.query_analysis import analyze_clinical_query
from app.services.chat_service import ChatService
from app.services.case_study_service import search_related_cases
from app.services.user_upload_service import UserUploadService
from app.services.message_service.llm_clients import (
    get_stream_llm,
    get_title_llm,
    BLOB_URL_PATTERN,
    SYSTEM_PROMPT,
    CONTEXT_TEMPLATE,
)
from app.services.message_service.context_builder import (
    group_and_dedupe_sources,
    build_context_text,
    build_analysis_block,
)

logger = logging.getLogger(__name__)




class MessageService:

    # ── Query classification helpers ───────────────────────────────────────────
    @staticmethod
    def _is_trivial_query(query: str) -> bool:
        """Return True if the query is a simple greeting (e.g. 'hi')."""
        trivial_terms = {"hi", "hello", "hey", "how are you", "good morning", "good afternoon"}
        return query.lower().strip().strip("?!.") in trivial_terms

    @staticmethod
    def _is_ambiguous_followup(query: str) -> bool:
        """
        Return True if the query lacks enough context to be a standalone search query.
        Uses a content-word heuristic instead of keyword matching.
        """
        clean_q = query.lower().strip().strip("?!.")
        words = clean_q.split()
        if len(words) <= 2:
            return True
        NON_CONTENT = {
            "show", "tell", "give", "explain", "describe", "elaborate", "detail",
            "more", "further", "deeper", "about", "how", "what", "why", "when",
            "can", "could", "would", "should", "please", "also", "just",
            "me", "the", "a", "an", "in", "of", "to", "for", "is", "are",
            "it", "its", "this", "that", "these", "those", "do", "does",
            "i", "be", "with", "and", "or", "but", "not", "on", "at",
            "very", "much", "way", "some", "any", "all", "go", "let",
            "steps", "done", "again", "same", "like",
        }
        content_words = [w for w in words if w not in NON_CONTENT and len(w) > 2]
        return len(words) <= 6 and len(content_words) == 0

    # ── URL hydration ──────────────────────────────────────────────────────────
    @staticmethod
    def _hydrate_message_urls(doc: dict) -> dict:
        """Replace stored blob names with fresh 7-day SAS URLs."""
        if doc.get("image_urls"):
            doc["image_urls"] = UserUploadService.get_sas_urls(doc["image_urls"])
        if doc.get("image_url"):
            doc["image_url"] = UserUploadService.get_sas_urls([doc["image_url"]])[0]

        content = doc.get("content", "")
        if content and "blob.core.windows.net" in content:
            def replacer(match):
                blob_name_and_path = match.group(2)
                try:
                    return UserUploadService.get_sas_urls([blob_name_and_path])[0]
                except Exception as exc:
                    logger.warning("Failed to sign Azure Blob URL: %s", exc)
                    return match.group(0)
            doc["content"] = BLOB_URL_PATTERN.sub(replacer, content)
        return doc

    # ── Message CRUD ───────────────────────────────────────────────────────────
    @staticmethod
    async def save_message(
        chat_id: str,
        role: str,
        content: str,
        image_url: Optional[str] = None,
        image_urls: Optional[List[str]] = None,
        sources: Optional[List[Dict[str, Any]]] = None,
        token_usage: Optional[Dict[str, Any]] = None,
        related_case_ids: Optional[List[str]] = None,
    ) -> dict:
        db = await get_database()
        now = datetime.now(timezone.utc)
        msg = {
            "chat_id": chat_id,
            "role": role,
            "content": content,
            "image_url": image_url,
            "image_urls": image_urls or [],
            "sources": sources or [],
            "related_case_ids": related_case_ids or [],
            "token_usage": token_usage,
            "created_at": now,
        }
        result = await db[COLLECTION_MESSAGES].insert_one(msg)
        
        # ── Usage Analytics Logging ──
        if role == MessageRole.ASSISTANT and token_usage:
            chat = await db["chats"].find_one({"_id": ObjectId(chat_id)})
            if chat:
                user_id = chat.get("user_id")
                org_id = None
                if user_id and ObjectId.is_valid(user_id):
                    user = await db["users"].find_one({"_id": ObjectId(user_id)})
                    if user:
                        org_id = user.get("organization_id")
                
                await db["usage_logs"].insert_one({
                    "type": "message",
                    "chat_id": str(chat_id),
                    "user_id": str(user_id) if user_id else None,
                    "organization_id": str(org_id) if org_id else None,
                    "token_usage": token_usage,
                    "created_at": now
                })

        msg["id"] = str(result.inserted_id)
        msg.pop("_id", None)
        return MessageService._hydrate_message_urls(msg)

    @staticmethod
    async def _hydrate_cases(messages: List[dict]) -> List[dict]:
        db = await get_database()
        all_case_ids = set()
        for msg in messages:
            if msg.get("related_case_ids"):
                for cid in msg["related_case_ids"]:
                    if ObjectId.is_valid(cid):
                        all_case_ids.add(ObjectId(cid))
                        
        if all_case_ids:
            db_cases = await db[COLLECTION_CASE_STUDY].find(
                {"_id": {"$in": list(all_case_ids)}}
            ).to_list(None)
            
            case_map = {}
            for c in db_cases:
                c["id"] = str(c.pop("_id"))
                case_map[c["id"]] = c
                
            for msg in messages:
                if "related_case_ids" in msg:
                    msg["cases"] = [case_map[cid] for cid in msg["related_case_ids"] if cid in case_map]
                    msg.pop("related_case_ids", None)
        else:
            for msg in messages:
                if "related_case_ids" in msg:
                    msg["cases"] = []
                    msg.pop("related_case_ids", None)
                    
        return messages

    @staticmethod
    async def get_messages(chat_id: str) -> List[dict]:
        db = await get_database()
        cursor = (
            db[COLLECTION_MESSAGES]
            .find({"chat_id": chat_id})
            .sort("created_at", 1)
        )
        messages = []
        async for doc in cursor:
            doc["id"] = str(doc.pop("_id"))
            messages.append(MessageService._hydrate_message_urls(doc))
        
        messages = await MessageService._hydrate_cases(messages)
        return messages

    @staticmethod
    async def get_message(chat_id: str, message_id: str) -> dict:
        db = await get_database()
        if not ObjectId.is_valid(message_id):
            raise HTTPException(status_code=400, detail="Invalid message ID format")
        doc = await db[COLLECTION_MESSAGES].find_one({
            "_id": ObjectId(message_id),
            "chat_id": chat_id,
        })
        if not doc:
            raise HTTPException(status_code=404, detail="Message not found")
        doc["id"] = str(doc.pop("_id"))
        hydrated = MessageService._hydrate_message_urls(doc)
        hydrated_list = await MessageService._hydrate_cases([hydrated])
        return hydrated_list[0]

    @staticmethod
    async def delete_message(chat_id: str, message_id: str) -> None:
        db = await get_database()
        if not ObjectId.is_valid(message_id):
            raise HTTPException(status_code=400, detail="Invalid message ID format")
        result = await db[COLLECTION_MESSAGES].delete_one({
            "_id": ObjectId(message_id),
            "chat_id": chat_id,
        })
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Message not found")

    # ── History builder ────────────────────────────────────────────────────────
    @staticmethod
    async def _build_history(chat_id: str, limit: int = 20) -> list:
        """Load last *limit* messages and convert to LangChain message objects."""
        db = await get_database()
        cursor = (
            db[COLLECTION_MESSAGES]
            .find({"chat_id": chat_id}, {"sources": 0, "token_usage": 0})
            .sort("created_at", -1)
            .limit(limit)
        )
        raw: list = []
        async for doc in cursor:
            raw.append(doc)
        raw.reverse()

        lc_messages: list = []
        for m in raw:
            if m["role"] == MessageRole.USER:
                all_blob_names: List[str] = list(m.get("image_urls") or [])
                if m.get("image_url") and m["image_url"] not in all_blob_names:
                    all_blob_names.append(m["image_url"])
                if all_blob_names:
                    sas_urls = UserUploadService.get_sas_urls(all_blob_names)
                    content_parts: List[Dict[str, Any]] = [{"type": "text", "text": m["content"]}]
                    for sas_url in sas_urls:
                        content_parts.append({"type": "image_url", "image_url": {"url": sas_url}})
                    lc_messages.append(HumanMessage(content=content_parts))
                else:
                    lc_messages.append(HumanMessage(content=m["content"]))
            elif m["role"] == MessageRole.ASSISTANT:
                lc_messages.append(AIMessage(content=m["content"]))
        return lc_messages

    # ── Title generation ───────────────────────────────────────────────────────
    @staticmethod
    async def generate_title(query: str) -> str:
        """Generate a short (≤6 word) chat title using the analysis Gemini model."""
        try:
            llm = get_title_llm()
            resp = await llm.ainvoke([
                SystemMessage(content="Generate a concise chat title (maximum 6 words) for the following user message. Return ONLY the title, nothing else."),
                HumanMessage(content=query),
            ])
            content = resp.content
            if isinstance(content, list):
                content = "".join([
                    part.get("text", "") if isinstance(part, dict) else str(part)
                    for part in content
                ])
            return content.strip().strip('"')
        except Exception as exc:
            logger.warning("Title generation failed, using fallback: %s", exc)
            return query[:50] if len(query) <= 50 else query[:47] + "..."

    # ── SSE Streaming response ─────────────────────────────────────────────────
    @staticmethod
    async def stream_response(
        chat_id: str,
        query: str,
        image_url: Optional[str] = None,
        image_urls: Optional[List[str]] = None,
        request: Optional[Request] = None,
    ) -> AsyncGenerator[str, None]:
        """
        Full RAG pipeline that yields Server-Sent Events:
          - event: status   → pipeline stage name
          - event: token    → incremental AI text
          - event: sources  → retrieved document references
          - event: done     → final message ID
        """
        try:
            chat_oid = ObjectId(chat_id)
        except Exception:
            yield json.dumps({"event": "error", "content": "invalid chat ID format"}) + "\n\n"
            return

        db = await get_database()
        chat = await db["chats"].find_one({"_id": chat_oid})
        if not chat:
            yield json.dumps({"event": "error", "content": "chat not found"}) + "\n\n"
            return

        user_id = chat.get("user_id")
        from app.services.restriction_service import RestrictionService
        is_restricted, error_message, error_code = await RestrictionService.check_message_restriction(user_id)
        if is_restricted:
            yield json.dumps({"event": "error", "code": error_code, "content": error_message}) + "\n\n"
            return

        raw_blob_names: List[str] = list(image_urls or [])
        if image_url and image_url not in raw_blob_names:
            raw_blob_names.append(image_url)

        _stream_t0 = time.perf_counter()
        logger.info(
            "[TIMING] stream_response() started — chat_id=%s, has_images=%s",
            chat_id, bool(raw_blob_names),
        )

        is_trivial = MessageService._is_trivial_query(query)

        # ── PHASE A: Both embeddings in parallel at t=0 ───────────────────
        # Single task that runs embed_text + embed_text_vision concurrently.
        # Starts before image promotion / save / history so network I/O overlaps.
        # Even if rag_required turns out to be False, the result gets cached
        # and will be reused by the follow-up question that DOES need RAG.
        _embed_both_task: Optional[asyncio.Task] = None
        if not is_trivial:
            async def _embed_both():
                (vec_3072, _), (vec_vis, __) = await asyncio.gather(
                    embed_text(query, dimensions=3072),
                    embed_text_vision(query),
                )
                return vec_3072, vec_vis

            _embed_both_task = asyncio.create_task(_embed_both())
            logger.info("[TIMING] embed_text + embed_text_vision launched in parallel at t=0")


        # ── PHASE B: Image promotion ───────────────────────────────────────────
        perm_blob_names: List[str] = []
        perm_sas_urls: List[str] = []

        if raw_blob_names:
            yield json.dumps({"event": "status", "content": "processing_images"}) + "\n\n"
            try:
                _t0 = time.perf_counter()
                promoted = await UserUploadService.promote_images(raw_blob_names)
                logger.info(
                    "[TIMING] Step 0 — image_promotion: %.3fs | images=%d",
                    time.perf_counter() - _t0, len(promoted),
                )
                perm_blob_names = [p["blob_name"] for p in promoted]
                perm_sas_urls = [p["url"] for p in promoted]
            except Exception as exc:
                logger.error("Image processing failed: %s", exc, exc_info=True)
                yield json.dumps({"event": "error", "content": str(exc)}) + "\n\n"
                return

        # ── PHASE C: Save + History + Anchor — all in parallel ────────────────
        _t0 = time.perf_counter()

        async def safe_touch_chat(cid):
            try:
                await ChatService.touch_chat(cid)
            except Exception as exc:
                logger.error("Failed to update chat timestamp: %s", exc)

        asyncio.create_task(safe_touch_chat(chat_id))  # fire-and-forget

        async def _get_anchor() -> Optional[Dict[str, Any]]:
            if is_trivial or not MessageService._is_ambiguous_followup(query):
                return None
            try:
                db = await get_database()
                last_assistant = await db[COLLECTION_MESSAGES].find_one(
                    {"chat_id": chat_id, "role": MessageRole.ASSISTANT},
                    sort=[("created_at", -1)],
                )
                if last_assistant and last_assistant.get("sources"):
                    top_src = last_assistant["sources"][0]
                    anchor = {
                        "topic_id": top_src.get("topic_id"),
                        "document_name": top_src.get("document_name"),
                        "chunk_id": top_src.get("chunk_id"),
                    }
                    logger.info("Anchoring follow-up to previous context: %s", anchor)
                    return anchor
            except Exception as exc:
                logger.warning("Failed to extract anchor context: %s", exc)
            return None

        user_msg, history, anchor_context = await asyncio.gather(
            MessageService.save_message(
                chat_id=chat_id,
                role=MessageRole.USER,
                content=query,
                image_url=None,
                image_urls=perm_blob_names,
            ),
            MessageService._build_history(chat_id, limit=6),
            _get_anchor(),
        )
        logger.info(
            "[TIMING] Steps 1+2 — save_user_message + build_history + anchor (parallel): %.3fs | msgs=%d",
            time.perf_counter() - _t0, len(history),
        )

        # Build history_text for Gemini co-reference resolution
        history_text = ""
        for msg in history[-3:]:
            if isinstance(msg.content, str):
                history_text += f"{msg.type}: {msg.content}\n"
            elif isinstance(msg.content, list):
                text_parts = [p.get("text", "") for p in msg.content if p.get("type") == "text"]
                history_text += f"{msg.type}: {' '.join(text_parts)}\n"

        # ── PHASE C.5: Analysis with full history context ──────────────────────
        # Runs AFTER Phase C so it has real history for co-reference resolution
        # and rag_required determination. This is the single analysis call for
        # the entire request — retrieve() reuses it via pre_analysis.
        analysis: Dict[str, Any] = {}
        retrieve_usage: List[Dict[str, Any]] = []

        if not is_trivial:
            yield json.dumps({"event": "status", "content": "analyzing_query"}) + "\n\n"
            _t0 = time.perf_counter()
            try:
                analysis, analysis_usage = await analyze_clinical_query(
                    query,
                    image_urls=perm_sas_urls or None,
                    history_text=history_text,
                )
                if analysis_usage:
                    retrieve_usage.append(analysis_usage)
                logger.info(
                    "[TIMING] Step 2.5 — query_analysis: %.3fs | rag_required=%s | rationale: %s",
                    time.perf_counter() - _t0,
                    analysis.get("rag_required", True),
                    analysis.get("rag_rationale", ""),
                )
            except Exception as exc:
                logger.warning("[ANALYSIS] Failed (%s) — defaulting rag_required=True", exc)
                analysis = {"rag_required": True, "standalone_query": query}

        rag_required = (not is_trivial) and analysis.get("rag_required", True)

        # ── Out-of-scope guard: short-circuit non-dental queries ───────────────
        # If the analysis flagged this query as unrelated to dentistry, emit a
        # polite canned response and stop — no RAG, no LLM.
        is_dental = analysis.get("is_dental_query", True)
        if not is_trivial and not is_dental:
            topic_hint = query.strip().rstrip("?.!")
            out_of_scope_msg = (
                f"The database doesn't provide any information on \"{topic_hint}\". "
                "This knowledge base is limited to dental and oral health topics. "
                "Please ask a dentistry-related question."
            )
            logger.info("[SCOPE] Non-dental query detected — short-circuiting pipeline")
            oos_msg = await MessageService.save_message(
                chat_id=chat_id,
                role=MessageRole.ASSISTANT,
                content=out_of_scope_msg,
                sources=[],
                token_usage=None,
                related_case_ids=[],
            )
            yield json.dumps({"event": "token", "content": out_of_scope_msg}) + "\n\n"
            yield json.dumps({"event": "done", "message_id": oos_msg["id"]}) + "\n\n"
            return

        # ── PHASE D: RAG Retrieve (skipped when history is sufficient) ─────────
        sources: List[Dict[str, Any]] = []
        context_text = ""
        rag_image_sas_urls: List[str] = []

        if rag_required:
            yield json.dumps({"event": "status", "content": "searching_literature"}) + "\n\n"
            try:
                _t0 = time.perf_counter()
                # Embeddings have been running since t=0 — they're done by now
                pre_vec_3072, pre_vec_vis = await _embed_both_task if _embed_both_task else (None, None)
                
                # Launch RAG and Case Study search in parallel
                search_q = analysis.get("standalone_query") or query
                rag_task = asyncio.create_task(RagService.retrieve(
                    search_q,
                    image_urls=perm_sas_urls,
                    anchor_context=anchor_context,
                    history_text=history_text,
                    pre_embed_vec_3072=pre_vec_3072,
                    pre_embed_vec_vision=pre_vec_vis,
                    pre_analysis=analysis,
                ))
                
                case_study_task = asyncio.create_task(search_related_cases(
                    query=search_q,
                    top_k=2,
                    pre_embedded_vector=pre_vec_3072,
                ))
                
                (raw_sources, _, extra_usage), matching_cases = await asyncio.gather(
                    rag_task,
                    case_study_task,
                    return_exceptions=False
                )
                
                retrieve_usage.extend(extra_usage)

                if raw_sources:
                    logger.info("[RAG] Retrieved %d raw sources", len(raw_sources))
                    sources = group_and_dedupe_sources(raw_sources)
                    context_text, rag_image_sas_urls = build_context_text(sources)

                    if logger.isEnabledFor(logging.DEBUG):
                        logger.debug("📄 SENDING %d CHUNKS TO LLM:", len(sources))
                        for idx, s in enumerate(sources, 1):
                            logger.debug(
                                "[%d] %s (Pg %s) | Score: %.2f | %s > %s",
                                idx,
                                s.get("document_name", "Unknown"),
                                s.get("page", "?"),
                                float(s.get("_final_score") or 0),
                                s.get("chapter_name") or "N/A",
                                s.get("topic_name") or "N/A",
                            )

                logger.info(
                    "[TIMING] Step 3 — rag_retrieve+context_build: %.3fs | sources=%d",
                    time.perf_counter() - _t0, len(sources),
                )

            except Exception as exc:
                logger.error("RAG retrieval error: %s", exc, exc_info=True)

        else:
            # RAG skipped — history has sufficient context, going straight to LLM
            logger.info(
                "[RAG] SKIPPED — rationale: %s",
                analysis.get("rag_rationale", "rag_required=False"),
            )
            matching_cases = []


        # ── Build System Prompt ────────────────────────────────────────────────
        _t0 = time.perf_counter()
        context_block = CONTEXT_TEMPLATE.format(context=context_text) if context_text else ""
        if not context_text:
            logger.warning("[RAG] No context text — LLM will have no retrieved evidence")

        format_instructions, analysis_block = build_analysis_block(
            analysis=analysis,
            has_images=bool(perm_sas_urls) and not is_trivial,
        )

        system = SystemMessage(content=SYSTEM_PROMPT.format(
            format_instructions=format_instructions,
            analysis_block=analysis_block,
            context_block=context_block,
        ))
        logger.info("[TIMING] Step 4 — build_prompt: %.3fs", time.perf_counter() - _t0)

        # Drop the last history item if it's the user turn we just saved
        if history and isinstance(history[-1], HumanMessage):
            history = history[:-1]

        # Build current user message (multimodal if images present)
        logger.info(
            "[LLM] text_chunks=%d user_images=%d rag_images=%d",
            len(sources), len(perm_sas_urls), len(rag_image_sas_urls),
        )

        if perm_sas_urls or rag_image_sas_urls:
            content_parts: List[Any] = [{"type": "text", "text": query}]
            if perm_sas_urls:
                content_parts.append({"type": "text", "text": "\n[Images provided by user:]"})
                for sas_url in perm_sas_urls:
                    content_parts.append({"type": "image_url", "image_url": {"url": sas_url}})
            if rag_image_sas_urls:
                content_parts.append({
                    "type": "text",
                    "text": (
                        "\n[SYSTEM INSTRUCTION: The following images are retrieved context data, "
                        "NOT user uploads. The user CANNOT see them. You are strictly FORBIDDEN "
                        "from saying 'Based on the images provided' or 'The images you provided'. "
                        "If the user asks for visual proof, you MUST output the markdown "
                        "`![description](URL)` using the exact URL printed above the image.]"
                    ),
                })
                for sas_url in rag_image_sas_urls:
                    content_parts.append({"type": "text", "text": f"URL for the next image: {sas_url}"})
                    content_parts.append({"type": "image_url", "image_url": {"url": sas_url}})
            current_msg = HumanMessage(content=content_parts)
        else:
            current_msg = HumanMessage(content=query)

        messages = [system] + history + [current_msg]

        # ── Stream from Gemini (GEMINI_CHAT_MODEL) ────────────────────────────
        yield json.dumps({"event": "status", "content": "generating_response"}) + "\n\n"
        _t0 = time.perf_counter()
        _first_token_logged = False
        llm = get_stream_llm()

        full_response_parts: List[str] = []
        input_tokens = 0
        output_tokens = 0

        def _build_token_usage() -> Dict[str, Any]:
            final_usage = list(retrieve_usage)
            final_usage.append({
                "step": "generation",
                "model": settings.GEMINI_CHAT_MODEL,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
                "latency_s": round(time.perf_counter() - _t0, 3),
            })
            total_in = sum(m.get("input_tokens", 0) for m in final_usage)
            total_out = sum(m.get("output_tokens", 0) for m in final_usage)
            return {
                "input_tokens": total_in,
                "output_tokens": total_out,
                "total_tokens": total_in + total_out,
                "steps": final_usage,
            }

        try:
            async for chunk in llm.astream(messages):
                if request and await request.is_disconnected():
                    logger.info("Client disconnect detected. Aborting stream.")
                    raise asyncio.CancelledError("Client disconnected")

                if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                    usage = chunk.usage_metadata
                    if isinstance(usage, dict):
                        input_tokens += usage.get("input_tokens", 0) or 0
                        output_tokens += usage.get("output_tokens", 0) or 0
                    else:
                        input_tokens += getattr(usage, "input_tokens", 0) or 0
                        output_tokens += getattr(usage, "output_tokens", 0) or 0

                content = chunk.content
                if isinstance(content, list):
                    token = "".join([
                        part.get("text", "") if isinstance(part, dict) else str(part)
                        for part in content
                    ])
                else:
                    token = content

                if token:
                    if not _first_token_logged:
                        logger.info("[TIMING] Step 5 — llm_first_token: %.3fs", time.perf_counter() - _t0)
                        _first_token_logged = True
                    full_response_parts.append(token)
                    yield json.dumps({"event": "token", "content": token}) + "\n\n"

        except asyncio.CancelledError:
            logger.info("Client aborted stream. Saving partial response in background.")
            full_response = "".join(full_response_parts)
            if full_response:
                asyncio.create_task(MessageService.save_message(
                    chat_id=chat_id,
                    role=MessageRole.ASSISTANT,
                    content=full_response,
                    sources=sources,
                    token_usage=_build_token_usage(),
                    related_case_ids=[c["id"] for c in matching_cases if "id" in c],
                ))
            raise

        except Exception as exc:
            logger.error("LLM streaming error: %s", exc, exc_info=True)
            error_msg = "I'm sorry, I encountered an error generating a response. Please try again."
            full_response_parts = [error_msg]
            yield json.dumps({"event": "token", "content": error_msg}) + "\n\n"

        full_response = "".join(full_response_parts)
        logger.info(
            "[TIMING] Step 5 — llm_streaming_complete: %.3fs | chars=%d | in_tok=%d out_tok=%d",
            time.perf_counter() - _t0, len(full_response), input_tokens, output_tokens,
        )

        # ── Emit sources (disabled — sources still saved to DB for anchor context) ──
        # if sources:
        #     clean_sources = [{k: v for k, v in s.items() if k != "content"} for s in sources]
        #     yield json.dumps({"event": "sources", "sources": clean_sources}) + "\n\n"

        # ── Save assistant message with full usage metrics ─────────────────────
        _t0 = time.perf_counter()
        assistant_msg = await MessageService.save_message(
            chat_id=chat_id,
            role=MessageRole.ASSISTANT,
            content=full_response,
            sources=sources,
            token_usage=_build_token_usage(),
            related_case_ids=[c["id"] for c in matching_cases if "id" in c],
        )
        logger.info("[TIMING] Step 7 — save_assistant_message: %.3fs", time.perf_counter() - _t0)
        logger.info("[TIMING] stream_response() TOTAL: %.3fs", time.perf_counter() - _stream_t0)

        # ── Emit Case Studies ──────────────────────────────────────────────────────
        if matching_cases:
            yield json.dumps({"event": "case_study", "cases": matching_cases}) + "\n\n"

        yield json.dumps({"event": "done", "message_id": assistant_msg["id"]}) + "\n\n"
