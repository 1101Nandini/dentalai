from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Existing response models
# ---------------------------------------------------------------------------

class DocumentResponse(BaseModel):
    id: str
    original_filename: str
    blob_name: str
    blob_url: str
    file_hash: Optional[str] = None
    file_type: Optional[str] = None
    status: str
    is_active: bool = True
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

    class Config:
        from_attributes = True


class DocumentUploadResponse(BaseModel):
    message: str
    documents: List[Dict[str, Any]]


class DocumentStatusResponse(BaseModel):
    document_id: str
    status: str
    chunk_count: int
    error_message: Optional[str] = None


class QueryRequest(BaseModel):
    query: str
    session_id: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    sources: List[dict] = []
    query: str
    session_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Deduplication models
# ---------------------------------------------------------------------------

class DuplicateCheckItem(BaseModel):
    """One file entry in a batch duplicate-check request.

    uuid is a frontend-generated session correlation ID.
    It is echoed back in the response so the frontend can match results
    to files — it is NEVER stored in the database.
    """
    uuid: str
    filename: str
    file_hash: str
    metadata: Dict[str, Any] = Field(default_factory=dict)
    # metadata includes at minimum: file_type
    # For XLSX-backed images it also carries: Image Name, Description, Symptoms, Treatment

    # ── Versioning metadata (optional — for metadata-based version detection) ──
    document_title: Optional[str] = None    # title of the document
    author: Optional[str] = None            # author / issuing authority


class DuplicateCheckResult(BaseModel):
    """Per-file result returned by the check-duplicate endpoint."""
    uuid: str                          # echoed back — frontend correlation only
    filename: str
    is_duplicate: bool
    duplicate_type: Optional[str] = None  # "hash_match" | "filename_match" | "metadata_match" | None
    duplicate_ids: List[str] = []         # MongoDB _id strings of matching existing docs
    message: str

    # ── Versioning fields ──────────────────────────────────────────────────────
    is_new_version: bool = False           # True if this looks like a newer version
    existing_version: Optional[int] = None # version number of the matched existing doc
    existing_title: Optional[str] = None   # title of the matched existing doc
