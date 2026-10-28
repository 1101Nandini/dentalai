from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, EmailStr


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    role: str
    is_active: bool  # true=active, false=deactivated
    is_verified: bool
    auth_provider: str
    subscription_plan: str
    query_count: int
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    total: int
    skip: int
    limit: int
    users: List[UserResponse]


class UserUpdateRequest(BaseModel):
    full_name: Optional[str] = None
    is_active: Optional[bool] = None   # Admin: true=activate, false=deactivate


class RoleUpdateRequest(BaseModel):
    role: str

class BulkActionRequest(BaseModel):
    user_ids: List[str]
