from typing import Optional
from pydantic import BaseModel, EmailStr, Field


class AcceptInvitationRequest(BaseModel):
    """Org Admin accepts the invitation sent by Super Admin and sets up their account."""
    token: str = Field(..., description="Invitation token from the email link")
    full_name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr = Field(..., description="Must match the org email set by Super Admin")
    password: str = Field(..., min_length=8, description="Minimum 8 characters")
