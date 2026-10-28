from fastapi import APIRouter, Depends, Query

from app.core.response import api_response
from app.schemas.video_schema import VideoCreateRequest, VideoUpdateRequest
from app.services.video_service import VideoService
from app.services.permission_service import PermissionService
from app.dependencies import get_current_user, get_super_admin

router = APIRouter()


# ── Super Admin: Create ───────────────────────────────────────────────────────
@router.post("", response_model=dict)
async def create_video(
    request: VideoCreateRequest,
    current_user=Depends(get_current_user),
):
    """Create a new teaching video/podcast."""
    await PermissionService.check_permission(current_user, "videos:manage")
    video = await VideoService.create_video(
        data=request.model_dump(),
        created_by=current_user["email"],
    )
    return api_response(success=True, message="Video created successfully.", data=video, status_code=201)


# ── List videos (students + super admin) ─────────────────────────────────────
@router.get("", response_model=dict)
async def list_videos(
    active_only: bool = True,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user=Depends(get_current_user),
):
    """List teaching videos. Students (and Super Admins) can access this."""
    await PermissionService.check_permission(current_user, "videos:list")
    result = await VideoService.list_videos(active_only=active_only, skip=skip, limit=limit)
    return api_response(success=True, message="Videos retrieved successfully.", data=result)


# ── Get single video ──────────────────────────────────────────────────────────
@router.get("/{video_id}", response_model=dict)
async def get_video(
    video_id: str,
    current_user=Depends(get_current_user),
):
    """Get a single teaching video by ID."""
    await PermissionService.check_permission(current_user, "videos:list")
    video = await VideoService.get_video(video_id)
    return api_response(success=True, message="Video retrieved successfully.", data=video)


# ── Super Admin: Update ───────────────────────────────────────────────────────
@router.patch("/{video_id}", response_model=dict)
async def update_video(
    video_id: str,
    request: VideoUpdateRequest,
    current_user=Depends(get_current_user),
):
    """Update a teaching video."""
    await PermissionService.check_permission(current_user, "videos:manage")
    video = await VideoService.update_video(video_id, request.model_dump(exclude_none=True))
    return api_response(success=True, message="Video updated successfully.", data=video)


# ── Super Admin: Delete ───────────────────────────────────────────────────────
@router.delete("/{video_id}", response_model=dict)
async def delete_video(
    video_id: str,
    current_user=Depends(get_current_user),
):
    """Delete a teaching video."""
    await PermissionService.check_permission(current_user, "videos:manage")
    video = await VideoService.delete_video(video_id)
    return api_response(success=True, message="Video deleted successfully.", data=video)
