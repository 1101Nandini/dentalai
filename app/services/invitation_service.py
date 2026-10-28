"""
Invitation service — encrypted org_id link for organization registration.

Flow:
  1. Super Admin creates org (name, email, org name, logo)
  2. Super Admin calls "generate invitation link" API → returns permanent link
     Link format: http://localhost:3000/register?org=<encrypted_org_id>
  3. This link is emailed to the org admin
  4. Org Admin registers via the link:
     - If email matches the org email → role = org_admin, is_active = True
     - Otherwise → treated as a regular member
  5. Org Admin shares the SAME link with team members
  6. Members register via the link:
     - role = staff, is_active = False (no access until approved)
  7. Org Admin activates a member → email sent: "You can now log in"
  8. Org Admin changes a member's role → email sent: "Your role has been changed"
"""
from datetime import datetime, timezone
from jose import jwt, JWTError

from bson import ObjectId
from fastapi import HTTPException, status

from app.core.database import get_database
from app.core.constants import (
    COLLECTION_ORGANIZATIONS,
    COLLECTION_USERS,
    UserRole,
)
from app.core.security import hash_password
from app.background.report_processor import log_audit_event
from app.config import settings

import aiosmtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import logging
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Encrypt / Decrypt org_id for the invitation link
# ═══════════════════════════════════════════════════════════════════════════

