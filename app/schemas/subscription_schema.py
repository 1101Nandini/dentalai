from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel

class PlanResponse(BaseModel):
    plan_id: str
    stripe_price_id: str
    name: str
    display_name: str
    user_type: str
    cycle: str
    price: float
    currency: str
    description: Optional[str] = None
    features: List[str] = []
    is_active: bool
    allowed_roles: List[str] = []
    trial_period: int = 14

class SubscriptionResponse(BaseModel):
    id: str
    user_id: str
    plan: str
    plan_id: Optional[str] = None
    organization_id: Optional[str] = None
    is_active: bool
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    created_at: datetime
    transaction_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_customer_id: Optional[str] = None
    is_trial: Optional[bool] = False
    trial_days_left: Optional[int] = 0
    trial_end_date: Optional[datetime] = None

    class Config:
        from_attributes = True

class SubscriptionListResponse(BaseModel):
    total: int
    subscriptions: List[SubscriptionResponse]

