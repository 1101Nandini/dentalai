from typing import Optional
from fastapi import APIRouter, Depends
from app.core.response import api_response
from app.dependencies import get_current_user
from app.services.subscription_service import SubscriptionService
from app.schemas.subscription_schema import SubscriptionListResponse
from app.background.report_processor import log_audit_event

router = APIRouter()

@router.get("/")
async def list_my_subscriptions(
    skip: int = 0, 
    limit: int = 50, 
    current_user=Depends(get_current_user)
):
    """
    List all subscription plans for the authenticated user.
    """
    user_id = str(current_user["_id"])
    result = await SubscriptionService.list_user_subscriptions(user_id, skip, limit)
    
    return api_response(
        success=True,
        message="Subscriptions retrieved successfully.",
        data=result
    )

@router.post("/cancel")
async def cancel_subscription(current_user=Depends(get_current_user)):
    """
    Cancel an active subscription.
    """
    user_id = str(current_user["_id"])
    result = await SubscriptionService.cancel_subscription(user_id)
    
    await log_audit_event(
        actor_id=user_id,
        action="SUBSCRIPTION_CANCEL_REQUESTED",
        resource="subscriptions",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "result_message": result.get("message"),
        }
    )
    
    return api_response(
        success=True,
        message=result["message"],
        data=result
    )

@router.post("/resume")
async def resume_subscription(current_user=Depends(get_current_user)):
    """
    Resume/reactivate a subscription that is currently set to cancel at period end.
    """
    user_id = str(current_user["_id"])
    result = await SubscriptionService.resume_subscription(user_id)
    
    await log_audit_event(
        actor_id=user_id,
        action="SUBSCRIPTION_RESUME_REQUESTED",
        resource="subscriptions",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "result_message": result.get("message"),
        }
    )
    
    return api_response(
        success=True,
        message=result["message"],
        data=result
    )

@router.get("/plans")
async def list_available_plans(
    role: Optional[str] = None
):
    """
    Get all active subscription plans.
    """
    from app.core.database import get_database
    db = await get_database()
    
    query = {"is_active": True, "cycle": "monthly"}
    if role:
        query["allowed_roles"] = role
        
    plans_cursor = db["plans"].find(query)
    plans = []
    async for plan in plans_cursor:
        plan["id"] = str(plan.pop("_id"))
        plans.append(plan)
        
    return api_response(
        success=True,
        message="Plans retrieved successfully.",
        data=plans
    )

@router.post("/skip-trial")
async def skip_trial(current_user=Depends(get_current_user)):
    """
    Skip the trial phase of the current active subscription and start full benefits immediately.
    """
    user_id = str(current_user["_id"])
    result = await SubscriptionService.skip_trial(user_id)
    
    await log_audit_event(
        actor_id=user_id,
        action="SUBSCRIPTION_TRIAL_SKIPPED",
        resource="subscriptions",
        resource_id=user_id,
        metadata={
            "actor_email": current_user.get("email"),
            "actor_role": current_user.get("role"),
            "account_type": current_user.get("account_type"),
            "result_message": result.get("message"),
        }
    )
    
    return api_response(
        success=True,
        message=result["message"],
        data=result
    )