def _encrypt_org_id(org_id: str, email: str, role: str = None) -> str:
    """Encode org_id, target email, and optional role into a JWT (no expiration — permanent link)."""
    payload = {"org_id": org_id, "email": email.lower(), "purpose": "org_invite"}
    if role:
        payload["role"] = role
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _decrypt_org_id(token: str) -> dict:
    """Decode token to get org_id, authorized email, and role."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        org_id = payload.get("org_id")
        email = payload.get("email")
        role = payload.get("role")
        if not org_id or not email or payload.get("purpose") != "org_invite":
            raise HTTPException(status_code=400, detail="Invalid invitation link")
        return {"org_id": org_id, "email": email, "role": role}
    except JWTError:
        raise HTTPException(status_code=400, detail="Invalid or corrupted invitation link")


class InvitationService:

    # ── Generate Invitation Link ─────────────────────────────────────────

    @staticmethod
    async def generate_invite_link(org_id: str, invited_by: str) -> dict:
        """
        Generate a permanent invitation link for an organization.
        The org_id is encrypted in the URL — link never expires.
        
        Link format: http://localhost:3000/register?org=<encrypted_org_id>
        """
        db = await get_database()

        org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(org_id)})
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        encrypted = _encrypt_org_id(org_id, org["email"], UserRole.ORG_ADMIN)
        invite_link = f"{settings.FRONTEND_URL}/register?org={encrypted}"

        # Send the link to the org admin email
        await _send_invitation_email(
            to_email=org["email"],
            org_name=org["name"],
            invite_link=invite_link,
        )

        return {
            "invite_link": invite_link,
            "organization_name": org["name"],
            "organization_id": org_id,
        }

    @staticmethod
    async def invite_member(org_id: str, email: str, invited_by: str, role: str) -> dict:
        """
        Generate a targeted invitation link for a specific email address and send it.
        """
        db = await get_database()

        org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(org_id)})
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")

        # Check if user already exists
        existing_user = await db[COLLECTION_USERS].find_one({"email": email.lower()})
        if existing_user:
            org_name_err = "another organization"
            if existing_user.get("organization_id") == org_id:
                raise HTTPException(status_code=409, detail=f"User '{email}' is already a member of this organization.")
            elif existing_user.get("organization_id"):
                try:
                    existing_org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(existing_user["organization_id"])})
                    if existing_org:
                        org_name_err = existing_org.get("name", org_name_err)
                except Exception:
                    pass
            raise HTTPException(status_code=409, detail=f"User '{email}' already exists in '{org_name_err}'.")

        encrypted = _encrypt_org_id(org_id, email, role)
        invite_link = f"{settings.FRONTEND_URL}/register?org={encrypted}"

        # Send the link to the targeted email
        await _send_invitation_email(
            to_email=email,
            org_name=org["name"],
            invite_link=invite_link,
            role=role,
        )

        return {
            "invite_link": invite_link,
            "email": email,
            "organization_name": org["name"],
            "organization_id": org_id,
        }

    # ── Validate Invitation Token ────────────────────────────────────────

    @staticmethod
    async def validate_invitation(token: str) -> dict:
        """
        Validate the encrypted org token from the link.
        Returns org name and org admin email so frontend can show context.
        """
        payload = _decrypt_org_id(token)
        org_id = payload["org_id"]
        authorized_email = payload["email"]
        role = payload.get("role")
        db = await get_database()

        org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(org_id)})
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if not org.get("is_active", True):
            raise HTTPException(status_code=400, detail="Organization is no longer active")

        return {
            "valid": True,
            "organization_name": org["name"],
            "organization_id": str(org["_id"]),
            "org_email": org.get("email"),   # Frontend can hint who the org admin is
            "authorized_email": authorized_email, # The only email allowed to register with this link
            "role": role,
        }

    # ── Register via Invitation Link ─────────────────────────────────────

    @staticmethod
    async def register_via_invite(
        token: str,
        full_name: str,
        email: str,
        password: str,
    ) -> dict:
        """
        Register a user via the organization invitation link.
        """
        payload = _decrypt_org_id(token)
        org_id = payload["org_id"]
        authorized_email = payload["email"]
        token_role = payload.get("role")
        
        if email.lower() != authorized_email:
            raise HTTPException(
                status_code=403, 
                detail=f"This invitation link is specifically for {authorized_email}. You cannot register with {email}."
            )

        db = await get_database()

        # Verify org exists and is active
        org = await db[COLLECTION_ORGANIZATIONS].find_one({"_id": ObjectId(org_id)})
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if not org.get("is_active", True):
            raise HTTPException(status_code=400, detail="Organization is no longer active")

        # Domain check
        from app.services.auth_service import is_domain_legal, _generate_otp, _send_verification_email, COLLECTION_OTP, OTP_EXPIRY_MINUTES
        if not is_domain_legal(email):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Registration is restricted to @gmail.com or @petaldental.com addresses."
            )

        # Check if user already exists
        existing = await db[COLLECTION_USERS].find_one({"email": email.lower()})
        if existing:
            raise HTTPException(status_code=409, detail="An account with this email already exists")

        org_email = org.get("email", "").lower()
        now = datetime.now(timezone.utc)

        # Determine role & access based on email match and token role
        is_org_admin = (email.lower() == org_email)

        if is_org_admin:
            role = UserRole.ORG_ADMIN
            is_active = False  # Remains inactive until OTP verification
            message = "Account created as Organization Admin. A verification OTP has been sent to your email."
        else:
            role = token_role or UserRole.STAFF
            is_active = False  # Remains inactive until OTP verification
            message = "Account created as Organization Admin. A verification OTP has been sent to your email."

        user_doc = {
            "email": email.lower(),
            "full_name": full_name,
            "hashed_password": hash_password(password),
            "role": role,
            "organization_id": str(org["_id"]),
            "is_active": is_active,
            "is_verified": False,             # Now requires OTP verification
            "auth_provider": "email",
            "query_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        result = await db[COLLECTION_USERS].insert_one(user_doc)
        user_id = str(result.inserted_id)

        # Audit log: User creation
        await log_audit_event(
            actor_id=user_id,
            action="USER_CREATE",
            resource="users",
            resource_id=user_id,
            metadata={"email": email.lower(), "role": role},
            organization_id=str(org["_id"])
        )

        # Generate + send verification OTP automatically
        otp = _generate_otp()
        from datetime import timedelta
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)
        await db[COLLECTION_OTP].delete_many({"email": email.lower(), "purpose": "verify_email"})
        await db[COLLECTION_OTP].insert_one({
            "email": email.lower(),
            "otp": otp,
            "purpose": "verify_email",
            "expires_at": expires_at,
            "used": False,
            "created_at": now,
        })
        from app.services.auth_service import _send_otp_email
        await _send_otp_email(email.lower(), otp, "register")

        return {
            "user_id": str(result.inserted_id),
            "email": email.lower(),
            "full_name": full_name,
            "role": role,
            "organization_id": str(org["_id"]),
            "is_active": is_active,
            "message": message,
        }

    @staticmethod
    def _role_display_name(role: str) -> str:
        role_map = {
            "super_admin": "Super Administrator",
            "org_admin": "Organization Administrator",
            "dentist": "Dentist",
            "staff": "Staff",
            "front_office": "Front Office",
            "student": "Student"
        }
        return role_map.get(role, role.replace("_", " ").title())


# ═══════════════════════════════════════════════════════════════════════════
# Email helpers
# ═══════════════════════════════════════════════════════════════════════════

async def _send_invitation_email(
    to_email: str,
    org_name: str,
    invite_link: str,
    role: str = None,
) -> None:
    """Send invitation email with the registration link to org admin or team member."""
    if role:
        display_role = InvitationService._role_display_name(role)
        subject = f"Smile Guide AI – You've been invited to join {org_name}"
        invitation_text = f"You have been invited to join the organization <strong>{org_name}</strong> as a <strong>{display_role}</strong>."
    else:
        subject = f"Smile Guide AI – You've been invited to manage {org_name}"
        invitation_text = f"You have been invited to manage the organization <strong>{org_name}</strong> as an Organization Admin."

    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #1F2937;">Welcome to Smile Guide AI!</h2>
        <p>{invitation_text}</p>
        <p>Click the button below to set up your account:</p>
        <p style="text-align: center; margin: 30px 0;">
            <a href="{invite_link}"
               style="background: #4F46E5; color: #fff; padding: 14px 28px; text-decoration: none; border-radius: 8px; font-weight: bold;">
                Set Up Your Account
            </a>
        </p>
        <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 20px 0;">
        <p style="color: #9CA3AF; font-size: 12px;">— Smile Guide AI Team</p>
    </div>
    """

    try:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = to_email
        message.attach(MIMEText(body, "html"))

        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Invitation sent to {to_email}")
    except Exception as e:
        logger.error(f"[EMAIL ERROR] Could not send invitation to {to_email}: {e}")


