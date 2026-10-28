"""
Context building for the RAG pipeline output.

Handles:
  - Source grouping/deduplication by (document_name, page, blob_url)
  - Document name normalisation (strip paths, extensions)
  - Image chunk filtering (skip scanned text/heading artifacts)
  - Context string assembly for the LLM system prompt
"""
import logging
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Keywords that indicate a chunk is a scanned text/heading artifact, not a real image
_TEXT_SCAN_KEYWORDS = frozenset({
    "textual heading", "section heading", "section title",
    "heading for", "title for", "header for",
    "typographic", "text-based", "scanned text",
    "serves as a heading", "serves as a title",
    "chapter heading", "section discussing",
    "table index", "figure index",
})


def normalise_doc_name(doc_name: str) -> str:
    """Strip folder paths and common file extensions from a document name."""
    if not doc_name or not isinstance(doc_name, str):
        return doc_name or "Unknown"
    if "/" in doc_name:
        doc_name = doc_name.split("/")[-1]
    if "\\" in doc_name:
        doc_name = doc_name.split("\\")[-1]
    lower = doc_name.lower()
    if lower.endswith(".xlsx"):
        doc_name = doc_name[:-5]
    elif lower.endswith(".pdf"):
        doc_name = doc_name[:-4]
    return doc_name


def _is_text_scan_image(source: Dict[str, Any]) -> bool:
    """Return True if an image chunk looks like a scanned text/heading artifact."""
    clinical_desc = (source.get("clinical_description") or "").lower()
    content = (source.get("content") or "").lower()
    if any(kw in clinical_desc or kw in content for kw in _TEXT_SCAN_KEYWORDS):
        return True
    # Extra check: very short content that only says "table X" or "figure Y"
    word_count = len(content.split())
    if word_count < 10 and any(kw in content for kw in ("table", "figure", "part")):
        return True
    return False


def group_and_dedupe_sources(
    raw_sources: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Deduplicate raw RAG results by (document_name, page, blob_url) and
    merge content from duplicate chunks into the primary entry.
    """
    grouped: Dict[tuple, Dict[str, Any]] = {}
    ordered: List[Dict[str, Any]] = []

    for s in raw_sources:
        doc_name = normalise_doc_name(s.get("document_name") or "Unknown")
        s["document_name"] = doc_name

        page = s.get("page")
        blob_url = s.get("blob_url", "")
        key = (doc_name, page, blob_url)

        if key in grouped:
            existing = grouped[key]
            extra = (s.get("content") or "").strip()
            if extra and extra not in existing.get("content", ""):
                existing["content"] += "\n...\n" + extra
        else:
            s_copy = s.copy()
            grouped[key] = s_copy
            ordered.append(s_copy)

    return ordered


def build_context_text(
    sources: List[Dict[str, Any]],
) -> Tuple[str, List[str]]:
    """
    Build the context string for the LLM and collect RAG image SAS URLs.

    Returns:
        (context_text, rag_image_sas_urls)
    """
    context_parts: List[str] = []
    rag_image_sas_urls: List[str] = []

    for s in sources:
        content = (s.get("content") or "").strip()
        page = s.get("page")
        label = s.get("document_name", "Unknown")
        clinical_desc = s.get("clinical_description") or ""

        chunk_type = str(s.get("chunk_type", "")).lower()
        is_image_chunk = chunk_type in ("figure", "image")
        is_xlsx_image = s.get("source_type") == "xlsx"
        has_real_clinical_image = (
            bool(clinical_desc and len(clinical_desc) > 20)
            and not _is_text_scan_image(s)
        )
        should_show_image = (
            s.get("blob_url")
            and is_image_chunk
            and (is_xlsx_image or has_real_clinical_image)
        )

        if should_show_image:
            sas_url = s["blob_url"]
            s["image_url"] = sas_url  # expose to frontend source object

            image_context = "\n[FIGURE CONTEXT]"
            if clinical_desc:
                image_context += f"\nRETRIEVED CLINICAL DESCRIPTION (Expert Visual Analysis): {clinical_desc}"
            image_context += f"\nRETRIEVED IMAGE URL (CITABLE EVIDENCE): {sas_url}"
            content += image_context

            if sas_url not in rag_image_sas_urls:
                rag_image_sas_urls.append(sas_url)

        page_str = f"Pg {page}" if page else "No Pg"
        context_parts.append(f"[Source: {label} | {page_str}]\n{content}")

    return "\n\n".join(context_parts), rag_image_sas_urls


def build_analysis_block(
    analysis: Dict[str, Any],
    has_images: bool,
) -> Tuple[str, str]:
    """
    Build the analysis_block and format_instructions strings for the system prompt.

    Returns:
        (format_instructions, analysis_block)
    """
    from app.services.message_service.llm_clients import (
        VISUAL_FORMAT_INSTRUCTIONS,
        TEXT_FORMAT_INSTRUCTIONS,
    )

    if has_images and analysis:
        format_instructions = VISUAL_FORMAT_INSTRUCTIONS
        anatomy_str = ", ".join(analysis.get("anatomy", [])) or "None specified"
        signs_str = ", ".join(analysis.get("visual_signs", [])) or "None specified"
        analysis_block = f"""
            --- IMAGE SEARCH CONTEXT (for matching only) ---
            The following observations were extracted from the uploaded image to help find matching conditions in the knowledge base.
            These are NOT a diagnosis. Use them ONLY to identify which retrieved content best matches the image.

            Image Type: {analysis.get('modality', 'unknown')}
            Anatomical Region: {anatomy_str}
            Observed Visual Features: {signs_str}
            -----------------------------------------------

            CRITICAL: Your response must come ONLY from the retrieved clinical content below.
            Match the observed visual features against conditions described in the retrieved documents.
            If no retrieved content describes a matching condition, say so explicitly.
            Do NOT generate your own clinical assessment or diagnosis from the image.
            """
    else:
        format_instructions = TEXT_FORMAT_INSTRUCTIONS
        analysis_block = """
            TEXT-ONLY RESPONSE REMINDER:
            This query does not include an uploaded image.
            Prefer a concise clinical-reference style answer unless the question clearly requires a more detailed explanation.
            Do NOT use visual-analysis headers.
            If a RETRIEVED IMAGE URL is provided in the context below and it is a relevant clinical photo or diagram, display it using markdown: ![description](url).
            """

    return format_instructions, analysis_block
