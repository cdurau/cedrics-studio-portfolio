import pytest
import os
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import source.database as database
from source.main import app, lifespan
from source.models import Entry
from source.database import Base, get_db

@pytest.fixture
def override_db(tmp_path):
    db_path = tmp_path / "test.db"
    test_db_url = f"sqlite:///{db_path}"

    test_engine = create_engine(
        test_db_url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        hide_parameters=True
    )
    assert test_engine.hide_parameters is True, "Engine should hide parameters"

    TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    def override_get_db():
        db = TestSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    app.dependency_overrides.clear()

@pytest.mark.asyncio
async def test_healthz(override_db):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

@pytest.mark.asyncio
async def test_identity_headers_and_crud(override_db):
    headers = {
        "x-authentik-username": "testuser",
        "x-authentik-email": "test@example.com",
        "x-authentik-name": "Test User",
        "x-authentik-uid": "uid123",
        "x-authentik-groups": "group1,group2"
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        res = await ac.post("/api/entries", json={"content": "Identity Test"}, headers=headers)
        assert res.status_code == 200
        data = res.json()
        assert data["content"] == "Identity Test"
        assert data["author"] == "testuser"

        res2 = await ac.get("/api/entries")
        assert res2.status_code == 200
        assert len(res2.json()) == 1

        res3 = await ac.get("/", headers=headers)
        assert res3.status_code == 200
        text = res3.text
        assert "Welcome, Test User" in text
        assert "Identity Test" in text
        assert "testuser" in text
        assert 'action="api/entries"' in text
        assert "fetch('api/entries'" in text

@pytest.mark.asyncio
async def test_lifespan(override_db):
    with patch("source.main.engine.dispose") as mock_dispose:
        async with lifespan(app):
            pass
        mock_dispose.assert_called_once()
