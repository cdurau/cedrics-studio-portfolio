import pytest
from httpx import AsyncClient, ASGITransport
from source.main import app, lifespan

@pytest.mark.asyncio
async def test_healthz():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

@pytest.mark.asyncio
async def test_api_info_no_auth():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/info")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "python-service"
    assert data["authenticated"] is False

@pytest.mark.asyncio
async def test_api_info_with_auth():
    headers = {
        "x-authentik-username": "testuser",
        "x-authentik-email": "test@example.com",
        "x-authentik-name": "Test User",
        "x-authentik-uid": "uid-1",
        "x-authentik-groups": "group-a,group-b",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/api/info", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["authenticated"] is True

@pytest.mark.asyncio
async def test_lifespan():
    async with lifespan(app):
        assert app.state.cleanup_counter == 0
    assert app.state.cleanup_counter == 1
