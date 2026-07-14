"""Native OKK login bridge tests."""

from __future__ import annotations

import json

import httpx
import pytest

from okk_mcp.config import Settings
from okk_mcp.platform_client import OKKAuthenticationError, OKKPlatformClient


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_password_is_forwarded_once_and_never_persisted():
    observed: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/auth/login"
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "access_token": "okk-access",
                "user": {
                    "id": "user-1",
                    "role": "viewer",
                    "department_ids": ["department-1"],
                    "is_active": True,
                },
            },
            headers={"set-cookie": "refresh_token=okk-refresh; HttpOnly; Path=/api/v1/auth"},
        )

    client = OKKPlatformClient(Settings(okk_api_base_url="https://okk.example/api/v1"))
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://okk.example/api/v1",
        transport=httpx.MockTransport(handler),
    )
    session = await client.authenticate("user@example.com", "plain-password")
    await client.close()

    assert observed == {"email": "user@example.com", "password": "plain-password"}
    assert not hasattr(session, "password")
    assert "plain-password" not in session.encrypted_access_token
    assert "plain-password" not in session.encrypted_refresh_token
    assert client.cipher.open(session.encrypted_access_token) == "okk-access"
    assert client.cipher.open(session.encrypted_refresh_token) == "okk-refresh"


@pytest.mark.anyio
async def test_invalid_login_returns_generic_authentication_error():
    client = OKKPlatformClient(Settings(okk_api_base_url="https://okk.example/api/v1"))
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://okk.example/api/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(401)),
    )
    with pytest.raises(OKKAuthenticationError, match="Invalid OKK login or password"):
        await client.authenticate("user@example.com", "wrong")
    await client.close()


@pytest.mark.anyio
async def test_analytics_client_rejects_auth_and_non_normalized_paths_before_network():
    client = OKKPlatformClient(Settings())
    with pytest.raises(ValueError):
        await client.get("session", "/auth/users")
    with pytest.raises(ValueError):
        await client.get("session", "/departments/../auth/users")
    with pytest.raises(ValueError):
        await client.get("session", "https://example.com/")
    await client.close()
