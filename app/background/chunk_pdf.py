"""
PDF Chunker — Multimodal Clinical RAG Ingestion

• Converts parsed PDF elements into RAG-ready chunks for the dental knowledge base.
• Text is chunked using hierarchy-aware semantic grouping (H1/H2 sections preserved).
• Sentence-level overlap is applied to maintain clinical context across chunk boundaries.
• Figures (images/X-rays) are stored as atomic retrieval units enriched via Gemini Vision.
• Extracted figure metadata includes modality, anatomy, visual_signs, and candidate_conditions.
• Figures are linked to captions, callout paragraphs (e.g., “See Fig. 4.2”), nearby text, and topic_id.
• Tables are rendered as Markdown to improve LLM reasoning over structured clinical data.
• Repeated images are deduplicated using SHA-256 hashing to reuse visual analysis and reduce API cost.
• Output chunks include content, embedding_text, structured metadata, and relational links for retrieval.
hierarchy-first + semantic grouping + sentence overlap for text + atomic enriched chunks for figures/tables + relational linking for later context expansion.
"""
import hashlib

def _generate_chunk_hash(content: str) -> str:
    """Generate a stable SHA-256 hash for chunk content to support payload-specific deduplication."""
    if not content:
        return "empty"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()

def get_topic_id(document_id: str, topic_name: str) -> str:
    """Generate a stable URI-safe ID for a topic within a document."""
    if not topic_name:
        return f"{document_id}_topic_general"
    # Use re to strip symbols
    import re
    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', topic_name.lower())
    return f"{document_id}_{safe_name}"

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import re
import json

from bson import ObjectId
from tenacity import retry, stop_after_attempt, wait_exponential
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage
import base64

from app.config import settings
from app.core.database import get_database
from app.core.constants import DocumentStatus
from app.utils.text_splitter import (
    MAX_WORDS_PER_TEXT_CHUNK,
    OVERLAP_WORDS,
    MIN_PARAGRAPH_WORDS,
    MIN_FINAL_TEXT_CHUNK_WORDS,
    MAX_FINAL_TEXT_CHUNK_WORDS,
    HEADING_ROLES,
    TABLE_REF_RE,
    FIGURE_CALLOUT_ONLY_RE,
    TABLE_ROWISH_RE,
    count_words,
    normalize_whitespace,
    normalize_for_compare,
    clean_paragraph_text,
    is_noise_line,
    is_probable_figure_caption,
    looks_like_figure_callout_or_label,
    looks_like_local_heading,
    looks_incomplete_start,
    looks_incomplete_end,
    split_long_text,
    merge_texts,
    strip_repeated_heading_prefix,
    remove_heading_lines_from_body,
    clean_inline_noise_tokens,
    dedupe_repeated_sentences,
    headings_compatible_for_merge,
    make_embedding_text,
    extract_figure_reference,
    collect_table_span_ranges,
    paragraph_overlaps_table,
)
from app.utils.blob_utils import download_blob
from app.utils.spatial_utils import (
    collect_nearby_paragraphs,
    find_section_heading_for_element,
    infer_content_type_hint,
    get_bbox,
    calculate_overlap_ratio,
)

logger = logging.getLogger(__name__)

# ── Parallel Processing Config ──────────────────────────────────────────────
MAX_CONCURRENT_GEMINI_ENRICHMENT = 3   # Concurrent Gemini Vision calls (respects Vertex QPM)
PROGRESSIVE_SAVE_BATCH = 20            # Flush chunks to MongoDB every N items

# ── Modality Normalization Mapping ──────────────────────────────────────────
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

def normalize_modality_value(raw: Optional[str]) -> str:
    """Map raw clinical modality detection to a canonical string."""
    if not raw:
        return "unknown"
    val = str(raw).lower().strip().replace(" ", "_").replace("-", "_")
    
    # Direct match first
    if val in CANONICAL_MODALITIES:
        return val
        
    # Pattern matching
    if "periapical" in val: return "periapical_xray"
    if "pan" in val or "opg" in val: return "panoramic_xray"
    if "photo" in val or "intraoral" in val: return "intraoral_photo"
    if "bitewing" in val: return "bitewing_xray"
    if "cone" in val or "cbct" in val: return "cbct"
    if "histo" in val or "patho" in val: return "histology"
    if "diag" in val or "schem" in val or "illus" in val: return "diagram"
    
    return "unknown"
# ──────────────────────────────────────────────────────────────────────────────
async def _get_clinical_description(
    image_bytes: bytes, 
    caption: str, 
    topic: str,
    chapter: str
) -> dict:
    """Entry point for clinical enrichment with final fallback support."""
    try:
        return await _get_clinical_description_core(image_bytes, caption, topic, chapter)
    except Exception as e:
        logger.warning(f"Gemini Vision enrichment permanently failed for this image: {e}")
        return {
            "clinical_description": "",
            "modality": "unknown",
            "anatomy": [],
            "visual_signs": [],
            "candidate_conditions": [],
            "context_summary": ""
        }


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True
)
async def _get_clinical_description_core(
    image_bytes: bytes, 
    caption: str, 
    topic: str,
    chapter: str
) -> dict:
    """Internal core logic for Gemini Vision enrichment (wrapped with tenacity retries)."""
    model = ChatGoogleGenerativeAI(
        model=settings.GEMINI_CHAT_MODEL,
        project=settings.GOOGLE_CLOUD_PROJECT,
        location=settings.GOOGLE_CLOUD_LOCATION,
        credentials=settings.get_vertex_credentials(),
        vertexai=True,
        temperature=0,
    )
    
    image_data = base64.b64encode(image_bytes).decode("utf-8")
    
    prompt = f"""You are a specialized clinical dental AI and image analyst. 
Analyze this figure from a dental textbook.
Chapter: {chapter}
Topic: {topic}
Caption: {caption}

Task: Provide a normalized structured object for clinical analysis in JSON format. 

JSON FIELDS:
1. "clinical_description": A professional, searchable analysis of visual findings (pathognomonic features, density, texture, borders). (Under 150 words).
2. "modality": The specific normalized type of image (e.g., 'periapical_xray', 'panoramic_xray', 'intraoral_photo', 'bitewing_xray', 'histology', 'clinical_illustration', 'diagram').
3. "anatomy": A list of anatomical structures visible (e.g., ['mandibular_ molar', 'maxillary_sinus', 'enamel', 'pulp_chamber']).
4. "visual_signs": A list of specific clinical/radiographic signs (e.g., ['radiolucency', 'bone_loss', 'resorption', 'opaqueness', 'fracture_line']).
5. "candidate_conditions": A list of likely diseases or defects discussed (e.g., ['caries', 'periodontitis', 'periapical_abscess', 'internal_resorption']).
6. "context_summary": A brief one-sentence summary of the clinical context shown.

Return ONLY the JSON object."""

    message = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_data}"},
            },
        ]
    )
    
    response = await model.ainvoke([message])
    
    # Robustly handle string or list response content
    raw_content = response.content
    if isinstance(raw_content, list):
        # Join text parts if it's a multipart response
        content = "".join([
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in raw_content
        ])
    else:
        content = str(raw_content)

    content = content.strip() if content else "{}"
    
    # Clean potential markdown from Gemini
    if "```json" in content:
        content = content.split("```json")[1].split("```")[0].strip()
    elif "```" in content:
        content = content.split("```")[1].strip()

    try:
        return json.loads(content)
    except Exception as json_err:
        logger.warning(f"Failed to parse clinical JSON: {json_err}. Raw: {content}")
        return {
            "clinical_description": content,
            "modality": "unknown",
            "anatomy": [],
            "visual_signs": [],
            "candidate_conditions": [],
            "context_summary": ""
        }


