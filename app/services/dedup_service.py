"""
DedupService — duplicate detection for documents.

Design:
  - check_duplicates() accepts ALL file entries in one batch call.
  - Stage 0: Metadata match (document_title + author) for version detection.
  - Stage 1: Exact hash match using a single MongoDB $in query (one round-trip).
  - Stage 2: Content fingerprint match for first-pages similarity (PDFs).
  - Stage 3: Filename soft-match for PDFs with no hash or fingerprint match.
  - uuid from frontend is echoed back but NEVER stored.
"""
import logging
import os
from typing import List, Optional

from app.core.database import get_database
from app.schemas.document_schema import DuplicateCheckItem, DuplicateCheckResult

logger = logging.getLogger(__name__)

# MIME type helpers
PDF_TYPES = {"application/pdf"}
AUDIO_TYPES = {"audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "audio/aac", "audio/*"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}


def _is_pdf(file_type: str) -> bool:
    return file_type.strip().lower() in PDF_TYPES


def _normalize_filename(name: str) -> str:
    """Lowercase, strip spaces, extension, and folder path for soft matching."""
    base = os.path.splitext(os.path.basename(name).strip().lower())[0]
    return " ".join(base.split())  # collapse repeated spaces


def _normalize_title(title: str) -> str:
    """Normalize a document title for comparison: lowercase, strip punctuation, collapse whitespace."""
    import re
    t = title.strip().lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    return " ".join(t.split())


class DedupService:

    @staticmethod
    async def check_duplicates(items: List[DuplicateCheckItem]) -> List[DuplicateCheckResult]:
        """
        Batch duplicate check for N files.

        Stage 0: Metadata match — title + author (version detection)
        Stage 1: One $in query for all file_hash values → exact (hard) matches.
        Stage 2: Content fingerprint match — same first-pages content.
        Stage 3: For PDFs with no hash match → one $in query on original_filename → soft matches.
        Stage 4: Assemble results, preserve original item order.
        """
        if not items:
            return []

        db = await get_database()

        all_hashes = [item.file_hash for item in items]

        # ── Stage 0: Metadata-based version detection ──────────────────────────
        # Check if any item has matching title+author in the existing knowledge base
        metadata_matches: dict[int, dict] = {}  # item_index → matching doc info
        metadata_items = [
            (i, item) for i, item in enumerate(items)
            if item.document_title or item.author
        ]

        if metadata_items:
            for i, item in metadata_items:
                query_filter: dict = {}
                if item.document_title:
                    norm_title = _normalize_title(item.document_title)
                    # Use regex for case-insensitive partial match on title
                    query_filter["document_title"] = {
                        "$regex": f"^{_escape_regex(norm_title)}",
                        "$options": "i",
                    }
                if item.author:
                    query_filter["author"] = {"$regex": item.author.strip(), "$options": "i"}

                if query_filter:
                    existing = await db["documents"].find_one(
                        query_filter,
                        {"_id": 1, "document_title": 1, "document_version": 1, "author": 1}
                    )
                    if existing:
                        metadata_matches[i] = {
                            "doc_id": str(existing["_id"]),
                            "title": existing.get("document_title", ""),
                            "version": existing.get("document_version", 1),
                            "author": existing.get("author", ""),
                        }

        # ── Stage 1: Batch hash lookup ──────────────────────────────────────────
        hash_cursor = db["documents"].find(
            {"file_hash": {"$in": all_hashes}},
            {"_id": 1, "file_hash": 1}
        )
        # Map: file_hash → list of existing mongo ids
        hash_to_ids: dict[str, list[str]] = {}
        async for doc in hash_cursor:
            h = doc["file_hash"]
            hash_to_ids.setdefault(h, []).append(str(doc["_id"]))

        # ── Stage 2: Content fingerprint match for unmatched PDFs ──────────────
        # After parsing, documents get a content_fingerprint (SHA-256 of first 4 pages).
        # This runs post-ingestion, but we can check any provided fingerprints here.
        # (Fingerprints are compared during embedding — see embed_chunks.py)

        # ── Stage 3: PDF filename soft-match for unmatched items ────────────────
        unmatched_pdf_items = [
            item for item in items
            if _is_pdf(item.metadata.get("file_type", ""))
            and item.file_hash not in hash_to_ids
        ]

        filename_to_docs: dict[str, list[dict]] = {}
        if unmatched_pdf_items:
            upload_norms = {_normalize_filename(i.filename): i for i in unmatched_pdf_items}
            # Fetch all existing PDF documents and compare normalized filenames
            fn_cursor = db["documents"].find(
                {"file_type": "application/pdf"},
                {"_id": 1, "original_filename": 1, "document_title": 1, "document_version": 1}
            )
            async for doc in fn_cursor:
                fn = _normalize_filename(doc["original_filename"])
                if fn in upload_norms:
                    filename_to_docs.setdefault(fn, []).append({
                        "id": str(doc["_id"]),
                        "filename": doc.get("original_filename", ""),
                        "title": doc.get("document_title") or doc.get("original_filename", ""),
                        "version": doc.get("document_version", 1)
                    })

        # ── Stage 4: Build results ──────────────────────────────────────────────
        results: List[DuplicateCheckResult] = []
        for i, item in enumerate(items):
            # Hard duplicate — hash match
            if item.file_hash in hash_to_ids:
                results.append(DuplicateCheckResult(
                    uuid=item.uuid,
                    filename=item.filename,
                    is_duplicate=True,
                    duplicate_type="hash_match",
                    duplicate_ids=hash_to_ids[item.file_hash],
                    message=f"Exact duplicate found (hash match). Existing ID(s): {', '.join(hash_to_ids[item.file_hash])}"
                ))
                continue

            # Metadata match — same title+author → flag as new version candidate
            if i in metadata_matches:
                match_info = metadata_matches[i]
                results.append(DuplicateCheckResult(
                    uuid=item.uuid,
                    filename=item.filename,
                    is_duplicate=False,
                    duplicate_type="metadata_match",
                    duplicate_ids=[match_info["doc_id"]],
                    message=(
                        f"Possible new version: document with matching title/author already exists "
                        f"('{match_info['title']}' V{match_info['version']}). "
                        f"Upload will create V{match_info['version'] + 1}."
                    ),
                    is_new_version=True,
                    existing_version=match_info["version"],
                    existing_title=match_info["title"],
                ))
                continue

            # Soft duplicate — filename match (PDF only)
            if _is_pdf(item.metadata.get("file_type", "")):
                norm = _normalize_filename(item.filename)
                if norm in filename_to_docs:
                    matched_docs = filename_to_docs[norm]
                    # Sort to get the highest version as our primary reference
                    matched_docs.sort(key=lambda x: x["version"], reverse=True)
                    ref = matched_docs[0]

                    results.append(DuplicateCheckResult(
                        uuid=item.uuid,
                        filename=item.filename,
                        is_duplicate=False,
                        duplicate_type="filename_match",
                        duplicate_ids=[d["id"] for d in matched_docs],
                        message=f"Possible new version: existing document found with name '{ref['title']}' (V{ref['version']}).",
                        is_new_version=True,
                        existing_version=ref["version"],
                        existing_title=ref["title"],
                    ))
                    continue

            # No duplicate
            results.append(DuplicateCheckResult(
                uuid=item.uuid,
                filename=item.filename,
                is_duplicate=False,
                duplicate_type=None,
                duplicate_ids=[],
                message="No duplicate found. File can be uploaded."
            ))

        return results


def _escape_regex(text: str) -> str:
    """Escape special regex characters in a string for safe use in MongoDB $regex."""
    import re
    return re.sub(r'([.^$*+?{}\\|()\[\]])', r'\\\1', text)
