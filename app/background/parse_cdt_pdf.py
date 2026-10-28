"""
CDT Code PDF Parser — Extract CDT Code + Description of Service from tables.

Specialized parser for the CDT Procedure Guidelines PDF:
  - 4-column tables: CDT Code | Description of Service | Procedure Guidelines | Submission Requirements
  - We extract ONLY columns 0 (CDT Code) and 1 (Description of Service)
  - Section headers appear as full-width orange rows (e.g., "Diagnostic Services")
  - CDT codes follow pattern: D followed by 4 digits (D0120, D0140, etc.)

Delta detection:
  - On re-upload, compares extracted codes against existing DB entries
  - Only inserts new codes / updates changed descriptions
  - Marks codes removed from the new PDF as is_active=False
"""
import asyncio
import logging
import re
import time
from datetime import datetime, timezone

from bson import ObjectId

from app.config import settings
from app.core.database import get_database
from app.core.constants import DocumentStatus, COLLECTION_CDT_DOCUMENTS
from app.utils.blob_utils import download_blob
from app.utils.file_utils import generate_sas_url_with_expiry

logger = logging.getLogger(__name__)

# Regex to match CDT code format: D followed by 4 digits
# Strict match for exact cell content
CDT_CODE_RE = re.compile(r"^D\d{4}$")
# Flexible search for CDT code anywhere in cell text (handles newlines, extra chars)
CDT_CODE_SEARCH_RE = re.compile(r"(D\d{4})")


def _extract_cdt_from_tables(tables: list[dict]) -> list[dict]:
    """
    Extract CDT Code + Description pairs from parsed table data.

    Strategy (cell-centric, NOT grid-based):
      1. Group cells by row_index
      2. For each row, find the cell containing a CDT code and the adjacent description cell
      3. Rows without CDT codes are treated as category/section headers
      4. Return list of {code, description, category}

    This approach is robust against merged cells, split tables, and shifted columns
    that Azure Document Intelligence produces for complex PDF tables.
    """
    codes = []
    seen_codes: set[str] = set()  # Dedup across tables
    current_category = "General"
    last_code_ref = None

    for table in tables:
        cells = table.get("cells", [])
        if not cells:
            continue

        row_count = table.get("row_count", 0)
        col_count = table.get("column_count", 0)

        # ── Step 1: Determine column layout ──────────────────────────────
        # Find which column has "CDT Code" and which has "Description"
        cdt_col = None
        desc_col = None
        header_rows: set[int] = set()

        for cell in cells:
            r = cell.get("row_index", 0)
            kind = cell.get("kind", "")
            c = cell.get("column_index", 0)
            content = (cell.get("content") or "").strip().lower()

            if kind == "columnHeader":
                header_rows.add(r)

            if r <= 2:  # Only check first 3 rows for headers
                if "cdt code" in content or "cdt\ncode" in content:
                    cdt_col = c
                    header_rows.add(r)
                if "description" in content and "service" in content:
                    desc_col = c
                    header_rows.add(r)
                if desc_col is None and "description" in content:
                    desc_col = c

        # Fallback defaults
        if cdt_col is None:
            cdt_col = 0
        if desc_col is None:
            desc_col = 1

        # ── Step 2: Group cells by row ───────────────────────────────────
        rows_by_index: dict[int, dict[int, str]] = {}
        for cell in cells:
            r = cell.get("row_index", 0)
            c = cell.get("column_index", 0)
            content = (cell.get("content") or "").strip()
            if r not in rows_by_index:
                rows_by_index[r] = {}
            rows_by_index[r][c] = content

        logger.info(
            f"[CDT-PARSE] Table: {row_count} rows x {col_count} cols, "
            f"header_rows={header_rows}, cdt_col={cdt_col}, desc_col={desc_col}, "
            f"actual_rows_with_cells={sorted(rows_by_index.keys())}"
        )

        # ── Step 3: Process each row ─────────────────────────────────────
        for r in sorted(rows_by_index.keys()):
            # Skip header rows
            if r in header_rows:
                continue

            row_cells = rows_by_index[r]

            # Get CDT code from the expected column
            raw_code = row_cells.get(cdt_col, "")
            desc_text = row_cells.get(desc_col, "").strip()

            # Clean code cell
            code_clean = re.sub(r"[\s\u00a0]+", "", raw_code).strip().upper()

            # Try to match CDT code
            code_match = None
            if CDT_CODE_RE.match(code_clean):
                code_match = code_clean
            else:
                # Flexible: find D#### anywhere in the cell
                m = CDT_CODE_SEARCH_RE.search(raw_code)
                if m:
                    code_match = m.group(1).upper()

            # If no code in expected column, scan ALL cells in this row
            if not code_match:
                for c_idx, cell_content in sorted(row_cells.items()):
                    m = CDT_CODE_SEARCH_RE.search(cell_content)
                    if m:
                        code_match = m.group(1).upper()
                        # Description is likely the next column
                        desc_text = row_cells.get(c_idx + 1, desc_text).strip()
                        break

            # Log key codes for debugging
            if code_match and code_match in ("D0120", "D1110", "D1120"):
                cell_summary = {k: v[:40] for k, v in row_cells.items()}
                logger.info(
                    f"[CDT-PARSE] KEY CODE {code_match} at row {r}: "
                    f"cells={cell_summary}"
                )

            if code_match and desc_text and code_match not in seen_codes:
                # For table fallback (especially for non-bold blue-ink additions), split by newline.
                # Stop appending to title if we hit a line starting with an Uppercase word (descriptor).
                lines = [line.strip() for line in desc_text.split('\n') if line.strip()]
                title_parts = []
                if lines:
                    title_parts.append(lines[0])
                    for line in lines[1:]:
                        if line[0].isupper() and (len(line) == 1 or line[1].islower() or line[1] == ' '):
                            break
                        title_parts.append(line)
                        
                desc_clean = " ".join(title_parts)
                desc_clean = " ".join(desc_clean.split())
                
                # Ensure the description is not just a page number (e.g., from a Table of Contents)
                if len(desc_clean) > 3 and re.search(r"[a-zA-Z]{3,}", desc_clean):
                    new_entry = {
                        "code": code_match,
                        "description": desc_clean,
                        "category": current_category,
                    }
                    codes.append(new_entry)
                    seen_codes.add(code_match)
                    logger.debug(f"[CDT-PARSE]   {code_match} = {desc_clean[:50]}")
                else:
                    logger.debug(f"[CDT-PARSE] Skipping suspicious entry {code_match} with desc: '{desc_clean}'")
            elif not code_match:
                full_row_text = " ".join(
                    v for _, v in sorted(row_cells.items())
                ).strip()

                if full_row_text and len(full_row_text) > 3:
                    # Don't use text that's clearly a header cell repeat
                    if not any(kw in full_row_text.lower() for kw in
                               ["cdt code", "description of service",
                                "procedure guidelines", "submission requirements"]):
                        current_category = _clean_category(full_row_text)

    logger.info(f"[CDT-PARSE] Total extracted: {len(codes)} codes")
    return codes


