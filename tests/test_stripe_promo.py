import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.services.payment_service.core import PaymentServiceCore
from app.services.payment_service.handlers import (
    handle_checkout_completed,
    handle_invoice_upcoming,
    handle_invoice_failed
)
from app.services.subscription_service import SubscriptionService

@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    await connect_to_mongo()
    yield
    await close_mongo_connection()

@pytest_asyncio.fixture
async def db():
    return await get_database()


@pytest.mark.asyncio
async def test_validate_promotion_code_valid():
    mock_coupon = MagicMock()
    mock_coupon.percent_off = 100.0
    mock_coupon.amount_off = None
    mock_coupon.currency = "usd"
    mock_coupon.duration = "repeating"
    mock_coupon.duration_in_months = 1

    mock_promo = MagicMock()
    mock_promo.code = "FREEMONTH100"
    mock_promo.coupon = mock_coupon

    mock_list_response = MagicMock()
    mock_list_response.data = [mock_promo]

    with patch("stripe.PromotionCode.list", return_value=mock_list_response):
        result = await PaymentServiceCore.validate_promotion_code("FREEMONTH100")
        assert result["valid"] is True
        assert result["code"] == "FREEMONTH100"
        assert result["percent_off"] == 100.0
        assert result["duration_in_months"] == 1


@pytest.mark.asyncio
async def test_validate_promotion_code_invalid():
    mock_list_response = MagicMock()
    mock_list_response.data = []

    with patch("stripe.PromotionCode.list", return_value=mock_list_response):
        result = await PaymentServiceCore.validate_promotion_code("INVALID999")
        assert result["valid"] is False
        assert "Invalid" in result["message"]


@pytest.mark.asyncio
async def test_create_checkout_session_with_promo_suppresses_trial(db):
    user_id = str(ObjectId())
    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": "promouser@example.com",
        "account_type": "individual",
        "role": "individual",
        "has_had_trial": False
    })
    
    # Ensure plan exists with trial_period
    await db["plans"].update_one(
        {"plan_id": "test_promo_plan"},
        {"$set": {
            "name": "test_promo_plan",
            "price": 99.0,
            "currency": "usd",
            "trial_period": 14,
            "is_active": True
        }},
        upsert=True
    )

    mock_promo = MagicMock()
    mock_promo.id = "promo_123"
    mock_promo.code = "FREEMONTH100"
    mock_promo.coupon = MagicMock(percent_off=100.0, amount_off=None)
    mock_promo_list = MagicMock(data=[mock_promo])

    mock_session = MagicMock()
    mock_session.id = "cs_test_promo"
    mock_session.url = "https://checkout.stripe.com/test_promo"

    captured_kwargs = {}
    def mock_create(**kwargs):
        nonlocal captured_kwargs
        captured_kwargs = kwargs
        return mock_session

    try:
        with patch("stripe.PromotionCode.list", return_value=mock_promo_list), \
             patch("stripe.checkout.Session.create", side_effect=mock_create):
            
            res = await PaymentServiceCore.create_checkout_session(
                user_id=user_id,
                plan="test_promo_plan",
                success_url="http://test/success",
                cancel_url="http://test/cancel",
                promo_code="FREEMONTH100"
            )
            assert res["session_id"] == "cs_test_promo"
            assert captured_kwargs.get("payment_method_collection") == "if_required"
            assert captured_kwargs.get("discounts") == [{"promotion_code": "promo_123"}]
            # Verify trial period is SUPPRESSED because promo code is applied
            sub_data = captured_kwargs.get("subscription_data", {})
            assert "trial_period_days" not in sub_data

    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["plans"].delete_one({"plan_id": "test_promo_plan"})
        await db["transaction_logs"].delete_many({"session_id": "cs_test_promo"})


@pytest.mark.asyncio
async def test_create_checkout_session_general_trial_requires_card(db):
    user_id = str(ObjectId())
    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": f"trialuser_{user_id}@example.com",
        "account_type": "individual",
        "role": "individual",
        "has_had_trial": False
    })
    
    await db["plans"].update_one(
        {"plan_id": "test_trial_plan"},
        {"$set": {
            "name": "test_trial_plan",
            "price": 99.0,
            "currency": "usd",
            "trial_period": 14,
            "is_active": True
        }},
        upsert=True
    )

    mock_session = MagicMock(id="cs_trial_card", url="https://checkout.stripe.com/trial_card")
    captured_kwargs = {}
    def mock_create(**kwargs):
        nonlocal captured_kwargs
        captured_kwargs = kwargs
        return mock_session

    try:
        with patch("stripe.checkout.Session.create", side_effect=mock_create):
            res = await PaymentServiceCore.create_checkout_session(
                user_id=user_id,
                plan="test_trial_plan",
                success_url="http://test/success",
                cancel_url="http://test/cancel"
            )
            assert res["session_id"] == "cs_trial_card"
            # General trial MUST strictly require credit card details
            assert captured_kwargs.get("payment_method_collection") == "always"
            sub_data = captured_kwargs.get("subscription_data", {})
            assert sub_data.get("trial_period_days") == 14
    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["plans"].delete_one({"plan_id": "test_trial_plan"})
        await db["transaction_logs"].delete_many({"session_id": "cs_trial_card"})


