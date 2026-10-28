import httpx
from fastapi import HTTPException, status
from app.config import settings

async def verify_recaptcha(token: str) -> bool:
    """
    Verifies the reCAPTCHA v2 token with Google's API.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reCAPTCHA token is missing."
        )

    # Google's verification URL
    url = "https://www.google.com/recaptcha/api/siteverify"
    payload = {
        "secret": settings.RECAPTCHA_SECRET_KEY,
        "response": token
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(url, data=payload)
        result = response.json()

    if not result.get("success"):
        # For security, you can log 'result.get("error-codes")' here
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="reCAPTCHA verification failed. Please try again."
        )

    return True