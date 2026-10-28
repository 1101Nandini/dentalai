from datetime import datetime
from typing import Optional, Literal
from pydantic import BaseModel, EmailStr, Field


# ── Register ────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8, description="Minimum 8 characters")
    # OTP no longer required at registration — sent automatically after account creation

class AccountRequestSchema(BaseModel):
    account_type: str = Field(..., description="'individual' or 'organization'")
    full_name: str = Field(..., min_length=2, max_length=100)
    email: EmailStr
    password: str = Field(..., min_length=8)
    role: Optional[str] = Field(None, description="Optional role from the dropdown")
    organization_name: Optional[str] = Field(None, description="Required if account_type is 'organization'")
    number_of_locations: Optional[Literal["1", "2-5", "6-20", "21+"]] = Field(None, description="Required if account_type is 'organization'")


class RegisterResponse(BaseModel):
    status: str = "success"
    email: EmailStr


# ── Verify Email ─────────────────────────────────────────────────────────────

class VerifyEmailRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6, description="6-digit OTP sent to email after registration")


# ── Resend OTP ───────────────────────────────────────────────────────────────

class ResendOTPRequest(BaseModel):
    email: EmailStr


# ── Login ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str
    recaptcha_token: str


class LoginResponse(BaseModel):
    """User profile returned in the login response body."""
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    is_verified: bool
    auth_provider: str
    subscription_plan: str
    created_at: datetime


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


# ── OTP (Forgot Password only) ───────────────────────────────────────────────

class SendOTPRequest(BaseModel):
    email: EmailStr
    purpose: str = Field(
        ...,
        description="Purpose of OTP: 'forgot_password'"
    )


# ── Forgot Password ──────────────────────────────────────────────────────────

class ForgotPasswordRequest(BaseModel):
    email: EmailStr
    otp: str = Field(..., min_length=6, max_length=6)
    new_password: str = Field(..., min_length=8)


# ── Refresh / Logout ─────────────────────────────────────────────────────────

class RefreshTokenRequest(BaseModel):
    refresh_token: str

class LogoutRequest(BaseModel):
    refresh_token: Optional[str] = Field(None, description="The refresh token to revoke (logs out of current device only)")
    all_devices: bool = Field(False, description="If true, revokes all refresh tokens (logs out of all devices)")


# ── Google OAuth ─────────────────────────────────────────────────────────────

class GoogleAuthRequest(BaseModel):
    id_token: str
    invitation_token: Optional[str] = None
