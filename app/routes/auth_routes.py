from fastapi import APIRouter, Depends, HTTPException, Request, status, Query
from fastapi.responses import JSONResponse

from app.config import settings
from app.core.response import api_response
from app.core.security import *
from app.schemas.auth_schema import (
    RegisterRequest,
    AccountRequestSchema,
    LoginRequest,
    SendOTPRequest,
    ForgotPasswordRequest,
    RefreshTokenRequest,
    GoogleAuthRequest,
    VerifyEmailRequest,
    ResendOTPRequest,
    LogoutRequest,
)
from app.schemas.invitation_schema import AcceptInvitationRequest
from app.services.auth_service import AuthService
from app.services.invitation_service import InvitationService
from app.dependencies import get_current_user
from app.core.recaptcha import verify_recaptcha
from app.middleware.rate_limit_middleware import RateLimiter
router = APIRouter()


# ── Send OTP ─────────────────────────────────────────────────────────────────
@router.post("/send-otp", status_code=status.HTTP_200_OK, dependencies=[Depends(RateLimiter(limit=5, window=60, group="send_email"))])
async def send_otp(payload: SendOTPRequest):
    """
    Send a 6-digit OTP to the user's email.

    - **purpose = 'register'** → validates email is not already registered
    - **purpose = 'forgot_password'** → validates email exists in the system
    """
    data = await AuthService.send_otp(payload)
    return api_response(
        success=True,
        message=data["message"],
        data={"email": data["email"]},
    )


# ── Register ──────────────────────────────────────────────────────────────────
@router.post("/register")
async def register(payload: RegisterRequest):
    """
    Direct registration is disabled. Users must use an invitation link.
    """
    raise HTTPException(
        status_code=403,
        detail="You are not linked with any organization. Registration requires an invitation link."
    )


# ── Request Account ───────────────────────────────────────────────────────────
@router.post("/request-account", status_code=status.HTTP_201_CREATED)
async def request_account(payload: AccountRequestSchema):
    """
    Handle individual (.edu) or organization account requests.
    Creates user (and org if applicable) with is_active=False and is_verified=False.
    Sends OTP to email.
    """
    data = await AuthService.request_account(payload)
    return api_response(
        success=True,
        message=data["message"],
        data={"email": data["email"]},
        status_code=201,
    )


# ── Verify Email ──────────────────────────────────────────────────────
@router.post("/verify-email", status_code=status.HTTP_200_OK)
async def verify_email(payload: VerifyEmailRequest):
    """
    Verify email using the OTP sent after registration.
    Sets `is_verified=True` so the user can log in.
    """
    data = await AuthService.verify_email(payload)

    if data.get("_error_code") == "approval_pending":
        return api_response(
            success=True,
            message=data["message"],
            data=data,
            status_code=200,
        )

    return api_response(
        success=True,
        message=data["message"],
        data={
            "email": data["email"],
            "full_name": data["full_name"],
            "role": data["role"],
            "is_active": data["is_active"],
            "is_verified": data["is_verified"],
            "auth_provider": data["auth_provider"],
            "subscription_plan": data["subscription_plan"],
            "created_at": data["created_at"],
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "token_type": data["token_type"],
        },
        status_code=200,
    )


# ── Resend Verification OTP ──────────────────────────────────────────────
@router.post("/resend-otp", status_code=status.HTTP_200_OK)
async def resend_otp(payload: ResendOTPRequest):
    """
    Resend the email verification OTP to an unverified account.
    Use this if the original OTP expired or was not received.
    """
    data = await AuthService.resend_verification_otp(payload)
    return api_response(success=True, message=data["message"], data={"email": data["email"]})


# ── Login ─────────────────────────────────────────────────────────────────────
@router.post("/login", status_code=status.HTTP_200_OK)
async def login(payload: LoginRequest):
    # 1. Verify reCAPTCHA first
    await verify_recaptcha(payload.recaptcha_token)

    # 2. Proceed with normal login logic
    data = await AuthService.login(payload)

    # Unverified account — return 403 with user data so front-end can redirect
    if data.get("_error_code") == "verification_pending":
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "message": data["message"],
                "data": data,
            },
        )

    # Student account pending super admin approval
    if data.get("_error_code") == "approval_pending":
        return JSONResponse(
            status_code=403,
            content={
                "success": False,
                "message": data["message"],
                "data": data,
            },
        )

    return api_response(
        success=True,
        message="Login successful.",
        data=data,
    )


