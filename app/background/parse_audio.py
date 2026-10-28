import httpx
import asyncio
import logging
import os
import filetype
from datetime import datetime, timezone
from bson import ObjectId
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from app.config import settings
from app.core.database import get_database
from app.core.constants import DocumentStatus
from app.utils.blob_utils import download_blob

logger = logging.getLogger(__name__)


async def parse_audio(document_id: str, blob_name: str) -> None:
    """
    Phase 1: Transcribe audio file using Azure OpenAI Whisper (REST).

    - Downloads audio bytes from Azure blob
    - Sends to Azure OpenAI Whisper endpoint (with retries for rate limits)
    - Automatically detects audio format from file content
    - Saves transcript to the document
    """
    db = await get_database()

    doc = await db["documents"].find_one({"_id": ObjectId(document_id)})
    if not doc:
        raise RuntimeError(f"Document {document_id} not found")

    filename = doc.get("original_filename", "audio.mp3")
    
    # ── 1. Download Audio Bytes ───────────────────────────────────────────
    logger.info(f"📥 Downloading audio bytes: {blob_name}")
    audio_bytes = await download_blob(blob_name)

    # ── 2. Submit to Azure OpenAI Whisper ─────────────────────────────────
    logger.info(f"📡 Transcribing via Azure OpenAI Whisper: {filename}")
    transcript = await _transcribe_audio_whisper(audio_bytes, filename)

    # ── 3. Save transcript on document ────────────────────────────────────
    await db["documents"].update_one(
        {"_id": ObjectId(document_id)},
        {"$set": {
            "extracted_text": transcript,
            "status": DocumentStatus.PARSED,
            "updated_at": datetime.now(timezone.utc),
        }}
    )

    print(f"\n--- DEBUG TRANSCRIPT START ({len(transcript)} chars) ---\n{transcript}\n--- DEBUG TRANSCRIPT END ---\n")
    logger.info(f"✅ Audio transcribed: {len(transcript)} chars for doc {document_id}")


def _should_retry(exception):
    """Retry on Rate Limit (429) or Server Errors (5xx)."""
    if isinstance(exception, httpx.HTTPStatusError):
        # Retry on rate limit (429) or common server issues (5xx)
        return exception.response.status_code == 429 or exception.response.status_code >= 500
    # Retry on connectivity issues
    return isinstance(exception, (httpx.ConnectError, httpx.TimeoutException))


@retry(
    stop=stop_after_attempt(6),
    wait=wait_exponential(multiplier=15, min=15, max=180), # Wait longer for 3 RPM limit
    retry=retry_if_exception(_should_retry),
    before_sleep=lambda rs: logger.warning(f"⚠️ Whisper API busy/failed (Attempt {rs.attempt_number}). Retrying in {rs.next_action.sleep}s...")
)
async def _transcribe_audio_whisper(audio_bytes: bytes, filename: str) -> str:
    """Submit a transcription job to Azure OpenAI Whisper via REST with retries."""
    base_url = settings.AZURE_OPENAI_ENDPOINT.rstrip("/")
    deployment = settings.AZURE_OPENAI_WHISPER_DEPLOYMENT
    api_version = "2024-06-01"
    
    url = f"{base_url}/openai/deployments/{deployment}/audio/transcriptions?api-version={api_version}"
    
    headers = {
        "api-key": settings.AZURE_OPENAI_API_KEY,
    }
    
    # ── Detect MIME type from bytes (Supports all audio types) ──────────
    kind = filetype.guess(audio_bytes)
    mime_type = kind.mime if kind else "audio/mpeg" # fallback
    logger.debug(f"🔍 Detected audio type: {mime_type}")

    files = {
        "file": (filename, audio_bytes, mime_type)
    }
    
    data = {
        "response_format": "json"
    }

    async with httpx.AsyncClient() as client:
        # Long timeout (10 mins) as Whisper can take time for long audio
        response = await client.post(url, headers=headers, files=files, data=data, timeout=600.0)
        
        if response.status_code != 200:
            # Raise so tenacity can catch and retry
            response.raise_for_status()
        
        return response.json().get("text", "")
