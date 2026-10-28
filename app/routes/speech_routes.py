from fastapi import APIRouter, Depends
from app.services.speech_service import SpeechService
from app.dependencies import get_current_user
from app.core.response import api_response

router = APIRouter()

@router.get("/token")
async def get_speech_token(current_user=Depends(get_current_user)):
    """
    Get an Azure Speech access token.
    Requires authentication.
    """
    result = await SpeechService.get_speech_token()
    
    return api_response(
        success=True,
        message="Azure Speech token fetched successfully.",
        data=result
    )
