"""
Pydantic schemas for the Dental Voice Assistant API.

Covers:
  - CDT Code Lookup (Scenario 1)
  - SOAP Note + Patient Summary Generation (Scenario 2)
  - CDT Code listing
"""
from pydantic import BaseModel, Field
from typing import Optional, List


# ── Scenario 1: CDT Code Lookup ──────────────────────────────────────────────

class CDTLookupRequest(BaseModel):
    """Request body for CDT code lookup from voice transcript."""
    transcript: str = Field(
        ...,
        description="Voice transcript containing dental shorthand, e.g. '#2 MO, #3 MOD, #4 DO'",
        min_length=1,
    )


class CDTCodeMatch(BaseModel):
    """A single CDT code match result."""
    tooth: int = Field(..., description="Tooth number (1-32)")
    surfaces: str = Field(..., description="Surface abbreviation, e.g. MO, MOD, DO")
    surface_count: int = Field(..., description="Number of surfaces involved")
    cdt_code: Optional[str] = Field(None, description="Matched CDT code, e.g. D2392")
    description: Optional[str] = Field(None, description="CDT code description")
    confidence: float = Field(0.0, description="Match confidence score (0-1)")
    raw_input: str = Field("", description="Original parsed input fragment")


class CDTLookupResponse(BaseModel):
    """Response body for CDT code lookup."""
    results: List[CDTCodeMatch]
    transcript: str
    codes_found: int


# ── Scenario 2: SOAP Note + Patient Summary ──────────────────────────────────

class GenerateNotesRequest(BaseModel):
    """Request body for generating multiple Ghostwriter notes from a session/report."""
    report_id: str = Field(
        ...,
        description="The ID of the session/report.",
        min_length=24
    )
    template_ids: List[str] = Field(
        ...,
        description="List of template IDs to generate notes for.",
        min_items=1
    )
    xray_selection: Optional[List[str]] = Field(
        None,
        description="List of x-ray type keys (e.g. ['4_bitewings', '1_periapical']) or null if none selected",
    )


class SOAPNote(BaseModel):
    """Structured SOAP note output."""
    subjective: str = Field("", description="S — What the patient reports")
    objective: str = Field("", description="O — Clinical findings")
    assessment: str = Field("", description="A — Diagnosis/interpretation")
    plan: str = Field("", description="P — Treatment done + next steps")


class GenerateNotesResponse(BaseModel):
    """Response body with both SOAP note and patient summary."""
    soap_note: SOAPNote
    patient_summary: str = Field("", description="Patient-friendly visit summary")
    entities_extracted: Optional[dict] = Field(
        None,
        description="Structured clinical entities extracted from transcript",
    )
    ghostwriter_note: Optional[str] = Field(None, description="Generated Ghostwriter narrative note")
    completed_today: Optional[List[dict]] = Field(None, description="Procedures completed today")
    planned_future: Optional[List[dict]] = Field(None, description="Procedures planned for future")


# ── CDT Code Listing ────────────────────────────────────────────────────────

class CDTCodeEntry(BaseModel):
    """A CDT code entry from the database."""
    code: str
    description: str
    category: str = ""
    is_active: bool = True


class GeneratedReportSchema(BaseModel):
    """Represents a generated report inside a session."""
    template_id: str
    template_name: str
    report: str


# ── Session Editing & Staff Notes ────────────────────────────────────────────

class SessionEditRequest(BaseModel):
    """Request body for editing a session's SOAP note, patient summary, or staff note."""
    soap_note: Optional[str] = Field(None, description="Updated SOAP note markdown content")
    patient_summary: Optional[str] = Field(None, description="Updated patient summary content")
    staff_note: Optional[str] = Field(None, description="Updated staff note content")
    ghostwriter_note: Optional[str] = Field(None, description="Updated Ghostwriter note content")
    completed_today: Optional[List[dict]] = Field(None, description="Updated completed procedures")
    planned_future: Optional[List[dict]] = Field(None, description="Updated planned procedures")
    is_verified: Optional[bool] = Field(None, description="Verification status of the session")
    is_billed: Optional[bool] = Field(None, description="Billing status of the session")
    reports: Optional[List[GeneratedReportSchema]] = Field(None, description="Updated list of template reports")
    xray_selection: Optional[List[str]] = Field(None, description="List of x-ray type keys (e.g. ['4_bitewings', '1_periapical']) or null")



