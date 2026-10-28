"""
Pydantic schemas for the Note Templates system.
"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


class TemplateCreateRequest(BaseModel):
    procedure_name: str
    cdt_refs: List[str] = []
    video_url: Optional[str] = None
    body_template: str


class TemplateUpdateRequest(BaseModel):
    procedure_name: Optional[str] = None
    cdt_refs: Optional[List[str]] = None
    video_url: Optional[str] = None
    is_active: Optional[bool] = None
    body_template: Optional[str] = None


class TemplateListResponse(BaseModel):
    id: str
    organization_id: Optional[str] = None
    source_template_id: Optional[str] = None
    is_custom: bool
    is_default: bool
    is_active: bool
    procedure_name: str
    video_url: Optional[str] = None


class TemplateDetailResponse(TemplateListResponse):
    cdt_refs: List[str]
    body_template: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class OrgVariableCreate(BaseModel):
    name: str
    value: str

class OrgVariableUpdate(BaseModel):
    value: str

class OrgVariableResponse(BaseModel):
    id: str
    organization_id: str
    name: str
    value: str
    updated_at: Optional[str] = None


class TemplateEnhanceRequest(BaseModel):
    raw_text: str
    procedure_name: Optional[str] = None


class TemplateEnhanceResponse(BaseModel):
    procedure_name: str
    body_template: str
    cdt_refs: List[str]


class GhostwriterNoteResponse(BaseModel):
    ghostwriter_note: str
    completed_today: List[dict] = []
    planned_future: List[dict] = []
    patient_summary: Optional[str] = None
