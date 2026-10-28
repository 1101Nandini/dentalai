"""
PDF Parser — Phase 1: Extract raw layout elements using Azure Document Intelligence.

Uses the prebuilt-layout model to extract:
  - Paragraphs (with heading roles)
  - Tables (with cells)
  - Figures (with captions and bounding regions)

Optimized for S0 Standard tier:
  - Single API call for PDFs ≤ 1000 pages (no batching overhead)
  - Concurrent 1000-page batches for very large PDFs (> 1000 pages)
  - Concurrent figure image downloads
  - Non-blocking async throughout
"""
import asyncio
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from io import BytesIO

import fitz  # PyMuPDF — for page count detection
from bson import ObjectId
from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import (
    AnalyzeOutputOption,
    DocumentContentFormat,
)
from azure.core.credentials import AzureKeyCredential

from app.config import settings
from app.core.database import get_database
from app.core.constants import DocumentStatus
from app.utils.blob_utils import download_blob
from app.utils.file_utils import upload_blob, generate_sas_url_with_expiry

logger = logging.getLogger(__name__)

# ── Max pages to use for content fingerprint (dedup comparison) ─────────────
FINGERPRINT_MAX_PAGES = 4

# S0 Standard Tier Configuration ──────────────────────────────────────────
# S0 supports up to 2000 pages per call; we use 100 for reliable URL batches.
PAGES_PER_BATCH = 100

# Max concurrent Azure DI calls per document (Reduced to 2 for URL fetching)
MAX_CONCURRENT_DI_CALLS = 2

# Max concurrent figure image downloads per batch
MAX_CONCURRENT_FIGURE_DOWNLOADS = 8


def _get_pdf_page_count(pdf_bytes: bytes, file_type: str = "application/pdf") -> int:
    """Use PyMuPDF to quickly count total pages without Azure (for PDFs only)."""
    if file_type != "application/pdf":
        return 1
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        count = len(doc)
        doc.close()
        return count
    except Exception as e:
        logger.warning(f"PyMuPDF could not read document for page count: {e}")
        return 1


def _serialize_paragraph(para) -> dict:
    """Convert an ADI paragraph to a serializable dict."""
    return {
        "content": para.content,
        "role": para.role,
        "page_number": para.bounding_regions[0].page_number if para.bounding_regions else None,
        "bounding_regions": [
            {
                "page_number": br.page_number,
                "polygon": br.polygon,
            }
            for br in (para.bounding_regions or [])
        ],
        "spans": [
            {"offset": s.offset, "length": s.length}
            for s in (para.spans or [])
        ] if hasattr(para, "spans") and para.spans else [],
    }


def _serialize_table(table, table_index: int) -> dict:
    """Convert an ADI table to a serializable dict."""
    cells = []
    for cell in table.cells:
        cells.append({
            "row_index": cell.row_index,
            "column_index": cell.column_index,
            "content": cell.content,
            "kind": cell.kind,
        })

    return {
        "table_index": table_index,
        "row_count": table.row_count,
        "column_count": table.column_count,
        "page_number": table.bounding_regions[0].page_number if table.bounding_regions else None,
        "bounding_regions": [
            {
                "page_number": br.page_number,
                "polygon": br.polygon,
            }
            for br in (table.bounding_regions or [])
        ],
        "caption": table.caption.content if table.caption else None,
        "cells": cells,
        "spans": [
            {"offset": s.offset, "length": s.length}
            for s in (table.spans or [])
        ] if hasattr(table, "spans") and table.spans else [],
    }


def _serialize_figure(figure, fig_index: int, blob_name: str | None = None) -> dict:
    """Convert an ADI figure to a serializable dict."""
    return {
        "figure_index": fig_index,
        "figure_id": figure.id,
        "caption": figure.caption.content if figure.caption else None,
        "page_number": figure.bounding_regions[0].page_number if figure.bounding_regions else None,
        "blob_name": blob_name,
        "bounding_regions": [
            {
                "page_number": br.page_number,
                "polygon": br.polygon,
            }
            for br in (figure.bounding_regions or [])
        ],
    }