def _get_image_hash(image_bytes: bytes) -> str:
    """Calculate SHA-256 hash of image bytes for de-duplication."""
    return hashlib.sha256(image_bytes).hexdigest()


async def _get_cached_clinical_analysis(db, image_hash: str) -> Optional[dict]:
    """Retrieve cached clinical analysis results by image hash."""
    cache = db["clinical_enrichment_cache"]
    result = await cache.find_one({"image_hash": image_hash})
    if result:
        # Don't return the MongoDB ID
        result.pop("_id", None)
        return result
    return None


async def _save_clinical_analysis_to_cache(db, image_hash: str, analysis: dict) -> None:
    """Save clinical analysis results to cache."""
    cache = db["clinical_enrichment_cache"]
    # Ensure we store the hash
    analysis["image_hash"] = image_hash
    analysis["cached_at"] = datetime.now(timezone.utc)
    await cache.update_one(
        {"image_hash": image_hash},
        {"$set": analysis},
        upsert=True
    )


# ══════════════════════════════════════════════════════════════════════════════
# HEADING-AS-FIGURE DEDUPLICATION
# ══════════════════════════════════════════════════════════════════════════════

def _is_misclassified_heading_figure(
    figure: dict,
    paragraph_records: List[dict],
) -> bool:
    """
    Detect figures that are actually styled text headings misclassified by
    Azure Document Intelligence (common in scanned PDFs).

    E.g. "PROPHYLACTIC ANTIBIOTIC COVERAGE" rendered as white text on a dark
    banner gets extracted as a figure instead of a paragraph.

    Strategy: if the figure's bounding box overlaps >90% with any paragraph's
    bounding box on the same page, it's spatially redundant → skip it.
    """
    fig_brs = figure.get("bounding_regions") or []
    if not fig_brs:
        return False

    fig_poly = fig_brs[0].get("polygon", [])
    fig_page = fig_brs[0].get("page_number") or figure.get("page_number")
    if not fig_poly or len(fig_poly) < 8:
        return False

    fig_bbox = get_bbox(fig_poly)
    # Skip zero-area bboxes
    if (fig_bbox[2] - fig_bbox[0]) <= 0 or (fig_bbox[3] - fig_bbox[1]) <= 0:
        return False

    for para in paragraph_records:
        if para.get("page_number") != fig_page:
            continue

        para_brs = para.get("bounding_regions") or []
        if not para_brs:
            continue

        para_poly = para_brs[0].get("polygon", [])
        if not para_poly or len(para_poly) < 8:
            continue

        para_bbox = get_bbox(para_poly)
        ratio = calculate_overlap_ratio(para_bbox, fig_bbox)
        if ratio > 0.9:
            return True

    return False


# ══════════════════════════════════════════════════════════════════════════════
# PARALLEL GEMINI FIGURE ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

async def _enrich_single_figure(
    figure: dict,
    f_idx: int,
    db,
    gemini_semaphore: asyncio.Semaphore,
    download_semaphore: asyncio.Semaphore,
) -> dict:
    """Enrich a single figure: download blob → hash → cache check → Gemini call."""
    fig_blob_name = figure.get("blob_name")
    result = {
        "f_idx": f_idx,
        "img_hash": "unknown",
        "enrichment": {
            "clinical_description": "",
            "modality": "unknown",
            "anatomy": [],
            "visual_signs": [],
            "candidate_conditions": [],
            "context_summary": ""
        },
    }
    if not fig_blob_name:
        return result

    try:
        async with download_semaphore:
            img_bytes = await download_blob(fig_blob_name)
            if not img_bytes:
                return result

            img_hash = _get_image_hash(img_bytes)
            result["img_hash"] = img_hash

            # Check cache first
            cached = await _get_cached_clinical_analysis(db, img_hash)
            if cached:
                result["enrichment"] = cached
                logger.info(f"   ∟ [CACHE HIT] Figure {f_idx} (hash {img_hash[:8]})")
                return result

        # Gemini call under semaphore
        async with gemini_semaphore:
            caption = figure.get("caption") or ""
            # heading_data will be injected by caller via figure dict
            topic = figure.get("_topic_name") or "General"
            chapter = figure.get("_chapter_name") or "Document"
            enrichment = await _get_clinical_description(img_bytes, caption, topic, chapter)
            result["enrichment"] = enrichment

            if enrichment.get("modality") != "unknown":
                await _save_clinical_analysis_to_cache(db, img_hash, enrichment)

            logger.info(f"   ∟ [GEMINI] Figure {f_idx} enriched (modality={enrichment.get('modality')})")
            # Small delay to spread Vertex QPM
            await asyncio.sleep(0.3)

    except Exception as e:
        logger.error(f"Failed to enrich figure {f_idx}: {e}")

    return result


