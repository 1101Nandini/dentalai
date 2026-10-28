import asyncio
import json
from fastapi import APIRouter, Depends, Request, HTTPException, status
from fastapi.responses import StreamingResponse
from app.core.response import api_response
from app.core.database import get_database
from app.dependencies import get_current_user
from app.services.payment_service import PaymentService
from app.schemas.payment_schema import (
    CheckoutSessionRequest, 
    CheckoutSessionResponse, 
    ValidatePromoRequest, 
    ValidatePromoResponse,
    CustomerPortalRequest,
    CustomerPortalResponse
)

router = APIRouter()

@router.post("/create-checkout-session", response_model=CheckoutSessionResponse)
async def create_checkout_session(request: CheckoutSessionRequest, current_user=Depends(get_current_user)):
    """
    Start a Stripe Checkout Session for a subscription plan.
    Supports optional promo_code to pre-apply discounts and skip card on $0 totals.
    """
    user_id = str(current_user["_id"])
    
    try:
        session_info = await PaymentService.create_checkout_session(
            user_id=user_id,
            plan=request.plan,
            success_url=request.success_url,
            cancel_url=request.cancel_url,
            promo_code=request.promo_code
        )
        
        from app.background.report_processor import log_audit_event
        await log_audit_event(
            actor_id=user_id,
            action="SUBSCRIPTION_CHECKOUT_INITIATED",
            resource="subscriptions",
            resource_id=session_info.get("session_id", ""),
            metadata={
                "actor_email": current_user.get("email"),
                "actor_role": current_user.get("role"),
                "account_type": current_user.get("account_type"),
                "plan": request.plan,
                "promo_code": request.promo_code,
                "stripe_session_id": session_info.get("session_id"),
            }
        )
        
        return api_response(
            success=True,
            message="Checkout session created successfully.",
            data=session_info
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"message": str(e), "code": "STRIPE_CHECKOUT_ERROR"}
        )

@router.post("/validate-promo-code")
async def validate_promo_code(request: ValidatePromoRequest, current_user=Depends(get_current_user)):
    """
    Validate a Stripe promotion code in real-time before checkout.
    """
    user_id = str(current_user["_id"]) if current_user else None
    result = await PaymentService.validate_promotion_code(request.code, user_id=user_id)
    return api_response(
        success=result.get("valid", False),
        message=result.get("message", "Validation completed."),
        data=result
    )

@router.post("/customer-portal")
async def create_customer_portal_session(request: CustomerPortalRequest, current_user=Depends(get_current_user)):
    """
    Generate a Stripe Customer Portal link so the user can add or update their payment method.
    """
    user_id = str(current_user["_id"])
    try:
        result = await PaymentService.create_customer_portal_session(
            user_id=user_id,
            return_url=request.return_url
        )
        return api_response(
            success=True,
            message="Customer portal session created.",
            data=result
        )
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@router.post("/webhook")
async def stripe_webhook(request: Request):
    """
    Endpoint for Stripe webhooks to call when events complete.
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    
    try:
        await PaymentService.process_webhook(payload, sig_header)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )

@router.get("/verify-payment/{session_id}")
async def verify_payment(session_id: str):
    """
    Server-sent events endpoint to verify payment status.
    Polls the database for the session ID and returns the latest status,
    terminating the connection as soon as a final state is reached.
    """
    return StreamingResponse(PaymentService.verify_payment_generator(session_id), media_type="text/event-stream")

from app.dependencies import get_current_admin
from typing import Optional

@router.get("/transactions", dependencies=[Depends(get_current_admin)])
async def list_transactions(skip: int = 0, limit: int = 50, search: Optional[str] = None, status: Optional[str] = None, type: Optional[str] = None):
    """Fetch global transaction logs with pagination and optional filtering (admin only)."""
    from app.services.subscription_service import SubscriptionService
    result = await SubscriptionService.list_transaction_logs(skip, limit, search, status, type)
    return api_response(
        success=True,
        message="Transactions retrieved successfully.",
        data=result
    )
