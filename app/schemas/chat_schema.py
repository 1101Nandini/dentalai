"""
Pydantic schemas for Chat & Message endpoints.
"""
from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Chat schemas
# ---------------------------------------------------------------------------

class CreateChatRequest(BaseModel):
    content: str                         # first message — used to generate the title
    image_url: Optional[str] = None      # optional image for first message


class UpdateChatRequest(BaseModel):
    title: str


class ChatResponse(BaseModel):
    id: str
    title: str
    user_id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Image upload schemas
# ---------------------------------------------------------------------------

class ImageUploadItem(BaseModel):
    """Single uploaded image returned by POST /chats/upload-images."""
    filename: str
    blob_name: str   # full blob path — send this back when submitting the message
    url: str         # 4-hour SAS URL for preview


class ImageUploadResponse(BaseModel):
    uploads: List[ImageUploadItem]


# ---------------------------------------------------------------------------
# Message schemas
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    content: str
    image_url: Optional[str] = None       # legacy single image (kept for compat)
    image_urls: Optional[List[str]] = Field(
        default_factory=list,
        description=(
            "Blob names returned by POST /chats/upload-images. "
            "Images will be promoted from temp → permanent storage before saving."
        ),
    )


class MessageResponse(BaseModel):
    id: str
    chat_id: str
    role: str  # "user" | "assistant"
    content: str
    image_url: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)  # permanent blob names
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Stream request (same shape as SendMessageRequest, kept explicit)
# ---------------------------------------------------------------------------

class StreamMessageRequest(BaseModel):
    content: str
    image_url: Optional[str] = None       # legacy single image
    image_urls: Optional[List[str]] = Field(
        default_factory=list,
        description="Blob names from POST /chats/upload-images to attach to this message.",
    )
