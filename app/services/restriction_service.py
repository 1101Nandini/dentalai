from datetime import datetime, timezone
from typing import Optional, Tuple
from bson import ObjectId

from app.core.database import get_database
from app.services.subscription_service import SubscriptionService

class RestrictionService:
    @staticmethod
    async def is_on_trial_plan(user_id: str) -> bool:
        """
        Determine if the user is on a trial plan.
        A user is on a trial plan if:
        1. They have no active subscription.
        2. They have an active subscription where plan is 'free_trial' or is_trial is True.
        """
        active_sub = await SubscriptionService.get_active_subscription(user_id)
        if not active_sub:
            return True
        return active_sub.get("is_trial", False) or active_sub.get("plan") == "free_trial"

    @staticmethod
    async def get_daily_message_count(user_id: str) -> int:
        """
        Count the number of user messages sent today (UTC) across all chats.
        """
        db = await get_database()
        
        # 1. Get all chat IDs for the user
        chats = await db["chats"].find({"user_id": user_id}, {"_id": 1}).to_list(length=None)
        chat_ids = [str(chat["_id"]) for chat in chats]
        
        if not chat_ids:
            return 0
            
        # 2. Count user messages since start of today (UTC)
        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        
        count = await db["messages"].count_documents({
            "chat_id": {"$in": chat_ids},
            "role": "user",
            "created_at": {"$gte": start_of_day}
        })
        
        return count

    @staticmethod
    async def check_message_restriction(user_id: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Check if the user is restricted from sending any more messages.
        Returns:
            (is_restricted, error_message, error_code)
        """
        is_trial = await RestrictionService.is_on_trial_plan(user_id)
        if not is_trial:
            return False, None, None
            
        daily_count = await RestrictionService.get_daily_message_count(user_id)
        if daily_count >= 10:
            return True, "trial plan only includes 10 messages a day.", "quota_exceeded"
            
        return False, None, None