async def _enrich_figures_parallel(
    figures: List[dict],
    paragraph_records: List[dict],
    paragraphs_raw: List[dict],
    db,
) -> Dict[int, dict]:
    """
    Enrich all figures via Gemini Vision in parallel.
    Returns a dict mapping f_idx (1-based) → enrichment result.
    """
    if not figures:
        return {}

    gemini_semaphore = asyncio.Semaphore(MAX_CONCURRENT_GEMINI_ENRICHMENT)
    download_semaphore = asyncio.Semaphore(50)  # Limit concurrent Azure Blob downloads

    # Pre-compute heading data for each figure so Gemini gets accurate context
    enrichable_figures = []
    for f_idx, figure in enumerate(figures, start=1):
        if _is_misclassified_heading_figure(figure, paragraph_records):
            continue
        # Inject heading context into figure dict for the enrichment function
        brs = figure.get("bounding_regions")
        page = figure.get("page_number")
        heading_data = {"chapter_name": "Document", "topic_name": "General"}
        if brs and paragraphs_raw:
            poly = brs[0].get("polygon", [])
            heading_data = find_section_heading_for_element(paragraphs_raw, poly, page)
        figure["_topic_name"] = heading_data.get("topic_name", "General")
        figure["_chapter_name"] = heading_data.get("chapter_name", "Document")
        enrichable_figures.append((f_idx, figure))

    if not enrichable_figures:
        return {}

    total = len(enrichable_figures)
    print(f"   ∟ [GEMINI] Enriching {total} figures in parallel (concurrency={MAX_CONCURRENT_GEMINI_ENRICHMENT})...", flush=True)

    tasks = [
        _enrich_single_figure(fig, f_idx, db, gemini_semaphore, download_semaphore)
        for f_idx, fig in enrichable_figures
    ]
    results = await asyncio.gather(*tasks)

    enrichment_map: Dict[int, dict] = {}
    for r in results:
        enrichment_map[r["f_idx"]] = r

    cached_count = sum(1 for r in results if r["img_hash"] != "unknown" and r["enrichment"].get("modality") != "unknown")
    print(f"   ∟ [GEMINI] Done. {cached_count}/{total} figures enriched successfully.", flush=True)

    return enrichment_map


# ══════════════════════════════════════════════════════════════════════════════
# PARAGRAPH EXTRACTION & ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

def _get_paragraph_records(parsed: dict) -> List[dict]:
    """Extract, clean, and index paragraphs from parsed_elements."""
    records: List[dict] = []
    for idx, para in enumerate(parsed.get("paragraphs", [])):
        raw_text = para.get("content") or ""
        text = clean_paragraph_text(raw_text)
        if not text:
            continue

        regions = para.get("bounding_regions") or []
        page_number = regions[0].get("page_number") if regions else None

        records.append({
            "paragraph_index": idx,
            "role": para.get("role"),
            "text": text,
            "page_number": page_number,
            "spans": para.get("spans") or [],
            "bounding_regions": regions,
        })
    return records


def _assign_section_headings(
    records: List[dict]
) -> List[dict]:
    """
    Walk paragraphs in order and assign a 3-level rolling hierarchy:
    Chapter (H1) -> Topic (H2) -> Subheading (H3)
    """
    # Patterns for hierarchical levels
    # H1: "Chapter 1", "CHAPTER ONE", or single digit "1. "
    H1_RE = re.compile(r"^(?:chapter\s+\d+|ch\s*\.\s*\d+|[1-9]\.)(?:\s|$)", re.IGNORECASE)
    # H2: "1.1", "2.5"
    H2_RE = re.compile(r"^[1-9]\.[1-9](?:\s|$)", re.IGNORECASE)
    # H3: "1.1.1", "2.1.3"
    H3_RE = re.compile(r"^[1-9]\.[1-9]\.[1-9](?:\s|$)", re.IGNORECASE)

    ch_name: Optional[str] = None
    topic_name: Optional[str] = None
    sub_name: Optional[str] = None
    
    enriched: List[dict] = []

    for record in records:
        role = record.get("role")
        text = record.get("text") or ""

        is_heading = role in HEADING_ROLES or looks_like_local_heading(text)
        
        if is_heading:
            if H1_RE.match(text):
                ch_name = text
                topic_name = None
                sub_name = None
            elif H2_RE.match(text):
                topic_name = text
                sub_name = None
            elif H3_RE.match(text) or role == "sectionHeading":
                sub_name = text
            else:
                # Fallback: if it's a heading but doesn't match a pattern, 
                # treat it as a Subheading within the current Topic
                sub_name = text

        enriched.append({
            **record, 
            "chapter_name": ch_name or "Document",
            "topic_name": topic_name or ch_name or "General",
            "subheading_name": sub_name or topic_name or ch_name or "General",
            "section_heading": sub_name or topic_name or ch_name or "Document"
        })

    return enriched


# ══════════════════════════════════════════════════════════════════════════════
# FIGURE CAPTION & TABLE CONTENT COLLECTION
# ══════════════════════════════════════════════════════════════════════════════

def _collect_figure_caption_texts(figures: List[dict]) -> Set[str]:
    """Collect all known figure captions for exclusion from text chunks."""
    captions: Set[str] = set()
    for fig in figures:
        caption = clean_paragraph_text(fig.get("caption") or "")
        if caption:
            captions.add(caption.lower())
    return captions


def _collect_table_caption_texts(tables: List[dict]) -> Set[str]:
    """Collect known table captions for exclusion from text chunks."""
    captions: Set[str] = set()
    for table in tables:
        cells = table.get("cells") or []
        for cell in cells[:3]:
            content = clean_paragraph_text(cell.get("content") or "")
            if TABLE_REF_RE.match(content):
                captions.add(content.lower())
    return captions


# ══════════════════════════════════════════════════════════════════════════════
# TEXT PARAGRAPH FILTERING
# ══════════════════════════════════════════════════════════════════════════════

def _filter_text_paragraphs(
    records: List[dict],
    known_figure_captions: Set[str],
    known_table_captions: Set[str],
    table_span_ranges: List[Tuple[int, int]],
) -> List[dict]:
    """Remove paragraphs that are noise, table content, or figure captions."""
    filtered: List[dict] = []
    for rec in records:
        role = rec.get("role")
        text = rec.get("text") or ""

        if not text:
            continue
        # Keep headings always
        if role in HEADING_ROLES:
            filtered.append(rec)
            continue
        if is_noise_line(text):
            continue
        if text.lower() in known_figure_captions:
            continue
        if text.lower() in known_table_captions:
            continue
        if is_probable_figure_caption(text):
            continue
        if TABLE_REF_RE.match(text):
            continue
        if looks_like_figure_callout_or_label(text):
            continue
        if TABLE_ROWISH_RE.match(text):
            continue
        if paragraph_overlaps_table(rec.get("spans") or [], table_span_ranges):
            continue
        filtered.append(rec)
    return filtered


