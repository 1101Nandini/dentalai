"""
Background worker: Email tasks
Sends transactional emails (welcome, password reset, subscription alerts).
Integrates with SMTP via aiosmtplib or can be wired to Azure Communication Services.
"""
import aiosmtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from app.config import settings


async def send_email(to: str, subject: str, body_html: str) -> None:
    """Send a transactional HTML email."""
    message = MIMEMultipart("alternative")
    message["Subject"] = subject
    message["From"] = settings.EMAIL_FROM
    message["To"] = to
    message.attach(MIMEText(body_html, "html"))

    await aiosmtplib.send(
        message,
        hostname=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        start_tls=True,
    )


async def send_welcome_email(to: str, full_name: str) -> None:
    body = f"""
    <h2>Welcome to Smile Guide AI, {full_name}!</h2>
    <p>Your account has been created. You have <b>10 free queries</b> to get started.</p>
    <p>Upgrade your plan anytime to unlock more queries.</p>
    """
    await send_email(to, "Welcome to Smile Guide AI – Dental Assistant", body)


async def send_query_limit_email(to: str, plan: str) -> None:
    body = f"""
    <h2>Query Limit Reached</h2>
    <p>You have exhausted your <b>{plan}</b> plan queries.</p>
    <p>Please upgrade your subscription to continue using Smile Guide AI.</p>
    """
    await send_email(to, "Smile Guide AI – Query Limit Reached", body)


async def notify_super_admins_pending_approval(user_email: str, user_name: str, account_type: str) -> None:
    from app.core.database import get_database
    from app.core.constants import COLLECTION_USERS, UserRole
    
    db = await get_database()
    super_admins = await db[COLLECTION_USERS].find({"role": UserRole.SUPER_ADMIN, "is_active": True}).to_list(length=None)
    
    from app.utils.email_templates import render_pending_approval
    body = render_pending_approval(user_name, user_email, account_type)
    
    for admin in super_admins:
        if admin.get("email"):
            try:
                await send_email(admin["email"], "Action Required: New Account Pending Approval", body)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Could not send pending approval email to {admin['email']}: {e}")

async def send_rejection_email(to_email: str, full_name: str) -> None:
    """Notify user that their registration request was declined."""
    from app.utils.email_templates import render_rejection_email
    body = render_rejection_email(full_name)
    try:
        await send_email(to_email, "Smile Guide AI – Registration Update", body)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Could not send rejection email to {to_email}: {e}")

async def send_subscription_expiring_email(to_email: str, user_name: str, days_left: int, action_url: str) -> None:
    """Notify user that their trial / promo period expires in a few days so they can add a payment method."""
    from app.utils.email_templates import render_promo_expiring_email
    body = render_promo_expiring_email(user_name, days_left, action_url)
    try:
        await send_email(to_email, f"Action Required: Your Smile Guide AI Free Period Expires in {days_left} Days", body)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"Could not send promo expiring email to {to_email}: {e}")

