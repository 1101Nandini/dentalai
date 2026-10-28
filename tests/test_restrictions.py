import pytest
import pytest_asyncio
import json
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.services.restriction_service import RestrictionService
from app.services.message_service.message_service import MessageService

@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    await connect_to_mongo()
    yield
    await close_mongo_connection()

@pytest_asyncio.fixture
async def db():
    return await get_database()

@pytest.mark.asyncio
async def test_trial_vs_paid_restrictions(db):
    trial_user_id = str(ObjectId())
    paid_user_id = str(ObjectId())
    
    # Pre-clean any leftover test records
    await db["users"].delete_many({"email": {"$in": ["trial@example.com", "paid@example.com"]}})
    
    # Create test users
    await db["users"].insert_many([
        {"_id": ObjectId(trial_user_id), "email": "trial@example.com", "account_type": "individual"},
        {"_id": ObjectId(paid_user_id), "email": "paid@example.com", "account_type": "individual"},
    ])
    
    # Create paid subscription for paid user
    await db["subscriptions"].insert_one({
        "user_id": paid_user_id,
        "plan": "pro",
        "is_trial": False,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    })
    
    try:
        # Test trial detection
        assert await RestrictionService.is_on_trial_plan(trial_user_id) is True
        assert await RestrictionService.is_on_trial_plan(paid_user_id) is False
        
        # Test check with 0 messages
        is_restricted, msg, code = await RestrictionService.check_message_restriction(trial_user_id)
        assert is_restricted is False
        
        # Create chats
        trial_chat = {"user_id": trial_user_id, "title": "Trial Chat", "created_at": datetime.now(timezone.utc)}
        await db["chats"].insert_one(trial_chat)
        trial_chat_id = str(trial_chat["_id"])
        
        paid_chat = {"user_id": paid_user_id, "title": "Paid Chat", "created_at": datetime.now(timezone.utc)}
        await db["chats"].insert_one(paid_chat)
        paid_chat_id = str(paid_chat["_id"])
        
        # Add messages for trial user
        now_utc = datetime.now(timezone.utc)
        yesterday_utc = now_utc - timedelta(days=1)
        
        # Create 2 messages yesterday (should not count for today's quota)
        await db["messages"].insert_many([
            {"chat_id": trial_chat_id, "role": "user", "content": "Old 1", "created_at": yesterday_utc},
            {"chat_id": trial_chat_id, "role": "user", "content": "Old 2", "created_at": yesterday_utc},
        ])
        
        # Create 9 messages today
        trial_messages = []
        for i in range(9):
            trial_messages.append({
                "chat_id": trial_chat_id,
                "role": "user",
                "content": f"Trial Msg {i}",
                "created_at": now_utc
            })
        await db["messages"].insert_many(trial_messages)
        
        # Check count and restriction for trial user (should be 9 messages, not restricted)
        assert await RestrictionService.get_daily_message_count(trial_user_id) == 9
        is_restricted, msg, code = await RestrictionService.check_message_restriction(trial_user_id)
        assert is_restricted is False
        
        # Add 1 more message for trial user (total 10 user messages today)
        await db["messages"].insert_one({
            "chat_id": trial_chat_id,
            "role": "user",
            "content": "Trial Msg 10",
            "created_at": now_utc
        })
        
        # Now trial user should be restricted
        assert await RestrictionService.get_daily_message_count(trial_user_id) == 10
        is_restricted, msg, code = await RestrictionService.check_message_restriction(trial_user_id)
        assert is_restricted is True
        assert code == "quota_exceeded"
        
        # Paid user gets 11 messages today (should never be restricted)
        paid_messages = []
        for i in range(11):
            paid_messages.append({
                "chat_id": paid_chat_id,
                "role": "user",
                "content": f"Paid Msg {i}",
                "created_at": now_utc
            })
        await db["messages"].insert_many(paid_messages)
        
        assert await RestrictionService.get_daily_message_count(paid_user_id) == 11
        is_restricted, msg, code = await RestrictionService.check_message_restriction(paid_user_id)
        assert is_restricted is False
        
    finally:
        # Clean up
        await db["users"].delete_many({"_id": {"$in": [ObjectId(trial_user_id), ObjectId(paid_user_id)]}})
        await db["subscriptions"].delete_many({"user_id": paid_user_id})
        await db["chats"].delete_many({"user_id": {"$in": [trial_user_id, paid_user_id]}})
        await db["messages"].delete_many({"chat_id": {"$in": [trial_chat_id, paid_chat_id]}})

