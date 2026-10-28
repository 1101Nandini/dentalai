from typing import Optional, Literal
from pydantic import BaseModel, EmailStr, Field


# ── Create / Update ─────────────────────────────────────────────────────────

class CreateOrganizationRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=150)
    email: EmailStr = Field(..., description="Primary email for the Org Admin invitation")


class UpdateOrganizationRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=150)
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = None
    auto_delete_sessions: Optional[bool] = Field(None, description="Automatically delete voice sessions after 30 days")
    number_of_locations: Optional[Literal["1 location", "2-5 locations", "6-20 locations", "21+ locations"]] = None

class UpdateOrgMemberRequest(BaseModel):
    role: Optional[str] = Field(None, description="New role: dentist, staff, front_office")
    is_active: Optional[bool] = Field(None, description="true=activate, false=deactivate")

class InviteMemberRequest(BaseModel):
    email: EmailStr = Field(..., description="The exact email address the user must use to register.")
    role: str = Field(..., description="Role to assign upon registration (dentist, staff, front_office)")
