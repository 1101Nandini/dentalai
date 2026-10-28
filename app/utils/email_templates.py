from pathlib import Path
from app.config import settings

_TEMPLATES_DIR = Path(__file__).parent/ ".." / "templates" 


def _load(filename: str) -> str:
    return (_TEMPLATES_DIR / filename).read_text(encoding="utf-8")


def _inject(template: str, otp: str, expiry_minutes: int) -> str:
    """Inject OTP digits individually and expiry time into the template."""
    # Pad/truncate to exactly 6 digits for safety
    digits = otp.ljust(6)[:6]
    result = template
    for i, digit in enumerate(digits, start=1):
        result = result.replace(f"{{d{i}}}", digit)
    result = result.replace("{OTP_EXPIRY_MINUTES}", str(expiry_minutes))
    result = result.replace("{FRONTEND_URL}", settings.FRONTEND_URL)
    return result


def render_register_otp(otp: str, expiry_minutes: int) -> str:
    """Return the branded HTML email body for the registration OTP."""
    return _inject(_load("otp_register.html"), otp, expiry_minutes)


def render_forgot_password_otp(otp: str, expiry_minutes: int) -> str:
    """Return the branded HTML email body for the forgot-password OTP."""
    return _inject(_load("otp_forgot_password.html"), otp, expiry_minutes)


def render_pending_approval(user_name: str, user_email: str, account_type: str) -> str:
    """Return the branded HTML email body for pending approvals sent to super admins."""
    template = _load("pending_approval.html")
    template = template.replace("{USER_NAME}", user_name)
    template = template.replace("{USER_EMAIL}", user_email)
    template = template.replace("{ACCOUNT_TYPE}", account_type)
    template = template.replace("{FRONTEND_URL}", settings.FRONTEND_URL)
    return template

def render_rejection_email(user_name: str) -> str:
    """Return the branded HTML email body for rejected registrations."""
    template = _load("rejection.html")
    template = template.replace("{USER_NAME}", user_name)
    template = template.replace("{FRONTEND_URL}", settings.FRONTEND_URL)
    return template

def render_promo_expiring_email(user_name: str, days_left: int, action_url: str) -> str:
    """Return the branded HTML email body for promo/trial expiration reminder."""
    template = _load("promo_expiring.html")
    template = template.replace("{{ user_name }}", user_name)
    template = template.replace("{{ days_left }}", str(days_left))
    template = template.replace("{{ action_url }}", action_url)
    return template