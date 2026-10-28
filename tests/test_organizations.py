import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from app.core.database import get_database, connect_to_mongo, close_mongo_connection
from app.services.organization_service import OrganizationService

@pytest_asyncio.fixture(scope="function", autouse=True)
async def setup_db():
    await connect_to_mongo()
    yield
    await close_mongo_connection()

@pytest_asyncio.fixture
async def db():
    return await get_database()

@pytest.mark.asyncio
async def test_organization_plan_details(db):
    # Setup mock organization
    org_id = str(ObjectId())
    now = datetime.now(timezone.utc)
    mock_org = {
        "_id": ObjectId(org_id),
        "name": "Test Dental Clinic Plan Details",
        "email": "contact@testdental.com",
        "is_active": True,
        "is_individual_organization": False,
        "created_at": now,
        "updated_at": now
    }
    await db["organizations"].insert_one(mock_org)

    # Setup mock active subscription
    expiry_date = now + timedelta(days=30)
    mock_sub = {
        "organization_id": org_id,
        "is_active": True,
        "plan": "organization_monthly",
        "is_trial": False,
        "end_date": expiry_date,
        "created_at": now
    }
    await db["subscriptions"].insert_one(mock_sub)

    try:
        # Test OrganizationService.list_organizations
        list_res = await OrganizationService.list_organizations(search="Test Dental Clinic Plan Details")
        assert list_res["total"] >= 1
        
        found_org = None
        for org in list_res["organizations"]:
            if org["id"] == org_id:
                found_org = org
                break
                
        assert found_org is not None
        assert found_org["plan_detail"] is not None
        assert found_org["plan_detail"]["plan"] == "organization_monthly"
        assert found_org["plan_detail"]["is_trial"] is False
        assert found_org["plan_detail"]["status"] == "ACTIVE"
        assert "expiry_date" in found_org["plan_detail"]

        # Test OrganizationService.get_organization
        get_res = await OrganizationService.get_organization(org_id)
        assert get_res["plan_detail"] is not None
        assert get_res["plan_detail"]["plan"] == "organization_monthly"
        assert get_res["plan_detail"]["is_trial"] is False
        assert get_res["plan_detail"]["status"] == "ACTIVE"
        
    finally:
        # Clean up
        await db["organizations"].delete_one({"_id": ObjectId(org_id)})
        await db["subscriptions"].delete_one({"organization_id": org_id})
