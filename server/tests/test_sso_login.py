"""Corporate (SSO) login through OKK: login page button, start, callback and code exchange."""

from __future__ import annotations

import asyncio
import uuid
from html import unescape
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from okk_mcp import oauth
from okk_mcp.config import Settings, get_settings
from okk_mcp.db import get_db
from okk_mcp.main import app
from okk_mcp.platform_client import OKKAuthenticationError, OKKPlatformClient

SSO_URL = "https://okk.example/api/v1/auth/sso/authorize"
SECRET = "handoff-secret-for-tests-0123456789abcdef"
CALLBACK = "http://127.0.0.1:3210/callback"


def sso_settings(**overrides) -> Settings:
    return Settings(**{"okk_sso_authorize_url": SSO_URL, "okk_sso_handoff_secret": SECRET, **overrides})


class FakeRedis:
    store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def getdel(self, key):
        return self.store.pop(key, None)

    async def aclose(self):
        pass


@pytest.fixture
def flow(monkeypatch):
    settings = sso_settings()
    FakeRedis.store = {}
    monkeypatch.setattr(oauth.redis, "from_url", lambda *_a, **_k: FakeRedis())
    monkeypatch.setattr(
        oauth,
        "_load_client",
        AsyncMock(return_value=SimpleNamespace(client_name="Claude Code", client_id="client")),
    )
    account_session = SimpleNamespace(id=uuid.uuid4())
    exchange = AsyncMock(return_value=account_session)
    monkeypatch.setattr(oauth.platform_client, "exchange_sso_handoff", exchange)
    added = []

    async def fake_db():
        yield SimpleNamespace(add=added.append, flush=AsyncMock())

    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[get_settings] = lambda: settings
    signed = oauth._serializer(settings).dumps(
        {
            "client_id": "client",
            "redirect_uri": CALLBACK,
            "state": "client-state",
            "scope": "okk.statistics.read okk.transcripts.read okk.phones.read",
            "resource": settings.resource_url,
            "code_challenge": "challenge",
            "csrf_token": "csrf",
        }
    )
    yield SimpleNamespace(
        client=TestClient(app, base_url="https://okk-mcp.example"),
        settings=settings,
        signed=signed,
        exchange=exchange,
        added=added,
        account_session=account_session,
    )
    app.dependency_overrides.clear()


def _start(env, **form):
    return env.client.post(
        "/authorize/sso",
        data={"authorization_request": env.signed, "csrf_token": "csrf", "consent_version": "2", **form},
        follow_redirects=False,
    )


def _okk_url(page: str) -> str:
    start = page.index("url=") + 4
    return unescape(page[start : page.index('">', start)])


def test_sso_settings_are_all_or_nothing_and_https_in_production():
    assert not Settings().sso_enabled
    assert sso_settings().sso_enabled
    with pytest.raises(ValueError, match="together"):
        Settings(okk_sso_authorize_url=SSO_URL)
    with pytest.raises(ValueError, match="32"):
        Settings(okk_sso_authorize_url=SSO_URL, okk_sso_handoff_secret="short")
    with pytest.raises(ValueError, match="HTTPS"):
        sso_settings(
            app_env="production",
            okk_sso_authorize_url="http://okk.example/api/v1/auth/sso/authorize",
            mcp_issuer_url="https://okk-mcp.example",
            mcp_resource_url="https://okk-mcp.example/mcp",
            okk_api_base_url="https://okk.example/api/v1",
            mcp_oauth_secret="o" * 40,
            mcp_session_encryption_secret="s" * 40,
            database_url="postgresql+asyncpg://u:strong@db/okk_mcp",
            redis_url="redis://:pw@redis:6379/0",
        )


def test_login_page_offers_corporate_login_only_when_configured():
    common = dict(
        client_name="Claude Code",
        signed_request="signed",
        csrf_token="csrf",
        redirect_uri=CALLBACK,
        scope="okk.statistics.read",
    )
    without = oauth._render_login(settings=Settings(), **common).body.decode()
    with_sso = oauth._render_login(settings=sso_settings(), **common).body.decode()

    assert "/authorize/sso" not in without
    assert 'formaction="/authorize/sso" formnovalidate' in with_sso
    # The password button stays first in the form: Enter in the password field signs in by password.
    assert with_sso.index("Войти и разрешить доступ") < with_sso.index("/authorize/sso")