@pytest.mark.asyncio
async def test_message_service_stream_response_restriction(db):
    trial_user_id = str(ObjectId())
    
    # Pre-clean any leftover test records
    await db["users"].delete_many({"email": "stream_trial@example.com"})
    
    await db["users"].insert_one({
        "_id": ObjectId(trial_user_id),
        "email": "stream_trial@example.com",
        "account_type": "individual"
    })
    
    chat = {"user_id": trial_user_id, "title": "Stream Chat", "created_at": datetime.now(timezone.utc)}
    await db["chats"].insert_one(chat)
    chat_id = str(chat["_id"])
    
    try:
        # Insert 10 user messages today
        now_utc = datetime.now(timezone.utc)
        messages = []
        for i in range(10):
            messages.append({
                "chat_id": chat_id,
                "role": "user",
                "content": f"Stream Msg {i}",
                "created_at": now_utc
            })
        await db["messages"].insert_many(messages)
        
        # Execute stream_response and collect events
        events = []
        async for event in MessageService.stream_response(chat_id=chat_id, query="Blocked question"):
            events.append(event)
            
        # Verify it yielded exactly the quota_exceeded event and stopped
        assert len(events) == 1
        event_data = json.loads(events[0].strip())
        assert event_data["event"] == "error"
        assert event_data["code"] == "quota_exceeded"
        assert "trial plan only includes 10 messages a day" in event_data["content"]
        
    finally:
        await db["users"].delete_one({"_id": ObjectId(trial_user_id)})
        await db["chats"].delete_one({"_id": ObjectId(chat_id)})
        await db["messages"].delete_many({"chat_id": chat_id})

@pytest.mark.asyncio
async def test_skip_trial(db):
    user_id = str(ObjectId())
    
    # Pre-clean
    await db["users"].delete_many({"email": "skip_trial@example.com"})
    
    # 1. Create a dummy user
    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": "skip_trial@example.com",
        "account_type": "individual"
    })
    
    # 2. Create a trial subscription for this user
    trial_sub = {
        "user_id": user_id,
        "plan": "pro",
        "plan_id": "pro-plan-id",
        "is_active": True,
        "start_date": datetime.now(timezone.utc),
        "end_date": datetime.now(timezone.utc) + timedelta(days=14),
        "is_trial": True,
        "trial_end_date": datetime.now(timezone.utc) + timedelta(days=14),
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc)
    }
    await db["subscriptions"].insert_one(trial_sub)
    
    try:
        # Check that user is on trial plan first
        assert await RestrictionService.is_on_trial_plan(user_id) is True
        
        # 3. Call skip_trial
        from app.services.subscription_service import SubscriptionService
        result = await SubscriptionService.skip_trial(user_id)
        assert result["success"] is True
        
        # 4. Check that user is no longer on trial plan
        assert await RestrictionService.is_on_trial_plan(user_id) is False
        
        # 5. Fetch updated subscription to verify fields
        updated_sub = await SubscriptionService.get_active_subscription(user_id)
        assert updated_sub["is_trial"] is False
        assert updated_sub["trial_end_date"] is None
        updated_end = updated_sub["end_date"]
        if updated_end.tzinfo is None:
            updated_end = updated_end.replace(tzinfo=timezone.utc)
        assert updated_end > datetime.now(timezone.utc) + timedelta(days=28) # extended end_date
        
    finally:
        # Cleanup
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["subscriptions"].delete_many({"user_id": user_id})

