"""OAuth token lifecycle and OKK session renewal on a real PostgreSQL.

Row locks, transactions and concurrent requests cannot be faked: these tests run only with
OKK_MCP_TEST_DATABASE_URL (an empty database the tests may migrate and truncate), as in CI.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from starlette.requests import Request

from okk_mcp import maintenance, oauth
from okk_mcp import platform_client as platform_module
from okk_mcp.client_metadata import ClientMetadata
from okk_mcp.config import Settings
from okk_mcp.models import OAuthAuthorizationCode, OAuthClient, OAuthToken, OkkAccountSession
from okk_mcp.platform_client import OKKPlatformClient
from okk_mcp.security import DEFAULT_SCOPES, token_hash

DATABASE_URL = os.getenv("OKK_MCP_TEST_DATABASE_URL")
SERVER_DIR = Path(__file__).resolve().parents[1]
OKK_URL = "https://okk.example/api/v1"
REDIRECT_URI = "http://127.0.0.1:40123/callback"

pytestmark = [
    pytest.mark.skipif(not DATABASE_URL, reason="needs OKK_MCP_TEST_DATABASE_URL (PostgreSQL)"),
    pytest.mark.anyio,
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _alembic(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", *args],
        cwd=SERVER_DIR,
        env={**os.environ, "DATABASE_URL": DATABASE_URL or ""},
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def migrated():
    _alembic("downgrade", "base")
    _alembic("upgrade", "head")


class FakeOKK:
    """OKK auth API: valid access tokens, one rotating refresh token, optional delay or outage."""

    def __init__(self) -> None:
        self.valid_access = {"okk-access-1"}
        self.refresh_token = "okk-refresh-1"
        self.refresh_calls = 0
        self.rejected_reads = 0
        self.me_delay = 0.0
        self.down = False
        self.user = {
            "id": "user-1",
            "email": "user@example.com",
            "role": "admin",
            "department_ids": [],
            "is_active": True,
        }

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        if request.url.path.endswith("/auth/me"):
            await asyncio.sleep(self.me_delay)
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            return httpx.Response(200, json=self.user) if token in self.valid_access else httpx.Response(401)
        if request.url.path.endswith("/auth/refresh"):
            self.refresh_calls += 1
            if request.headers.get("cookie") != f"refresh_token={self.refresh_token}":
                return httpx.Response(401)
            access = f"okk-access-{self.refresh_calls + 1}"
            self.valid_access = {access}
            self.refresh_token = f"okk-refresh-{self.refresh_calls + 1}"
            return httpx.Response(
                200,
                json={"access_token": access, "user": self.user},
                headers={"set-cookie": f"refresh_token={self.refresh_token}; Path=/api/v1/auth"},
            )
        if request.url.path.endswith("/auth/logout"):
            return httpx.Response(200)
        if request.url.path.endswith("/calls"):
            if request.headers.get("authorization", "").removeprefix("Bearer ") in self.valid_access:
                return httpx.Response(200, json={"items": []})
            self.rejected_reads += 1
            return httpx.Response(401)
        return httpx.Response(404)


@pytest.fixture
def env(monkeypatch, migrated):
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    okk = FakeOKK()
    settings = Settings(okk_api_base_url=OKK_URL)
    platform = OKKPlatformClient(settings, transport=httpx.MockTransport(okk.handler))
    for module in (platform_module, oauth, maintenance):
        monkeypatch.setattr(module, "session_factory", factory)
    monkeypatch.setattr(oauth, "platform_client", platform)
    return SimpleNamespace(factory=factory, okk=okk, platform=platform, settings=settings)


async def _reset(env) -> None:
    async with env.factory() as db:
        await db.execute(
            text(
                "TRUNCATE oauth_tokens, oauth_authorization_codes, oauth_clients, okk_account_sessions"
                " CASCADE"
            )
        )
        await db.commit()


async def _seed(env, *, okk_access: str = "okk-access-1") -> SimpleNamespace:
    await _reset(env)
    cipher = env.platform.cipher
    async with env.factory() as db:
        session = OkkAccountSession(
            okk_user_id="user-1",
            encrypted_access_token=cipher.seal(okk_access),
            encrypted_refresh_token=cipher.seal("okk-refresh-1"),
            role_snapshot="admin",
            department_ids_snapshot=[],
        )
        db.add_all(
            [
                session,
                OAuthClient(
                    client_id="client",
                    client_name="Codex",
                    redirect_uris=[REDIRECT_URI],
                    grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=DEFAULT_SCOPES,
                    first_authorized_at=datetime.now(UTC),
                ),
            ]
        )
        await db.flush()
        access, refresh, _ = oauth._add_token_pair(
            db,
            client_id="client",
            session_id=session.id,
            scope=DEFAULT_SCOPES,
            resource=env.settings.resource_url,
            family_id=uuid.uuid4(),
            settings=env.settings,
        )
        await db.commit()
    return SimpleNamespace(access=access, refresh=refresh, session_id=session.id)


async def _refresh(env, raw_refresh: str, settings: Settings | None = None) -> tuple[int, dict]:
    async with env.factory() as db:
        response = await oauth.token_endpoint(
            grant_type="refresh_token",
            client_id="client",
            code=None,
            redirect_uri=None,
            code_verifier=None,
            refresh_token=raw_refresh,
            scope=None,
            resource=None,
            db=db,
            settings=settings or env.settings,
        )
        await db.commit()
    return response.status_code, json.loads(response.body)


async def test_lost_refresh_answer_retried_gets_the_same_pair_and_the_chain_continues(env):
    seeded = await _seed(env)
    status, first = await _refresh(env, seeded.refresh)
    status_retry, retry = await _refresh(env, seeded.refresh)
    status_next, _ = await _refresh(env, first["refresh_token"])

    assert (status, status_retry, status_next) == (200, 200, 200)
    assert (retry["access_token"], retry["refresh_token"]) == (first["access_token"], first["refresh_token"])


async def test_parallel_refresh_with_one_token_gives_both_callers_the_same_working_pair(env):
    seeded = await _seed(env)
    (status_a, a), (status_b, b) = await asyncio.gather(
        _refresh(env, seeded.refresh), _refresh(env, seeded.refresh)
    )
    assert (status_a, status_b) == (200, 200)
    assert a["refresh_token"] == b["refresh_token"]
    assert (await _refresh(env, a["refresh_token"]))[0] == 200


async def test_refresh_replayed_after_the_retry_window_revokes_the_whole_family(env):
    seeded = await _seed(env)
    no_window = Settings(okk_api_base_url=OKK_URL, mcp_refresh_retry_seconds=0)
    _, first = await _refresh(env, seeded.refresh, no_window)
    status, body = await _refresh(env, seeded.refresh, no_window)

    assert status == 400 and "reuse detected" in body["error_description"]
    assert (await _refresh(env, first["refresh_token"]))[0] == 400


async def test_refresh_replayed_after_the_new_token_was_used_revokes_the_whole_family(env):
    seeded = await _seed(env)
    _, first = await _refresh(env, seeded.refresh)
    _, second = await _refresh(env, first["refresh_token"])
    status, _ = await _refresh(env, seeded.refresh)

    assert status == 400
    assert (await _refresh(env, second["refresh_token"]))[0] == 400


async def test_expired_okk_token_is_renewed_once_for_parallel_requests(env):
    seeded = await _seed(env, okk_access="okk-access-expired")
    contexts = await asyncio.gather(*(env.platform.live_context(seeded.session_id) for _ in range(5)))

    assert env.okk.refresh_calls == 1
    assert {context.access_token for context in contexts} == {"okk-access-2"}


async def test_okk_token_expiring_during_a_tool_call_is_renewed_once_for_all_its_reads(env):
    seeded = await _seed(env)
    context = await env.platform.live_context(seeded.session_id)
    env.okk.valid_access = set()  # the OKK access token expires while the tool call runs

    first = await asyncio.gather(*(env.platform.get_with_context(context, "/calls") for _ in range(4)))
    rejected = env.okk.rejected_reads
    later = await env.platform.get_with_context(context, "/calls")

    assert first == [{"items": []}] * 4 and later == {"items": []}
    assert env.okk.refresh_calls == 1
    # The tool call keeps its context; its later reads carry the renewed token at once.
    assert env.okk.rejected_reads == rejected


async def _exchange_code(env, code: str, verifier: str) -> tuple[int, dict]:
    async with env.factory() as db:
        response = await oauth.token_endpoint(
            grant_type="authorization_code",
            client_id="client",
            code=code,
            redirect_uri=REDIRECT_URI,
            code_verifier=verifier,
            refresh_token=None,
            scope=None,
            resource=None,
            db=db,
            settings=env.settings,
        )
        await db.commit()
    return response.status_code, json.loads(response.body)


async def test_replayed_authorization_code_revokes_the_tokens_it_issued(env):
    other_sign_in = await _seed(env)
    code, verifier = "authorization-code", "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    async with env.factory() as db:
        session = OkkAccountSession(
            okk_user_id="user-1",
            encrypted_access_token=env.platform.cipher.seal("okk-access-1"),
            encrypted_refresh_token=env.platform.cipher.seal("okk-refresh-1"),
            role_snapshot="admin",
            department_ids_snapshot=[],
        )
        db.add(session)
        await db.flush()
        db.add(
            OAuthAuthorizationCode(
                code_hash=token_hash(code),
                client_id="client",
                session_id=session.id,
                redirect_uri=REDIRECT_URI,
                scope=DEFAULT_SCOPES,
                code_challenge=challenge,
                resource=env.settings.resource_url,
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.commit()

    status, issued = await _exchange_code(env, code, verifier)
    replay_status, replay = await _exchange_code(env, code, verifier)
    verifier_ = oauth.OKKTokenVerifier(env.settings.resource_url)

    assert (status, replay_status, replay["error"]) == (200, 400, "invalid_grant")
    assert await verifier_.verify_token(issued["access_token"]) is None
    assert (await _refresh(env, issued["refresh_token"]))[0] == 400
    assert await verifier_.verify_token(other_sign_in.access) is not None


async def test_parallel_requests_of_one_person_are_not_serialized_while_okk_answers(env):
    seeded = await _seed(env)
    env.okk.me_delay = 0.4
    started = time.perf_counter()
    await asyncio.gather(*(env.platform.live_context(seeded.session_id) for _ in range(4)))
    # Serialized, four calls take 1.6 s.
    assert time.perf_counter() - started < 1.0


async def test_okk_outage_keeps_the_mcp_token_valid(env):
    seeded = await _seed(env)
    env.okk.down = True
    verified = await oauth.OKKTokenVerifier(env.settings.resource_url).verify_token(seeded.access)

    assert verified is not None
    assert verified.claims == {}


async def test_cleanup_removes_abandoned_registrations_and_old_retry_pairs(env):
    seeded = await _seed(env)
    _, first = await _refresh(env, seeded.refresh)
    now = datetime.now(UTC)
    async with env.factory() as db:
        for client_id, created, authorized in (
            ("abandoned", now - timedelta(days=2), None),
            ("just-registered", now - timedelta(hours=1), None),
            ("in-use", now - timedelta(days=90), now - timedelta(days=90)),
        ):
            db.add(
                OAuthClient(
                    client_id=client_id,
                    client_name="x",
                    redirect_uris=["http://127.0.0.1:1/cb"],
                    grant_types=["authorization_code"],
                    response_types=["code"],
                    token_endpoint_auth_method="none",
                    scope=DEFAULT_SCOPES,
                    created_at=created,
                    first_authorized_at=authorized,
                )
            )
        old = (
            await db.execute(select(OAuthToken).where(OAuthToken.token_hash == token_hash(seeded.refresh)))
        ).scalar_one()
        old.consumed_at = now - timedelta(minutes=5)
        await db.commit()

    report = await maintenance.cleanup_once(env.settings, env.platform)

    async with env.factory() as db:
        clients = set((await db.scalars(select(OAuthClient.client_id))).all())
        sealed = (
            await db.execute(select(OAuthToken).where(OAuthToken.token_hash == token_hash(seeded.refresh)))
        ).scalar_one()
    assert clients == {"client", "just-registered", "in-use"}
    assert sealed.successor_sealed is None
    assert report["abandoned_clients_deleted"] == 1
    assert first["refresh_token"]


async def test_client_named_by_its_metadata_document_signs_in_exchanges_and_refreshes(env, monkeypatch):
    """Codes and tokens reference `oauth_clients`: a CIMD client gets its row on sign-in, a second
    sign-in updates it, and /token accepts the URL as client_id."""

    # CIMD выключен по умолчанию с 27.10 (`mcp_client_metadata_documents_enabled`): тест включает его.
    env.settings = Settings(okk_api_base_url=OKK_URL, mcp_client_metadata_documents_enabled=True)
    await _reset(env)
    client_id = "https://chatgpt.com/oauth/client.json"
    redirect_uri = "https://chatgpt.com/connector_platform_oauth_redirect"
    metadata = ClientMetadata(client_id, "ChatGPT", (redirect_uri,), None)
    monkeypatch.setattr(oauth, "load_client_metadata", AsyncMock(return_value=metadata))
    monkeypatch.setattr(oauth, "_check_login_rate_limit", AsyncMock())

    def new_okk_session(*_args):
        return OkkAccountSession(
            okk_user_id="user-1",
            encrypted_access_token=env.platform.cipher.seal("okk-access-1"),
            encrypted_refresh_token=env.platform.cipher.seal("okk-refresh-1"),
            role_snapshot="admin",
            department_ids_snapshot=[],
        )

    monkeypatch.setattr(env.platform, "authenticate", AsyncMock(side_effect=new_okk_session))
    verifier = "v" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    signed = oauth._serializer(env.settings).dumps(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": "s",
            "scope": DEFAULT_SCOPES,
            "resource": env.settings.resource_url,
            "code_challenge": challenge,
            "csrf_token": "csrf",
        }
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/authorize", "headers": [], "client": ("127.0.0.1", 1)}
    )
    codes = []
    for _ in range(2):
        async with env.factory() as db:
            response = await oauth.authorize_login(
                request=request,
                authorization_request=signed,
                csrf_token="csrf",
                email="user@example.com",
                password="test-password",
                consent_version=None,
                grant_transcripts=None,
                grant_phones=None,
                db=db,
                settings=env.settings,
            )
            await db.commit()
        codes.append(parse_qs(urlsplit(response.headers["location"]).query)["code"][0])

    async with env.factory() as db:
        exchanged = await oauth.token_endpoint(
            grant_type="authorization_code",
            client_id=client_id,
            code=codes[1],
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            refresh_token=None,
            scope=None,
            resource=None,
            db=db,
            settings=env.settings,
        )
        await db.commit()
    async with env.factory() as db:
        refreshed = await oauth.token_endpoint(
            grant_type="refresh_token",
            client_id=client_id,
            code=None,
            redirect_uri=None,
            code_verifier=None,
            refresh_token=json.loads(exchanged.body)["refresh_token"],
            scope=None,
            resource=None,
            db=db,
            settings=env.settings,
        )
        await db.commit()
        stored = await db.get(OAuthClient, client_id)

    assert (exchanged.status_code, refreshed.status_code) == (200, 200)
    assert stored.client_name == "ChatGPT (chatgpt.com)"
    assert stored.redirect_uris == [redirect_uri]
    assert stored.first_authorized_at is not None


async def test_migration_marks_clients_in_use_and_rolls_back(env):
    await _reset(env)
    _alembic("downgrade", "20260714_01")
    try:
        async with env.factory() as db:
            for statement in (
                """
                INSERT INTO okk_account_sessions (id, okk_user_id, encrypted_access_token,
                    encrypted_refresh_token, role_snapshot, department_ids_snapshot, created_at, updated_at)
                VALUES ('00000000-0000-0000-0000-000000000001', 'u', 'a', 'r', 'admin', '[]', now(), now())
                """,
                """
                INSERT INTO oauth_clients (client_id, client_name, redirect_uris, grant_types,
                    response_types, token_endpoint_auth_method, scope, created_at, updated_at)
                VALUES ('used', 'Codex', '[]', '[]', '[]', 'none', '', now(), now()),
                       ('unused', 'Codex', '[]', '[]', '[]', 'none', '', now(), now())
                """,
                """
                INSERT INTO oauth_tokens (id, token_hash, token_type, client_id, session_id, scope,
                    resource, family_id, expires_at, created_at, updated_at)
                VALUES (gen_random_uuid(), 'h', 'refresh', 'used', '00000000-0000-0000-0000-000000000001',
                    '', '', gen_random_uuid(), now(), now(), now())
                """,
            ):
                await db.execute(text(statement))
            await db.commit()
    finally:
        _alembic("upgrade", "head")
    async with env.factory() as db:
        marked = dict(
            (
                await db.execute(text("SELECT client_id, first_authorized_at IS NOT NULL FROM oauth_clients"))
            ).all()
        )
    assert marked == {"used": True, "unused": False}


class _MemoryRedis:
    """Redis входа через OKK в памяти: `set` со сроком и одноразовый `getdel`, как настоящий."""

    stored: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.stored[key] = value

    async def getdel(self, key):
        return self.stored.pop(key, None)

    async def aclose(self):
        return None


OKK_LOGIN_SECRET = "gateway-secret-0123456789"


@pytest.fixture
def okk_login(env, monkeypatch):
    """Шлюз в режиме входа через OKK: OKK меняет одноразовый код `okk-code` на сессию, только с
    секретом шлюза, его точным адресом возврата и PKCE шлюза."""

    settings = Settings(
        okk_api_base_url=OKK_URL,
        mcp_login_mode="okk",
        okk_mcp_client_secret=OKK_LOGIN_SECRET,
        okk_public_api_url="https://okk-public.example/api/v1",
    )
    exchanged: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/mcp/token"):
            form = dict(parse_qsl(request.content.decode()))
            exchanged.append(form)
            valid = (
                form.get("code") == "okk-code"
                and form.get("client_secret") == OKK_LOGIN_SECRET
                and form.get("redirect_uri") == settings.okk_login_callback_url
            )
            if not valid:
                return httpx.Response(400, json={"detail": "invalid_grant"})
            user = {**env.okk.user, "role": "employee"}
            return httpx.Response(
                200,
                json={"access_token": "okk-access-1", "user": user},
                headers={"set-cookie": "refresh_token=okk-refresh-1; Path=/api/v1/auth"},
            )
        return await env.okk.handler(request)

    platform = OKKPlatformClient(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(oauth, "platform_client", platform)
    _MemoryRedis.stored = {}
    monkeypatch.setattr(oauth.redis, "from_url", lambda *_args, **_kwargs: _MemoryRedis())
    return SimpleNamespace(settings=settings, exchanged=exchanged)


async def _seed_client(env) -> None:
    await _reset(env)
    async with env.factory() as db:
        db.add(
            OAuthClient(
                client_id="client",
                client_name="Codex",
                redirect_uris=[REDIRECT_URI],
                grant_types=["authorization_code", "refresh_token"],
                response_types=["code"],
                token_endpoint_auth_method="none",
                scope=DEFAULT_SCOPES,
            )
        )
        await db.commit()


async def _consent(env, settings: Settings, challenge: str):
    """Согласие на странице шлюза: доступ к транскриптам снят — в режиме OKK без логина и пароля."""

    signed = oauth._serializer(settings).dumps(
        {
            "client_id": "client",
            "redirect_uri": REDIRECT_URI,
            "state": "client-state",
            "scope": DEFAULT_SCOPES,
            "resource": settings.resource_url,
            "code_challenge": challenge,
            "csrf_token": "csrf",
        }
    )
    request = Request(
        {"type": "http", "method": "POST", "path": "/authorize", "headers": [], "client": ("127.0.0.1", 1)}
    )
    async with env.factory() as db:
        return await oauth.authorize_login(
            request=request,
            authorization_request=signed,
            csrf_token="csrf",
            email=None,
            password=None,
            consent_version="2",
            grant_transcripts=None,
            grant_phones="1",
            db=db,
            settings=settings,
        )


async def _callback(env, settings: Settings, **query):
    async with env.factory() as db:
        response = await oauth.okk_login_callback(db=db, settings=settings, **query)
        await db.commit()
    return response


async def test_okk_page_login_ends_in_a_working_mcp_token_without_the_gateway_seeing_a_password(
    env, okk_login
):
    await _seed_client(env)
    verifier = "c" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()

    to_okk = await _consent(env, okk_login.settings, challenge)
    okk_query = dict(parse_qsl(urlsplit(to_okk.headers["location"]).query))
    back = await _callback(env, okk_login.settings, state=okk_query["state"], code="okk-code")
    replayed = await _callback(env, okk_login.settings, state=okk_query["state"], code="okk-code")
    client_query = parse_qs(urlsplit(back.headers["location"]).query)
    status, issued = await _exchange_code(env, client_query["code"][0], verifier)

    # Браузер уходит на страницу OKK: клиент шлюза, его точный адрес возврата, PKCE шлюза — без
    # запроса клиента MCP и без ключа PKCE в адресе.
    assert to_okk.headers["location"].startswith("https://okk-public.example/api/v1/auth/mcp/authorize?")
    assert (okk_query["client_id"], okk_query["redirect_uri"]) == (
        "okk-mcp-gateway",
        okk_login.settings.okk_login_callback_url,
    )
    assert "client-state" not in to_okk.headers["location"] and verifier not in to_okk.headers["location"]
    # Код OKK меняется сервер-к-серверу с ключом PKCE, который подходит к вызову шлюза.
    sent_verifier = okk_login.exchanged[0]["code_verifier"]
    assert (
        base64.urlsafe_b64encode(hashlib.sha256(sent_verifier.encode()).digest()).rstrip(b"=").decode()
        == okk_query["code_challenge"]
    )
    # Клиент получает свой код со своим `state`; снятый доступ к транскриптам не выдан.
    assert client_query["state"] == ["client-state"]
    assert status == 200 and "okk.transcripts.read" not in issued["scope"].split()
    # Возврат из OKK одноразовый: повтор не выдаёт второй код.
    assert replayed.status_code == 400 and len(okk_login.exchanged) == 1


async def test_okk_refusal_reaches_the_client_as_an_error_with_its_state(env, okk_login):
    await _seed_client(env)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(b"d" * 64).digest()).rstrip(b"=").decode()

    states = []
    for _ in range(3):
        to_okk = await _consent(env, okk_login.settings, challenge)
        states.append(dict(parse_qsl(urlsplit(to_okk.headers["location"]).query))["state"])
    denied = await _callback(env, okk_login.settings, state=states[0], error="access_denied")
    unknown = await _callback(env, okk_login.settings, state=states[1], error="server_error")
    bad_code = await _callback(env, okk_login.settings, state=states[2], code="forged")

    for response in (denied, unknown, bad_code):
        assert parse_qs(urlsplit(response.headers["location"]).query) == {
            "error": ["access_denied"],
            "state": ["client-state"],
            "iss": [okk_login.settings.issuer_url],
        }
