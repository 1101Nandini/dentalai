"""
Voice Stream Service — WebSocket-based real-time voice transcription.

Flow:
  1. Frontend opens WebSocket connection
  2. Sends binary audio chunks as user speaks (browser MediaRecorder)
  3. Backend buffers audio chunks
  4. On "transcribe" command → sends buffered audio to Whisper → returns interim transcript
  5. On "stop" command → sends full accumulated audio to Whisper → returns final transcript
  6. Saves the full audio recording to Azure Blob Storage
  7. On "lookup" command → runs CDT code lookup on the transcript → returns suggestions

WebSocket Protocol:
  Client → Server:
    - Binary frames: raw audio data chunks
    - Text frames (JSON):
        {"action": "transcribe"}        — request interim transcript from buffered audio
        {"action": "stop"}              — finalize recording, get final transcript
        {"action": "lookup"}            — run CDT lookup on the current transcript
        {"action": "lookup_and_save"}   — run CDT lookup + save session to DB

  Server → Client:
    - Text frames (JSON):
        {"type": "interim", "transcript": "..."}
        {"type": "final",   "transcript": "...", "audio_saved": true}
        {"type": "lookup",  "mode": "...", "entries": [...]}
        {"type": "error",   "message": "..."}
        {"type": "status",  "message": "..."}
"""
import asyncio
import re
import json
import logging
import time
import uuid
import os
import subprocess

import httpx

from app.config import settings
from app.core.database import get_database
from app.utils.file_utils import upload_blob, create_append_blob, append_to_blob, read_blob
from app.services.cdt_service import CDTService
from app.services.cdt_service_direct import CDTServiceDirect


logger = logging.getLogger(__name__)

# ── Whisper Hallucination Patterns ────────────────────────────────────────────
# Regex to detect repetitive filler sequences that Whisper hallucinates
# when audio is silent, unclear, or very low volume.
_FILLER_WORDS = r"(?:Okay|Yeah|Yep|Yes|No|Good|Mm-hmm|Mm|Mhm|Hmm|Right|Uh-huh|Uh|Um|Exactly|Sure|All right|Alright)"
# Matches 3+ consecutive repetitions of filler words separated by punctuation/whitespace
_HALLUCINATION_PATTERN = re.compile(
    rf"(?:[\s]*{_FILLER_WORDS}[\s.,!?]*){{{3},}}",
    re.IGNORECASE,
)


def _clean_whisper_hallucinations(text: str) -> str:
    """
    Remove hallucinated repetitive filler sequences from Whisper output.

    Whisper tends to generate long runs of "Okay. Okay. Okay." or
    "Yeah. Yeah. Yeah." when audio is silent or unclear. This function
    detects runs of 4+ consecutive filler words and collapses them.
    Also collapses any word or number repeating 3 or more times consecutively.
    """
    if not text:
        return text

    def _collapse(match: re.Match) -> str:
        """Keep at most 2 instances of the filler word from a long run."""
        segment = match.group(0).strip()
        # Extract the individual filler words from the matched segment
        words = re.findall(_FILLER_WORDS, segment, re.IGNORECASE)
        if len(words) >= 4:
            # Keep the first two unique fillers only
            seen = []
            for w in words:
                if w.capitalize() not in seen:
                    seen.append(w.capitalize())
                if len(seen) >= 2:
                    break
            return ". ".join(seen) + "."
        return segment

    lines = text.splitlines()
    cleaned_lines = []
    for line in lines:
        if not line.strip():
            cleaned_lines.append("")
            continue

        line_cleaned = _HALLUCINATION_PATTERN.sub(_collapse, line)

        # Collapse general 3+ consecutive word/number repetitions (case-insensitive)
        words = line_cleaned.split()
        if words:
            cleaned_words = []
            i = 0
            n = len(words)
            while i < n:
                word = words[i]
                # Strip punctuation for comparison
                clean_w = re.sub(r'[^\w-]', '', word).lower()
                if not clean_w:
                    cleaned_words.append(word)
                    i += 1
                    continue
                    
                # Count consecutive repetitions
                count = 1
                j = i + 1
                while j < n:
                    next_w = re.sub(r'[^\w-]', '', words[j]).lower()
                    if next_w == clean_w:
                        count += 1
                        j += 1
                    else:
                        break
                        
                if count >= 3:
                    cleaned_words.append(word)
                    i = j
                else:
                    cleaned_words.append(word)
                    i += 1
            line_cleaned = " ".join(cleaned_words)
            
        cleaned_lines.append(line_cleaned.rstrip())

    # Rejoin lines with original breaks
    cleaned = "\n".join(cleaned_lines).rstrip()
    return cleaned



def _clean_gemini_transcript(text: str) -> str:
    """
    Remove hallucinated repeated lines from Gemini STT output.

    Gemini tends to generate the same speaker line dozens or hundreds of times
    when the audio contains a brief pause or repeated-sounding segment. This
    function detects runs of 3+ identical consecutive statements (A A A) OR
    alternating loops (A B A B A B) and collapses them to a maximum of 2 cycles.
    """
    if not text:
        return text

    import re
    lines = text.splitlines()
    deduplicated: list[str] = []
    history: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            # Preserve blank lines that separate sections
            if deduplicated and deduplicated[-1] != "":
                deduplicated.append("")
            continue

        # Strip standard speaker tags for comparison
        text_only = re.sub(r"^\*\*(?:Dentist|Patient|Assistant):\*\*\s*", "", stripped, flags=re.IGNORECASE)
        text_only = re.sub(r"^(?:Dentist|Patient|Assistant):\s*", "", text_only, flags=re.IGNORECASE)
        
        # Normalise: lowercase, collapse internal whitespace, strip punctuation
        normalised = re.sub(r"[^\w\s]", "", text_only.lower())
        normalised = re.sub(r"\s+", " ", normalised).strip()

        history.append(normalised)
        deduplicated.append(stripped)

        # Check for A-B A-B loop (alternating hallucination)
        if len(history) >= 6:
            if history[-1] == history[-3] == history[-5] and history[-2] == history[-4] == history[-6]:
                # We detected A B A B A B. Drop the last 2 lines.
                history.pop()
                history.pop()
                popped = 0
                while popped < 2 and deduplicated:
                    if deduplicated[-1] == "":
                        deduplicated.pop()
                    else:
                        deduplicated.pop()
                        popped += 1
                # clear trailing blanks
                while deduplicated and deduplicated[-1] == "":
                    deduplicated.pop()
                continue
                
        # Check for A A A loop (single line hallucination)
        if len(history) >= 3:
            if history[-1] == history[-2] == history[-3]:
                # We detected A A A. Drop the last line.
                history.pop()
                popped = 0
                while popped < 1 and deduplicated:
                    if deduplicated[-1] == "":
                        deduplicated.pop()
                    else:
                        deduplicated.pop()
                        popped += 1
                while deduplicated and deduplicated[-1] == "":
                    deduplicated.pop()
                continue

    # Filter out any internal empty strings before joining, then join with double newlines
    # to guarantee proper markdown rendering in the UI.
    final_lines = [line for line in deduplicated if line != ""]
    return "\n\n".join(final_lines).strip()


