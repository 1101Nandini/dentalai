from pydantic import BaseModel
from typing import Optional, Dict, Any

class CheckoutSessionRequest(BaseModel):
    plan: str
    promo_code: Optional[str] = None
    success_url: Optional[str] = "http://localhost:3000/plan?success=true"
    cancel_url: Optional[str] = "http://localhost:3000/plan?canceled=true"

class CheckoutSessionResponse(BaseModel):
    session_id: str
    url: str

class ValidatePromoRequest(BaseModel):
    code: str

class ValidatePromoResponse(BaseModel):
    valid: bool
    code: str
    percent_off: Optional[float] = None
    amount_off: Optional[float] = None
    currency: Optional[str] = None
    duration: Optional[str] = None
    duration_in_months: Optional[int] = None
    message: Optional[str] = None

class CustomerPortalRequest(BaseModel):
    return_url: Optional[str] = "http://localhost:3000/plan"

class CustomerPortalResponse(BaseModel):
    url: str