async def send_activation_email(to_email: str, full_name: str, org_name: str) -> None:
    """Notify user that their account has been activated and they can now log in."""
    subject = f"Smile Guide AI – Your account is now active!"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #1F2937;">Good news, {full_name}! 🎉</h2>
        <p>Your account at <strong>{org_name}</strong> has been approved by your organization admin.</p>
        <p><strong>You can now log in</strong> and start using Smile Guide AI.</p>
        <p style="text-align: center; margin: 30px 0;">
            <a href="{settings.FRONTEND_URL}/login"
               style="background: #4F46E5; color: #fff; padding: 14px 28px; text-decoration: none; border-radius: 8px; font-weight: bold;">
                Log In Now
            </a>
        </p>
        <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 20px 0;">
        <p style="color: #9CA3AF; font-size: 12px;">— Smile Guide AI Team</p>
    </div>
    """

    try:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = to_email
        message.attach(MIMEText(body, "html"))

        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Activation email sent to {to_email}")
    except Exception as e:
        logger.error(f"[EMAIL ERROR] Could not send activation email to {to_email}: {e}")

async def send_deactivation_email(to_email: str, full_name: str, org_name: str) -> None:
    """Notify user that their account has been deactivated by their admin."""
    subject = f"Smile Guide AI – Your account has been deactivated"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #1F2937;">Hello, {full_name}.</h2>
        <p>Your access to <strong>{org_name}</strong> on Smile Guide AI has been deactivated by your organization administrator.</p>
        <p>Please reach out to your organization admin directly.</p>
        <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 20px 0;">
        <p style="color: #9CA3AF; font-size: 12px;">— Smile Guide AI Team</p>
    </div>
    """

    try:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = to_email
        message.attach(MIMEText(body, "html"))

        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Deactivation email sent to {to_email}")
    except Exception as e:
        logger.error(f"[EMAIL ERROR] Could not send deactivation email to {to_email}: {e}")


async def send_org_status_email(to_email: str, full_name: str, org_name: str, is_active: bool) -> None:
    """Notify Org Admin that the Super Admin activated/deactivated their entire organization."""
    status_word = "reactivated" if is_active else "deactivated"
    subject = f"Smile Guide AI – Your organization has been {status_word}"
    
    if is_active:
        msg_text = "Good news! Your organization has been <strong>reactivated</strong>. Your team can now log in and continue using the platform."
    else:
        msg_text = "Your organization has been <strong>deactivated</strong> by the platform administrators. All access for you and your team members has been temporarily suspended."

    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #1F2937;">Hello, {full_name}.</h2>
        <p>This is a notice regarding your organization: <strong>{org_name}</strong>.</p>
        <p>{msg_text}</p>
        <p>If you have any questions, please contact Smile Guide AI support.</p>
        <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 20px 0;">
        <p style="color: #9CA3AF; font-size: 12px;">— Smile Guide AI Team</p>
    </div>
    """

    try:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = to_email
        message.attach(MIMEText(body, "html"))

        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Org status email ({status_word}) sent to {to_email}")
    except Exception as e:
        logger.error(f"[EMAIL ERROR] Could not send org status email to {to_email}: {e}")




async def send_role_change_email(to_email: str, full_name: str, new_role: str, org_name: str) -> None:
    """Notify user that their role has been changed."""
    role_label = InvitationService._role_display_name(new_role)

    subject = f"Smile Guide AI – Your role has been updated"
    body = f"""
    <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px;">
        <h2 style="color: #1F2937;">Role Update</h2>
        <p>Hi {full_name},</p>
        <p>Your role at <strong>{org_name}</strong> has been updated to: 
           <strong style="color: #4F46E5;">{role_label}</strong>
        </p>
        <p>This change takes effect immediately. Log in to access your updated permissions.</p>
        <p style="text-align: center; margin: 30px 0;">
            <a href="{settings.FRONTEND_URL}/login"
               style="background: #4F46E5; color: #fff; padding: 14px 28px; text-decoration: none; border-radius: 8px; font-weight: bold;">
                Log In
            </a>
        </p>
        <hr style="border: none; border-top: 1px solid #E5E7EB; margin: 20px 0;">
        <p style="color: #9CA3AF; font-size: 12px;">— Smile Guide AI Team</p>
    </div>
    """

    try:
        message = MIMEMultipart("alternative")
        message["Subject"] = subject
        message["From"] = settings.EMAIL_FROM
        message["To"] = to_email
        message.attach(MIMEText(body, "html"))

        await aiosmtplib.send(
            message,
            hostname=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USER,
            password=settings.SMTP_PASSWORD,
            start_tls=True,
        )
        logger.info(f"[EMAIL] Role change email sent to {to_email}")
    except Exception as e:
        logger.error(f"[EMAIL ERROR] Could not send role change email to {to_email}: {e}")