# ══════════════════════════════════════════════════════════════════════════════
# TEXT CHUNK GROUPING
# ══════════════════════════════════════════════════════════════════════════════

def _group_paragraphs_by_heading(records: List[dict]) -> List[dict]:
    """Group consecutive paragraphs under the same section heading."""
    groups: List[dict] = []
    current_group: Optional[dict] = None

    for rec in records:
        text = rec["text"]
        section_heading = rec.get("section_heading") or "Document"
        role = rec.get("role")

        if role in HEADING_ROLES:
            if current_group:
                groups.append(current_group)
                current_group = None
            continue

        if not text:
            continue

        if current_group is None:
            current_group = {
                "section_heading": section_heading,
                "chapter_name": rec.get("chapter_name", "Document"),
                "topic_name": rec.get("topic_name", "General"),
                "subheading_name": rec.get("subheading_name", "General"),
                "paragraph_indexes": [rec["paragraph_index"]],
                "paragraphs": [text],
                "page_numbers": [rec.get("page_number")],
            }
            continue

        if section_heading == current_group["section_heading"]:
            current_group["paragraph_indexes"].append(rec["paragraph_index"])
            current_group["paragraphs"].append(text)
            current_group["page_numbers"].append(rec.get("page_number"))
        else:
            groups.append(current_group)
            current_group = {
                "section_heading": section_heading,
                "chapter_name": rec.get("chapter_name", "Document"),
                "topic_name": rec.get("topic_name", "General"),
                "subheading_name": rec.get("subheading_name", "General"),
                "paragraph_indexes": [rec["paragraph_index"]],
                "paragraphs": [text],
                "page_numbers": [rec.get("page_number")],
            }

    if current_group:
        groups.append(current_group)
    return groups


# ══════════════════════════════════════════════════════════════════════════════
# TEXT CHUNK POST-PROCESSING
# ══════════════════════════════════════════════════════════════════════════════

def _repair_split_pieces(pieces: List[str], section_heading: Optional[str]) -> List[str]:
    """Merge undersized tail pieces and clean up after splitting."""
    if not pieces:
        return []

    cleaned: List[str] = []
    for piece in pieces:
        p = strip_repeated_heading_prefix(piece, section_heading)
        p = remove_heading_lines_from_body(p, section_heading)
        p = clean_inline_noise_tokens(p)
        p = dedupe_repeated_sentences(p)
        p = normalize_whitespace(p)
        if p:
            cleaned.append(p)

    if not cleaned:
        return []

    merged: List[str] = []
    for piece in cleaned:
        if not merged:
            merged.append(piece)
            continue

        prev = merged[-1]
        prev_words = count_words(prev)
        piece_words = count_words(piece)

        should_merge = False
        if piece_words < 70:
            should_merge = True
        elif looks_incomplete_end(prev) and looks_incomplete_start(piece):
            should_merge = True
        elif looks_incomplete_start(piece):
            should_merge = True
        elif prev_words < MIN_FINAL_TEXT_CHUNK_WORDS:
            should_merge = True

        if should_merge and (count_words(prev) + piece_words) <= (MAX_FINAL_TEXT_CHUNK_WORDS + 40):
            merged[-1] = merge_texts(prev, piece)
        else:
            merged.append(piece)

    return merged


def _should_merge_text_chunks(current: dict, next_chunk: dict) -> bool:
    """Decide whether two adjacent text chunks should be merged."""
    if current.get("chunk_type") != "text" or next_chunk.get("chunk_type") != "text":
        return False
    if not headings_compatible_for_merge(
        current.get("section_heading"), next_chunk.get("section_heading")
    ):
        return False

    current_text = normalize_whitespace(current.get("content", ""))
    next_text = normalize_whitespace(next_chunk.get("content", ""))
    current_words = count_words(current_text)
    next_words = count_words(next_text)
    combined_words = current_words + next_words

    if combined_words > MAX_FINAL_TEXT_CHUNK_WORDS:
        return False

    current_bad = current_words < MIN_FINAL_TEXT_CHUNK_WORDS or looks_incomplete_end(current_text)
    next_bad = next_words < MIN_FINAL_TEXT_CHUNK_WORDS or looks_incomplete_start(next_text)

    if looks_incomplete_end(current_text) and looks_incomplete_start(next_text):
        return True
    if current_words < MIN_FINAL_TEXT_CHUNK_WORDS and next_bad:
        return True
    if next_words < MIN_FINAL_TEXT_CHUNK_WORDS and current_bad:
        return True
    return False


def _finalize_text_chunk(chunk: dict, document_name: str) -> dict:
    """Apply final cleaning and rebuild embedding_text for a text chunk."""
    text = chunk.get("content", "") or ""
    text = clean_inline_noise_tokens(text)
    text = strip_repeated_heading_prefix(text, chunk.get("section_heading"))
    text = remove_heading_lines_from_body(text, chunk.get("section_heading"))
    text = dedupe_repeated_sentences(text)
    text = normalize_whitespace(text)

    start_page = chunk.get("page_number")
    end_page = chunk.get("_end_page")
    extra_lines = None
    if start_page and end_page and start_page != end_page:
        extra_lines = [f"Page range: {start_page}-{end_page}"]

    chunk["content"] = text
    chunk["embedding_text"] = make_embedding_text(
        chunk_type="text",
        chapter_name=chunk.get("chapter_name"),
        topic_name=chunk.get("topic_name"),
        subheading_name=chunk.get("subheading_name"),
        section_heading=chunk.get("section_heading"),
        page_number=start_page,
        main_text=text,
        document_name=document_name,
        extra_lines=extra_lines,
    )
    return chunk


def _postprocess_text_chunks(chunks: List[dict], document_name: str) -> List[dict]:
    """Merge undersized chunks and finalize all text chunks."""
    if not chunks:
        return []

    prepared = [_finalize_text_chunk(dict(c), document_name) for c in chunks]

    merged: List[dict] = []
    i = 0
    while i < len(prepared):
        current = prepared[i]
        while i + 1 < len(prepared) and _should_merge_text_chunks(current, prepared[i + 1]):
            next_chunk = prepared[i + 1]
            current["content"] = merge_texts(current.get("content", ""), next_chunk.get("content", ""))
            # Expand page range
            pages = []
            for c in [current, next_chunk]:
                if c.get("page_number"):
                    pages.append(c["page_number"])
                if c.get("_end_page"):
                    pages.append(c["_end_page"])
            if pages:
                current["page_number"] = min(pages)
                current["_end_page"] = max(pages)
            current = _finalize_text_chunk(current, document_name)
            i += 1
        merged.append(current)
        i += 1

    return merged


