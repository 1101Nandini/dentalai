"""
Chat & Message routes.

Chats (conversation level):
  POST   /chats                               – create chat
  GET    /chats                               – list user's chats
  GET    /chats/{chatId}                      – get single chat
  PATCH  /chats/{chatId}                      – rename chat
  DELETE /chats/{chatId}                      – delete chat + messages

Messages (inside a chat):
  POST   /chats/{chatId}/messages             – send a message
  GET    /chats/{chatId}/messages             – list messages
  GET    /chats/{chatId}/messages/{messageId} – get single message
  DELETE /chats/{chatId}/messages/{messageId} – delete message

Streaming:
  POST   /chats/{chatId}/stream              – SSE streaming AI response
"""
from typing import Optional, List

from fastapi import APIRouter, Depends, Query, UploadFile, File, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.response import api_response
from app.dependencies import get_current_user, require_permission
from app.schemas.chat_schema import (
    CreateChatRequest,
    UpdateChatRequest,
    SendMessageRequest,
    StreamMessageRequest,
    ImageUploadResponse,
    ImageUploadItem,
)
from app.services.chat_service import ChatService
from app.services.message_service import MessageService
from app.services.subscription_service import SubscriptionService
from app.services.user_upload_service import UserUploadService

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════════════
# CHATS
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/chats")
async def create_chat(
    body: CreateChatRequest,
    current_user=Depends(require_permission("chat:use")),
):
    """Create a new chat from the first message. Title is auto-generated."""
    user_id = str(current_user["_id"])
    
    # Check subscription limit first
  
    # await SubscriptionService.check_message_limit(user_id)  # Subscriptions disabled

    # Auto-generate title from the first message
    title = await MessageService.generate_title(body.content)

    # Create chat
    chat = await ChatService.create_chat(user_id=user_id, title=title)

    return api_response(
        success=True,
        message="Chat created successfully.",
        data={"chat": chat},
        status_code=201,
    )


@router.get("/chats")
async def list_chats(
    limit: int = Query(default=50, ge=1, le=100),
    cursor: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    current_user=Depends(require_permission("chat:use")),
):
    """List all chats for the current user, newest first."""
    user_id = str(current_user["_id"])
    result = await ChatService.list_chats(user_id=user_id, limit=limit, cursor=cursor, search=search)
    return api_response(
        success=True,
        message="Chats fetched successfully.",
        data=result,
    )


@router.get("/chats/{chat_id}")
async def get_chat(
    chat_id: str,
    current_user=Depends(require_permission("chat:use")),
):
    """Get a single chat by ID."""
    chat = await ChatService.get_chat(chat_id)
    # Ownership check via JWT
    if chat.get("user_id") != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="You do not have access to this chat")
    return api_response(success=True, message="Chat found.", data=chat)


@router.patch("/chats/{chat_id}")
async def update_chat(
    chat_id: str,
    body: UpdateChatRequest,
    current_user=Depends(require_permission("chat:use")),
):
    """Rename a chat."""
    # Ownership check via JWT
    existing = await ChatService.get_chat(chat_id)
    if existing.get("user_id") != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="You do not have access to this chat")
    chat = await ChatService.update_chat(chat_id, title=body.title)
    return api_response(
        success=True,
        message="Chat updated successfully.",
        data=chat,
    )


@router.delete("/chats/{chat_id}")
async def delete_chat(
    chat_id: str,
    current_user=Depends(require_permission("chat:use")),
):
    """Delete a chat and all its messages."""
    # Ownership check via JWT
    existing = await ChatService.get_chat(chat_id)
    if existing.get("user_id") != str(current_user["_id"]):
        raise HTTPException(status_code=403, detail="You do not have access to this chat")
    await ChatService.delete_chat(chat_id)
    return api_response(success=True, message="Chat deleted successfully.", data=None)


# ═══════════════════════════════════════════════════════════════════════
# IMAGE UPLOAD
# ═══════════════════════════════════════════════════════════════════════