# ── Forgot Password ───────────────────────────────────────────────────────────
@router.post("/forgot-password", status_code=status.HTTP_200_OK)
async def forgot_password(payload: ForgotPasswordRequest):
    """
    Reset password using an OTP.

    **Flow:**
    1. Call `/send-otp` with `purpose='forgot_password'`
    2. Submit `email`, `otp`, and `new_password`
    """
    data = await AuthService.forgot_password(payload)
    return api_response(
        success=True,
        message=data["message"],
        data=None,
    )


# ── Refresh Token ─────────────────────────────────────────────────────────────
@router.post("/refresh", status_code=status.HTTP_200_OK)
async def refresh_token(payload: RefreshTokenRequest):
    """
    Issue a new access + refresh token pair using the refresh_token from request body.

    - Reads the refresh token from the request body
    - Rotates both tokens and returns them in the response
    """
    data = await AuthService.refresh_token(payload.refresh_token)

    return api_response(
        success=True,
        message="Token refreshed successfully.",
        data={
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "token_type": "bearer",
        },
    )


# ── Logout ────────────────────────────────────────────────────────────────────
@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(payload: LogoutRequest = None, current_user=Depends(get_current_user)):
    """
    Log out the current user.

    - Reads identity from the Authorization Bearer token
    - No cookies to clear since tokens are in request headers
    """
    refresh_token = payload.refresh_token if payload else None
    all_devices = payload.all_devices if payload else False
    
    await AuthService.logout(str(current_user["_id"]), refresh_token, all_devices)

    return api_response(
        success=True,
        message="Logged out successfully.",
        data=None,
    )


# ── Google Sign-In / Sign-Up ──────────────────────────────────────────────────
@router.post("/google", status_code=status.HTTP_200_OK)
async def google_signin(payload: GoogleAuthRequest):
    """
    Sign in or sign up using a Google ID token.

    **Flow (front-end):**
    1. User taps "Sign in with Google" → receives a `credential` (ID token) from Google
    2. Front-end sends `POST /api/v1/auth/google` with body `{"id_token": "<credential>"}`
    3. Back-end verifies the token, upserts the user, and returns tokens in the response

    - **New users** are automatically registered (role=`dentist`, plan=`free_trial`)
    - **Existing users** have their name & picture refreshed silently
    - Tokens are returned in the response body
    """
    data = await AuthService.google_signin(payload.id_token, payload.invitation_token)

    # 1. 🔒 CHECK FOR ERROR CODES FIRST
    # Use .get() to prevent crashing if the key is missing
    error_code = data.get("_error_code")

    if error_code == "deactivated":
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "success": False,
                "message": data.get("message", "Account deactivated."),
                "data": data,
            },
        )

    # 2. ✅ SUCCESS PATH
    # Since we cleared the error check above, it's now safe to return success
    return api_response(
        success=True,
        message="Google sign-in successful.",
        data=data,
    )


# ── Invitation Endpoints ─────────────────────────────────────────────────────

@router.get("/validate-invitation", status_code=status.HTTP_200_OK)
async def validate_invitation(org: str = Query(..., description="Encrypted org_id from the invite link")):
    """
    Validate an invitation link before showing the registration form.
    The 'org' param is the encrypted org_id from the URL:
      http://localhost:3000/register?org=<encrypted>
    Returns org name and org admin email.
    """
    result = await InvitationService.validate_invitation(org)
    return api_response(success=True, message="Invitation is valid.", data=result)


@router.post("/accept-invitation", status_code=status.HTTP_201_CREATED)
async def register_via_invite(payload: AcceptInvitationRequest):
    """
    Register via the organization invitation link. ONE endpoint for everyone.

    Frontend sends the encrypted org_id (from the URL's `org` param) as `token` in the body.

    **How it works:**
    - If email matches `org_email` → user becomes **org_admin** (is_active=True)
    - If email does NOT match → user becomes **staff** (is_active=False, pending approval)

    **Flow:**
    1. Super Admin creates org → generates invite link with encrypted org_id
    2. Org Admin clicks link → registers → email matches → gets org_admin role → can log in
    3. Org Admin shares the SAME link with team members
    4. Members click link → register → role=staff, is_active=False → cannot log in
    5. Org Admin activates member → user gets email "You can now log in"
    """
    result = await InvitationService.register_via_invite(
        token=payload.token,
        full_name=payload.full_name,
        email=payload.email,
        password=payload.password,
    )
    return api_response(
        success=True,
        message=result["message"],
        data=result,
        status_code=201,
    )