@pytest.mark.asyncio
async def test_promo_checkout_completed_sets_has_had_trial_and_prevents_month2_trial(db):
    user_id = str(ObjectId())
    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": "month2user@example.com",
        "account_type": "individual",
        "role": "individual",
        "has_had_trial": False
    })
    
    await db["plans"].update_one(
        {"plan_id": "test_month2_plan"},
        {"$set": {
            "name": "test_month2_plan",
            "price": 99.0,
            "currency": "usd",
            "trial_period": 14,
            "is_active": True
        }},
        upsert=True
    )

    now = datetime.now(timezone.utc)
    event_data = {
        "data": {
            "object": {
                "id": "cs_completed_promo",
                "client_reference_id": user_id,
                "subscription": "sub_stripe_123",
                "customer": "cus_stripe_123",
                "metadata": {"plan": "test_month2_plan", "promo_code": "FREEMONTH100"}
            }
        }
    }

    try:
        # 1. Complete promo checkout
        await handle_checkout_completed(event_data, db, now)

        # Verify has_had_trial is now True in MongoDB
        updated_user = await db["users"].find_one({"_id": ObjectId(user_id)})
        assert updated_user["has_had_trial"] is True

        # 2. Month 2 checkout (no promo code, user enters card)
        mock_session = MagicMock(id="cs_month2", url="https://checkout.stripe.com/month2")
        captured_kwargs = {}
        def mock_create(**kwargs):
            nonlocal captured_kwargs
            captured_kwargs = kwargs
            return mock_session

        with patch("stripe.checkout.Session.create", side_effect=mock_create):
            await PaymentServiceCore.create_checkout_session(
                user_id=user_id,
                plan="test_month2_plan",
                success_url="http://test/success",
                cancel_url="http://test/cancel"
            )

            # In Month 2, because has_had_trial is True, trial_period_days MUST NOT be added
            sub_data = captured_kwargs.get("subscription_data", {})
            assert "trial_period_days" not in sub_data

    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["plans"].delete_one({"plan_id": "test_month2_plan"})
        await db["transaction_logs"].delete_many({"session_id": {"$in": ["cs_completed_promo", "cs_month2"]}})


@pytest.mark.asyncio
async def test_handle_invoice_failed_deactivates_and_cancels_sub(db):
    user_id = str(ObjectId())
    stripe_sub_id = "sub_failed_test"
    now = datetime.now(timezone.utc)

    await db["stripe_subscriptions"].insert_one({
        "user_id": user_id,
        "stripe_subscription_id": stripe_sub_id,
        "status": "active",
        "created_at": now,
        "updated_at": now
    })
    await db["subscriptions"].insert_one({
        "user_id": user_id,
        "stripe_subscription_id": stripe_sub_id,
        "is_active": True,
        "plan": "pro",
        "created_at": now,
        "updated_at": now
    })

    event_data = {
        "data": {
            "object": {
                "id": "in_failed_test",
                "subscription": stripe_sub_id,
                "amount_due": 9900,
                "currency": "usd"
            }
        }
    }

    mock_cancel = MagicMock()
    with patch("stripe.Subscription.cancel", mock_cancel):
        await handle_invoice_failed(event_data, db, now)

        # Stripe subscription canceled in Stripe API
        mock_cancel.assert_called_once_with(stripe_sub_id)

        # Internal subscription deactivated
        sub = await db["subscriptions"].find_one({"stripe_subscription_id": stripe_sub_id})
        assert sub["is_active"] is False

        stripe_sub = await db["stripe_subscriptions"].find_one({"stripe_subscription_id": stripe_sub_id})
        assert stripe_sub["status"] == "past_due"

    await db["subscriptions"].delete_many({"stripe_subscription_id": stripe_sub_id})
    await db["stripe_subscriptions"].delete_many({"stripe_subscription_id": stripe_sub_id})
    await db["transaction_logs"].delete_many({"session_id": "in_failed_test"})


