import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app


@pytest.mark.asyncio
async def test_health_check():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_register_user():
    """Test user registration endpoint."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        payload = {
            "full_name": "Test Dentist",
            "email": "test@example.com",
            "password": "securepassword123",
        }
        response = await client.post("/api/v1/auth/register", json=payload)
    # 201 on first register, 409 if run twice
    assert response.status_code in (201, 409)


@pytest.mark.asyncio
async def test_login_invalid_credentials():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        form = {"username": "wrong@example.com", "password": "badpassword"}
        response = await client.post("/api/v1/auth/login", data=form)
    assert response.status_code == 401
