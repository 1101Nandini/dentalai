import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.services.analytics_service import AnalyticsService

@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    await connect_to_mongo()
    yield
    await close_mongo_connection()

@pytest_asyncio.fixture
async def db():
    return await get_database()

@pytest.mark.asyncio
async def test_analytics_dashboard_insights(db):
    start_date = datetime.now(timezone.utc) - timedelta(days=7)
    end_date = datetime.now(timezone.utc)
    
    data = await AnalyticsService.get_dashboard_insights(start_date, end_date)
    
    # Verify standard and new premium insight keys
    assert "total_users" in data
    assert "active_trial_subscriptions" in data
    assert "active_paid_subscriptions" in data
    assert "active_subscriptions_by_plan" in data
    assert "total_users_had_trial" in data
    assert "trial_to_paid_conversion_count" in data
    assert "trial_to_paid_conversion_rate" in data
    assert "estimated_mrr" in data
    assert "canceled_stripe_subscriptions" in data
    assert "pending_cancellation_stripe_subscriptions" in data
    assert "past_due_stripe_subscriptions" in data
    assert "average_messages_per_chat" in data
    assert "average_chats_per_user" in data
    assert "total_voice_streaming_sessions" in data
    assert "dynamic_sections" in data
    assert "individual_users_count" in data
    assert "organization_users_count" in data
    assert "active_org_subscriptions" in data
    assert "active_individual_subscriptions" in data
    assert "active_org_trials" in data
    
    # Verify correct data types
    assert isinstance(data["active_trial_subscriptions"], int)
    assert isinstance(data["active_paid_subscriptions"], int)
    assert isinstance(data["active_subscriptions_by_plan"], dict)
    assert isinstance(data["estimated_mrr"], float)
    assert isinstance(data["trial_to_paid_conversion_rate"], float)
    assert isinstance(data["average_messages_per_chat"], float)
    assert isinstance(data["average_chats_per_user"], float)
    assert isinstance(data["individual_users_count"], int)
    assert isinstance(data["organization_users_count"], int)
    assert isinstance(data["active_org_subscriptions"], int)
    assert isinstance(data["active_individual_subscriptions"], int)
    assert isinstance(data["active_org_trials"], int)
    assert isinstance(data["dynamic_sections"], list)
    
    # Verify dynamic section items
    assert len(data["dynamic_sections"]) > 0
    first_section = data["dynamic_sections"][0]
    assert "title" in first_section
    assert "type" in first_section
    assert "widgets" in first_section
    assert isinstance(first_section["widgets"], list)
    assert len(first_section["widgets"]) > 0
    assert first_section["widgets"][0]["type"] in ["card", "chart"]


@pytest.mark.asyncio
async def test_analytics_dashboard_insights_for_org_admin(db):
    start_date = datetime.now(timezone.utc) - timedelta(days=7)
    end_date = datetime.now(timezone.utc)
    org_id = "507f1f77bcf86cd799439011"
    
    data = await AnalyticsService.get_dashboard_insights(start_date, end_date, org_id=org_id)
    
    assert "dynamic_sections" in data
    
    # Org admin dashboard currently only has Volume Metrics with active users; other widgets/sections are commented out
    volume_section = next((s for s in data["dynamic_sections"] if s["title"] == "Volume Metrics"), None)
    assert volume_section is not None
    widget_ids = [w["id"] for w in volume_section["widgets"]]
    assert widget_ids == ["active_users"]
    
    # Verify token efficiency and charts are excluded for org admins
    charts_section = next((s for s in data["dynamic_sections"] if s["type"] == "charts_grid"), None)
    assert charts_section is None
    
    token_section = next((s for s in data["dynamic_sections"] if s["title"] == "Token Efficiency"), None)
    assert token_section is None