def _clean_category(raw: str) -> str:
    """Clean and normalize a category name from section headers."""
    # Remove common prefixes/noise
    cleaned = raw.strip()
    # Truncate overly long category strings (some span full row text)
    if len(cleaned) > 100:
        # Try to get just the first meaningful phrase
        parts = cleaned.split(".")
        cleaned = parts[0].strip()
    if len(cleaned) > 100:
        cleaned = cleaned[:100]
    # Title case for consistency
    return cleaned.title()


def _extract_cdt_from_markdown(markdown_content: str, existing_codes: set[str]) -> list[dict]:
    if not markdown_content:
        return []

    recovered = []
    lines = markdown_content.split('\n')
    
    for i, line in enumerate(lines):
        m = re.search(r"\b(D\d{4})\b", line)
        if m:
            code = m.group(1).upper()
            if code in existing_codes:
                continue
                
            title_parts = []
            bolds = re.findall(r"\*\*(.*?)\*\*", line)
            
            if bolds:
                for b in bolds:
                    if b.strip() and b.strip().upper() != code:
                        title_parts.append(b.strip())
                        
                j = i + 1
                while j < len(lines):
                    next_line = lines[j].strip()
                    if not next_line:
                        j += 1
                        continue 
                        
                    clean_line = re.sub(r"[\s\|]+", "", next_line)
                    if clean_line.startswith("**") and clean_line.endswith("**"):
                        next_bolds = re.findall(r"\*\*(.*?)\*\*", next_line)
                        title_parts.extend([b.strip() for b in next_bolds if b.strip()])
                        j += 1
                    else:
                        break
            # If bolds was false, or if it was true but only the code was bolded (title_parts is empty)
            if not title_parts:
                # If this line is part of a table (HTML or markdown pipes) and lacks bolding,
                # skip it here. The Table parser is much better equipped to handle non-bolded
                # tables because it preserves structural newlines!
                if "<td>" in line or "<tr>" in line or "<th>" in line or "|" in line:
                    continue
                    
                post_code_text = line[m.end():]
                desc = post_code_text.replace("|", "").replace("**", "").replace("*", "").strip()
                desc = re.sub(r"^[#\s]+", "", desc)
                if desc:
                    title_parts.append(desc)
                    
                j = i + 1
                while j < len(lines):
                    if "<td>" in lines[j] or "<tr>" in lines[j] or "<th>" in lines[j] or "|" in lines[j]:
                        title_parts = []
                        break
                        
                    next_line = lines[j].replace("|", "").replace("**", "").replace("*", "").strip()
                    next_line = re.sub(r"^[#\s]+", "", next_line)
                    if not next_line:
                        j += 1
                        continue
                        
                    if next_line[0].isupper() and (len(next_line) == 1 or next_line[1].islower() or next_line[1] == ' '):
                        break
                        
                    if re.search(r"\b(D\d{4})\b", next_line):
                        break
                        
                    title_parts.append(next_line)
                    j += 1
                    
            if title_parts:
                title = " ".join(title_parts)
                title = " ".join(title.split())
                
                # Clean up stray HTML comments and tags that Azure DI occasionally leaks into Markdown
                title = re.sub(r"<[^>]+>", "", title)
                title = title.replace("-->", "").replace("<!--", "").strip()
                if title.startswith('"'):
                    title = title[1:].strip()
                if title.endswith('"'):
                    title = title[:-1].strip()
                    
                if len(title) > 3 and re.search(r"[a-zA-Z]{3,}", title):
                    recovered.append({
                        "code": code,
                        "description": title,
                        "category": "General",
                    })
                    existing_codes.add(code)
                    logger.info(f"[CDT-PARSE] EXTRACTED from markdown: {code} = {title[:50]}")

    return recovered


