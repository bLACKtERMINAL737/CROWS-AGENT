import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crows.security import LocalAPIProtection


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
async def test_api_documentation_requires_token(monkeypatch, path):
    monkeypatch.setenv("AETHER_API_TOKEN", "test-token")
    app = FastAPI()
    app.add_middleware(LocalAPIProtection)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8000",
    ) as client:
        unauthenticated = await client.get(path)
        authenticated = await client.get(path, headers={"X-Aether-Token": "test-token"})

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
