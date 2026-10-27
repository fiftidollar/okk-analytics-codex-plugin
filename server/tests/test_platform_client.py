"""Native OKK login bridge tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from okk_mcp import platform_client as platform_module
from okk_mcp.config import Settings
from okk_mcp.platform_client import (
    AccountContext,
    OKKAuthenticationError,
    OKKNotAvailable,
    OKKPlatformClient,
    OKKUnavailable,
)

OKK_URL = "https://okk.example/api/v1"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _platform(handler) -> OKKPlatformClient:
    return OKKPlatformClient(Settings(okk_api_base_url=OKK_URL), transport=httpx.MockTransport(handler))


def _context(access_token: str = "access") -> AccountContext:
    return AccountContext(uuid4(), "user-1", "user@example.com", "admin", (), access_token)


@pytest.mark.anyio
async def test_one_persons_refresh_cookie_never_reaches_another_persons_request():
    sent: list[tuple[str, str]] = []
    issued = iter(["refresh-a", "refresh-b", "refresh-b2"])

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, request.headers.get("cookie", "")))
        return httpx.Response(
            200,
            json={"access_token": "access", "user": {"id": "u", "email": "u@example.com", "role": "viewer"}},
            headers={"set-cookie": f"refresh_token={next(issued)}; HttpOnly; Secure; Path=/api/v1/auth"},
        )

    platform = _platform(handler)
    await platform.authenticate("a@example.com", "password-a")
    await platform.authenticate("b@example.com", "password-b")
    _access, rotated, _user = await platform._refresh("refresh-b")
    await platform.close()

    assert sent == [
        ("/api/v1/auth/login", ""),
        ("/api/v1/auth/login", ""),
        ("/api/v1/auth/refresh", "refresh_token=refresh-b"),
    ]
    assert rotated == "refresh-b2"


@pytest.mark.anyio
@pytest.mark.parametrize("renewed_token_accepted", [True, False])
async def test_okk_token_that_expired_during_a_tool_call_is_renewed_once(renewed_token_accepted):
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Bearer ")
        sent.append(token)
        if token == "renewed" and renewed_token_accepted:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(401)

    platform = _platform(handler)
    platform._rotate_session = AsyncMock(return_value=("renewed", {}))
    context = _context("expired")
    if renewed_token_accepted:
        assert await platform.get_with_context(context, "/calls") == {"items": []}
    else:
        with pytest.raises(OKKAuthenticationError):
            await platform.get_with_context(context, "/calls")
    await platform.close()

    assert sent == ["expired", "renewed"]
    platform._rotate_session.assert_awaited_once_with(context.session_id, "expired")


READS = {
    "get": lambda platform, context: platform.get_with_context(context, "/calls"),
    "post": lambda platform, context: platform.transcript_search_with_context(context, {"query": "доставка"}),
}


@pytest.mark.anyio
@pytest.mark.parametrize("read", sorted(READS))
@pytest.mark.parametrize(
    ("answers", "outcome"),
    [
        # A dropped connection, a timeout and 502/503/504 are repeated twice.
        ((503, "connect", 200), None),
        (("timeout", 502, 200), None),
        ((504, 503, 502), OKKUnavailable),
        (("connect", "timeout", "connect"), OKKUnavailable),
        # A 4xx is OKK's answer, not a failure in transit.
        ((400,), ValueError),
        ((403,), OKKNotAvailable),
        # OKK closes a restricted employee with 409: the neutral not_available, not a bad filter.
        ((409,), OKKNotAvailable),
    ],
)
async def test_read_repeats_only_failures_in_transit(monkeypatch, read, answers, outcome):
    monkeypatch.setattr(platform_module, "_READ_RETRY_DELAYS_SECONDS", (0, 0))
    pending = list(answers)

    def handler(request: httpx.Request) -> httpx.Response:
        answer = pending.pop(0)
        if answer == "connect":
            raise httpx.ConnectError("connection refused", request=request)
        if answer == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(answer, json={"items": []})

    platform = _platform(handler)
    if outcome is None:
        assert await READS[read](platform, _context()) == {"items": []}
    else:
        with pytest.raises(outcome):
            await READS[read](platform, _context())
    await platform.close()

    assert pending == []


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