async def parse_cdt_pdf(document_id: str, blob_name: str) -> None:
    """
    Parse a CDT code PDF and extract CDT Code + Description of Service.

    Pipeline:
      1. Download PDF from blob
      2. Submit to Azure Document Intelligence (prebuilt-layout)
      3. Extract CDT codes from table structures
      4. Delta-compare with existing codes in MongoDB
      5. Insert new / update changed / deactivate removed codes
    """
    from azure.ai.documentintelligence import DocumentIntelligenceClient
    from azure.ai.documentintelligence.models import (
        DocumentContentFormat,
    )
    from azure.core.credentials import AzureKeyCredential

    db = await get_database()

    print(f"   ∟ [CDT] Starting CDT PDF parsing for doc {document_id[:8]}", flush=True)

    # ── 1. Generate SAS URL for Azure DI ─────────────────────────────────
    sas_url = generate_sas_url_with_expiry(blob_name, expiry_hours=2.0)

    # ── 2. Submit to Azure Document Intelligence ─────────────────────────
    client = DocumentIntelligenceClient(
        endpoint=settings.AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT,
        credential=AzureKeyCredential(settings.AZURE_DOCUMENT_INTELLIGENCE_KEY),
        headers={"x-ms-useragent": "petal-ai/1.0"},
        polling_interval=5,
    )

    t_start = time.perf_counter()
    print(f"   ∟ [CDT] Submitting 105-page PDF to Azure Document Intelligence...", flush=True)

    loop = asyncio.get_event_loop()

    def _run_analysis():
        poller = client.begin_analyze_document(
            model_id="prebuilt-layout",
            body={"urlSource": sas_url},
            output_content_format=DocumentContentFormat.MARKDOWN,
        )
        # 105-page PDF may take 3-5 minutes on S0
        return poller.result(timeout=600)

    result = await loop.run_in_executor(None, _run_analysis)

    parse_elapsed = time.perf_counter() - t_start
    table_count = len(result.tables or [])
    print(
        f"   ∟ [CDT] Azure DI complete: {table_count} tables found ({parse_elapsed:.1f}s)",
        flush=True,
    )

    # ── 3. Serialize tables ──────────────────────────────────────────────
    tables = []
    for t_idx, table in enumerate(result.tables or []):
        cells = []
        for cell in table.cells:
            cells.append({
                "row_index": cell.row_index,
                "column_index": cell.column_index,
                "row_span": getattr(cell, "row_span", 1) or 1,
                "column_span": getattr(cell, "column_span", 1) or 1,
                "content": cell.content,
                "kind": cell.kind,
            })
        tables.append({
            "table_index": t_idx,
            "row_count": table.row_count,
            "column_count": table.column_count,
            "page_number": table.bounding_regions[0].page_number if table.bounding_regions else None,
            "cells": cells,
        })

    # ── 4. Extract CDT codes ─────────────────────────────────────────────
    extracted_codes = []
    seen_codes = set()
    
    # 1. First run the robust Markdown parser
    markdown_content = getattr(result, 'content', "")
    if markdown_content:
        markdown_codes = _extract_cdt_from_markdown(markdown_content, seen_codes)
        if markdown_codes:
            extracted_codes.extend(markdown_codes)
            logger.info(f"[CDT-PARSE] Extracted {len(markdown_codes)} codes from Markdown (primary)")

    # 2. Then run the Table parser as a fallback for any codes missed
    if result.tables:
        table_codes = _extract_cdt_from_tables(tables)
        fallback_count = 0
        for entry in table_codes:
            if entry["code"] not in seen_codes:
                extracted_codes.append(entry)
                seen_codes.add(entry["code"])
                fallback_count += 1
        if fallback_count:
            logger.info(f"[CDT-PARSE] Extracted {fallback_count} additional codes from Tables (fallback)")

    if not extracted_codes:
        logger.warning(f"No CDT codes found in PDF {document_id}")
        await db[COLLECTION_CDT_DOCUMENTS].update_one(
            {"_id": ObjectId(document_id)},
            {"$set": {
                "status": DocumentStatus.COMPLETED,
                "error_message": "No CDT codes found in PDF tables",
                "updated_at": datetime.now(timezone.utc),
            }}
        )
        return

    # ── 5. Delta detection — compare with existing codes ─────────────────
    existing_codes = {}
    async for doc in db["cdt_codes"].find({}, {"code": 1, "description": 1, "category": 1}):
        existing_codes[doc["code"]] = {
            "description": doc.get("description", ""),
            "category": doc.get("category", ""),
        }

    new_codes = []
    updated_codes = []
    unchanged_count = 0

    extracted_code_set = set()
    for entry in extracted_codes:
        code = entry["code"]
        extracted_code_set.add(code)

        if code not in existing_codes:
            new_codes.append(entry)
        elif (
            existing_codes[code]["description"] != entry["description"]
            or existing_codes[code]["category"] != entry["category"]
        ):
            updated_codes.append(entry)
        else:
            unchanged_count += 1

    # Codes that were in DB but not in new PDF → deactivate
    removed_codes = set(existing_codes.keys()) - extracted_code_set

    print(
        f"   ∟ [CDT] Delta: {len(new_codes)} new, {len(updated_codes)} updated, "
        f"{unchanged_count} unchanged, {len(removed_codes)} removed",
        flush=True,
    )

    # ── 6. Apply changes to MongoDB ──────────────────────────────────────
    now = datetime.now(timezone.utc)
    from pymongo import UpdateOne

    bulk_ops = []

    for entry in new_codes:
        bulk_ops.append(UpdateOne(
            {"code": entry["code"]},
            {"$set": {
                "code": entry["code"],
                "description": entry["description"],
                "category": entry["category"],
                "source_document_id": document_id,
                "is_active": True,
                "is_embedded": False,
                "created_at": now,
                "updated_at": now,
            }},
            upsert=True,
        ))

    for entry in updated_codes:
        bulk_ops.append(UpdateOne(
            {"code": entry["code"]},
            {"$set": {
                "description": entry["description"],
                "category": entry["category"],
                "source_document_id": document_id,
                "is_active": True,
                "is_embedded": False,  # Needs re-embedding
                "updated_at": now,
            }},
        ))

    if removed_codes:
        bulk_ops.append(UpdateOne(
            {"code": {"$in": list(removed_codes)}},
            {"$set": {"is_active": False, "updated_at": now}},
        ))

    if bulk_ops:
        result = await db["cdt_codes"].bulk_write(bulk_ops)
        print(
            f"   ∟ [CDT] MongoDB: {result.upserted_count} inserted, "
            f"{result.modified_count} modified",
            flush=True,
        )

    # ── 7. Update document status ────────────────────────────────────────
    await db[COLLECTION_CDT_DOCUMENTS].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {
            "status": DocumentStatus.PARSED,
            "cdt_code_count": len(extracted_codes),
            "cdt_new_count": len(new_codes),
            "cdt_updated_count": len(updated_codes),
            "parse_time_seconds": round(parse_elapsed, 1),
            "updated_at": now,
        }}
    )

    logger.info(
        f"[CDT] Parsed {len(extracted_codes)} codes from doc {document_id[:8]} "
        f"({len(new_codes)} new, {len(updated_codes)} updated)"
    )
