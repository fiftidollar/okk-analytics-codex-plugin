"""Native OKK login bridge tests."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from okk_mcp.config import Settings
from okk_mcp.platform_client import (
    AccountContext,
    OKKAuthenticationError,
    OKKPlatformClient,
    OKKUnavailable,
)


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
                    "email": "user@example.com",
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


@pytest.mark.anyio
async def test_phone_lookup_sends_number_only_in_post_body():
    phone = "79991234567"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/v1/calls/phone-lookup"
        assert not request.url.query
        assert json.loads(request.content) == {"phone_number": phone, "page": 1}
        return httpx.Response(200, json={"items": [], "total": 0})

    client = OKKPlatformClient(Settings(okk_api_base_url="https://okk.example/api/v1"))
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://okk.example/api/v1",
        transport=httpx.MockTransport(handler),
    )
    context = AccountContext(uuid4(), "user-1", "user@example.com", "admin", (), "access")
    assert await client.phone_lookup_with_context(context, {"phone_number": phone, "page": 1}) == {
        "items": [],
        "total": 0,
    }
    await client.close()


@pytest.mark.anyio
async def test_phone_lookup_missing_upstream_route_is_not_reported_as_no_call():
    client = OKKPlatformClient(Settings(okk_api_base_url="https://okk.example/api/v1"))
    await client.client.aclose()
    client.client = httpx.AsyncClient(
        base_url="https://okk.example/api/v1",
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )
    context = AccountContext(uuid4(), "user-1", "user@example.com", "admin", (), "access")
    with pytest.raises(OKKUnavailable, match="contract"):
        await client.phone_lookup_with_context(context, {"phone_number": "79991234567"})
    await client.close()