# ══════════════════════════════════════════════════════════════════════════════
# TABLE RENDERING
# ══════════════════════════════════════════════════════════════════════════════

def _table_to_text(table: dict) -> str:
    """Convert a parsed table to high-fidelity Markdown format for RAG."""
    rows: dict[int, dict[int, str]] = {}
    max_col = 0
    for cell in table.get("cells", []):
        r = cell.get("row_index", 0)
        c = cell.get("column_index", 0)
        content = clean_paragraph_text(cell.get("content") or "").replace("|", "\\|")
        rows.setdefault(r, {})[c] = content
        if c > max_col:
            max_col = c

    rendered: List[str] = []
    sorted_rows = sorted(rows.keys())
    if not sorted_rows:
        return ""

    for pos, row_idx in enumerate(sorted_rows):
        cols = rows[row_idx]
        ordered = [cols.get(c, "").strip() for c in range(max_col + 1)]
        row_text = "| " + " | ".join(ordered) + " |"
        rendered.append(row_text)
        
        # Add Markdown separator after header row
        if pos == 0:
            separator = "| " + " | ".join(["---"] * (max_col + 1)) + " |"
            rendered.append(separator)

    return "\n".join(rendered).strip()


# Token budget for table content inside embedding_text.
# 8192 (model limit) - ~200 (metadata prefix from make_embedding_text) = ~7992.
# Use 6000 as a conservative budget so tables + caption + nearby context fit safely.
_TABLE_CHUNK_MAX_TOKENS = 6000

def _split_table_text(table_md: str, max_tokens: int = _TABLE_CHUNK_MAX_TOKENS) -> List[str]:
    """Split a large markdown table into multiple parts, each under max_tokens.

    Each part retains the header row and separator for context.
    Small tables are returned as-is in a single-element list.
    """
    import tiktoken
    _enc_local = tiktoken.get_encoding("cl100k_base")

    if len(_enc_local.encode(table_md)) <= max_tokens:
        return [table_md]

    lines = table_md.split("\n")
    if len(lines) < 3:
        # Can't meaningfully split a table with fewer than 3 lines
        return [table_md]

    # First two lines are header + separator
    header_lines = lines[:2]
    header_text = "\n".join(header_lines)
    header_tokens = len(_enc_local.encode(header_text + "\n"))
    data_rows = lines[2:]

    parts: List[str] = []
    current_rows: List[str] = []
    current_tokens = header_tokens

    for row in data_rows:
        row_tokens = len(_enc_local.encode(row + "\n"))
        if current_rows and (current_tokens + row_tokens) > max_tokens:
            # Flush current part
            part = header_text + "\n" + "\n".join(current_rows)
            parts.append(part.strip())
            current_rows = []
            current_tokens = header_tokens

        current_rows.append(row)
        current_tokens += row_tokens

    if current_rows:
        part = header_text + "\n" + "\n".join(current_rows)
        parts.append(part.strip())

    return parts if parts else [table_md]


def _is_misclassified_heading_figure(figure: dict, paragraphs: List[dict]) -> bool:
    """
    Check if a figure is likely just a stylized heading misclassified as an image.
    This happens in image-over-text PDFs where headings have background colors or borders.
    """
    # 1. If it has a caption, it's very likely a real clinical figure
    caption = (figure.get("caption") or "").strip()
    if caption:
        return False

    fig_brs = figure.get("bounding_regions") or []
    if not fig_brs:
        return False
    
    fig_bbox = get_bbox(fig_brs[0].get("polygon", []))
    fig_page = fig_brs[0].get("page_number")
    
    # 2. Check for overlapping paragraphs with heading roles or heading-like text
    for para in paragraphs:
        if para.get("page_number") != fig_page:
            continue
            
        role = para.get("role")
        text = para.get("text") or ""
        
        # Only check against headings or heading-like short text
        is_heading_candidate = role in HEADING_ROLES or looks_like_local_heading(text)
        if not is_heading_candidate:
            continue
            
        para_brs = para.get("bounding_regions") or []
        if not para_brs:
            continue
            
        para_bbox = get_bbox(para_brs[0].get("polygon", []))
        
        # Calculate how much of the paragraph is "inside" the figure
        # If a heading paragraph is 90% contained within a caption-less figure, 
        # the figure is redundant.
        ratio = calculate_overlap_ratio(para_bbox, fig_bbox)
        if ratio > 0.9:
            return True
            
    return False


# ══════════════════════════════════════════════════════════════════════════════
# MAIN CHUNKER
# ══════════════════════════════════════════════════════════════════════════════