@pytest.mark.asyncio
async def test_check_expiring_promo_subscriptions_dispatches_email(db):
    user_id = str(ObjectId())
    test_email = f"expiring_{user_id}@example.com"
    now = datetime.now(timezone.utc)
    three_days_later = now + timedelta(days=2, hours=12)

    await db["users"].delete_many({"email": {"$in": ["expiring@example.com", test_email]}})

    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": test_email,
        "full_name": "Dr. Expiring",
        "account_type": "individual"
    })
    sub_id = await db["subscriptions"].insert_one({
        "user_id": user_id,
        "is_active": True,
        "end_date": three_days_later,
        "expiring_email_sent": False,
        "plan": "pro",
        "created_at": now,
        "updated_at": now
    })

    mock_send = AsyncMock()
    try:
        with patch("app.background.email_tasks.send_subscription_expiring_email", mock_send):
            res = await SubscriptionService.check_expiring_promo_subscriptions()
            assert res["status"] == "success"
            assert res["notified_count"] == 1

            mock_send.assert_called_once()
            args, kwargs = mock_send.call_args
            assert kwargs["to_email"] == test_email
            assert kwargs["days_left"] <= 3

            # Verify flag set to prevent duplicates
            updated_sub = await db["subscriptions"].find_one({"_id": sub_id.inserted_id})
            assert updated_sub["expiring_email_sent"] is True
    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["subscriptions"].delete_one({"_id": sub_id.inserted_id})


@pytest.mark.asyncio
async def test_create_subscription_fetches_once_repeating_forever_duration(db):
    user_id = str(ObjectId())
    now = datetime.now(timezone.utc)

    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": f"dur_{user_id}@example.com",
        "account_type": "individual",
        "role": "dentist"
    })

    try:
        # Case 1: "once" coupon (1 month)
        mock_sub_once = MagicMock()
        mock_sub_once.discount = MagicMock()
        mock_sub_once.discount.coupon = MagicMock()
        mock_sub_once.discount.coupon.id = "c_once"
        mock_sub_once.discount.coupon.name = "1 Month Free"
        mock_sub_once.discount.coupon.duration = "once"
        mock_sub_once.discount.coupon.duration_in_months = None
        mock_sub_once.discount.coupon.percent_off = 100.0
        mock_sub_once.discount.coupon.amount_off = None
        mock_sub_once.discount.promotion_code = MagicMock(code="ONEMONTH")
        mock_sub_once.discount.end = None
        mock_sub_once.metadata = {}

        with patch("stripe.Subscription.retrieve", return_value=mock_sub_once):
            sub1 = await SubscriptionService.create_subscription(
                user_id=user_id,
                plan="pro",
                stripe_subscription_id="sub_once_123"
            )
            assert sub1["coupon_duration"] == "once"
            assert sub1["coupon_duration_in_months"] == 1
            assert sub1["coupon_percent_off"] == 100.0
            assert sub1["is_trial"] is False

        # Case 2: "repeating" coupon (3 months)
        mock_sub_rep = MagicMock()
        mock_sub_rep.discount = MagicMock()
        mock_sub_rep.discount.coupon = MagicMock()
        mock_sub_rep.discount.coupon.id = "c_rep"
        mock_sub_rep.discount.coupon.name = "3 Months Promo"
        mock_sub_rep.discount.coupon.duration = "repeating"
        mock_sub_rep.discount.coupon.duration_in_months = 3
        mock_sub_rep.discount.coupon.percent_off = 50.0
        mock_sub_rep.discount.coupon.amount_off = None
        mock_sub_rep.discount.promotion_code = MagicMock(code="SAVE3MOS")
        mock_sub_rep.discount.end = int((now + timedelta(days=90)).timestamp())
        mock_sub_rep.metadata = {}

        with patch("stripe.Subscription.retrieve", return_value=mock_sub_rep):
            sub2 = await SubscriptionService.create_subscription(
                user_id=user_id,
                plan="pro",
                stripe_subscription_id="sub_rep_123"
            )
            assert sub2["coupon_duration"] == "repeating"
            assert sub2["coupon_duration_in_months"] == 3
            assert sub2["coupon_percent_off"] == 50.0
            assert sub2["discount_end"] is not None

        # Case 3: "forever" coupon
        mock_sub_forev = MagicMock()
        mock_sub_forev.discount = MagicMock()
        mock_sub_forev.discount.coupon = MagicMock()
        mock_sub_forev.discount.coupon.id = "c_forever"
        mock_sub_forev.discount.coupon.name = "Lifetime Access"
        mock_sub_forev.discount.coupon.duration = "forever"
        mock_sub_forev.discount.coupon.duration_in_months = None
        mock_sub_forev.discount.coupon.percent_off = 100.0
        mock_sub_forev.discount.coupon.amount_off = None
        mock_sub_forev.discount.promotion_code = MagicMock(code="LIFETIME")
        mock_sub_forev.discount.end = None
        mock_sub_forev.metadata = {}

        with patch("stripe.Subscription.retrieve", return_value=mock_sub_forev):
            sub3 = await SubscriptionService.create_subscription(
                user_id=user_id,
                plan="pro",
                stripe_subscription_id="sub_forever_123"
            )
            assert sub3["coupon_duration"] == "forever"
            assert sub3["coupon_duration_in_months"] is None
            assert sub3["coupon_percent_off"] == 100.0

        # Case 4: check_expiring_promo_subscriptions skips forever coupons
        await db["subscriptions"].update_one(
            {"_id": ObjectId(sub3["id"])},
            {"$set": {"end_date": now + timedelta(days=2, hours=12), "expiring_email_sent": False}}
        )
        mock_email = AsyncMock()
        with patch("app.background.email_tasks.send_subscription_expiring_email", mock_email):
            await SubscriptionService.check_expiring_promo_subscriptions()
            mock_email.assert_not_called()
    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["subscriptions"].delete_many({"user_id": user_id})


