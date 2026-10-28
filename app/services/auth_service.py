import random
import string
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from bson import ObjectId
from fastapi import HTTPException, status
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token
from jose import JWTError, ExpiredSignatureError

from app.config import settings
from app.core.database import get_database
from app.core.security import (
    hash_password,
    verify_password,
    create_access_token,
    create_refresh_token,
    decode_token,
)
from app.core.constants import UserRole, COLLECTION_USERS
from app.schemas.auth_schema import (
    RegisterRequest,
    AccountRequestSchema,
    LoginRequest,
    SendOTPRequest,
    ForgotPasswordRequest,
    VerifyEmailRequest,
    ResendOTPRequest,
)

import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from app.utils.email_templates import render_forgot_password_otp, render_register_otp
# from app.services.subscription_service import SubscriptionService  # COMMENTED OUT — subscriptions disabled
from app.background.report_processor import log_audit_event
from app.services.user_service import UserService


COLLECTION_OTP = "otps"
OTP_EXPIRY_MINUTES = 10


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────
# LEGAL_DOMAINS removed — registration now happens via invitation links
# Legacy open registration kept for backward compat but invitation flow is preferred

def _generate_otp(length: int = 6) -> str:
    """Generate a random numeric OTP."""
    return "".join(random.choices(string.digits, k=length))


async def _send_otp_email(email: str, otp: str, purpose: str) -> None:
    """Send OTP via SMTP using branded HTML templates.
    Falls back to console log if SMTP is not configured.
    """
    subject_map = {
        "register": "Smile Guide AI \u2013 Verify Your Email",
        "forgot_password": "Smile Guide AI \u2013 Password Reset OTP",
    }
    body_map = {
        "register": render_register_otp(otp, OTP_EXPIRY_MINUTES),
        "forgot_password": render_forgot_password_otp(otp, OTP_EXPIRY_MINUTES),
    }
 
    subject = subject_map.get(purpose, "Smile Guide AI OTP")
    body = body_map.get(
        purpose,
        f"<p>Your Smile Guide AI OTP is: <b>{otp}</b>. Valid for {OTP_EXPIRY_MINUTES} minutes.</p>",
    )
 
    print(f"========== [DEV TESTING] OTP for {email} ({purpose}): {otp} ==========")

    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        # SMTP not configured yet \u2014 log to console for development
        print(f"[DEV] OTP for {email} ({purpose}): {otp}")
        return
 
    try:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = email
        message.attach(MIMEText(body, "html"))
 
        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
    except Exception as e:
        print(f"[EMAIL ERROR] Could not send OTP email to {email}: {e}")
 

async def _send_verification_email(email: str, otp: str) -> None:
    """Send email-verification OTP after registration."""
    subject = "Smile Guide AI \u2013 Verify Your Email"
    body = (
        f"<h2>Welcome to Smile Guide AI!</h2>"
        f"<p>Your email verification OTP is: <b>{otp}</b></p>"
        f"<p>Valid for {OTP_EXPIRY_MINUTES} minutes.</p>"
        f"<p>Enter this OTP to activate your account and log in.</p>"
    )

    # print(f"========== [DEV TESTING] Verification OTP for {email}: {otp} ==========")

    if not settings.SMTP_USER or not settings.SMTP_PASSWORD:
        print(f"[DEV] Verification OTP for {email}: {otp}")
        return

    try:
        import aiosmtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = email
        message.attach(MIMEText(body, "html"))

        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
    except Exception as e:
        print(f"[EMAIL ERROR] Could not send verification email to {email}: {e}")

def is_domain_legal(email: str) -> bool:
    """Restrict registration to specific domains."""
    email_domain = email.split("@")[-1].lower()
    return email_domain in ["gmail.com", "petaldental.com"]

# ────────────────────────────────────────────────────────────────────────────
# Auth Service
# ────────────────────────────────────────────────────────────────────────────