@router.post("/chats/upload-images", response_model=ImageUploadResponse)
async def upload_images(
    files: List[UploadFile] = File(..., description="One or more image files to upload."),
    current_user=Depends(require_permission("chat:use")),
):
    """
    Upload one or more images to temporary Azure Blob storage.

    Returns blob names and 4-hour SAS preview URLs.
    The caller must include the ``blob_name`` values in the
    ``image_urls`` field of the subsequent stream/send request so the
    backend can promote them to permanent storage before saving the message.
    """
    user_id = str(current_user["_id"])
    results = await UserUploadService.upload_to_temp(user_id=user_id, files=files)
    uploads = [ImageUploadItem(**r) for r in results]
    return api_response(
        success=True,
        message=f"{len(uploads)} image(s) uploaded successfully.",
        data={"uploads": [u.model_dump() for u in uploads]},
        status_code=201,
    )


# ═══════════════════════════════════════════════════════════════════════════
# MESSAGES
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/chats/{chat_id}/messages")
async def send_message(
    chat_id: str,
    body: SendMessageRequest,
    current_user=Depends(require_permission("chat:use")),
):
    """Send a user message (non-streaming). Saves the message and returns it."""
    # Check subscription limit first
    user_id = str(current_user["_id"])
    plan = current_user.get("subscription_plan", "free_trial")
    # await SubscriptionService.check_message_limit(user_id, plan)  # Subscriptions disabled

    # Ownership check via JWT
    chat = await ChatService.get_chat(chat_id)
    if chat.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this chat")

    # ── Promote temp images → permanent storage ────────────────────────
    raw_blob_names: List[str] = list(body.image_urls or [])
    if body.image_url and body.image_url not in raw_blob_names:
        raw_blob_names.append(body.image_url)

    perm_blob_names: List[str] = []
    if raw_blob_names:
        promoted = await UserUploadService.promote_images(raw_blob_names)
        perm_blob_names = [p["blob_name"] for p in promoted]

    msg = await MessageService.save_message(
        chat_id=chat_id,
        role="user",
        content=body.content,
        image_url=None,              # no longer used for new uploads
        image_urls=perm_blob_names,  # permanent paths stored in DB
    )
    await ChatService.touch_chat(chat_id)

    return api_response(
        success=True,
        message="Message sent.",
        data=msg,
        status_code=201,
    )


@router.get("/chats/{chat_id}/messages")
async def list_messages(
    chat_id: str,
    current_user=Depends(require_permission("chat:use")),
):
    """Get all messages in a chat, ordered chronologically."""
    # Ownership check via JWT
    user_id = str(current_user["_id"])
    chat = await ChatService.get_chat(chat_id)
    if chat.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this chat")
    messages = await MessageService.get_messages(chat_id)
    return api_response(
        success=True,
        message="Messages fetched successfully.",
        data=messages,
    )


@router.get("/chats/{chat_id}/messages/{message_id}")
async def get_message(
    chat_id: str,
    message_id: str,
    current_user=Depends(require_permission("chat:use")),
):
    """Get a single message by ID."""
    msg = await MessageService.get_message(chat_id, message_id)
    return api_response(success=True, message="Message found.", data=msg)


@router.delete("/chats/{chat_id}/messages/{message_id}")
async def delete_message(
    chat_id: str,
    message_id: str,
    current_user=Depends(require_permission("chat:use")),
):
    """Delete a single message."""
    await MessageService.delete_message(chat_id, message_id)
    return api_response(success=True, message="Message deleted.", data=None)


# ═══════════════════════════════════════════════════════════════════════════
# STREAMING
# ═══════════════════════════════════════════════════════════════════════════

@router.post("/chats/{chat_id}/stream")
async def stream_chat(
    request: Request,
    chat_id: str,
    body: StreamMessageRequest,
    current_user=Depends(require_permission("chat:use")),
):
    """
    SSE endpoint — streams the AI response token-by-token.

    Events emitted:
      - ``token``   : ``{"content": "…"}``
      - ``sources`` : ``{"sources": [...]}``
      - ``done``    : ``{"message_id": "…"}``
      - ``error``   : ``{"content": "…"}``  (image promotion failures etc.)
    """
    # Check subscription limit first
    user_id = str(current_user["_id"])
    plan = current_user.get("subscription_plan", "free_trial")
    # await SubscriptionService.check_message_limit(user_id, plan)  # Subscriptions disabled

    # Ownership check via JWT
    chat = await ChatService.get_chat(chat_id)
    if chat.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="You do not have access to this chat")

    return StreamingResponse(
        MessageService.stream_response(
            chat_id=chat_id,
            query=body.content,
            image_url=body.image_url,            # legacy single image
            image_urls=body.image_urls or [],    # new multi-image blob names
            request=request,
        ),
        media_type="application/x-ndjson",
    )