async def chunk_pdf(document_id: str) -> None:
    """
    Phase 2: Build RAG chunks from parsed PDF elements.

    1. Text chunks — heading-aware grouping with noise filtering, overlap, post-merge
    2. Table chunks — readable text format + nearby context
    3. Figure chunks — caption + section + nearby text + blob_name for frontend
    """
    db = await get_database()

    # ── Load parsed data ────────────────────────────────────────────────────
    doc = await db["documents"].find_one({"_id": ObjectId(document_id)})
    if not doc:
        raise RuntimeError(f"Document {document_id} not found")

    document_name = doc.get("name", "Document")
    source_filename = doc.get("filename", "unknown.pdf")

    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {"status": DocumentStatus.CHUNKING, "updated_at": datetime.now(timezone.utc)}}
    )

    # ── Safety: Wipe existing chunks if this is a retry ─────────────────────
    await db["chunks"].delete_many({"document_id": document_id})

    parsed = doc.get("parsed_elements", {})
    paragraphs_raw = parsed.get("paragraphs", [])
    tables = parsed.get("tables", [])
    figures = parsed.get("figures", [])
    source_filename = doc.get("original_filename", "")
    blob_name = doc.get("blob_name", "")

    sequence = 0

    # ══════════════════════════════════════════════════════════════════════
    # 1. TEXT CHUNKS — advanced heading-aware grouping with noise filtering
    # ══════════════════════════════════════════════════════════════════════

    # Extract and clean paragraphs
    paragraph_records = _get_paragraph_records(parsed)
    paragraph_records = _assign_section_headings(paragraph_records)

    # Collect things to exclude from text chunks
    known_figure_captions = _collect_figure_caption_texts(figures)
    known_table_captions = _collect_table_caption_texts(tables)
    table_span_ranges = collect_table_span_ranges(tables)

    # Filter out noise, captions, table content
    filtered_records = _filter_text_paragraphs(
        paragraph_records,
        known_figure_captions,
        known_table_captions,
        table_span_ranges,
    )

    # Group into sections (H1/H2/H3)
    groups = _group_paragraphs_by_heading(filtered_records)
    
    # ── Text chunking with overlap ───────────────────────────────────────────

    # Build text chunks with semantic grouping
    raw_text_chunks: List[dict] = []
    for group in groups:
        section_heading = group["section_heading"]
        chapter_name = group.get("chapter_name", "Document")
        topic_name = group.get("topic_name", "General")
        sub_name = group.get("subheading_name", "General")
        
        paragraph_indexes = group["paragraph_indexes"]
        paragraphs = group["paragraphs"]

        current_parts: List[str] = []
        current_indexes: List[int] = []
        current_pages: List[int] = []
        current_words = 0

        async def _flush_text_group():
            nonlocal sequence
            if not current_parts:
                return
            combined = "\n\n".join(current_parts).strip()
            
            # USE SENTENCE-AWARE SPLITTING WITH TOKEN LIMITS
            split_pieces = split_long_text(
                combined, 
                max_words=MAX_WORDS_PER_TEXT_CHUNK, 
                overlap_words=OVERLAP_WORDS
            )
            split_pieces = _repair_split_pieces(split_pieces, section_heading)

            start_page = min(current_pages) if current_pages else None
            end_page = max(current_pages) if current_pages else None

            for piece_idx, piece in enumerate(split_pieces, start=1):
                piece = strip_repeated_heading_prefix(piece, section_heading)
                piece = remove_heading_lines_from_body(piece, section_heading)
                piece = clean_inline_noise_tokens(piece)
                piece = dedupe_repeated_sentences(piece)
                piece = normalize_whitespace(piece)

                if not piece:
                    continue

                extra_lines = None
                if start_page and end_page and start_page != end_page:
                    extra_lines = [f"Page range: {start_page}-{end_page}"]

                embedding_text = make_embedding_text(
                    chunk_type="text",
                    chapter_name=chapter_name,
                    topic_name=topic_name,
                    subheading_name=sub_name,
                    document_name=document_name,
                    section_heading=section_heading,
                    page_number=start_page,
                    main_text=piece,
                    modality="text",
                    extra_lines=extra_lines,
                )

                topic_id = get_topic_id(document_id, topic_name)

                raw_text_chunks.append({
                    "chunk_id": f"{document_id}_text_{sequence}",
                    "document_id": document_id,
                    "sequence": sequence,
                    "source_type": "pdf",
                    "source_filename": source_filename,
                    "chunk_type": "text",
                    "content": piece,
                    "embedding_text": embedding_text,
                    "page_number": start_page,
                    "page_range": [start_page, end_page] if start_page else None,
                    "_end_page": end_page,
                    "section_heading": section_heading,
                    "chapter_name": chapter_name,
                    "topic_name": topic_name,
                    "subheading_name": sub_name,
                    "source_document": document_name,
                    "topic_id": topic_id,
                    "modality": None,
                    "anatomy": [],
                    "visual_signs": [],
                    "candidate_conditions": [],
                    "linked_chunk_ids": [],
                    "figure_id": None,
                    "table_id": None,
                    "sheet_name": None,
                    "row_number": None,
                    "blob_name": None,
                    "caption": None,
                    "nearby_text": None,
                    "embedding_model": None,
                    "embedding": None,
                    "created_at": datetime.now(timezone.utc),
                    # Hard-tracking fields
                    "chunk_hash": _generate_chunk_hash(piece),
                    "is_embedded": False,
                    "is_indexed": False,
                    "embedding_attempts": 0,
                    "indexing_attempts": 0,
                    "last_processed_at": None,
                    "embedding_error": None,
                    "indexing_error": None,
                    "embedded_at": None,
                    "indexed_at": None,
                    "embedding_version": "v1",
                })
                sequence += 1

        for p_idx, para, page_no in zip(paragraph_indexes, paragraphs, group["page_numbers"]):
            para_words = count_words(para)

            if para_words < MIN_PARAGRAPH_WORDS and current_parts:
                current_parts.append(para)
                current_indexes.append(p_idx)
                if page_no is not None:
                    current_pages.append(page_no)
                current_words += para_words
                continue

            if current_parts and (current_words + para_words > MAX_WORDS_PER_TEXT_CHUNK):
                await _flush_text_group()
                current_parts = [para]
                current_indexes = [p_idx]
                current_pages = [page_no] if page_no is not None else []
                current_words = para_words
            else:
                current_parts.append(para)
                current_indexes.append(p_idx)
                if page_no is not None:
                    current_pages.append(page_no)
                current_words += para_words

        await _flush_text_group()

    # Post-process: merge undersized chunks, finalize
    text_chunks = _postprocess_text_chunks(raw_text_chunks, document_name)

    # Re-number after merge
    for idx, chunk in enumerate(text_chunks):
        chunk["chunk_id"] = f"{document_id}_text_{idx + 1}"
        chunk["sequence"] = idx
        chunk.pop("_end_page", None)  # Remove internal field

    # ── Progressive Save: text chunks → MongoDB immediately ──────────────
    if text_chunks:
        await db["chunks"].insert_many(text_chunks)
        print(f"   ∟ [SAVE] Flushed {len(text_chunks)} text chunks to DB", flush=True)
    sequence = len(text_chunks)
    saved_count = len(text_chunks)

    # ══════════════════════════════════════════════════════════════════════
    # 2. TABLE CHUNKS — readable text + nearby context
    # ══════════════════════════════════════════════════════════════════════

    table_buffer: List[dict] = []
    for t_idx, table in enumerate(tables, start=1):
        table_text = _table_to_text(table)
        if not table_text:
            continue

        caption = table.get("caption", "")
        page = table.get("page_number")
        table_id = f"table_{page}_{t_idx}"

        # Spatial context
        nearby = ""
        heading_data = {
            "chapter_name": "Document",
            "topic_name": "General",
            "subheading_name": "General",
            "section_heading": "Document"
        }
        brs = table.get("bounding_regions")
        if brs and paragraphs_raw:
            poly = brs[0].get("polygon", [])
            context = collect_nearby_paragraphs(paragraphs_raw, poly, page, max_before=2, max_after=2)
            nearby = context["combined_text"]
            heading_data = find_section_heading_for_element(paragraphs_raw, poly, page)

        topic_id = get_topic_id(document_id, heading_data["topic_name"])

        # Split oversized tables into multiple chunks (each keeps the header row)
        table_parts = _split_table_text(table_text)
        num_parts = len(table_parts)

        for part_idx, table_part in enumerate(table_parts, start=1):
            content_parts = ["Table:"]
            content_parts.append(table_part)
            if caption:
                content_parts.insert(1, f"Caption: {caption}")
            # Only attach nearby context to the first part to avoid redundancy
            if nearby and part_idx == 1:
                content_parts.append(f"Context: {nearby}")
            if num_parts > 1:
                content_parts.append(f"(Part {part_idx} of {num_parts})")
            full_text = "\n".join(content_parts).strip()

            extra_lines = [
                f"Row count: {table.get('row_count')}",
                f"Column count: {table.get('column_count')}",
            ]
            if num_parts > 1:
                extra_lines.append(f"Part {part_idx} of {num_parts}")

            embedding_text = make_embedding_text(
                chunk_type="table",
                chapter_name=heading_data["chapter_name"],
                topic_name=heading_data["topic_name"],
                subheading_name=heading_data["subheading_name"],
                document_name=document_name,
                section_heading=heading_data["section_heading"],
                page_number=page,
                main_text=full_text,
                modality="table",
                extra_lines=extra_lines,
            )

            # Chunk ID: table_1 for single-part, table_1_p1/table_1_p2 for multi-part
            part_suffix = f"_p{part_idx}" if num_parts > 1 else ""

            table_buffer.append({
                "chunk_id": f"{document_id}_table_{t_idx}{part_suffix}",
                "document_id": document_id,
                "sequence": sequence,
                "source_type": "pdf",
                "source_filename": source_filename,
                "chunk_type": "table",
                "content": full_text,
                "embedding_text": embedding_text,
                "page_number": page,
                "page_range": None,
                "section_heading": heading_data["section_heading"],
                "chapter_name": heading_data["chapter_name"],
                "topic_name": heading_data["topic_name"],
                "subheading_name": heading_data["subheading_name"],
                "source_document": document_name,
                "topic_id": topic_id,
                "modality": None,
                "anatomy": [],
                "visual_signs": [],
                "candidate_conditions": [],
                "linked_chunk_ids": [],
                "figure_id": None,
                "table_id": table_id,
                "sheet_name": None,
                "row_number": None,
                "blob_name": None,
                "caption": caption,
                "nearby_text": nearby if part_idx == 1 else None,
                "embedding_model": None,
                "embedding": None,
                "created_at": datetime.now(timezone.utc),
                # Hard-tracking fields
                "chunk_hash": _generate_chunk_hash(full_text),
                "is_embedded": False,
                "is_indexed": False,
                "embedding_attempts": 0,
                "indexing_attempts": 0,
                "last_processed_at": None,
                "embedding_error": None,
                "indexing_error": None,
                "embedded_at": None,
                "indexed_at": None,
                "embedding_version": "v1",
            })
            sequence += 1

        # Progressive flush every PROGRESSIVE_SAVE_BATCH
        if len(table_buffer) >= PROGRESSIVE_SAVE_BATCH:
            await db["chunks"].insert_many(table_buffer)
            saved_count += len(table_buffer)
            table_buffer.clear()

    # Flush remaining table chunks
    if table_buffer:
        await db["chunks"].insert_many(table_buffer)
        saved_count += len(table_buffer)
        table_buffer.clear()
    print(f"   ∟ [SAVE] Table chunks saved to DB", flush=True)

    # ══════════════════════════════════════════════════════════════════════
    # 3. FIGURE CHUNKS — rich contextual text + blob for frontend
    #    Gemini enrichment runs in PARALLEL before building chunks
    # ══════════════════════════════════════════════════════════════════════

    # ── Pre-enrich all figures via parallel Gemini Vision ─────────────────
    enrichment_map = await _enrich_figures_parallel(figures, paragraph_records, paragraphs_raw, db)

    figure_buffer: List[dict] = []
    for f_idx, figure in enumerate(figures, start=1):
        # ── Dedup: Skip figures that are just misclassified headings ─────────
        if _is_misclassified_heading_figure(figure, paragraph_records):
            logger.info(f"   ∟ [CHUNK] Skipping figure {f_idx}: misclassified heading")
            continue

        page = figure.get("page_number")
        caption = figure.get("caption", "")
        figure_id = figure.get("figure_id") or f"fig_{page}_{f_idx}"
        fig_blob_name = figure.get("blob_name")

        # Parse figure reference from caption
        ref_info = extract_figure_reference(caption or "")
        fig_num = ref_info.get("figure_reference_number")

        # --- 1. Spatial Context ---
        nearby = ""
        heading_data = {
            "chapter_name": "Document",
            "topic_name": "General",
            "subheading_name": "General",
            "section_heading": "Document"
        }
        brs = figure.get("bounding_regions")
        if brs and paragraphs_raw:
            poly = brs[0].get("polygon", [])
            context = collect_nearby_paragraphs(
                paragraphs_raw, 
                poly, 
                page, 
                max_before=2, 
                max_after=2,
                figure_number=fig_num
            )
            nearby = context["combined_text"]
            heading_data = find_section_heading_for_element(paragraphs_raw, poly, page)

        # --- 2. Use pre-computed enrichment (parallel Gemini) ---
        enrichment_data = enrichment_map.get(f_idx)
        if enrichment_data:
            rich_clinical_meta = enrichment_data["enrichment"]
            img_hash = enrichment_data["img_hash"]
        else:
            rich_clinical_meta = {
                "clinical_description": "",
                "modality": "unknown",
                "anatomy": [],
                "visual_signs": [],
                "candidate_conditions": [],
                "context_summary": ""
            }
            img_hash = "unknown"

        # --- 3. Build Text Content ---

        figure_text_parts: List[str] = []
        if ref_info["printed_figure_reference"]:
            figure_text_parts.append(f"Figure reference: {ref_info['printed_figure_reference']}.")
        if caption:
            figure_text_parts.append(f"Caption: {ref_info.get('caption_without_reference') or caption}.")
        if heading_data["section_heading"]:
            figure_text_parts.append(f"Section: {heading_data['section_heading']}.")
        
        ctx_summary = rich_clinical_meta.get("context_summary")
        if ctx_summary:
            figure_text_parts.append(f"Visual Context Summary: {ctx_summary}")

        if nearby:
            figure_text_parts.append(f"Nearby Text Explanation: {nearby}")

        content = " ".join(figure_text_parts).strip()
        if not content:
            content = caption or nearby or f"Figure {figure_id}"

        # Normalize modality
        canonical_modality = normalize_modality_value(rich_clinical_meta.get("modality"))

        # Build structured embedding text for vector search
        extra_lines = [f"Figure ID: {figure_id}"]
        if ref_info["figure_reference_number"]:
            extra_lines.append(f"Figure number: {ref_info['figure_reference_number']}")

        embedding_text = make_embedding_text(
            chunk_type="figure",
            chapter_name=heading_data["chapter_name"],
            topic_name=heading_data["topic_name"],
            subheading_name=heading_data["subheading_name"],
            document_name=document_name,
            section_heading=heading_data["section_heading"],
            page_number=page,
            main_text=content,
            clinical_description=rich_clinical_meta.get("clinical_description"),
            modality=canonical_modality,
            anatomy=rich_clinical_meta.get("anatomy"),
            visual_signs=rich_clinical_meta.get("visual_signs"),
            candidate_conditions=rich_clinical_meta.get("candidate_conditions"),
            extra_lines=extra_lines,
        )

        topic_id = get_topic_id(document_id, heading_data["topic_name"])

        figure_buffer.append({
            "chunk_id": f"{document_id}_figure_{f_idx}",
            "document_id": document_id,
            "sequence": sequence,
            "source_type": "pdf",
            "source_filename": source_filename,
            "chunk_type": "figure",
            "content": content,
            "embedding_text": embedding_text,
            "page_number": page,
            "page_range": None,
            "section_heading": heading_data["section_heading"],
            "chapter_name": heading_data["chapter_name"],
            "topic_name": heading_data["topic_name"],
            "subheading_name": heading_data["subheading_name"],
            "source_document": document_name,
            "topic_id": topic_id,
            "modality": canonical_modality,
            "anatomy": rich_clinical_meta.get("anatomy") or [],
            "visual_signs": rich_clinical_meta.get("visual_signs") or [],
            "candidate_conditions": rich_clinical_meta.get("candidate_conditions") or [],
            "linked_chunk_ids": [],
            "clinical_description": rich_clinical_meta.get("clinical_description"),
            "context_summary": rich_clinical_meta.get("context_summary"),
            "figure_id": figure_id,
            "table_id": None,
            "sheet_name": None,
            "row_number": None,
            "blob_name": fig_blob_name,
            "caption": caption,
            "figure_caption": caption,
            "nearby_text": nearby,
            "content_type_hint": infer_content_type_hint(caption or "", nearby),
            "embedding_model": None,
            "embedding": None,
            "created_at": datetime.now(timezone.utc),
            # Hard-tracking fields
            "chunk_hash": img_hash if img_hash != "unknown" else _generate_chunk_hash(content),
            "is_embedded": False,
            "is_indexed": False,
            "embedding_attempts": 0,
            "indexing_attempts": 0,
            "last_processed_at": None,
            "embedding_error": None,
            "indexing_error": None,
            "embedded_at": None,
            "indexed_at": None,
            "embedding_version": "v1",
        })
        sequence += 1

        # Progressive flush every PROGRESSIVE_SAVE_BATCH
        if len(figure_buffer) >= PROGRESSIVE_SAVE_BATCH:
            await db["chunks"].insert_many(figure_buffer)
            saved_count += len(figure_buffer)
            figure_buffer.clear()

    # Flush remaining figure chunks
    if figure_buffer:
        await db["chunks"].insert_many(figure_buffer)
        saved_count += len(figure_buffer)
        figure_buffer.clear()
    print(f"   ∟ [SAVE] Figure chunks saved to DB", flush=True)

    # ══════════════════════════════════════════════════════════════════════
    # 4. POST-SAVE CROSS-LINKING (lightweight MongoDB update)
    # ══════════════════════════════════════════════════════════════════════

    # Query all saved chunks for this document (only IDs + page + topic)
    all_chunk_metas = []
    link_cursor = db["chunks"].find(
        {"document_id": document_id},
        {"chunk_id": 1, "page_number": 1, "topic_id": 1, "sequence": 1}
    ).sort("sequence", 1)
    async for meta in link_cursor:
        all_chunk_metas.append(meta)

    if all_chunk_metas:
        from collections import defaultdict
        page_map = defaultdict(list)
        topic_map = defaultdict(list)
        
        for c in all_chunk_metas:
            p = c.get("page_number")
            t = c.get("topic_id")
            if p: page_map[p].append(c["chunk_id"])
            if t: topic_map[t].append(c["chunk_id"])

        # Build bulk updates for linked_chunk_ids
        from pymongo import UpdateOne
        bulk_ops = []
        for i, c in enumerate(all_chunk_metas):
            p = c.get("page_number")
            t = c.get("topic_id")
            
            links = set()
            # 1. Neighbor sequence links
            if i > 0: links.add(all_chunk_metas[i-1]["chunk_id"])
            if i < len(all_chunk_metas) - 1: links.add(all_chunk_metas[i+1]["chunk_id"])
            
            # 2. Same page siblings (Cap at 10)
            if p:
                p_siblings = [cid for cid in page_map[p] if cid != c["chunk_id"]]
                links.update(p_siblings[:10])
                        
            # 3. Same H2 topic siblings (Cap at 10, centered on self)
            if t:
                try:
                    self_idx = topic_map[t].index(c["chunk_id"])
                    start_link = max(0, self_idx - 5)
                    end_link = min(len(topic_map[t]), self_idx + 6)
                    focused = topic_map[t][start_link:end_link]
                    links.update([cid for cid in focused if cid != c["chunk_id"]])
                except ValueError:
                    t_siblings = [cid for cid in topic_map[t] if cid != c["chunk_id"]]
                    links.update(t_siblings[:10])
            
            bulk_ops.append(UpdateOne(
                {"_id": c["_id"]},
                {"$set": {"linked_chunk_ids": list(links)}}
            ))

        if bulk_ops:
            await db["chunks"].bulk_write(bulk_ops)
            print(f"   ∟ [LINK] Cross-linked {len(bulk_ops)} chunks", flush=True)

    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {
            "status": DocumentStatus.CHUNKED,
            "chunk_count": saved_count,
            "updated_at": datetime.now(timezone.utc),
        }}
    )

    print(f"   ∟ [CHUNK] Created {saved_count} chunks total (progressively saved)", flush=True)
