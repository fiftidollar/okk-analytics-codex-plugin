"""OAuth security and MCP tool contract tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from okk_mcp import oauth
from okk_mcp.config import Settings
from okk_mcp.main import app
from okk_mcp.platform_client import OKKAuthenticationError
from okk_mcp.security import (
    redirect_origin,
    token_hash,
    validate_redirect_uri,
    validate_scopes,
    verify_pkce,
)
from okk_mcp.server import create_mcp_server


def test_pkce_s256_and_token_hash_contract():
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert verify_pkce(verifier, challenge)
    assert not verify_pkce("wrong" * 10, challenge)
    assert len(token_hash("opaque-token")) == 64
    assert token_hash("opaque-token") != "opaque-token"


@pytest.mark.parametrize(
    "uri",
    [
        "https://chatgpt.com/connector/callback",
        "http://127.0.0.1:3210/callback",
        "http://[::1]:3210/callback",
        "http://localhost:3210/callback",
    ],
)
def test_redirect_uri_allows_https_and_loopback(uri):
    assert validate_redirect_uri(uri) == uri


@pytest.mark.parametrize(
    "uri",
    [
        "http://example.com/callback",
        "javascript:alert(1)",
        "https://user:pass@example.com/callback",
        "https://example.com/callback#fragment",
        "https://example.com:invalid/callback",
        "https://example.com:0/callback",
        "https://evil.com;form-action */callback",
        "https://example.com\\@evil.example/callback",
        "https://example.com/callback\r\nform-action *",
        "http://[::1%25eth0]:3210/callback",
    ],
)
def test_redirect_uri_rejects_unsafe_values(uri):
    with pytest.raises(ValueError):
        validate_redirect_uri(uri)


@pytest.mark.parametrize(
    ("uri", "origin"),
    [
        ("https://Example.COM/callback", "https://example.com"),
        ("http://127.0.0.1:3210/callback", "http://127.0.0.1:3210"),
        ("http://[::1]:3210/callback", "http://[::1]:3210"),
        ("http://localhost:3210/callback", "http://localhost:3210"),
    ],
)
def test_redirect_origin_is_csp_safe_and_keeps_the_exact_port(uri, origin):
    assert redirect_origin(uri) == origin


def test_scopes_are_allowlisted_and_canonicalized():
    assert validate_scopes("okk.scenarios.read okk.statistics.read") == (
        "okk.scenarios.read okk.statistics.read"
    )
    with pytest.raises(ValueError):
        validate_scopes("okk.statistics.write")


def test_mcp_has_exact_typed_read_only_tool_inventory():
    settings = Settings()
    client = AsyncMock()
    mcp = create_mcp_server(settings, client)
    tools = asyncio.run(mcp.list_tools())
    assert [tool.name for tool in tools] == [
        "get_access_context",
        "get_statistics_catalog",
        "get_overview_statistics",
        "list_departments",
        "get_department_statistics",
        "compare_departments",
        "list_employees",
        "get_employee_card",
        "compare_employees",
        "get_call_statistics",
        "get_plan_fact_statistics",
        "get_client_statistics",
        "get_crm_statistics",
        "get_growth_insights",
        "get_mentoring_statistics",
        "list_scenarios",
        "get_scenario_criteria",
        "get_scenario_performance",
        "get_criterion_performance",
    ]
    for tool in tools:
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False
        assert tool.inputSchema["type"] == "object"
        assert tool.outputSchema["type"] == "object"
        assert tool.meta["securitySchemes"][0]["type"] == "oauth2"

    compare_schema = next(tool for tool in tools if tool.name == "compare_employees").inputSchema
    assert compare_schema["properties"]["employee_ids"]["minItems"] == 1
    assert compare_schema["properties"]["employee_ids"]["maxItems"] == 20


def test_mcp_transport_allows_only_the_configured_public_origin():
    settings = Settings(
        mcp_issuer_url="https://okk-mcp.akfixdev.ru",
        mcp_resource_url="https://okk-mcp.akfixdev.ru/mcp",
    )
    mcp = create_mcp_server(settings, AsyncMock())
    security = mcp.settings.transport_security
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["okk-mcp.akfixdev.ru"]
    assert security.allowed_origins == ["https://okk-mcp.akfixdev.ru"]


def test_metadata_and_mcp_auth_challenge_are_discoverable():
    with TestClient(app, base_url="http://localhost:8020") as client:
        authorization = client.get("/.well-known/oauth-authorization-server")
        protected = client.get("/.well-known/oauth-protected-resource/mcp")
        challenge = client.post(
            "/mcp",
            headers={"Accept": "application/json, text/event-stream"},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "contract-test", "version": "1"},
                },
            },
        )
    assert authorization.status_code == 200
    assert authorization.json()["code_challenge_methods_supported"] == ["S256"]
    assert protected.json()["resource"].endswith("/mcp")
    assert challenge.status_code == 401
    assert "resource_metadata=" in challenge.headers["www-authenticate"]


def test_parallel_authorization_pages_keep_independent_signed_csrf_tokens(monkeypatch):
    settings = Settings()
    csrf_a, csrf_b = "csrf-a", "csrf-b"
    signed_a = oauth._serializer(settings).dumps(
        {
            "client_id": "client",
            "redirect_uri": "http://127.0.0.1:3210/callback",
            "state": "a",
            "scope": "okk.statistics.read",
            "resource": settings.resource_url,
            "code_challenge": "challenge",
            "csrf_token": csrf_a,
        }
    )
    signed_b = oauth._serializer(settings).dumps(
        {
            "client_id": "client",
            "redirect_uri": "http://127.0.0.1:3210/callback",
            "state": "b",
            "scope": "okk.statistics.read",
            "resource": settings.resource_url,
            "code_challenge": "challenge",
            "csrf_token": csrf_b,
        }
    )
    assert signed_a != signed_b

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/authorize",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    monkeypatch.setattr(
        oauth,
        "_load_client",
        AsyncMock(return_value=SimpleNamespace(client_name="Codex")),
    )
    monkeypatch.setattr(oauth, "_check_login_rate_limit", AsyncMock())
    monkeypatch.setattr(
        oauth.platform_client,
        "authenticate",
        AsyncMock(side_effect=OKKAuthenticationError("invalid credentials")),
    )
    response = asyncio.run(
        oauth.authorize_login(
            request=request,
            authorization_request=signed_a,
            csrf_token=csrf_a,
            email="user@example.com",
            password="invalid-password",
            db=SimpleNamespace(),
            settings=settings,
        )
    )
    assert response.status_code == 200
    assert "Неверный логин или пароль" in response.body.decode()


def test_successful_login_redirects_to_exact_codex_callback(monkeypatch):
    settings = Settings()
    csrf = "csrf"
    signed = oauth._serializer(settings).dumps(
        {
            "client_id": "client",
            "redirect_uri": "http://127.0.0.1:3210/callback",
            "state": "state",
            "scope": "okk.statistics.read",
            "resource": settings.resource_url,
            "code_challenge": "challenge",
            "csrf_token": csrf,
        }
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/authorize",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    account_session = SimpleNamespace(id=uuid.uuid4())
    db = SimpleNamespace(add=lambda _row: None, flush=AsyncMock())
    monkeypatch.setattr(
        oauth,
        "_load_client",
        AsyncMock(return_value=SimpleNamespace(client_name="Codex", client_id="client")),
    )
    monkeypatch.setattr(oauth, "_check_login_rate_limit", AsyncMock())
    monkeypatch.setattr(oauth.platform_client, "authenticate", AsyncMock(return_value=account_session))

    response = asyncio.run(
        oauth.authorize_login(
            request=request,
            authorization_request=signed,
            csrf_token=csrf,
            email="user@example.com",
            password="one-time-test-password",
            db=db,
            settings=settings,
        )
    )

    assert response.status_code == 302
    assert response.headers["location"].startswith("http://127.0.0.1:3210/callback?code=")
    assert response.headers["location"].endswith("&state=state")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "one-time-test-password" not in response.headers["location"]
    assert "user@example.com" not in response.headers["location"]


@pytest.mark.parametrize(
    ("redirect_uri", "allowed_origin"),
    [
        ("http://127.0.0.1:3210/callback/codex", "http://127.0.0.1:3210"),
        ("http://[::1]:3210/callback/codex", "http://[::1]:3210"),
        ("https://chatgpt.com/connector/callback?client=codex", "https://chatgpt.com"),
    ],
)
def test_login_form_csp_allows_only_self_and_registered_callback_origin(
    redirect_uri,
    allowed_origin,
):
    response = oauth._render_login(
        settings=Settings(),
        client_name="Codex",
        signed_request="signed-request",
        csrf_token="csrf-token",
        redirect_uri=redirect_uri,
    )

    policy = response.headers["content-security-policy"]
    assert f"form-action 'self' {allowed_origin}" in policy
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy


def test_tampered_authorization_csrf_refreshes_the_login_form(monkeypatch):
    settings = Settings()
    signed = oauth._serializer(settings).dumps(
        {
            "client_id": "client",
            "redirect_uri": "http://127.0.0.1:3210/callback",
            "state": "state",
            "scope": "okk.statistics.read",
            "resource": settings.resource_url,
            "code_challenge": "challenge",
            "csrf_token": "signed-csrf",
        }
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/authorize",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    monkeypatch.setattr(
        oauth,
        "_load_client",
        AsyncMock(return_value=SimpleNamespace(client_name="Codex")),
    )
    response = asyncio.run(
        oauth.authorize_login(
            request=request,
            authorization_request=signed,
            csrf_token="tampered-csrf",
            email="user@example.com",
            password="invalid-password",
            db=SimpleNamespace(),
            settings=settings,
        )
    )
    body = response.body.decode()
    assert response.status_code == 200
    assert "Сеанс входа был обновлён" in body
    assert 'name="authorization_request"' in body
    assert "set-cookie" not in response.headers
    assert response.headers["cache-control"] == "no-store"


def test_invalid_authorization_request_returns_recovery_page():
    settings = Settings()
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/authorize",
            "headers": [],
            "client": ("127.0.0.1", 12345),
        }
    )
    response = asyncio.run(
        oauth.authorize_login(
            request=request,
            authorization_request="not-signed",
            csrf_token="missing-cookie",
            email="user@example.com",
            password="invalid-password",
            db=SimpleNamespace(),
            settings=settings,
        )
    )
    assert response.status_code == 400
    assert "Вернитесь в Codex" in response.body.decode()


def test_production_configuration_requires_https_and_real_secrets():
    with pytest.raises(ValueError):
        Settings(app_env="production")


def test_production_configuration_requires_distinct_secrets_and_authenticated_storage():
    common = {
        "app_env": "production",
        "mcp_issuer_url": "https://mcp.example",
        "mcp_resource_url": "https://mcp.example/mcp",
        "okk_api_base_url": "https://okk.example/api/v1",
        "mcp_oauth_secret": "o" * 40,
        "mcp_session_encryption_secret": "s" * 40,
        "database_url": "postgresql+asyncpg://user:strong-password@db/okk_mcp",
        "redis_url": "rediss://:strong-password@redis/0",
    }
    Settings(**common)
    with pytest.raises(ValueError):
        Settings(**{**common, "mcp_session_encryption_secret": "o" * 40})
    with pytest.raises(ValueError):
        Settings(**{**common, "database_url": "postgresql+asyncpg://user:change-me@db/okk_mcp"})
    with pytest.raises(ValueError):
        Settings(**{**common, "redis_url": "redis://redis/0"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("okk_api_base_url", "https://user:pass@okk.example/api/v1"),
        ("okk_api_base_url", "https://okk.example/api/v1?token=secret"),
        ("mcp_resource_url", "https://evil.example/mcp"),
    ],
)
def test_service_urls_reject_credentials_queries_and_cross_origin_resources(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_consumed_refresh_reuse_revokes_family_even_when_old_token_is_already_revoked(monkeypatch):
    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class DB:
        def __init__(self, values):
            self.values = iter(values)
            self.executed = []

        async def execute(self, statement):
            self.executed.append(statement)
            return Result(next(self.values))

    family_id = uuid.uuid4()
    session_id = uuid.uuid4()
    stored = SimpleNamespace(
        client_id="client",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        revoked_at=datetime.now(UTC),
        consumed_at=datetime.now(UTC),
        family_id=family_id,
        session_id=session_id,
    )
    db = DB([SimpleNamespace(client_id="client"), stored, None])
    revoke = AsyncMock()
    monkeypatch.setattr(oauth.platform_client, "revoke_upstream", revoke)
    response = asyncio.run(
        oauth.token_endpoint(
            grant_type="refresh_token",
            client_id="client",
            refresh_token="reused-token",
            db=db,
            settings=Settings(),
        )
    )
    assert response.status_code == 400
    assert "reuse detected" in json.loads(response.body)["error_description"]
    assert len(db.executed) == 3  # client lookup, token lookup, family revocation update
    revoke.assert_awaited_once_with(session_id)