async def _download_and_upload_figure(
    client: DocumentIntelligenceClient,
    model_id: str,
    operation_id: str,
    figure,
    fig_index: int,
    document_id: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Download a single figure image and upload to blob storage (concurrently)."""
    figure_id = getattr(figure, "id", None)
    fig_blob_name = None

    if figure_id:
        async with semaphore:
            try:
                # Run synchronous SDK call in a thread to avoid blocking
                loop = asyncio.get_event_loop()
                image_stream = await loop.run_in_executor(
                    None,
                    lambda: client.get_analyze_result_figure(
                        model_id=model_id,
                        result_id=operation_id,
                        figure_id=figure_id,
                    ),
                )
                figure_bytes = b"".join(chunk for chunk in image_stream)
                safe_fig_id = re.sub(r"[^\w.\-]+", "_", figure_id)
                fig_blob_name = f"admin_doc/figures/{document_id}_{safe_fig_id}.png"
                await upload_blob(fig_blob_name, figure_bytes, "image/png")
            except Exception as e:
                logger.warning(f"Failed to download figure {figure_id}: {e}")

    return _serialize_figure(figure, fig_index, fig_blob_name)


async def _analyze_page_range(
    client: DocumentIntelligenceClient,
    sas_url: str,
    page_range: str | None,
    document_id: str,
    batch_label: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """
    Analyze a page range (or entire document) with Azure Document Intelligence.
    Uses a semaphore to limit concurrent DI calls for the same resource.
    Returns a dict with paragraphs, tables, figures lists and pages_parsed count.
    """
    async with semaphore:
        t0 = time.perf_counter()
        print(f"  [SUBMITTING] {batch_label}: submitting to Azure DI...", flush=True)

        try:
            # Run the synchronous SDK poller in a thread pool
            loop = asyncio.get_event_loop()

            def _run_analysis():
                kwargs = {
                    "model_id": "prebuilt-layout",
                    "body": {"urlSource": sas_url},
                    "output_content_format": DocumentContentFormat.MARKDOWN,
                    "output": [AnalyzeOutputOption.FIGURES],
                }
                if page_range:
                    kwargs["pages"] = page_range

                poller = client.begin_analyze_document(**kwargs)
                # Large PDFs (500+ pages) can take 5-10 minutes on S0
                return poller.result(timeout=600), poller.details["operation_id"], poller

            result, operation_id, poller = await loop.run_in_executor(None, _run_analysis)
            model_id = result.model_id

            elapsed = time.perf_counter() - t0
            batch_pages = len(result.pages) if result.pages else 0
            batch_paras = len(result.paragraphs or [])
            batch_tables = len(result.tables or [])
            batch_figures = len(result.figures or [])
            print(
                f"    [SUCCESS] {batch_label}: {batch_pages} pages, "
                f"{batch_paras} paragraphs, {batch_tables} tables, "
                f"{batch_figures} figures ({elapsed:.1f}s)",
                flush=True
            )

            # ── Serialize paragraphs & tables ───────────────────────────────
            paragraphs = [_serialize_paragraph(p) for p in (result.paragraphs or [])]
            tables = []
            for t_idx, t in enumerate(result.tables or []):
                tables.append(_serialize_table(t, t_idx))

            # ── Download figures concurrently ───────────────────────────────
            fig_semaphore = asyncio.Semaphore(MAX_CONCURRENT_FIGURE_DOWNLOADS)
            figure_tasks = [
                _download_and_upload_figure(
                    client, model_id, operation_id,
                    f, f_idx, document_id, fig_semaphore,
                )
                for f_idx, f in enumerate(result.figures or [])
            ]
            figures = await asyncio.gather(*figure_tasks) if figure_tasks else []

            return {
                "paragraphs": paragraphs,
                "tables": tables,
                "figures": list(figures),
                "pages_parsed": batch_pages,
            }

        except Exception as e:
            logger.error(f"Failed to parse {batch_label}: {e}")
            return {"paragraphs": [], "tables": [], "figures": [], "pages_parsed": 0}


async def parse_pdf(document_id: str, blob_name: str) -> None:
    """
    Phase 1: Extract raw layout elements from a PDF.

    S0 Standard tier strategy:
      - PDFs ≤ 1000 pages → single API call (fastest)
      - PDFs > 1000 pages → concurrent 1000-page batches
      - Figures downloaded concurrently within each batch
    """
    db = await get_database()

    # ── Skip if already parsed ──────────────────────────────────────────────
    doc = await db["documents"].find_one({"_id": ObjectId(document_id)})
    if doc and doc.get("parsed_elements"):
        print(f"   ∟ [PARSE] Skipping Azure DI (layout data already exists)", flush=True)
        return

    # ── Download document ────────────────────────────────────────────────────────
    pdf_bytes = await download_blob(blob_name)
    size_mb = len(pdf_bytes) / (1024 * 1024)
    file_type = doc.get("file_type", "application/pdf") if doc else "application/pdf"
    print(f"   ∟ [PARSE] Downloaded {size_mb:.1f} MB document", flush=True)

    # ── Count pages locally with PyMuPDF (PDF only) ────────────────────────
    total_pages = _get_pdf_page_count(pdf_bytes, file_type)
    print(f"[PAGES] [PARSE] Document has {total_pages} pages", flush=True)

    # ── Build Azure DI client ───────────────────────────────────────────────
    client = DocumentIntelligenceClient(
        endpoint=settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
        credential=AzureKeyCredential(settings.AZURE_DOCUMENT_INTELLIGENCE_KEY),
        headers={"x-ms-useragent": "petal-ai/1.0"},
        polling_interval=5,  # check every 5s instead of default
        connection_pool_size=50,
    )

    # ── Generate Azure DI SAS URL ───────────────────────────────────────────
    sas_url = generate_sas_url_with_expiry(blob_name, expiry_hours=2.0)

    di_semaphore = asyncio.Semaphore(MAX_CONCURRENT_DI_CALLS)
    t_start = time.perf_counter()

    if total_pages <= PAGES_PER_BATCH:
        # ── Small/medium PDF: single API call ───────────────────────────────
        print(f"[MODE] [PARSE] Single-call mode ({total_pages} pages <= {PAGES_PER_BATCH} batch limit)", flush=True)

        batch_result = await _analyze_page_range(
            client, sas_url,
            page_range=None,  # None = process entire document
            document_id=document_id,
            batch_label=f"Full document ({total_pages} pages)",
            semaphore=di_semaphore,
        )

        all_paragraphs = batch_result["paragraphs"]
        all_tables = batch_result["tables"]
        all_figures = batch_result["figures"]
        total_parsed_pages = batch_result["pages_parsed"]
        num_batches = 1

    else:
        # ── Large PDF: concurrent batches of PAGES_PER_BATCH ────────────────
        num_batches = (total_pages + PAGES_PER_BATCH - 1) // PAGES_PER_BATCH
        print(f"   ∟ [PARSE] Processing {total_pages} pages in {num_batches} concurrent batches", flush=True)

        batch_tasks = []
        for batch_num in range(num_batches):
            page_start = batch_num * PAGES_PER_BATCH + 1
            page_end = min(page_start + PAGES_PER_BATCH - 1, total_pages)
            page_range = f"{page_start}-{page_end}"
            label = f"Batch {batch_num + 1}/{num_batches} (pages {page_range})"

            batch_tasks.append(
                _analyze_page_range(
                    client, sas_url, page_range,
                    document_id, label, di_semaphore,
                )
            )
        batch_results = await asyncio.gather(*batch_tasks)

        # ── Merge all batch results ─────────────────────────────────────────
        all_paragraphs = []
        all_tables = []
        all_figures = []
        total_parsed_pages = 0
        table_offset = 0
        figure_offset = 0

        for br in batch_results:
            all_paragraphs.extend(br["paragraphs"])

            # Re-index tables and figures to have globally unique indexes
            for t in br["tables"]:
                t["table_index"] += table_offset
                all_tables.append(t)
            table_offset += len(br["tables"])

            for f in br["figures"]:
                f["figure_index"] += figure_offset
                all_figures.append(f)
            figure_offset += len(br["figures"])

            total_parsed_pages += br["pages_parsed"]

    elapsed_total = time.perf_counter() - t_start

    # ── Build combined parsed_elements ───────────────────────────────────────
    print(f"   ∟ [PARSE] Extracted: {len(all_paragraphs)} paragraphs, {len(all_tables)} tables, {len(all_figures)} figures", flush=True)

    parsed_elements = {
        "paragraphs": all_paragraphs,
        "tables": all_tables,
        "figures": all_figures,
        "page_count": total_parsed_pages,
    }

    # ── Extract document metadata (title, author) from early paragraphs ─────
    doc_metadata = _extract_document_metadata(all_paragraphs)

    # ── Generate content fingerprint from first N pages ─────────────────────
    content_fingerprint = _generate_content_fingerprint(all_paragraphs, FINGERPRINT_MAX_PAGES)

    # ── Save to document ────────────────────────────────────────────────────
    update_fields = {
        "parsed_elements": parsed_elements,
        "status": DocumentStatus.PARSED,
        "parse_time_seconds": round(elapsed_total, 1),
        "content_fingerprint": content_fingerprint,
        "updated_at": datetime.now(timezone.utc),
    }
    # Only set metadata fields if extracted (don't overwrite user-provided ones)
    if doc_metadata.get("document_title"):
        update_fields.setdefault("document_title", doc_metadata["document_title"])
    if doc_metadata.get("author"):
        update_fields.setdefault("author", doc_metadata["author"])

    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": update_fields}
    )

    if doc_metadata.get("document_title") or doc_metadata.get("author"):
        print(
            f"   ∟ [META] Title='{doc_metadata.get('document_title', '')}' "
            f"Author='{doc_metadata.get('author', '')}'",
            flush=True,
        )
    print(f"   ∟ [FINGERPRINT] Content fingerprint generated from first {FINGERPRINT_MAX_PAGES} pages", flush=True)

    logger.info(
        f"[SUCCESS] PDF parsed: {len(all_paragraphs)} paragraphs, "
        f"{len(all_tables)} tables, {len(all_figures)} figures "
        f"({total_parsed_pages} pages in {elapsed_total:.1f}s)"
    )


def _extract_document_metadata(paragraphs: list[dict]) -> dict:
    """
    Extract document title and author from the first few paragraphs.

    Strategy:
      Title:
        - First paragraph with role="title"
        - Fallback: first heading-like paragraph

      Author (3-pass detection):
        1. Explicit prefix: "Author:", "By:", "Written by:", "Edited by:"
        2. Credential pattern: names followed by professional degrees
           (DDS, DMD, MD, PhD, BDS, MDS, MS, FRCS, FACS, etc.)
           e.g. "Stephen J. Stefanac, DDS, MS"
        3. Affiliation context: paragraphs near credential-bearing names
           that mention "Professor", "University", "Department", etc.
           are skipped (those are affiliations, not names).
    """
    title_parts: list[str] = []
    authors: list[str] = []
    title_found_at_index = -1
    last_title_index = -1

    # Scan pages 1-6 (some textbooks have inner title page on page 5-6)
    early_paragraphs = [p for p in paragraphs if (p.get("page_number") or 999) <= 6]
    if not early_paragraphs:
        early_paragraphs = paragraphs[:40]

    HEADING_ROLES = {"title", "sectionHeading"}

    # Pass 1: Explicit author prefix (must start at beginning of paragraph)
    # "by" alone is too noisy (matches "by registering", "by purchasing", etc.)
    # so we require "by" to be followed by a capitalized name, not a verb
    AUTHOR_PREFIX_RE = re.compile(
        r"^(?:authors?|written\s+by|edited\s+by|prepared\s+by)[:\s]+(.+)",
        re.IGNORECASE,
    )
    # Separate, stricter pattern for standalone "by" (must be followed by a name, not a verb)
    BY_RE = re.compile(
        r"^by\s+([A-Z][a-z]+(?:\s+[A-Z][a-zA-Z.\-']+){1,4})",
    )

    # Pass 2: Professional credential patterns (medical/dental textbooks)
    # Matches: "Name, DDS, MS" or "Name, BDS, MDS, PhD" etc.
    CREDENTIAL_SUFFIXES = (
        r"(?:DDS|DMD|BDS|MDS|MD|PhD|MS|MSc|MPH|FRCS|FACS|FACD|FICD|"
        r"FRCDC|FADSA|DABOM|DrPH|DO|RDH|BSDH|EdD|MBA|JD|"
        r"FRCD\(C\)|MRCS|FDS|FDSRCS|DrMedDent)"
    )
    # Match: "Firstname [Middle] Lastname, CREDENTIAL" (may have trailing affiliation text)
    CREDENTIAL_RE = re.compile(
        rf"^([A-Z][a-zA-Z.\-']+(?:\s+[A-Z][a-zA-Z.\-']+){{1,4}})\s*,\s*{CREDENTIAL_SUFFIXES}",
    )

    # Affiliation/noise lines to skip (these follow author names but aren't names)
    AFFILIATION_RE = re.compile(
        r"(?:professor|associate|assistant|department|university|school|college|"
        r"faculty|institute|hospital|clinic|director|dean|chair|"
        r"senior|clinical|fellow|resident|adjunct)",
        re.IGNORECASE,
    )

    # Edition pattern (skip these)
    EDITION_RE = re.compile(r"\d+(?:st|nd|rd|th)\s+edition", re.IGNORECASE)
    title_page = None  # Track which page the title started on

    for idx, para in enumerate(early_paragraphs):
        content = (para.get("content") or "").strip()
        role = para.get("role")
        page = para.get("page_number")
        if not content:
            continue

        # ── Title detection ────────────────────────────────────────────────
        # Only merge consecutive title-role paragraphs on the SAME page.
        # Once we see a non-title para after collecting title parts, stop.
        if role == "title":
            if title_found_at_index < 0:
                # First title paragraph — start collecting
                title_parts.append(content)
                title_found_at_index = idx
                last_title_index = idx
                title_page = page
            elif page == title_page and idx == last_title_index + 1:
                # Consecutive title on same page — merge
                title_parts.append(content)
                last_title_index = idx
            # else: title on a later page — ignore (duplicate inner title page)
            continue

        # Once we have title parts and see a non-title, lock the title
        # (don't collect more title parts from later pages)

        # Skip very early paragraphs (before any title is found) for author detection
        if title_found_at_index < 0:
            continue

        # Skip edition lines
        if EDITION_RE.search(content):
            continue

        # Skip affiliation/role description lines
        if AFFILIATION_RE.match(content):
            continue

        # Skip short noise (page numbers, single words)
        if len(content) < 5:
            continue

        # Skip lines that are just locations (City, State)
        if re.match(r"^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?,\s*[A-Z][a-z]+", content) and len(content) < 40:
            continue

        # ── Pass 1: Explicit prefix ────────────────────────────────────────
        if not authors:
            m = AUTHOR_PREFIX_RE.match(content)
            if m:
                authors.append(m.group(1).strip().rstrip("."))
                continue
            m = BY_RE.match(content)
            if m:
                authors.append(m.group(1).strip())
                continue

        # ── Pass 2: Credential pattern ─────────────────────────────────────
        # e.g. "Stephen J. Stefanac, DDS, MS"
        m = CREDENTIAL_RE.match(content)
        if m:
            author_name = m.group(1).strip()
            # Sanity check: ensure it's a real name (2-5 words, not a heading)
            word_count = len(author_name.split())
            if 2 <= word_count <= 5 and role not in HEADING_ROLES:
                authors.append(author_name)
                continue

        # Stop scanning if we've gone too far past the title without finding authors
        if idx > title_found_at_index + 25:
            break

    # ── Build title from collected parts ──────────────────────────────────
    # Merge consecutive title-role paragraphs (e.g. "DIAGNOSIS and" + "TREATMENT PLANNING in Dentistry")
    title = " ".join(title_parts).strip() if title_parts else None

    # Fallback: if no role="title" found, use the first heading
    if not title:
        for para in early_paragraphs:
            content = (para.get("content") or "").strip()
            role = para.get("role")
            if role in HEADING_ROLES and len(content) > 3:
                title = content
                break

    # Combine multiple authors
    author_str = "; ".join(authors) if authors else None

    return {"document_title": title, "author": author_str}


def _generate_content_fingerprint(paragraphs: list[dict], max_pages: int = 4) -> str:
    """
    Generate a SHA-256 fingerprint from the text content of the first N pages.

    This fingerprint is used for fast document-level dedup:
    if two documents have the same fingerprint for pages 1-4, they are
    almost certainly the same (or very similar) document.
    """
    texts = []
    for para in paragraphs:
        page = para.get("page_number")
        if page is not None and page > max_pages:
            continue
        content = (para.get("content") or "").strip()
        if content:
            # Normalize whitespace for stable hashing
            texts.append(" ".join(content.split()).lower())

    combined = "\n".join(texts)
    if not combined:
        return ""
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()
