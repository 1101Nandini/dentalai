import httpx
import logging
from fastapi import HTTPException
from app.config import settings

logger = logging.getLogger(__name__)

class SpeechService:
    @staticmethod
    async def get_speech_token():
        """
        Fetch a short-lived access token from Azure Speech service.
        """
        if not settings.AZURE_SPEECH_KEY or not settings.AZURE_SPEECH_TOKEN_ENDPOINT:
            logger.error("Azure Speech credentials are not configured.")
            raise HTTPException(
                status_code=500,
                detail="Azure Speech credentials are not configured on the server."
            )

        headers = {
            "Ocp-Apim-Subscription-Key": settings.AZURE_SPEECH_KEY,
            "Content-Type": "application/x-www-form-urlencoded"
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    settings.AZURE_SPEECH_TOKEN_ENDPOINT,
                    headers=headers,
                    timeout=10.0
                )

                if response.status_code != 200:
                    logger.error(f"Azure Speech token request failed: {response.status_code} - {response.text}")
                    raise HTTPException(
                        status_code=response.status_code,
                        detail="Failed to fetch Azure Speech token."
                    )

                token = response.text
                
                # Extract region if possible from the token endpoint URL
                # Standard endpoint: https://<region>.api.cognitive.microsoft.com/sts/v1.0/issueToken
                region = "eastus" # fallback
                if ".api.cognitive.microsoft.com" in settings.AZURE_SPEECH_TOKEN_ENDPOINT:
                    try:
                        region = settings.AZURE_SPEECH_TOKEN_ENDPOINT.split("//")[1].split(".")[0]
                    except Exception:
                        pass
                
                return {
                    "token": token,
                    "region": region,
                    "endpoint": settings.AZURE_SPEECH_ENDPOINT
                }

        except httpx.RequestError as exc:
            logger.error(f"An error occurred while requesting Azure Speech token: {exc}")
            raise HTTPException(
                status_code=500,
                detail="Communication error with Azure Speech service."
            )