@pytest.mark.asyncio
async def test_validate_promotion_code_rejects_returning_user(db):
    user_id = str(ObjectId())
    now = datetime.now(timezone.utc)
    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": f"returning_{user_id}@example.com",
        "account_type": "individual",
        "has_had_trial": True
    })

    mock_coupon = MagicMock()
    mock_coupon.percent_off = 100.0
    mock_coupon.amount_off = None
    mock_coupon.currency = "usd"
    mock_coupon.duration = "once"
    mock_coupon.duration_in_months = 1

    mock_promo = MagicMock()
    mock_promo.code = "FREEMONTH100"
    mock_promo.coupon = mock_coupon
    mock_promo.restrictions = {"first_time_transaction": True}

    mock_list_response = MagicMock(data=[mock_promo])

    try:
        with patch("stripe.PromotionCode.list", return_value=mock_list_response):
            result = await PaymentServiceCore.validate_promotion_code("FREEMONTH100", user_id=user_id)
            assert result["valid"] is False
            assert "only valid for first-time subscribers" in result["message"]
    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})


@pytest.mark.asyncio
async def test_create_checkout_session_blocks_and_hides_promo_for_returning_user(db):
    from fastapi import HTTPException
    user_id = str(ObjectId())
    await db["users"].insert_one({
        "_id": ObjectId(user_id),
        "email": f"blockpromo_{user_id}@example.com",
        "account_type": "individual",
        "has_had_trial": True
    })

    await db["plans"].update_one(
        {"plan_id": "test_block_plan"},
        {"$set": {
            "name": "test_block_plan",
            "price": 99.0,
            "currency": "usd",
            "is_active": True
        }},
        upsert=True
    )

    mock_coupon = MagicMock()
    mock_coupon.percent_off = 100.0
    mock_promo = MagicMock(id="promo_block", code="FREEMONTH100", coupon=mock_coupon, restrictions={"first_time_transaction": True})
    mock_promo_list = MagicMock(data=[mock_promo])

    try:
        # 1. Applying first-time promo code should raise HTTPException 400
        with patch("stripe.PromotionCode.list", return_value=mock_promo_list):
            with pytest.raises(HTTPException) as exc_info:
                await PaymentServiceCore.create_checkout_session(
                    user_id=user_id,
                    plan="test_block_plan",
                    success_url="http://test/success",
                    cancel_url="http://test/cancel",
                    promo_code="FREEMONTH100"
                )
            assert exc_info.value.status_code == 400
            assert "first-time subscribers" in str(exc_info.value.detail)

        # 2. Checkout without promo code should NOT set allow_promotion_codes=True (hides coupon input in Stripe)
        captured_kwargs = {}
        def mock_create(**kwargs):
            nonlocal captured_kwargs
            captured_kwargs = kwargs
            return MagicMock(id="cs_returning", url="https://checkout.stripe.com/returning")

        with patch("stripe.checkout.Session.create", side_effect=mock_create):
            res = await PaymentServiceCore.create_checkout_session(
                user_id=user_id,
                plan="test_block_plan",
                success_url="http://test/success",
                cancel_url="http://test/cancel"
            )
            assert res["session_id"] == "cs_returning"
            assert "allow_promotion_codes" not in captured_kwargs
            assert "discounts" not in captured_kwargs
    finally:
        await db["users"].delete_one({"_id": ObjectId(user_id)})
        await db["plans"].delete_one({"plan_id": "test_block_plan"})
        await db["transaction_logs"].delete_many({"session_id": "cs_returning"})

