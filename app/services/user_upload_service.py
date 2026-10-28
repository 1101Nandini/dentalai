"""
UserUploadService — handles user-uploaded chat images.

Flow:
  1. User calls  POST /chats/upload-images
     → files land in  user-upload/temp/{user_id}/{uuid}_{filename}
     → returns 4-hour SAS URLs (temp_blob_name + temp_sas_url per file)

  2. User calls  POST /chats/{chat_id}/stream  with image blob names
     → service promotes each blob: temp → user-upload/{uuid}_{filename}
     → stores permanent blob names in MongoDB message document
     → generates fresh SAS URLs for RAG + LLM at query time
"""
import uuid
import logging
from typing import List, Dict, Any

from fastapi import HTTPException, UploadFile, status

from app.utils.file_utils import upload_blob, move_blob, generate_sas_url_with_expiry

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp"}
MAX_IMAGE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

TEMP_PREFIX = "user-upload/temp"
PERM_PREFIX = "user-upload"

# 4-hour SAS for temp blobs (user has time to compose message)
TEMP_SAS_EXPIRY_HOURS = 4.0

# 7-day SAS for permanent blobs passed to RAG / LLM
PERM_SAS_EXPIRY_HOURS = 168.0


class UserUploadService:

    # ── Validate a single file ─────────────────────────────────────────────────
    @staticmethod
    def _validate(file: UploadFile, content: bytes) -> None:
        if not file.filename:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File has no name.",
            )
        ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
        if ext not in ALLOWED_IMAGE_EXTENSIONS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Unsupported file type '.{ext}'. "
                    f"Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}"
                ),
            )
        if len(content) > MAX_IMAGE_SIZE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File '{file.filename}' exceeds the 10 MB size limit.",
            )

    # ── Upload to temp folder ──────────────────────────────────────────────────
    @staticmethod
    async def upload_to_temp(
        user_id: str,
        files: List[UploadFile],
    ) -> List[Dict[str, Any]]:
        """
        Upload one or more image files to the temp folder.

        Returns a list of dicts, each containing:
          - filename   : original filename
          - blob_name  : full blob path  (user-upload/temp/{user_id}/{uuid}_{filename})
          - url        : 4-hour SAS URL
        """
        if not files:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No files provided.",
            )

        results: List[Dict[str, Any]] = []

        for file in files:
            content = await file.read()
            UserUploadService._validate(file, content)

            short_id = str(uuid.uuid4())[:8]
            safe_filename = file.filename.replace(" ", "_")
            blob_name = f"{TEMP_PREFIX}/{user_id}/{short_id}_{safe_filename}"

            content_type = file.content_type or "image/jpeg"
            await upload_blob(blob_name, content, content_type)

            sas_url = generate_sas_url_with_expiry(blob_name, expiry_hours=TEMP_SAS_EXPIRY_HOURS)

            results.append(
                {
                    "filename": file.filename,
                    "blob_name": blob_name,
                    "url": sas_url,
                }
            )
            logger.info("Uploaded temp image: %s", blob_name)

        return results

    # ── Promote temp blobs to permanent storage ────────────────────────────────
    @staticmethod
    async def promote_images(
        temp_blob_names: List[str],
    ) -> List[Dict[str, Any]]:
        """
        Move each temp blob name to permanent storage (concurrently).

        Expects blob names in the format:
          user-upload/temp/{user_id}/{uuid}_{filename}

        Returns a list of dicts:
          - temp_blob_name  : original temp path
          - blob_name       : permanent blob path  (user-upload/{uuid}_{filename})
          - url             : 7-day SAS URL
        """
        import asyncio

        async def _promote_one(temp_name: str) -> Dict[str, Any]:
            if not temp_name.startswith(TEMP_PREFIX + "/"):
                logger.warning(
                    "Skipping non-temp blob during promotion: %s", temp_name
                )
                # Already promoted or unexpected path — generate fresh SAS as-is
                sas_url = generate_sas_url_with_expiry(
                    temp_name, expiry_hours=PERM_SAS_EXPIRY_HOURS
                )
                return {"temp_blob_name": temp_name, "blob_name": temp_name, "url": sas_url}

            # Strip the temp prefix: user-upload/temp/{user_id}/{file} → keep {file}
            parts = temp_name.split("/")
            filename_part = parts[-1]  # uuid_filename.ext
            perm_blob_name = f"{PERM_PREFIX}/{filename_part}"

            try:
                await move_blob(temp_name, perm_blob_name)
                logger.info("Promoted blob: %s → %s", temp_name, perm_blob_name)
            except Exception as exc:
                logger.error(
                    "Failed to promote blob %s: %s", temp_name, exc, exc_info=True
                )
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to promote image '{filename_part}'. Please retry.",
                )

            sas_url = generate_sas_url_with_expiry(
                perm_blob_name, expiry_hours=PERM_SAS_EXPIRY_HOURS
            )
            return {
                "temp_blob_name": temp_name,
                "blob_name": perm_blob_name,
                "url": sas_url,
            }

        # Run all promotions concurrently
        promoted = await asyncio.gather(*[_promote_one(name) for name in temp_blob_names])
        return list(promoted)

    # ── Generate fresh SAS URLs from stored blob names ─────────────────────────
    @staticmethod
    def get_sas_urls(blob_names: List[str]) -> List[str]:
        """Generate fresh 7-day SAS URLs from a list of permanent blob names."""
        return [
            generate_sas_url_with_expiry(bn, expiry_hours=PERM_SAS_EXPIRY_HOURS)
            for bn in blob_names
        ]

    # ── Delete Images ──────────────────────────────────────────────────────────
    @staticmethod
    async def delete_images(blob_names: List[str]) -> None:
        """Permanently delete a list of blobs from Azure Storage."""
        import asyncio
        from app.utils.file_utils import delete_blob

        async def _delete_one(name: str):
            try:
                await delete_blob(name)
                logger.info("Deleted blob: %s", name)
            except Exception as exc:
                logger.warning("Failed to delete blob %s: %s", name, exc)

        if blob_names:
            await asyncio.gather(*[_delete_one(name) for name in blob_names])