# Preprocessing (VAD, Demucs, Whisper) removed, replaced by Gemini STT


class VoiceStreamService:
    """Manages a single WebSocket voice streaming session."""

    def __init__(self, user_id: str, stream_id: str | None = None, organization_id: str | None = None):
        self.user_id = user_id
        self.organization_id = organization_id
        self.stream_id = stream_id or str(uuid.uuid4())
        self.session_id: str | None = None
        self.session_name: str | None = None
        self.template_ids: list[str] | None = None
        self.xray_selection: list[str] | None = None
        self.processing_stage: str = "pending_remux"  # pending_remux -> remux_completed -> ai_processing -> completed
        self.audio_blob_name = f"audio_sessions/{self.user_id}/stream_{self.stream_id}.webm"
        self.transcript = ""             # Diarized transcript (DR/PATIENT tags)
        self.raw_transcript = ""           # Raw Whisper output (flat text)
        self.cdt_transcript = ""           # Raw transcript with CDT codes injected inline
        self.original_transcript = ""      # Transcript provided by the frontend client (if any)
        self.session_start = time.perf_counter()
        self.total_bytes_appended = 0
        self.audio_duration_seconds = None

    async def initialize(self):
        """Creates the append blob in Azure if it doesn't already exist and recovers existing DB session."""
        try:
            # We blindly create it. If it exists, Azure handles it or we can ignore ResourceExistsError
            await create_append_blob(self.audio_blob_name)
        except Exception as e:
            logger.warning(f"[VOICE-WS] Blob might already exist or error: {e}")

        # If stream_id is provided, check if a session already exists in DB to resume seamlessly
        if self.stream_id:
            try:
                db = await get_database()
                existing = await db["dental_voice_sessions"].find_one({"stream_id": self.stream_id, "user_id": self.user_id})
                if existing:
                    self.session_id = str(existing["_id"])
                    if not self.session_name and existing.get("session_name"):
                        self.session_name = existing["session_name"]
                    if not self.template_ids and existing.get("template_ids"):
                        self.template_ids = existing["template_ids"]
                    if not self.xray_selection and existing.get("xray_selection"):
                        self.xray_selection = existing["xray_selection"]
                    if existing.get("processing_stage"):
                        self.processing_stage = existing["processing_stage"]
                    logger.info(
                        f"[VOICE-WS] Resumed existing DB session {self.session_id} for stream {self.stream_id} "
                        f"(session_name='{self.session_name}', stage='{self.processing_stage}')"
                    )
            except Exception as db_err:
                logger.warning(f"[VOICE-WS] Failed to check existing DB session on init: {db_err}")

    # ── Audio Buffering (Azure Append Blob) ──────────────────────────────

    async def add_audio_chunk(self, chunk: bytes):
        """Buffer an incoming audio chunk directly to Azure Append Blob."""
        await append_to_blob(self.audio_blob_name, chunk)
        self.total_bytes_appended += len(chunk)

    async def get_buffered_audio(self) -> bytes:
        """Get all buffered audio from Azure as a single byte string."""
        try:
            audio_data = await read_blob(self.audio_blob_name)
            if audio_data:
                self.total_bytes_appended = max(self.total_bytes_appended, len(audio_data))
            return audio_data
        except Exception as e:
            logger.warning(f"[VOICE-WS] Could not read blob {self.audio_blob_name}: {e}")
            return b""

    def get_buffer_size(self) -> int:
        """Get current buffer size in bytes."""
        return self.total_bytes_appended

    # ── Whisper Transcription ────────────────────────────────────────────

    async def transcribe_buffer(self) -> str:
        """
        Send buffered audio to Azure Speech Batch API for interim transcription.
        Does NOT clear the buffer — audio keeps accumulating.
        """
        audio_data = await self.get_buffered_audio()
        if not audio_data or len(audio_data) < 1000:
            # Too little audio to transcribe (< 1KB)
            return self.transcript

        try:
            from app.utils.file_utils import generate_sas_url
            audio_url = generate_sas_url(self.audio_blob_name)
            transcript = await self._call_gemini_stt(audio_url)
            self.transcript = _clean_whisper_hallucinations(transcript)
            return self.transcript
        except Exception as e:
            logger.error(f"[VOICE-WS] Interim transcription failed: {e}")
            raise

    async def remux_audio(self) -> bytes | None:
        """
        Generates a SAS URL for the audio blob, downloads it via URL, 
        remuxes it with ffmpeg to fix duration/seek metadata, 
        and replaces the original blob with the remuxed audio.
        """
        from app.utils.file_utils import generate_sas_url
        try:
            # 1. Generate SAS URL and download the audio
            audio_url = generate_sas_url(self.audio_blob_name)
            logger.info(f"[VOICE-WS] Downloading audio for remux from URL: {audio_url.split('?')[0]}")
            
            import tempfile
            import os
            
            # Create a workspace-local temp directory to comply with workspace restrictions
            workspace_temp_dir = os.path.join(os.getcwd(), "temp_remux")
            os.makedirs(workspace_temp_dir, exist_ok=True)
            
            # Create temporary files for input and output
            temp_in = tempfile.NamedTemporaryFile(dir=workspace_temp_dir, suffix="_in.webm", delete=False)
            temp_out = tempfile.NamedTemporaryFile(dir=workspace_temp_dir, suffix="_out.webm", delete=False)
            
            try:
                # Download the audio in chunks and stream it directly to the input temp file
                total_bytes = 0
                async with httpx.AsyncClient() as client:
                    # Disable read timeout so large downloads do not trigger ReadTimeout
                    timeout_config = httpx.Timeout(connect=60.0, read=None, write=60.0, pool=60.0)
                    async with client.stream("GET", audio_url, timeout=timeout_config) as response:
                        if response.status_code != 200:
                            logger.error(f"[VOICE-WS] Failed to download audio from URL: {response.status_code}")
                            return None
                        async for chunk in response.aiter_bytes():
                            temp_in.write(chunk)
                            total_bytes += len(chunk)
                
                temp_in.close()
                temp_out.close() # Close output file descriptor so ffmpeg can write to it

                if total_bytes < 1000:
                    logger.warning("[VOICE-WS] Audio too short to remux")
                    return None
                
                # Probe codec using ffprobe
                codec = "opus"  # default fallback
                try:
                    probe_process = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error",
                        "-select_streams", "a:0",
                        "-show_entries", "stream=codec_name",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        temp_in.name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await probe_process.communicate()
                    if probe_process.returncode == 0 and stdout:
                        codec = stdout.decode().strip().lower()
                    logger.info(f"[VOICE-WS] Probed audio codec: {codec}")
                except Exception as probe_err:
                    logger.error(f"[VOICE-WS] Failed to probe audio codec: {probe_err}")

                # Build ffmpeg arguments based on codec
                if codec == "opus":
                    logger.info("[VOICE-WS] Using stream copy for Opus stream")
                    ffmpeg_args = [
                        "ffmpeg", "-y",
                        "-i", temp_in.name,
                        "-c:a", "copy",
                        "-cues_to_front", "true",
                        temp_out.name
                    ]
                else:
                    logger.info(f"[VOICE-WS] Codec is {codec}. Transcoding to Opus using libopus")
                    ffmpeg_args = [
                        "ffmpeg", "-y",
                        "-i", temp_in.name,
                        "-c:a", "libopus",
                        "-b:a", "128k",
                        "-cues_to_front", "true",
                        temp_out.name
                    ]

                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout_err = await process.communicate()
                
                if process.returncode == 0:
                    # Probe duration using ffprobe
                    try:
                        duration_process = await asyncio.create_subprocess_exec(
                            "ffprobe", "-v", "error",
                            "-show_entries", "format=duration",
                            "-of", "default=noprint_wrappers=1:nokey=1",
                            temp_out.name,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE
                        )
                        d_stdout, d_stderr = await duration_process.communicate()
                        if duration_process.returncode == 0 and d_stdout:
                            self.audio_duration_seconds = float(d_stdout.decode().strip())
                            logger.info(f"[VOICE-WS] Final audio duration probed successfully: {self.audio_duration_seconds}s")
                    except Exception as dur_err:
                        logger.error(f"[VOICE-WS] Failed to probe final audio duration: {dur_err}")

                    # Read the remuxed bytes from the output temp file
                    with open(temp_out.name, "rb") as f:
                        remuxed_data = f.read()
                        
                    # 3. Create a backup of the original append blob by renaming it (moves to .backup)
                    from app.utils.file_utils import move_blob, delete_blob
                    backup_blob_name = f"{self.audio_blob_name}.backup"
                    
                    try:
                        logger.info(f"[VOICE-WS] Backing up original blob to: {backup_blob_name}")
                        await move_blob(self.audio_blob_name, backup_blob_name)
                    except Exception as backup_err:
                        logger.error(f"[VOICE-WS] Failed to create backup blob: {backup_err}. Aborting replacement to prevent data loss.")
                        return None
    
                    # 4. Upload the fixed WebM (as a standard block blob)
                    # Retry logic to handle potential Azure Blob Storage eventual consistency issues (InvalidBlobType)
                    max_retries = 3
                    retry_delay = 0.5
                    uploaded_url = None
                    for attempt in range(1, max_retries + 1):
                        try:
                            uploaded_url = await upload_blob(self.audio_blob_name, remuxed_data, "audio/webm")
                            logger.info(f"[VOICE-WS] Audio remuxed and replaced successfully: {self.audio_blob_name}")
                            self.processing_stage = "remux_completed"
                            if self.session_id:
                                try:
                                    db = await get_database()
                                    from bson import ObjectId
                                    from datetime import datetime, timezone
                                    await db["dental_voice_sessions"].update_one(
                                        {"_id": ObjectId(self.session_id)},
                                        {"$set": {
                                            "processing_stage": "remux_completed",
                                            "updated_at": datetime.now(timezone.utc)
                                        }}
                                    )
                                except Exception as err:
                                    logger.warning(f"[VOICE-WS] Failed to update remux_completed state in DB: {err}")
                            break
                        except Exception as upload_err:
                            logger.warning(
                                f"[VOICE-WS] Failed to upload remuxed audio (attempt {attempt}/{max_retries}): {upload_err}"
                            )
                            if attempt < max_retries:
                                await asyncio.sleep(retry_delay)
                            else:
                                raise upload_err
                                
                    # 5. Clean up the backup blob on success
                    try:
                        await delete_blob(backup_blob_name)
                    except Exception as del_err:
                        logger.warning(f"[VOICE-WS] Failed to clean up backup blob {backup_blob_name}: {del_err}")
                        
                    return remuxed_data
                else:
                    stderr_msg = stdout_err[1].decode() if stdout_err else "Unknown error"
                    logger.error(f"[VOICE-WS] ffmpeg remux failed with code {process.returncode}: {stderr_msg}")
                    self.processing_stage = "remux_failed"
                    if self.session_id:
                        try:
                            db = await get_database()
                            from bson import ObjectId
                            from datetime import datetime, timezone
                            await db["dental_voice_sessions"].update_one(
                                {"_id": ObjectId(self.session_id)},
                                {"$set": {
                                    "status": "failed",
                                    "processing_stage": "remux_failed",
                                    "error_message": f"FFmpeg remux failed with code {process.returncode}: {stderr_msg}",
                                    "updated_at": datetime.now(timezone.utc)
                                }}
                            )
                        except Exception as update_err:
                            logger.error(f"[VOICE-WS] Failed to update session error state for remux: {update_err}")
                    return None
            finally:
                # Clean up temp files
                for temp_file_path in (temp_in.name, temp_out.name):
                    try:
                        if os.path.exists(temp_file_path):
                            os.remove(temp_file_path)
                    except Exception as clean_err:
                        logger.warning(f"[VOICE-WS] Failed to delete temp file {temp_file_path}: {clean_err}")
                # Clean up temp directory
                try:
                    if os.path.exists(workspace_temp_dir) and not os.listdir(workspace_temp_dir):
                        os.rmdir(workspace_temp_dir)
                except Exception:
                    pass
        except FileNotFoundError:
            logger.error(
                "[VOICE-WS] 'ffmpeg' command not found! Please install ffmpeg on your system "
                "(e.g., 'sudo apt-get install ffmpeg') to support audio remuxing."
            )
            self.processing_stage = "remux_failed"
            return None
        except Exception as e:
            logger.error(f"[VOICE-WS] Error in remux_audio: {e}")
            self.processing_stage = "remux_failed"
            if self.session_id:
                try:
                    db = await get_database()
                    from bson import ObjectId
                    from datetime import datetime, timezone
                    await db["dental_voice_sessions"].update_one(
                        {"_id": ObjectId(self.session_id)},
                        {"$set": {
                            "status": "failed",
                            "processing_stage": "remux_failed",
                            "error_message": f"FFmpeg remux failed: {e}",
                            "updated_at": datetime.now(timezone.utc)
                        }}
                    )
                except Exception as update_err:
                    logger.error(f"[VOICE-WS] Failed to update session error state for remux: {update_err}")
            return None

    async def finalize(self) -> str:
        """
        Finalize recording: transcribe full audio using Gemini STT API.
        Checks processing_stage state:
        - If remuxing was already completed ('remux_completed'), skips remuxing and resumes at AI stage.
        - If remuxing failed or was not completed, remuxes audio first before moving to AI stage.
        """
        # Create a pending DB entry before remuxing so we can recover if anything fails
        try:
            await self.create_pending_session()
        except Exception as err:
            logger.error(f"[VOICE-WS] Failed to create pending session entry before remuxing: {err}")

        # Check DB session stage if self.session_id is set and local stage is pending_remux
        if self.session_id and self.processing_stage == "pending_remux":
            try:
                db = await get_database()
                from bson import ObjectId
                doc = await db["dental_voice_sessions"].find_one({"_id": ObjectId(self.session_id)})
                if doc and doc.get("processing_stage") == "remux_completed":
                    self.processing_stage = "remux_completed"
            except Exception as db_err:
                logger.warning(f"[VOICE-WS] Failed to fetch session stage from DB: {db_err}")

        # Step 1: Remux Audio (if not already completed)
        if self.processing_stage == "remux_completed":
            logger.info(f"[VOICE-WS] Audio remuxing was already completed for session {self.session_id}. Skipping ffmpeg remux and moving directly to AI stage.")
        else:
            logger.info(f"[VOICE-WS] Starting audio remux for session {self.session_id} (stage: {self.processing_stage})...")
            remux_res = await self.remux_audio()
            if self.processing_stage == "remux_failed" or remux_res is None:
                logger.error(f"[VOICE-WS] Audio remuxing failed for session {self.session_id}. Cannot proceed to AI stage.")
                raise RuntimeError("Audio remuxing failed.")
        
        # Verify that we have some audio recorded
        audio_data = await self.get_buffered_audio()
        if not audio_data or len(audio_data) < 1000:
            logger.warning("[VOICE-WS] No meaningful audio to finalize")
            self.raw_transcript = ""
            self.transcript = ""
            return ""

        # Step 2: Final transcription with complete audio using Gemini STT (AI stage)
        try:
            self.processing_stage = "ai_processing"
            if self.session_id:
                try:
                    db = await get_database()
                    from bson import ObjectId
                    from datetime import datetime, timezone
                    await db["dental_voice_sessions"].update_one(
                        {"_id": ObjectId(self.session_id)},
                        {"$set": {
                            "processing_stage": "ai_processing",
                            "updated_at": datetime.now(timezone.utc)
                        }}
                    )
                except Exception as db_err:
                    logger.warning(f"[VOICE-WS] Failed to update stage to ai_processing in DB: {db_err}")

            from app.utils.file_utils import generate_sas_url
            audio_url = generate_sas_url(self.audio_blob_name)
            logger.info(f"[VOICE-WS] Submitting Gemini STT transcription for {self.audio_blob_name}")
            self.raw_transcript = await asyncio.wait_for(self._call_gemini_stt(audio_url), timeout=120.0)
            self.transcript = _clean_whisper_hallucinations(self.raw_transcript)
            self.processing_stage = "transcription_completed"

            # Persist transcript immediately to DB so it is never lost if subsequent steps fail
            if self.session_id:
                try:
                    db = await get_database()
                    from bson import ObjectId
                    from datetime import datetime, timezone
                    await db["dental_voice_sessions"].update_one(
                        {"_id": ObjectId(self.session_id)},
                        {"$set": {
                            "raw_transcript": self.raw_transcript,
                            "transcript": self.transcript,
                            "original_transcript": self.original_transcript,
                            "processing_stage": "transcription_completed",
                            "updated_at": datetime.now(timezone.utc)
                        }}
                    )
                    logger.info(f"[VOICE-WS] Persisted transcript ({len(self.transcript)} chars) to DB session {self.session_id}")
                except Exception as db_err:
                    logger.warning(f"[VOICE-WS] Failed to persist transcript to DB: {db_err}")
        except Exception as e:
            logger.error(f"[VOICE-WS] Final transcription (AI stage) failed: {e}")
            self.processing_stage = "ai_failed"
            if self.session_id:
                try:
                    db = await get_database()
                    from bson import ObjectId
                    from datetime import datetime, timezone
                    await db["dental_voice_sessions"].update_one(
                        {"_id": ObjectId(self.session_id)},
                        {"$set": {
                            "status": "failed",
                            "processing_stage": "ai_failed",
                            "error_message": f"AI STT transcription failed: {e}",
                            "updated_at": datetime.now(timezone.utc)
                        }}
                    )
                except Exception as update_err:
                    logger.error(f"[VOICE-WS] Failed to update session error state: {update_err}")
            raise

        return self.transcript

    # ── CDT Lookup ───────────────────────────────────────────────────────

    async def lookup_cdt(self) -> dict:
        """
        Run CDT code lookup on the current transcript.
        Auto-transcribes buffered audio if no transcript exists yet.

        After lookup, injects CDT codes back into the transcript text.
        """
        # Auto-transcribe if we have audio but no transcript
        if self.raw_transcript is None or self.raw_transcript == "":
            logger.info("[VOICE-WS] Auto-transcribing audio from blob before lookup...")
            await self.finalize()

        if not self.raw_transcript or not self.raw_transcript.strip():
            self.cdt_transcript = ""
            return {"mode": "empty", "entries": []}

        import re

        # ── Clean previous CDT injection artifacts ───────────────────────
        clean_transcript = self.raw_transcript
        # Strip CDT code blocks — handle nested parentheses by matching outermost balanced parens
        # containing a CDT code pattern (D followed by 4 digits)
        def _strip_cdt_parens(text: str) -> str:
            """Remove balanced parenthetical groups that contain CDT codes (Dxxxx)."""
            result = []
            i = 0
            while i < len(text):
                if text[i] == '(' and i + 1 < len(text) and re.match(r'D\d{4}', text[i+1:]):
                    # Found opening paren followed by CDT code — find balanced close
                    depth = 1
                    j = i + 1
                    while j < len(text) and depth > 0:
                        if text[j] == '(':
                            depth += 1
                        elif text[j] == ')':
                            depth -= 1
                        j += 1
                    # Skip the entire balanced group (and any leading whitespace)
                    while result and result[-1] == ' ':
                        result.pop()
                    i = j
                else:
                    result.append(text[i])
                    i += 1
            return ''.join(result)
        
        clean_transcript = _strip_cdt_parens(clean_transcript)
        # Also strip multi-code semicolon groups: (D3310 - ...; D2950 - ...; D2740 - ...)
        # These might have been partially stripped, clean up remaining fragments
        clean_transcript = re.sub(r'\s*;\s*D\d{4}\s*-\s*[^,)]*', '', clean_transcript)
        clean_transcript = re.sub(r'\s*\[D\d{4}\s*-\s*[^\]]*\]', '', clean_transcript)
        clean_transcript = re.sub(r'\s*\[[^\]]{5,}\]', '', clean_transcript)
        clean_transcript = re.sub(r'\s+code\s+D\d{4}', ' code', clean_transcript)
        # Strip surface expansion artifacts from previous injections: (Mesial Lingual), (Distal Occlusal), etc.
        clean_transcript = re.sub(
            r'\s*\((?:Mesial|Occlusal|Distal|Buccal|Lingual|Incisal|Facial)(?:\s+(?:Mesial|Occlusal|Distal|Buccal|Lingual|Incisal|Facial))*\)',
            '', clean_transcript, flags=re.IGNORECASE
        )
        # Clean up any orphaned closing parens or semicolons
        clean_transcript = re.sub(r'\s*\)', '', clean_transcript)
        clean_transcript = ' '.join(clean_transcript.split())

        # ── Normalize number words → digits ─────────────────────────────────
        clean_transcript = CDTService.normalize_number_words(clean_transcript)

        logger.info(f"[CDT] Clean transcript ({len(clean_transcript)} chars): {clean_transcript[:200]}...")

        # Switch to New Direct LLM Approach (No RAG) with defensive fallback
        try:
            result = await CDTServiceDirect.process_transcript(clean_transcript)
        except Exception as cdt_err:
            logger.error(f"[CDT] Direct lookup threw unhandled error: {cdt_err}", exc_info=True)
            result = {"mode": "error_fallback", "entries": []}
        logger.info(f"[CDT] Detection mode: {result.get('mode')}, entries: {len(result.get('entries', []))}")

        # ── Inject codes into transcript ──
        self.cdt_transcript = clean_transcript

        entries = []
        for entry in result.get("entries", []):
            inputs = entry.get("input", [])
            if not isinstance(inputs, list): inputs = [inputs]
            suggestions = entry.get("suggestions", [])
            if not inputs or not suggestions: continue
            
            entries.append({
                "input": inputs,
                "suggestions": suggestions,
                "conflict_note": entry.get("conflict_note")
            })

        # --- SORT BY INPUT LENGTH (DESCENDING) ---
        # Ensures "regular prophy" is matched before "prophy"
        entries.sort(key=lambda x: max(len(i) for i in x["input"]), reverse=True)

        for entry in entries:
            inputs = entry["input"]
            suggestions = entry["suggestions"]
            conflict_note = entry["conflict_note"]
            
            suggestion_parts = []
            for s in suggestions:
                c = s.get("code", "")
                d = s.get("description", "")
                if c:
                    suggestion_parts.append(f"{c} - {d}" if d else c)
            
            codes_str = "; ".join(suggestion_parts)
            if conflict_note:
                codes_str += f" | NOTE: {conflict_note}"

            combined_input = ", ".join(inputs)
            
            # Try each individual input — inject code on ALL that match
            injected = False
            for input_text in inputs:
                safe_input = re.escape(input_text)
                pattern = re.compile(
                    rf"\b({safe_input})\b(?!\s*[\(\[])", 
                    re.IGNORECASE
                )
                
                if pattern.search(self.cdt_transcript):
                    from app.services.cdt_service import SURFACE_FULL_NAMES
                    match_upper = re.search(r'\b([MODBLI]{2,5})\b', input_text.upper())
                    surface_expansion = ""
                    if match_upper:
                        expanded = " ".join(SURFACE_FULL_NAMES.get(char, char) for char in match_upper.group(1))
                        surface_expansion = f" ({expanded})"

                    self.cdt_transcript = pattern.sub(
                        rf"\1{surface_expansion} ({codes_str})",
                        self.cdt_transcript
                    )
                    logger.info(f"[CDT] Injected: '{input_text}' → ({codes_str})")
                    injected = True

            # Fallback: try combined input if no individual input matched
            if not injected and len(inputs) > 1:
                safe_input = re.escape(combined_input)
                pattern = re.compile(
                    rf"\b({safe_input})\b(?!\s*[\(\[])", 
                    re.IGNORECASE
                )
                if pattern.search(self.cdt_transcript):
                    self.cdt_transcript = pattern.sub(
                        rf"\1 ({codes_str})",
                        self.cdt_transcript
                    )
                    logger.info(f"[CDT] Injected (combined): '{combined_input}' → ({codes_str})")

        # ── Post-injection cleanup ────────────────────────────────────────

        # 2. Inject D1110 at any remaining bare "prophy" mentions
        bare_prophy = re.compile(r'\b(prophy)\b(?!\s*[\(\[])', re.IGNORECASE)
        if bare_prophy.search(self.cdt_transcript):
            # Find D1110 description from any existing prophy entry
            d1110_str = "D1110 - Prophylaxis - adult age 14+"
            for entry in entries:
                for s in entry.get("suggestions", []):
                    if s.get("code") == "D1110":
                        desc = s.get("description", "")
                        d1110_str = f"D1110 - {desc}" if desc else "D1110"
                        break
            self.cdt_transcript = bare_prophy.sub(
                rf"\1 ({d1110_str})",
                self.cdt_transcript
            )
            logger.info(f"[CDT] Injected remaining bare prophy → ({d1110_str})")

        # Clean up any double commas or leading/trailing commas from removal
        self.cdt_transcript = re.sub(r',\s*,', ',', self.cdt_transcript)
        self.cdt_transcript = re.sub(r'^\s*,\s*', '', self.cdt_transcript)
        self.cdt_transcript = re.sub(r'\s*,\s*$', '', self.cdt_transcript)
        self.cdt_transcript = ' '.join(self.cdt_transcript.split())

        return result

    async def create_pending_session(
        self,
        session_name: str = None,
        template_ids: list[str] = None,
        xray_selection: list[str] = None,
        template_id: str = None,
        **kwargs,
    ) -> str:
        """
        Create or update a database entry in 'dental_voice_sessions' with status 'pending'
        before processing/remuxing audio.
        """
        from datetime import datetime, timezone
        from bson import ObjectId

        if session_name is not None:
            self.session_name = session_name
        if template_ids is not None:
            self.template_ids = template_ids
        elif template_id is not None:
            self.template_ids = [template_id]
        if xray_selection is not None:
            self.xray_selection = xray_selection

        db = await get_database()
        session_data = {
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "stream_id": self.stream_id,
            "session_name": self.session_name,
            "template_ids": self.template_ids,
            "xray_selection": self.xray_selection,
            "session_type": "cdt_lookup",
            "status": "pending",
            "processing_stage": self.processing_stage,
            "transcript": self.transcript,
            "original_transcript": self.original_transcript,
            "audio_blob_name": self.audio_blob_name,
            "audio_size_bytes": self.total_bytes_appended,
            "session_duration_seconds": self.audio_duration_seconds or round(time.perf_counter() - self.session_start, 1),
            "is_verified": False,
            "updated_at": datetime.now(timezone.utc),
        }

        if self.session_id:
            await db["dental_voice_sessions"].update_one(
                {"_id": ObjectId(self.session_id)},
                {"$set": session_data}
            )
        else:
            session_data["created_at"] = datetime.now(timezone.utc)
            result = await db["dental_voice_sessions"].insert_one(session_data)
            self.session_id = str(result.inserted_id)

        logger.info(f"[VOICE-WS] Created/Updated pending DB session: {self.session_id} (stage: {self.processing_stage})")
        return self.session_id

    async def save_session(
        self,
        cdt_result: dict,
        session_name: str = None,
        template_ids: list[str] = None,
        xray_selection: list[str] = None,
        template_id: str = None,
        **kwargs,
    ) -> str:
        """Save the complete session (audio + transcript + CDT results) to DB."""
        from datetime import datetime, timezone
        from bson import ObjectId

        if session_name is not None:
            self.session_name = session_name
        if template_ids is not None:
            self.template_ids = template_ids
        elif template_id is not None:
            self.template_ids = [template_id]
        if xray_selection is not None:
            self.xray_selection = xray_selection

        self.processing_stage = "completed"
        db = await get_database()
        session_data = {
            "user_id": self.user_id,
            "organization_id": self.organization_id,
            "stream_id": self.stream_id,
            "session_name": self.session_name,
            "template_ids": self.template_ids,
            "xray_selection": self.xray_selection,
            "session_type": "cdt_lookup",
            "status": "transcripted",
            "processing_stage": "completed",
            "transcript": self.transcript,             
            "original_transcript": self.original_transcript, 
            "audio_blob_name": self.audio_blob_name,
            "audio_size_bytes": self.total_bytes_appended,
            "detection_mode": cdt_result.get("mode"),
            "entries": cdt_result.get("entries", []),
            "session_duration_seconds": self.audio_duration_seconds or round(time.perf_counter() - self.session_start, 1),
            "is_verified": False,
            "updated_at": datetime.now(timezone.utc),
        }

        if self.session_id:
            await db["dental_voice_sessions"].update_one(
                {"_id": ObjectId(self.session_id)},
                {"$set": session_data}
            )
        else:
            session_data["created_at"] = datetime.now(timezone.utc)
            result = await db["dental_voice_sessions"].insert_one(session_data)
            self.session_id = str(result.inserted_id)

        return self.session_id

    @classmethod
    async def reprocess_session(cls, session_id: str) -> dict:
        """
        Reprocesses a session based on its last saved stage:
        - If remuxing failed or was incomplete (processing_stage != 'remux_completed'),
          starts from the beginning: remuxing audio with ffmpeg, then moving to AI stage.
        - If remuxing was already completed (processing_stage == 'remux_completed'),
          skips remuxing and starts directly from the AI stage (Gemini STT + CDT + SOAP).
        """
        from bson import ObjectId

        db = await get_database()
        session_doc = await db["dental_voice_sessions"].find_one({"_id": ObjectId(session_id)})
        if not session_doc:
            raise ValueError(f"Session {session_id} not found in DB")

        user_id = session_doc.get("user_id")
        org_id = session_doc.get("organization_id")
        stream_id = session_doc.get("stream_id") or f"reprocess_{session_id}"
        audio_blob_name = session_doc.get("audio_blob_name")

        if not audio_blob_name:
            raise ValueError(f"Session {session_id} has no audio blob attached")

        # Instantiate VoiceStreamService with existing session metadata
        service = cls(user_id=user_id, stream_id=stream_id, organization_id=org_id)
        service.session_id = session_id
        service.session_name = session_doc.get("session_name")
        service.template_ids = session_doc.get("template_ids")
        service.xray_selection = session_doc.get("xray_selection")
        service.audio_blob_name = audio_blob_name
        service.original_transcript = session_doc.get("original_transcript", "")
        service.processing_stage = session_doc.get("processing_stage", "pending_remux")

        logger.info(
            f"[REPROCESS-SESSION] Reprocessing session {session_id}. "
            f"Current processing_stage: {service.processing_stage}"
        )

        # 1. Finalize (remuxes if needed based on stage, then runs Gemini STT)
        transcript = await service.finalize()

        # 2. CDT lookup
        cdt_result = await service.lookup_cdt()

        # 3. Save updated session to DB
        template_ids = service.template_ids
        xray_selection = service.xray_selection
        session_name = service.session_name

        saved_id = await service.save_session(
            cdt_result=cdt_result,
            session_name=session_name,
            template_ids=template_ids,
            xray_selection=xray_selection,
        )

        # 4. Run background SOAP generation
        asyncio.create_task(cls.run_soap_background(
            session_id=saved_id,
            transcript=service.raw_transcript,
            user_id=user_id,
            template_ids=template_ids,
            org_id=org_id,
            xray_selection=xray_selection,
        ))

        return {
            "session_id": saved_id,
            "status": "transcripted",
            "processing_stage": service.processing_stage,
            "transcript": service.transcript,
            "cdt_result": cdt_result,
        }

    @staticmethod
    async def run_soap_background(
        session_id: str,
        transcript: str,
        user_id: str,
        template_ids: list[str] = None,
        org_id: str = None,
        xray_selection: list[str] = None,
        template_id: str = None,
        **kwargs,
    ):
        """Background task to generate SOAP/Ghostwriter notes and update the DB."""
        from datetime import datetime, timezone
        from bson import ObjectId
        from app.services.dental_voice_service import DentalVoiceService
        
        if template_ids is None:
            if template_id is not None:
                template_ids = [template_id]
            elif kwargs.get("template_id"):
                template_ids = [kwargs["template_id"]]
            elif kwargs.get("template_ids"):
                template_ids = kwargs["template_ids"]

        try:
            logger.info(f"[VOICE-WS] Starting background note generation for session {session_id}")
            result = await DentalVoiceService.generate_soap_and_summary(
                transcript=transcript,
                template_ids=template_ids,
                org_id=org_id,
                xray_selection=xray_selection,
            )
            
            db = await get_database()
            update_data = {
                "status": "report generated",
                "reports": result.get("reports", []),
                "patient_summary": result.get("patient_summary"),
                "entities_extracted": result.get("entities_extracted"),
                "completed_today": result.get("completed_today", []),
                "planned_future": result.get("planned_future", []),
                "updated_at": datetime.now(timezone.utc)
            }

            # Maintain soap_note backwards compatibility by joining all reports
            reports = result.get("reports", [])
            if reports:
                combined_reports = []
                for r in reports:
                    name = r.get("template_name", "Report")
                    body = r.get("report", "")
                    combined_reports.append(f"# {name}\n{body}")
                update_data["soap_note"] = "\n\n---\n\n".join(combined_reports)
            else:
                update_data["soap_note"] = result.get("soap_note", "")

            await db["dental_voice_sessions"].update_one(
                {"_id": ObjectId(session_id)},
                {"$set": update_data}
            )
            logger.info(f"[VOICE-WS] Background note generation completed for session {session_id}")
            
        except Exception as e:
            logger.error(f"[VOICE-WS] Background SOAP generation failed for session {session_id}: {e}", exc_info=True)
            try:
                db = await get_database()
                await db["dental_voice_sessions"].update_one(
                    {"_id": ObjectId(session_id)},
                    {"$set": {
                        "status": "failed",
                        "processing_stage": "ai_failed",
                        "error_message": f"Background note generation failed: {e}",
                        "updated_at": datetime.now(timezone.utc)
                    }}
                )
            except Exception as db_err:
                logger.error(f"[VOICE-WS] Failed to write failure state to DB for session {session_id}: {db_err}")

    # ── Gemini STT Call ──────────────────────────────────────────────────

    async def _call_gemini_stt(self, audio_url: str) -> str:
        """
        Transcribe audio using Gemini 2.5 Flash via Vertex AI with speaker diarization.
        Downloads the audio from the given SAS URL, converts it to OGG/Opus via ffmpeg
        (to prevent container parsing issues that lead to loops), and sends it to Gemini.
        Returns a diarized, deduplicated transcript with Dentist/Patient labels.
        """
        from google import genai
        from google.genai import types
        import tempfile
        import os

        # Download audio bytes from Azure Blob SAS URL
        logger.info(f"[VOICE-WS] Downloading audio for Gemini STT from: {audio_url.split('?')[0]}")
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.get(audio_url)
            response.raise_for_status()
            audio_bytes = response.content

        logger.info(f"[VOICE-WS] Downloaded {len(audio_bytes)} bytes. Converting to OGG/Opus...")

        # Transcode WebM to OGG/Opus to prevent Gemini looping issues
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temp_in:
            temp_in.write(audio_bytes)
            temp_in_name = temp_in.name

        temp_out_name = temp_in_name + ".ogg"
        ogg_bytes = None

        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-i", temp_in_name,
                "-af", "silenceremove=stop_periods=-1:stop_duration=1.5:stop_threshold=-40dB",
                "-c:a", "libopus",
                "-b:a", "64k",
                temp_out_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout_err = await process.communicate()
            if process.returncode == 0:
                with open(temp_out_name, "rb") as f:
                    ogg_bytes = f.read()
                logger.info(f"[VOICE-WS] Converted to OGG/Opus: {len(ogg_bytes)} bytes")
                
                # Check non-silent duration using ffprobe
                duration = 0.0
                try:
                    probe_process = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error",
                        "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1",
                        temp_out_name,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    d_stdout, _ = await probe_process.communicate()
                    if probe_process.returncode == 0 and d_stdout:
                        duration = float(d_stdout.decode().strip())
                except Exception as dur_err:
                    logger.warning(f"[VOICE-WS] Failed to probe duration after silence removal: {dur_err}")

                # If non-silent audio duration is < 0.3s or byte size < 1500 bytes, return empty transcript
                if duration < 0.3 or len(ogg_bytes) < 1500:
                    logger.info(
                        f"[VOICE-WS] Audio is silent/empty after silence removal "
                        f"(duration={duration:.2f}s, size={len(ogg_bytes)}B). Returning empty transcript."
                    )
                    return ""
            else:
                stderr_msg = stdout_err[1].decode() if stdout_err else "Unknown error"
                logger.error(f"[VOICE-WS] ffmpeg OGG conversion failed with code {process.returncode}: {stderr_msg}")
                # Fallback to original audio_bytes if conversion fails
                ogg_bytes = audio_bytes
        except Exception as conv_err:
            logger.error(f"[VOICE-WS] Failed to convert WebM to OGG: {conv_err}")
            ogg_bytes = audio_bytes
        finally:
            for p in (temp_in_name, temp_out_name):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception as clean_err:
                    logger.warning(f"[VOICE-WS] Failed to delete temp file {p}: {clean_err}")

        mime_type = "audio/ogg" if ogg_bytes is not audio_bytes else "audio/webm"
        logger.info(f"[VOICE-WS] Submitting to Gemini STT ({settings.GEMINI_STT_MODEL}) using {mime_type}")

        gemini_client = genai.Client(
            vertexai=True,
            project=settings.GOOGLE_CLOUD_PROJECT,
            location=settings.GOOGLE_CLOUD_LOCATION,
            credentials=settings.get_vertex_credentials(),
        )

        system_instruction = (
            "You are a verbatim clinical audio transcription engine. "
            "Your ONLY job is to transcribe spoken audio — once and only once. "
            "CRITICAL DIRECTIVE: If the audio is silent, blank, contains no intelligible human speech, or only contains ambient noise, you MUST return NOTHING (an empty string). "
            "DO NOT invent, fabricate, mock, or hallucinate any dialogue, names, or clinical statements under any circumstances."
        )

        user_prompt = (
            "Transcribe the spoken audio verbatim.\n"
            "CRITICAL: If NO human speech is present in the audio, respond with an EMPTY string.\n"
            "If speech IS present, identify speakers EXACTLY as '**Dentist:**' and '**Patient:**' (you MUST include the asterisks for bolding). If and ONLY IF there is a third distinct clinical person assisting the Dentist, label them EXACTLY as '**Assistant:**'.\n"
            "IMPORTANT: The **Dentist** is ALWAYS the very first person to speak at the beginning of the audio. After that, use conversational context to correctly label the speakers. The **Patient** receives treatment. The **Assistant** (if present) provides support. Do NOT invent an Assistant if only two people are speaking.\n"
            "Start each speaker turn on a new line and separate speaker turns with a blank line (double line break).\n"
            "Capture all clinical details accurately.\n\n"
            "Important dental vocabulary to preserve accurately: "
            "prophy, prophylaxis, amalgam, composite, pontic, buildup, abutment, "
            "mesial, occlusal, distal, buccal, lingual, incisal, CDT codes, "
            "lidocaine, septocaine, articaine, mepivacaine, epinephrine, anesthesia."
        )

        try:
            response = await gemini_client.aio.models.generate_content(
                model=settings.GEMINI_STT_MODEL,
                contents=[
                    types.Part.from_bytes(data=ogg_bytes, mime_type=mime_type),
                    types.Part.from_text(text=user_prompt),
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.0,
                    max_output_tokens=8192,
                )
            )
            raw = (response.text or "").strip()
            if not raw or raw.lower() in ("[silence]", "(silence)", "no speech", "no speech detected", "[no speech]", "no spoken audio"):
                logger.info("[VOICE-WS] Gemini returned empty or silence indicator. Returning empty transcript.")
                return ""

            # Step 1: Remove intra-line word repetitions (e.g., "Good. Good. Good. Good.")
            cleaned = _clean_whisper_hallucinations(raw)
            # Step 2: Remove inter-line repetitions (e.g., alternating speaker loops)
            transcript = _clean_gemini_transcript(cleaned)
            logger.info(
                f"[VOICE-WS] Gemini STT completed. "
                f"Raw={len(raw)} chars → cleaned={len(transcript)} chars"
            )
            return transcript
        except Exception as e:
            logger.error(f"[VOICE-WS] Gemini STT failed: {e}")
            raise RuntimeError(f"Gemini STT transcription failed: {e}")

