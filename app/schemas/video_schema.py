from typing import Optional
from pydantic import BaseModel, Field, HttpUrl


class VideoCreateRequest(BaseModel):
    title: str = Field(..., min_length=2, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    youtube_url: str = Field(..., description="Full YouTube video URL")


class VideoUpdateRequest(BaseModel):
    title: Optional[str] = Field(None, min_length=2, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    youtube_url: Optional[str] = None
    is_active: Optional[bool] = None