def test_sso_routes_do_not_exist_without_configuration(flow):
    app.dependency_overrides[get_settings] = lambda: Settings()
    assert _start(flow).status_code == 404
    assert flow.client.get("/sso/callback?code=c&state=s").status_code == 404


def test_corporate_login_returns_the_client_code_with_the_scope_the_person_granted(flow):
    started = _start(flow, grant_transcripts="1")
    assert started.status_code == 200
    okk = urlsplit(_okk_url(started.text))
    assert f"{okk.scheme}://{okk.netloc}{okk.path}" == SSO_URL
    query = parse_qs(okk.query)
    assert query["handoff_redirect_uri"] == [f"{flow.settings.issuer_url}/sso/callback"]
    state = query["handoff_state"][0]
    cookie = started.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/sso" in cookie and "samesite=lax" in cookie.lower()

    done = flow.client.get(f"/sso/callback?code=okk-code&state={state}", follow_redirects=False)

    assert done.status_code == 302
    location = done.headers["location"]
    assert location.startswith(f"{CALLBACK}?code=") and location.endswith("&state=client-state")
    flow.exchange.assert_awaited_once_with("okk-code")
    code_row = flow.added[-1]
    assert code_row.scope == "okk.statistics.read okk.transcripts.read"
    assert code_row.session_id == flow.account_session.id
    assert code_row.redirect_uri == CALLBACK
    # The same state cannot be used twice.
    replay = flow.client.get(f"/sso/callback?code=okk-code&state={state}", follow_redirects=False)
    assert replay.status_code == 400


def test_callback_in_another_browser_is_refused_without_spending_the_okk_code(flow):
    state = parse_qs(urlsplit(_okk_url(_start(flow).text)).query)["handoff_state"][0]
    other_browser = TestClient(app, base_url="https://okk-mcp.example")

    response = other_browser.get(f"/sso/callback?code=okk-code&state={state}", follow_redirects=False)

    assert response.status_code == 400
    flow.exchange.assert_not_awaited()


def test_tampered_csrf_does_not_start_corporate_login(flow):
    response = _start(flow, csrf_token="other")
    assert response.status_code == 200
    assert "Сеанс входа был обновлён" in response.text
    assert FakeRedis.store == {}


def test_okk_refusing_the_code_shows_recovery_page(flow):
    flow.exchange.side_effect = OKKAuthenticationError("no")
    state = parse_qs(urlsplit(_okk_url(_start(flow).text)).query)["handoff_state"][0]
    response = flow.client.get(f"/sso/callback?code=bad&state={state}", follow_redirects=False)
    assert response.status_code == 400
    assert "не подтвердил" in response.text


def test_platform_client_exchanges_the_code_with_the_service_secret():
    settings = sso_settings(okk_api_base_url="https://okk.example/api/v1")
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        seen["body"] = request.content
        if request.content != b'{"code":"good"}':
            return httpx.Response(400)
        return httpx.Response(
            200,
            json={
                "access_token": "okk-access",
                "user": {"id": "u1", "email": "a@example.com", "role": "viewer", "department_ids": ["d1"]},
            },
            headers={"set-cookie": "refresh_token=okk-refresh; Path=/api/v1/auth"},
        )

    client = OKKPlatformClient(settings)
    client.client = httpx.AsyncClient(base_url=settings.api_base_url, transport=httpx.MockTransport(handler))

    session = asyncio.run(client.exchange_sso_handoff("good"))

    assert seen["auth"] == f"Bearer {SECRET}"
    assert seen["path"] == "/api/v1/auth/sso/handoff/token"
    assert session.okk_user_id == "u1"
    assert session.department_ids_snapshot == ["d1"]
    assert client.cipher.open(session.encrypted_refresh_token) == "okk-refresh"
    with pytest.raises(OKKAuthenticationError):
        asyncio.run(client.exchange_sso_handoff("bad"))
