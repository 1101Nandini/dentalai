"""
Pydantic schemas for the Case Study API.

Covers:
  - Case study creation (super_admin only)
  - Case study update
"""
from pydantic import BaseModel, Field
from typing import Optional


class CaseStudyCreateRequest(BaseModel):
    """Request body for creating a new case study entry."""
    case_id: str = Field(
        ...,
        description="Unique case identifier (e.g. '008', 'CS-101')",
        min_length=1,
    )
    title: str = Field(
        ...,
        description="Short descriptive title of the clinical case",
        min_length=1,
    )
    content: str = Field(
        ...,
        description="Full case study content including presentation, assessment, management, etc.",
        min_length=10,
    )


class CaseStudyUpdateRequest(BaseModel):
    """Request body for updating an existing case study."""
    title: Optional[str] = Field(None, description="Updated title")
    content: Optional[str] = Field(None, description="Updated content")