class AuthService:

    # ── Send OTP (forgot_password only) ──────────────────────────────────
    @staticmethod
    async def send_otp(payload: SendOTPRequest) -> dict:
        db = await get_database()
        email = payload.email.lower()
        purpose = payload.purpose

        if purpose != "forgot_password":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OTP purpose. Use 'forgot_password'.",
            )

        # For forgot_password: email MUST exist and be verified
        existing = await db[COLLECTION_USERS].find_one({"email": email})
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No account found with this email.",
            )

        # Invalidate any existing OTPs for this email + purpose
        await db[COLLECTION_OTP].delete_many({"email": email, "purpose": purpose})

        # Create new OTP
        otp = _generate_otp()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
        await db[COLLECTION_OTP].insert_one(
            {
                "email": email,
                "otp": otp,
                "purpose": purpose,
                "expires_at": expires_at,
                "used": False,
                "created_at": datetime.now(timezone.utc),
            }
        )

        await _send_otp_email(email, otp, purpose)

        return {
            "message": f"OTP sent to {email}. Valid for {OTP_EXPIRY_MINUTES} minutes.",
            "email": email,
        }

    # ── Request Account ────────────────────────────────────────────────────────
    @staticmethod
    async def request_account(payload: AccountRequestSchema) -> dict:
        db = await get_database()
        email = payload.email.lower()

        # Check if user already exists
        existing_user = await db[COLLECTION_USERS].find_one({"email": email})
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists."
            )

        now = datetime.now(timezone.utc)

        # Only students require super admin approval; all others can login after email verification
        is_student = (
            payload.account_type == "individual"
            and payload.role
            and payload.role.lower() == "student"
        )

        user_doc = {
            "email": email,
            "full_name": payload.full_name,
            "hashed_password": hash_password(payload.password),
            "is_active": False,
            "is_verified": False,
            "auth_provider": "email",
            "query_count": 0,
            "created_at": now,
            "updated_at": now,
            "requires_super_admin_approval": is_student,
            "account_type": payload.account_type,
        }

        if payload.account_type == "individual":
            from app.core.constants import COLLECTION_ORGANIZATIONS

            if payload.role and payload.role.lower() == "student":
                if not email.endswith(".edu"):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="Student registration requires a .edu email address."
                    )
                
                undergrads_org = await db[COLLECTION_ORGANIZATIONS].find_one({"name": "Under grads", "is_individual_organization": True})
                if not undergrads_org:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="The 'Under grads' organization was not found. Please contact support."
                    )
                
                user_doc["role"] = UserRole.STUDENT
                user_doc["organization_id"] = str(undergrads_org["_id"])
            else:
                # Solo Dentist flow
                solo_org = await db[COLLECTION_ORGANIZATIONS].find_one({"name": "Solo Practice", "is_individual_organization": True})
                if not solo_org:
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="The 'Solo Practice' organization was not found. Please contact support."
                    )

                user_doc["role"] = UserRole.SOLO_DENTIST
                user_doc["organization_id"] = str(solo_org["_id"])

        elif payload.account_type == "organization":
            if not payload.organization_name:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Organization name is required for organization accounts."
                )

            from app.core.constants import COLLECTION_ORGANIZATIONS
            # Check if org name is already taken
            if await db[COLLECTION_ORGANIZATIONS].find_one({"name": payload.organization_name}):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"An organization named '{payload.organization_name}' already exists."
                )

            org_doc = {
                "name": payload.organization_name,
                "email": email,
                "logo_url": None,
                "is_active": True,  # Org is active immediately — no super admin approval needed
                "auto_delete_sessions": True,
                "number_of_locations": payload.number_of_locations,
                "created_by": email,
                "created_at": now,
                "updated_at": now,
            }
            org_result = await db[COLLECTION_ORGANIZATIONS].insert_one(org_doc)
            
            user_doc["role"] = UserRole.ORG_ADMIN
            user_doc["organization_id"] = str(org_result.inserted_id)
        
        else:
            raise HTTPException(status_code=400, detail="Invalid account_type.")

        # Insert user
        user_result = await db[COLLECTION_USERS].insert_one(user_doc)
        user_id = str(user_result.inserted_id)

        # Audit log
        await log_audit_event(
            actor_id=user_id,
            action="USER_CREATED",
            resource="users",
            resource_id=user_id,
            metadata={"email": email, "type": payload.account_type},
            organization_id=user_doc.get("organization_id")
        )

        # Generate + send verification OTP
        otp = _generate_otp()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
        await db[COLLECTION_OTP].delete_many({"email": email, "purpose": "verify_email"})
        await db[COLLECTION_OTP].insert_one(
            {
                "email": email,
                "otp": otp,
                "purpose": "verify_email",
                "expires_at": expires_at,
                "used": False,
                "created_at": datetime.now(timezone.utc),
            }
        )
        await _send_otp_email(email, otp, "register")

        return {
            "message": "Account requested successfully. A verification OTP has been sent to your email.",
            "email": email
        }

    # ── Register ──────────────────────────────────────────────────────────
    @staticmethod
    async def register(payload: RegisterRequest) -> dict:
        """Create user immediately, then auto-send verification OTP."""

        # 🔒 THE ULTIMATE GATEKEEPER
        if not is_domain_legal(payload.email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is restricted to @gmail.com or @petaldental.com addresses."
            )
            
        db = await get_database()
        email = payload.email.lower()

        # Check email uniqueness — if already exists, return existing user data
        existing = await db[COLLECTION_USERS].find_one({"email": email})
        if existing:

            if existing.get("auth_provider") == "google":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This email is already linked to a Google account. Please sign in using the 'Sign in with Google' button."
                )

            return {
                "_already_exists": True,
                "message": "An account with this email already exists.",
                "email": existing["email"],
                "full_name": existing.get("full_name", ""),
                "role": existing.get("role", UserRole.STAFF),
                "is_active": existing.get("is_active", True),
                "is_verified": existing.get("is_verified", False),
                "auth_provider": existing.get("auth_provider", "email"),
                "subscription_plan": "free_trial",  # Subscriptions disabled
                "created_at": existing["created_at"].isoformat(),
            }

        # Save user — is_active=False, is_verified=False until email OTP is confirmed
        user_doc = {
            "email": email,
            "full_name": payload.full_name,
            "hashed_password": hash_password(payload.password),
            "role": UserRole.STAFF,
            "is_active": False,
            "is_verified": False,
            "auth_provider": "email",
            "query_count": 0,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }
        user_result = await db[COLLECTION_USERS].insert_one(user_doc)
        user_id = str(user_result.inserted_id)

        # Audit log: User creation
        await log_audit_event(
            actor_id=user_id,
            action="USER_CREATE",
            resource="users",
            resource_id=user_id,
            metadata={"email": email},
            organization_id=None
        )

        # Subscriptions disabled
        # await SubscriptionService.create_subscription(user_id, "free_trial")

        # Generate + send verification OTP automatically
        otp = _generate_otp()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
        await db[COLLECTION_OTP].delete_many({"email": email, "purpose": "verify_email"})
        await db[COLLECTION_OTP].insert_one(
            {
                "email": email,
                "otp": otp,
                "purpose": "verify_email",
                "expires_at": expires_at,
                "used": False,
                "created_at": datetime.now(timezone.utc),
            }
        )
        await _send_otp_email(email, otp, "register")

        return {
            "message": "Account created. A verification OTP has been sent to your email.",
            "email": email,
            "full_name": payload.full_name,  
            "role": user_doc["role"],
            "is_active": user_doc["is_active"],
            "is_verified": user_doc["is_verified"],
            "auth_provider": user_doc["auth_provider"],
            "subscription_plan": "free_trial",
            "created_at": user_doc["created_at"].isoformat(),
        }

    # ── Verify Email ─────────────────────────────────────────────────────
    @staticmethod
    async def verify_email(payload: VerifyEmailRequest) -> dict:
        db = await get_database()
        email = payload.email.lower()

        user = await db[COLLECTION_USERS].find_one({"email": email})
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found.")
        if user.get("is_verified"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already verified.")

        otp_doc = await db[COLLECTION_OTP].find_one(
            {"email": email, "purpose": "verify_email", "used": False}
        )
        if not otp_doc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No valid OTP found. Request a new one.")
        if otp_doc["otp"] != payload.otp:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid OTP.")
        if datetime.now(timezone.utc) > otp_doc["expires_at"].replace(tzinfo=timezone.utc):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="OTP expired. Request a new one.")

        # Mark user as verified
        now = datetime.now(timezone.utc)
        # Fetch user BEFORE update to check their flag
        user = await db[COLLECTION_USERS].find_one({"email": email})
        
        # If they registered via 'request_account', they require Super Admin approval.
        # Otherwise (invitations, open registration), they activate immediately upon email verification.
        requires_approval = user.get("requires_super_admin_approval", False)

        if requires_approval:
            update_fields = {"is_verified": True, "last_login_at": now, "updated_at": now}
        else:
            update_fields = {"is_verified": True, "is_active": True, "last_login_at": now, "updated_at": now}
        
        await db[COLLECTION_USERS].update_one(
            {"email": email},
            {"$set": update_fields},
        )
        await db[COLLECTION_OTP].update_one({"_id": otp_doc["_id"]}, {"$set": {"used": True}})

        # Fetch updated user
        user = await db[COLLECTION_USERS].find_one({"email": email})
        user_id = str(user["_id"])

        org_id = user.get("organization_id")
        if org_id and user.get("is_active"):
            try:
                from app.services.subscription_service import SubscriptionService
                await SubscriptionService.sync_org_dentist_subscription_quantity(org_id)
            except Exception:
                pass

        if requires_approval and not user.get("is_active"):
            from app.background.email_tasks import notify_super_admins_pending_approval
            import asyncio
            account_type = "Student"
            asyncio.create_task(notify_super_admins_pending_approval(
                user_email=user["email"],
                user_name=user.get("full_name", ""),
                account_type=account_type
            ))
            
            user_data = await UserService.get_user_by_id(user_id)
            user_data["_error_code"] = "approval_pending"
            user_data["message"] = "Your account has been registered successfully! Please allow us up to 24 hours to review and approve your account. You will be notified via email once approved."
            return user_data

        user_data = await UserService.get_user_by_id(user_id)
        user_data["message"] = "Email verified successfully. You can now log in."
        
        refresh_token = create_refresh_token(user_id)
        
        # Save refresh token to user document
        if user.get("role") == UserRole.STUDENT:
            session_id = str(uuid.uuid4())
            await db[COLLECTION_USERS].update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "refresh_tokens": [refresh_token],
                        "active_session_id": session_id,
                    }
                }
            )
        else:
            await db[COLLECTION_USERS].update_one(
                {"_id": user["_id"]},
                {
                    "$push": {
                        "refresh_tokens": {
                            "$each": [refresh_token],
                            "$slice": -5
                        }
                    }
                }
            )

        user_data["access_token"] = create_access_token(user_id, {"role": user["role"]})
        user_data["refresh_token"] = refresh_token
        user_data["token_type"] = "bearer"
        return user_data
    
    # ── Resend Verification OTP ───────────────────────────────────────────
    @staticmethod
    async def resend_verification_otp(payload: ResendOTPRequest) -> dict:
        """Resend the verification OTP to an unverified registered email."""
        db = await get_database()
        email = payload.email.lower()

        user = await db[COLLECTION_USERS].find_one({"email": email})
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No account found.")
        if user.get("is_verified"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already verified.")

        otp = _generate_otp()
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
        await db[COLLECTION_OTP].delete_many({"email": email, "purpose": "verify_email"})
        await db[COLLECTION_OTP].insert_one(
            {
                "email": email,
                "otp": otp,
                "purpose": "verify_email",
                "expires_at": expires_at,
                "used": False,
                "created_at": datetime.now(timezone.utc),
            }
        )
        await _send_otp_email(email, otp, "register")

        return {
            "message": f"Verification OTP resent to {email}. Valid for {OTP_EXPIRY_MINUTES} minutes.",
            "email": email,
        }

    # ── Login ─────────────────────────────────────────────────────────────
    @staticmethod
    async def login(payload: LoginRequest) -> dict:
        db = await get_database()
        email = payload.email.lower()
        user = await db[COLLECTION_USERS].find_one(
            {"email": payload.email.lower()}
        )

        # 🆕 LOGIC: Check if this is a Google account before checking password
        if user and user.get("auth_provider") == "google":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This account uses Google Sign-In. Please click the 'Sign in with Google' button to access your account."
            )

        # Check password first
        if not user or not verify_password(payload.password, user["hashed_password"]):
            if user:
                # Log failed login attempt only for valid emails
                await log_audit_event(
                    actor_id=str(user["_id"]),
                    action="LOGIN_FAILED",
                    resource="users",
                    resource_id=str(user["_id"]),
                    metadata={"email": payload.email.lower()},
                    organization_id=user.get("organization_id")
                )

            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password.",
            )
        
        # Check if account is pending email verification FIRST — before is_active check,
        # because unverified users will have is_active=False until OTP confirmation
        if not user.get("is_verified", False):
            return {
                "_error_code": "verification_pending",
                "message": "Email verification pending. Please verify your email using the OTP sent to your inbox.",
                "email": user["email"],
                "full_name": user.get("full_name", ""),
                "role": user.get("role", ""),
                "is_active": user.get("is_active", False),
                "is_verified": False,
                "auth_provider": user.get("auth_provider", "email"),
                "subscription_plan": "free_trial",  # Subscriptions disabled
                "created_at": user["created_at"].isoformat(),
            }

        # Check if student account is pending super admin approval
        if not user.get("is_active", True) and user.get("requires_super_admin_approval", False):
            return {
                "_error_code": "approval_pending",
                "message": "Your account has been registered successfully! Please allow us up to 24 hours to review and approve your account. You will be notified via email once approved.",
                "email": user["email"],
                "full_name": user.get("full_name", ""),
                "role": user.get("role", ""),
                "is_active": False,
                "is_verified": user.get("is_verified", False),
                "auth_provider": user.get("auth_provider", "email"),
                "subscription_plan": "free_trial",
                "created_at": user["created_at"].isoformat(),
            }

        # Check if account is deactivated by admin (only for verified users)
        if not user.get("is_active", True):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has been deactivated. Please contact support.",
            )

        user_id = str(user["_id"])
        
        refresh_token = create_refresh_token(user_id)
        
        now = datetime.now(timezone.utc)

        # Students: single session only — clear all previous tokens to prevent account sharing
        if user.get("role") == UserRole.STUDENT:
            session_id = str(uuid.uuid4())
            await db[COLLECTION_USERS].update_one(
                {"_id": user["_id"]},
                {
                    "$set": {
                        "last_login_at": now,
                        "refresh_tokens": [refresh_token],  # Replace all — only 1 session allowed
                        "active_session_id": session_id,  # Track active session for instant invalidation
                    }
                }
            )
        else:
            session_id = None
            await db[COLLECTION_USERS].update_one(
                {"_id": user["_id"]},
                {
                    "$set": {"last_login_at": now},
                    "$push": {
                        "refresh_tokens": {
                            "$each": [refresh_token],
                            "$slice": -5  # Keep only the 5 most recent active sessions
                        }
                    }
                }
            )

        # Permanent login audit record (unlike last_login_at which gets overwritten)
        await log_audit_event(
            actor_id=user_id,
            action="LOGIN",
            resource="users",
            resource_id=user_id,
            metadata={"email": email, "auth_provider": user.get("auth_provider", "email")},
            organization_id=user.get("organization_id"),
        )

        # Build access token claims — include session_id for students
        token_claims = {"role": user["role"], "org_id": user.get("organization_id")}
        if session_id:
            token_claims["session_id"] = session_id

        user_data = await UserService.get_user_by_id(user_id)
        user_data["access_token"] = create_access_token(user_id, token_claims)
        user_data["refresh_token"] = refresh_token
        user_data["token_type"] = "bearer"
        return user_data

    # ── Forgot Password ───────────────────────────────────────────────────
    @staticmethod
    async def forgot_password(payload: ForgotPasswordRequest) -> dict:
        db = await get_database()
        email = payload.email.lower()

        # Verify OTP
        otp_doc = await db[COLLECTION_OTP].find_one(
            {"email": email, "purpose": "forgot_password", "used": False}
        )
        if not otp_doc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No valid OTP found. Please request a new OTP.",
            )
        if otp_doc["otp"] != payload.otp:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid OTP.",
            )
        if datetime.now(timezone.utc) > otp_doc["expires_at"].replace(tzinfo=timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="OTP has expired. Please request a new one.",
            )

        # Step 1: Fetch the user
        user = await db[COLLECTION_USERS].find_one({"email": email, "is_active": True})
        if not user:
            raise HTTPException(status_code=404, detail="User not found.")

        # If user signed up with Google, switch auth_provider to email
        if user.get("auth_provider") == "google":
            await db[COLLECTION_USERS].update_one(
                {"_id": user["_id"]},
                {"$set": {"auth_provider": "email", "updated_at": datetime.now(timezone.utc)}},
            )

        # Step 2: Update password
        new_hashed = hash_password(payload.new_password)
        await db[COLLECTION_USERS].update_one(
            {"_id": user["_id"]},
            {"$set": {"hashed_password": new_hashed, "updated_at": datetime.now(timezone.utc)}},
        )

        # Mark OTP as used
        await db[COLLECTION_OTP].update_one(
            {"_id": otp_doc["_id"]}, {"$set": {"used": True}}
        )

        return {"message": "Password reset successfully. Please log in with your new password."}

    # ── Refresh Token ─────────────────────────────────────────────────────
    @staticmethod
    async def refresh_token(refresh_token: str) -> dict:
        """Validate the refresh token and issue a new token pair."""
        try:
            payload = decode_token(refresh_token)
            if payload.get("type") != "refresh":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid token type. Expected refresh token.",
                )
        except ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Refresh token has expired. Please log in again.",
            )
        except JWTError:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Invalid or malformed refresh token. Please log in again.",
            )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid refresh token. Please log in again.",
            )

        db = await get_database()
        # Convert sub (string user_id) to ObjectId for MongoDB lookup
        try:
            user_oid = ObjectId(payload["sub"])
        except Exception:
            raise HTTPException(status_code=401, detail="Malformed token subject.")

        user = await db[COLLECTION_USERS].find_one(
            {"_id": user_oid, "is_active": True, "is_verified": True, "refresh_tokens": refresh_token}
        )
        if not user:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Session expired, logged out, or account deactivated.",
            )

        user_id = str(user["_id"])
        new_refresh_token = create_refresh_token(user_id)

        # Rotate token: Append new refresh token and keep the last 5 valid sessions
        await db[COLLECTION_USERS].update_one(
            {"_id": user_oid},
            {"$push": {"refresh_tokens": {"$each": [new_refresh_token], "$slice": -5}}}
        )

        return {
            "access_token": create_access_token(user_id, {"role": user["role"], "org_id": user.get("organization_id")}),
            "refresh_token": new_refresh_token,
            "token_type": "bearer",
        }

    # ── Google Sign-In / Sign-Up ──────────────────────────────────────────
    @staticmethod
    async def google_signin(id_token_str: str, invitation_token: Optional[str] = None) -> dict:
        """
        Verify a Google ID token (from the front-end Google Sign-In button),
        then upsert the user in MongoDB.

        - New users are created with role=dentist, plan=free_trial.
        - Existing users (matched by email) have their name/picture refreshed.
        - Returns the same dict shape as `login()` so the route layer can set cookies.
        """
        if not settings.GOOGLE_CLIENT_ID:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Google OAuth is not configured on this server.",
            )

        # ── Verify the token with Google's public keys ──────────────────────
        try:
            id_info = google_id_token.verify_oauth2_token(
                id_token_str,
                google_requests.Request(),
                settings.GOOGLE_CLIENT_ID,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid Google ID token: {exc}",
            )

        email: str = id_info.get("email", "").lower()
        full_name: str = id_info.get("name", "")
        picture: str = id_info.get("picture", "")
        google_sub: str = id_info.get("sub", "")

        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Google token did not include an email address.",
            )

        db = await get_database()
        existing = await db[COLLECTION_USERS].find_one({"email": email})

        now = datetime.now(timezone.utc)

        if existing:
            # 1. Check if admin deactivated this account
            if not existing.get("is_active", True):
                return {
                "_error_code": "deactivated",
                "message": "Your account has been deactivated. Please contact support.",
                # 🆕 Add the user data here so the route can find it
                "email": existing["email"],
                "full_name": existing.get("full_name", ""),
                "role": existing.get("role", "staff"),
                "is_verified": existing.get("is_verified", False),
                "auth_provider": existing.get("auth_provider", "google"),
                "subscription_plan": "free_trial",  # Subscriptions disabled
                "created_at": existing["created_at"].isoformat() if "created_at" in existing else None
        }

            refresh_token = create_refresh_token(str(existing["_id"]))

            # 2. Update logic: If they are logging in via Google, 
            # they are now verified and Google is a valid provider.
            update_data = {
                "full_name": full_name or existing.get("full_name", ""),
                "picture": picture,
                "google_sub": google_sub,
                "last_login_at": now,
                "updated_at": now,
            }

            # If the user was previously 'email', you might want to 
            # keep 'email' or switch to 'google'. Usually, switching to 'google' 
            # or adding it to a list of providers is best.

            if existing.get("auth_provider") == "email" and existing.get("is_verified") == False:
                update_data["auth_provider"] = "google"
                update_data["is_verified"] = True

            # Students: single session only — clear all previous tokens to prevent account sharing
            if existing.get("role") == UserRole.STUDENT:
                session_id = str(uuid.uuid4())
                update_data["refresh_tokens"] = [refresh_token]  # Replace all — only 1 session allowed
                update_data["active_session_id"] = session_id  # Track active session for instant invalidation
                await db[COLLECTION_USERS].update_one(
                    {"_id": existing["_id"]},
                    {"$set": update_data},
                )
            else:
                await db[COLLECTION_USERS].update_one(
                    {"_id": existing["_id"]},
                    {
                        "$set": update_data,
                        "$push": {
                            "refresh_tokens": {
                                "$each": [refresh_token],
                                "$slice": -5
                            }
                        }
                    },
                )
            user = await db[COLLECTION_USERS].find_one({"_id": existing["_id"]})
        else:
            # ── New user — create account ───────────────────────────────────
            # Direct registration without invitation is blocked
            if not invitation_token:
                raise HTTPException(
                    status_code=403,
                    detail="You are not linked with any organization. Registration requires an invitation link."
                )

            from app.services.invitation_service import InvitationService
            try:
                org_info = await InvitationService.validate_invitation(invitation_token)
                org_id = org_info["organization_id"]
                authorized_email = org_info.get("authorized_email")
                
                if authorized_email and email.lower() != authorized_email:
                    raise HTTPException(
                        status_code=403,
                        detail=f"This invitation link is specifically for {authorized_email}. You cannot register with {email}."
                    )

                if email == org_info.get("org_email"):
                    user_role = UserRole.ORG_ADMIN
                    user_is_active = True
                else:
                    user_role = org_info.get("role") or UserRole.STAFF
                    user_is_active = True
            except Exception:
                raise HTTPException(
                    status_code=403,
                    detail="Invalid or expired invitation link. Registration failed."
                )

            new_user_id = ObjectId()
            refresh_token = create_refresh_token(str(new_user_id))

            user_doc = {
                "_id": new_user_id,
                "email": email,
                "full_name": full_name,
                "hashed_password": None,  # No password for OAuth users
                "role": user_role,
                "is_active": user_is_active,
                "is_verified": True,   # Google OAuth users are auto-verified
                "auth_provider": "google",
                "google_sub": google_sub,
                "picture": picture,
                "query_count": 0,
                "last_login_at": now,
                "created_at": now,
                "updated_at": now,
                "refresh_tokens": [refresh_token],
            }
            if org_id:
                user_doc["organization_id"] = org_id

            await db[COLLECTION_USERS].insert_one(user_doc)
            user = await db[COLLECTION_USERS].find_one({"_id": new_user_id})
            
            # Create default subscription for brand new user
            # await SubscriptionService.create_subscription(user_id, "free_trial")  # Subscriptions disabled

        user_id = str(user["_id"])

        # Audit log — permanent login record for both new and returning Google users
        await log_audit_event(
            actor_id=user_id,
            action="LOGIN",
            resource="users",
            resource_id=user_id,
            metadata={"email": user["email"], "auth_provider": "google"},
            organization_id=user.get("organization_id"),
        )

        # Build access token claims — include session_id for students
        token_claims = {"role": user["role"], "org_id": user.get("organization_id")}
        if user.get("role") == UserRole.STUDENT and user.get("active_session_id"):
            token_claims["session_id"] = user["active_session_id"]

        user_data = await UserService.get_user_by_id(user_id)
        user_data["access_token"] = create_access_token(user_id, token_claims)
        user_data["refresh_token"] = refresh_token
        user_data["token_type"] = "bearer"
        return user_data

    # ── Logout ────────────────────────────────────────────────────────────
    @staticmethod
    async def logout(user_id: str, refresh_token: Optional[str] = None, all_devices: bool = False) -> dict:
        """
        Record the logout time on the user document.
        Cookie deletion is handled by the route layer.
        """
        db = await get_database()
        try:
            user_oid = ObjectId(user_id)
        except Exception:
            return {"message": "Logged out successfully."}

        update_op = {"$set": {"last_logout_at": datetime.now(timezone.utc)}}
        if all_devices or not refresh_token:
            update_op["$set"]["refresh_tokens"] = [] # Force logout on all devices
        else:
            update_op["$pull"] = {"refresh_tokens": refresh_token} # Single device

        user = await db[COLLECTION_USERS].find_one_and_update(
            {"_id": user_oid},
            update_op,
            return_document=True
        )

        # Audit log: Logout
        await log_audit_event(
            actor_id=user_id,
            action="LOGOUT",
            resource="users",
            resource_id=user_id,
            metadata={},
            organization_id=user.get("organization_id") if user else None
        )

        return {"message": "Logged out successfully."}
