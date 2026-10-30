"""OAuth 2.1 authorization server backed by native OKK account sessions."""

from __future__ import annotations

import base64
import hashlib
import html
import ipaddress
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from okk_mcp.client_metadata import ClientMetadataError, is_metadata_document_client_id, load_client_metadata
from okk_mcp.config import Settings, get_settings
from okk_mcp.db import get_db, session_factory
from okk_mcp.models import OAuthAuthorizationCode, OAuthClient, OAuthToken
from okk_mcp.platform_client import AccountContext, OKKAuthenticationError, OKKUnavailable
from okk_mcp.runtime import platform_client
from okk_mcp.security import (
    ALLOWED_SCOPES,
    DEFAULT_SCOPES,
    random_token,
    redirect_destination,
    redirect_origin,
    token_hash,
    valid_pkce_challenge,
    validate_registered_redirect_uri,
    validate_scopes,
    verify_pkce,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(UTC)


def _oauth_error(error: str, description: str, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": error, "error_description": description},
        status_code=status_code,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


def _append_redirect_query(uri: str, values: dict[str, str]) -> str:
    parts = urlsplit(uri)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.extend(values.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _authorization_redirect(
    redirect_uri: str, values: dict[str, str], state: str | None, settings: Settings
) -> RedirectResponse:
    """Every redirect back to the client, with a code or an error: `state` and the RFC 9207 `iss`.

    `iss` lets the client check which server answered (mix-up attack); ChatGPT and Codex reject an
    answer without it once the metadata advertises it.
    """

    query = dict(values)
    if state is not None:
        query["state"] = state
    query["iss"] = settings.issuer_url
    response = RedirectResponse(_append_redirect_query(redirect_uri, query), status_code=302)
    response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
    return response


def _authorization_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "issuer": settings.issuer_url,
        "authorization_endpoint": f"{settings.issuer_url}/authorize",
        "token_endpoint": f"{settings.issuer_url}/token",
        "registration_endpoint": f"{settings.issuer_url}/register",
        "revocation_endpoint": f"{settings.issuer_url}/revoke",
        "revocation_endpoint_auth_methods_supported": ["none"],
        "userinfo_endpoint": f"{settings.issuer_url}/userinfo",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": sorted(ALLOWED_SCOPES),
        "service_documentation": f"{settings.issuer_url}/",
        "authorization_response_iss_parameter_supported": True,
        "client_id_metadata_document_supported": settings.mcp_client_metadata_documents_enabled,
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_metadata(settings: Settings = Depends(get_settings)):
    return _authorization_metadata(settings)


@router.get("/.well-known/openai-apps-challenge", response_class=PlainTextResponse)
async def openai_apps_challenge(settings: Settings = Depends(get_settings)):
    """Serve the exact submission-domain token without exposing it elsewhere."""

    if not settings.openai_apps_challenge_token:
        raise HTTPException(status_code=404, detail="Not found")
    return PlainTextResponse(
        settings.openai_apps_challenge_token,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


def _protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer_url],
        "bearer_methods_supported": ["header"],
        "scopes_supported": sorted(ALLOWED_SCOPES),
        "resource_documentation": f"{settings.issuer_url}/",
    }


def _client_registration_document(client: OAuthClient) -> dict[str, Any]:
    document = {
        "client_id": client.client_id,
        "client_id_issued_at": int(client.created_at.timestamp()),
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "grant_types": client.grant_types,
        "response_types": client.response_types,
        "token_endpoint_auth_method": client.token_endpoint_auth_method,
        "scope": client.scope,
    }
    if client.client_uri:
        document["client_uri"] = client.client_uri
    return document


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(settings: Settings = Depends(get_settings)):
    return _protected_resource_metadata(settings)


def _registration_bucket(request: Request, settings: Settings) -> tuple[str, int]:
    """The throttle bucket and its hourly limit for the caller's address.

    The address is `request.client`, which uvicorn takes from forwarded headers only of trusted
    proxies (`--forwarded-allow-ips`); a header the client sends itself is never read here. A
    trusted network shares one larger bucket: claude.ai registers a client per connection, and all
    of them come from Anthropic's egress network.
    """

    host = request.client.host if request.client else "unknown"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return f"ip:{host}", settings.mcp_registrations_per_hour
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    for network in settings.trusted_client_networks:
        if address in network:
            return f"net:{network}", settings.mcp_trusted_network_registrations_per_hour
    return f"ip:{host}", settings.mcp_registrations_per_hour


async def _check_registration_rate_limit(request: Request, settings: Settings) -> None:
    """Registration is open to everyone (clients register themselves), so it is bounded per address."""

    bucket, limit = _registration_bucket(request, settings)
    key = f"okk:mcp:register:{bucket}:{int(time.time() // 3600)}"
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 7200)
            count, _ = await pipe.execute()
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Registration is temporarily unavailable") from exc
    finally:
        await client.aclose()
    if int(count) > limit:
        raise HTTPException(status_code=429, detail="Too many client registrations")


@router.post("/register")
async def register_client(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    await _check_registration_rate_limit(request, settings)
    try:
        payload = await request.json()
    except Exception:
        return _oauth_error("invalid_client_metadata", "Request body must be valid JSON")
    redirect_uris = payload.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or len(redirect_uris) > 20:
        return _oauth_error("invalid_redirect_uri", "redirect_uris is required")
    try:
        redirects = list(
            dict.fromkeys(
                validate_registered_redirect_uri(str(uri), settings.oauth_redirect_uris)
                for uri in redirect_uris
            )
        )
        scope = validate_scopes(payload.get("scope"))
    except ValueError as exc:
        return _oauth_error(str(exc), "Unsupported client metadata")
    grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
    responses = payload.get("response_types", ["code"])
    auth_method = payload.get("token_endpoint_auth_method", "none")
    if set(grants) - {"authorization_code", "refresh_token"} or set(responses) != {"code"}:
        return _oauth_error("invalid_client_metadata", "Unsupported OAuth flow")
    if auth_method != "none":
        return _oauth_error("invalid_client_metadata", "Only public PKCE clients are supported")
    client = OAuthClient(
        client_id=secrets.token_urlsafe(32),
        client_name=str(payload.get("client_name") or "Codex MCP client").strip()[:200],
        redirect_uris=redirects,
        grant_types=list(dict.fromkeys(grants)),
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=scope,
        client_uri=str(payload.get("client_uri"))[:500] if payload.get("client_uri") else None,
    )
    db.add(client)
    await db.flush()
    return JSONResponse(
        _client_registration_document(client),
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def _load_client(
    db: AsyncSession, client_id: str, redirect_uri: str, settings: Settings
) -> OAuthClient:
    if is_metadata_document_client_id(client_id):
        client = await _metadata_document_client(db, client_id, settings)
    else:
        client = (
            await db.execute(
                select(OAuthClient).where(
                    OAuthClient.client_id == client_id,
                    OAuthClient.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
    if (
        not client
        or (client.expires_at and client.expires_at <= _now())
        or redirect_uri not in client.redirect_uris
        or not _allowed_redirect(redirect_uri, settings)
    ):
        raise HTTPException(status_code=400, detail="Invalid OAuth client or redirect URI")
    return client


async def _metadata_document_client(
    db: AsyncSession, client_id: str, settings: Settings
) -> OAuthClient | None:
    """A client named by the URL of its metadata document (CIMD), not yet saved.

    The document is read at every authorization (cached briefly), so the client's own current
    description decides; `_load_client` still applies our redirect rule on top of it. An operator
    blocks such a client the same way as a registered one: `deleted_at` on its row.
    """

    if not settings.mcp_client_metadata_documents_enabled:
        return None
    host = urlsplit(client_id).hostname or ""
    try:
        metadata = await load_client_metadata(client_id)
    except ClientMetadataError as exc:
        logger.warning("client_metadata_rejected", extra={"client_host": host, "reason": str(exc)})
        return None
    stored = await db.get(OAuthClient, client_id)
    if stored is not None and stored.deleted_at is not None:
        return None
    # The login page shows the document's host: a document can call itself anything.
    name = f"{metadata.client_name} ({host})" if metadata.client_name else host
    return OAuthClient(
        client_id=client_id,
        client_name=name[:200],
        redirect_uris=list(metadata.redirect_uris),
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
        scope=DEFAULT_SCOPES,
        client_uri=metadata.client_uri,
    )


async def _remember_metadata_document_client(db: AsyncSession, client: OAuthClient) -> None:
    """Codes and tokens reference `oauth_clients`, so a CIMD client gets its row when it receives a
    code: only after a person signed in, so a document alone creates nothing."""

    if not is_metadata_document_client_id(client.client_id):
        return
    values = {
        "client_name": client.client_name,
        "redirect_uris": client.redirect_uris,
        "client_uri": client.client_uri,
        "updated_at": _now(),
    }
    await db.execute(
        pg_insert(OAuthClient)
        .values(
            client_id=client.client_id,
            grant_types=client.grant_types,
            response_types=client.response_types,
            token_endpoint_auth_method="none",
            scope=client.scope,
            created_at=_now(),
            **values,
        )
        .on_conflict_do_update(index_elements=[OAuthClient.client_id], set_=values)
    )


def _allowed_redirect(redirect_uri: str, settings: Settings) -> bool:
    """The current allowlist also binds clients registered under an older, wider rule."""

    try:
        validate_registered_redirect_uri(redirect_uri, settings.oauth_redirect_uris)
    except ValueError:
        return False
    return True


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.mcp_oauth_secret, salt="okk-mcp-authorize-v1")


def _security_headers(
    response: Response,
    *,
    allow_form: bool = False,
    redirect_uri: str | None = None,
    login_origin: str | None = None,
) -> None:
    """`login_origin` — страница входа OKK: браузеры проверяют `form-action` и на переходе после формы."""

    form_action_sources = ["'self'"] if allow_form else ["'none'"]
    if allow_form and redirect_uri is not None:
        form_action_sources.append(redirect_origin(redirect_uri))
    if allow_form and login_origin is not None:
        form_action_sources.append(login_origin)
    directives = ["default-src 'none'", "style-src 'unsafe-inline'"]
    directives.extend(
        [
            f"form-action {' '.join(form_action_sources)}",
            "frame-ancestors 'none'",
            "base-uri 'none'",
        ]
    )
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Security-Policy": "; ".join(directives),
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )


def _render_authorization_error(message: str, *, status_code: int = 400) -> HTMLResponse:
    body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Подключение ОКК</title>
<style>:root{{--bg:#f4f6f8;--card:#fff;--text:#17202a;--muted:#637083;--brand:#1769e0}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font:16px/1.45 system-ui;color:var(--text)}}
main{{min-height:100vh;display:grid;place-items:center;padding:24px}}.card{{width:min(440px,100%);background:var(--card);padding:32px;border-radius:18px;box-shadow:0 16px 48px #17202a18}}
h1{{margin:0 0 8px}}p{{color:var(--muted)}}.hint{{background:#f7f9fb;padding:12px 14px;border-radius:10px;margin-top:18px}}</style></head>
<body><main><section class="card"><h1>Не удалось продолжить вход</h1>
<p>{html.escape(message)}</p><div class="hint">Вернитесь в приложение, из которого подключали OKK Analytics (Claude, Claude Code, Codex или ChatGPT), снова запустите подключение и используйте новую открывшуюся страницу входа.</div>
</section></main></body></html>"""
    response = HTMLResponse(body, status_code=status_code)
    _security_headers(response)
    return response


# Access a person may refuse on the login page: the scope, the form field and its label.
OPTIONAL_GRANTS = {
    "okk.transcripts.read": ("grant_transcripts", "транскрипции доступных звонков"),
    "okk.phones.read": ("grant_phones", "номера клиентов и поиск звонков по номеру"),
}


def _granted_scope(requested: str, form: dict[str, Any]) -> str:
    """Requested scopes minus the optional access the person unchecked. A form of the previous
    page version (no `consent_version`) had no checkboxes: it grants what was requested."""

    if form.get("consent_version") != "2":
        return requested
    refused = {scope for scope, (field, _) in OPTIONAL_GRANTS.items() if form.get(field) != "1"}
    return " ".join(scope for scope in requested.split() if scope not in refused)


def _render_login(
    *,
    settings: Settings,
    client_name: str,
    signed_request: str,
    csrf_token: str,
    redirect_uri: str,
    scope: str,
    granted_scope: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """`granted_scope` is the person's choice from a submitted form; a new page has every box checked."""

    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    destination, known_destination = redirect_destination(redirect_uri, settings.oauth_redirect_uris)
    destination_html = (
        ""
        if known_destination
        else '<div class="error">Этот адрес возврата не относится к известным приложениям. '
        "Не входите, если не начинали подключение сами.</div>"
    )
    checked_scopes = set((scope if granted_scope is None else granted_scope).split())
    optional_grants = "".join(
        f'<label class="grant"><input type="checkbox" name="{field}" value="1"'
        f"{' checked' if scope_name in checked_scopes else ''}> {html.escape(label)}</label>"
        for scope_name, (field, label) in OPTIONAL_GRANTS.items()
        if scope_name in scope.split()
    )
    # Страница шлюза — только согласие: пароль и второй фактор человек вводит на странице OKK.
    body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход в ОКК</title>
<style>:root{{--bg:#f4f6f8;--card:#fff;--text:#17202a;--muted:#637083;--brand:#1769e0;--danger:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font:16px/1.45 system-ui;color:var(--text)}}
main{{min-height:100vh;display:grid;place-items:center;padding:24px}}.card{{width:min(440px,100%);background:var(--card);padding:32px;border-radius:18px;box-shadow:0 16px 48px #17202a18}}
h1{{margin:0 0 8px}}p{{color:var(--muted)}}label{{display:block;font-weight:650;margin:14px 0 6px}}input{{width:100%;padding:12px 14px;border:1px solid #cbd3dc;border-radius:10px;font:inherit}}
button{{width:100%;margin-top:22px;padding:13px;border:0;border-radius:10px;background:var(--brand);color:#fff;font:700 16px system-ui}}.scope,.next{{font-size:14px;background:#f7f9fb;padding:12px 14px;border-radius:10px;margin-top:18px}}.next{{background:#eef6ff}}.error{{color:var(--danger);background:#fef3f2;padding:10px 12px;border-radius:10px}}label.grant{{font-weight:400;margin:8px 0 0}}label.grant input{{width:auto;margin-right:8px}}</style></head>
<body><main><section class="card"><h1>Вход в ОКК</h1><p>Подключение приложения <strong>{html.escape(client_name)}</strong>. После входа браузер вернёт вас <strong>{html.escape(destination)}</strong>.</p>{destination_html}{error_html}
<form method="post" action="/authorize"><input type="hidden" name="authorization_request" value="{html.escape(signed_request, quote=True)}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
<div class="scope">Только чтение, строго в пределах прав аккаунта: статистика, карточки сотрудников, сценарии и критерии.{optional_grants}<input type="hidden" name="consent_version" value="2"></div>
<button type="submit">Продолжить вход в ОКК</button></form>
<div class="next"><strong>Что произойдёт дальше</strong><br>Вы войдёте на странице ОКК, и браузер передаст вход обратно в приложение, из которого вы начали подключение (Codex, Claude Code или ChatGPT). Вернитесь в него и попросите «Проверить подключение OKK»: сообщение «OKK подключён» с ролью и отделами означает, что вход действительно завершён.</div>
<p><small>Вход — на странице ОКК: шлюз не видит пароль и получает только сессию вашей учётки.</small></p></section></main></body></html>"""
    response = HTMLResponse(body)
    _security_headers(
        response,
        allow_form=True,
        redirect_uri=redirect_uri,
        login_origin=settings.okk_browser_origin,
    )
    return response


async def _refresh_authorization_form(
    *,
    authorization_request: str,
    db: AsyncSession,
    settings: Settings,
    granted_scope: str,
    error: str,
) -> HTMLResponse:
    try:
        auth_request = _serializer(settings).loads(authorization_request, max_age=600)
        client = await _load_client(db, auth_request["client_id"], auth_request["redirect_uri"], settings)
    except (BadSignature, SignatureExpired):
        return _render_authorization_error("Срок действия запроса на авторизацию истёк.")
    except (KeyError, TypeError, HTTPException):
        return _render_authorization_error("Запрос на авторизацию больше недействителен.")
    refreshed_csrf = secrets.token_urlsafe(32)
    auth_request["csrf_token"] = refreshed_csrf
    refreshed_request = _serializer(settings).dumps(auth_request)
    return _render_login(
        settings=settings,
        client_name=client.client_name,
        signed_request=refreshed_request,
        csrf_token=refreshed_csrf,
        redirect_uri=auth_request["redirect_uri"],
        scope=auth_request["scope"],
        granted_scope=granted_scope,
        error=error,
    )


@router.get("/authorize")
async def authorize(
    client_id: str | None = None,
    redirect_uri: str | None = None,
    response_type: str | None = None,
    code_challenge: str | None = None,
    code_challenge_method: str | None = None,
    state: str | None = None,
    scope: str | None = None,
    resource: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not all((client_id, redirect_uri, response_type, code_challenge, code_challenge_method)):
        return _render_authorization_error(
            "Подключение не запущено или ссылка устарела. Начните подключение OKK Analytics заново в своём "
            "приложении (Claude, Claude Code, Codex или ChatGPT) и используйте новую страницу входа."
        )
    # An unknown client or return address gets a page, never a redirect (RFC 6749 §4.1.2.1).
    client = await _load_client(db, client_id, redirect_uri, settings)
    if response_type != "code":
        return _authorization_redirect(redirect_uri, {"error": "unsupported_response_type"}, state, settings)
    if not valid_pkce_challenge(code_challenge, code_challenge_method):
        return _authorization_redirect(
            redirect_uri,
            {"error": "invalid_request", "error_description": "PKCE S256 is required"},
            state,
            settings,
        )
    try:
        requested_scope = validate_scopes(scope or client.scope)
    except ValueError:
        return _authorization_redirect(redirect_uri, {"error": "invalid_scope"}, state, settings)
    requested_resource = (resource or settings.resource_url).rstrip("/")
    if requested_resource != settings.resource_url:
        return _authorization_redirect(redirect_uri, {"error": "invalid_target"}, state, settings)
    csrf_token = secrets.token_urlsafe(32)
    signed = _serializer(settings).dumps(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": requested_scope,
            "resource": requested_resource,
            "code_challenge": code_challenge,
            "csrf_token": csrf_token,
        }
    )
    return _render_login(
        settings=settings,
        client_name=client.client_name,
        signed_request=signed,
        csrf_token=csrf_token,
        redirect_uri=redirect_uri,
        scope=requested_scope,
    )


async def _check_login_rate_limit(request: Request, settings: Settings) -> None:
    """Подтверждения подключения с одного адреса: каждое держит запрос в Redis до возврата из OKK.
    Перебор пароля сдерживает страница входа OKK — шлюз пароля не видит."""

    host = request.client.host if request.client else "unknown"
    key = f"okk:mcp:login:ip:{host}:{int(time.time() // 60)}"
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, 120)
            attempts, _ = await pipe.execute()
        if int(attempts) > settings.mcp_login_attempts_per_minute:
            raise HTTPException(status_code=429, detail="Too many login attempts")
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Login protection is temporarily unavailable") from exc
    finally:
        await client.aclose()


# Вход на странице OKK: запрос подключения ждёт возврата из OKK в Redis под случайным `state`
# (одноразово, не дольше подписанного запроса). В адресе — только этот ключ: PKCE-ключ шлюза и
# запрос клиента в браузер не уходят.
OKK_LOGIN_KEY = "okk:mcp:okk-login:{state}"
OKK_LOGIN_SECONDS = 600
# Отказы OKK, которые клиент получает как есть; прочие — `access_denied`.
OKK_LOGIN_ERRORS = frozenset({"access_denied", "temporarily_unavailable"})


async def _remember_okk_login(settings: Settings, pending: dict[str, Any]) -> str:
    state = secrets.token_urlsafe(32)
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        await client.set(OKK_LOGIN_KEY.format(state=state), json.dumps(pending), ex=OKK_LOGIN_SECONDS)
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Login is temporarily unavailable") from exc
    finally:
        await client.aclose()
    return state


async def _take_okk_login(settings: Settings, state: str) -> dict[str, Any] | None:
    """Запрос подключения по `state` — один раз (GETDEL): повтор возврата из OKK его не найдёт."""

    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        raw = await client.getdel(OKK_LOGIN_KEY.format(state=state))
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Login is temporarily unavailable") from exc
    finally:
        await client.aclose()
    return json.loads(raw) if raw else None


def _okk_login_redirect(settings: Settings, state: str, verifier: str) -> RedirectResponse:
    """В браузере — на выдачу кода OKK: человек входит там (Keycloak, SSO, второй фактор)."""

    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
    )
    query = urlencode(
        {
            "client_id": settings.okk_mcp_client_id,
            "redirect_uri": settings.okk_login_callback_url,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    response = RedirectResponse(f"{settings.okk_browser_api_url}/auth/mcp/authorize?{query}", status_code=303)
    response.headers.update({"Cache-Control": "no-store", "Pragma": "no-cache"})
    return response


async def _issue_authorization_code(
    db: AsyncSession,
    *,
    client: OAuthClient,
    account_session: Any,
    auth_request: dict[str, Any],
    granted_scope: str,
    settings: Settings,
) -> RedirectResponse:
    """Сессия OKK человека → одноразовый код клиенту MCP."""

    db.add(account_session)
    await _remember_metadata_document_client(db, client)
    await db.flush()
    raw_code = secrets.token_urlsafe(48)
    db.add(
        OAuthAuthorizationCode(
            code_hash=token_hash(raw_code),
            client_id=client.client_id,
            session_id=account_session.id,
            redirect_uri=auth_request["redirect_uri"],
            scope=granted_scope,
            code_challenge=auth_request["code_challenge"],
            resource=auth_request["resource"],
            expires_at=_now() + timedelta(minutes=settings.mcp_authorization_code_minutes),
        )
    )
    await db.flush()
    return _authorization_redirect(
        auth_request["redirect_uri"], {"code": raw_code}, auth_request.get("state"), settings
    )


@router.post("/authorize")
async def authorize_login(
    request: Request,
    authorization_request: str = Form(...),
    csrf_token: str = Form(...),
    consent_version: str | None = Form(default=None),
    grant_transcripts: str | None = Form(default=None),
    grant_phones: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        auth_request = _serializer(settings).loads(authorization_request, max_age=600)
    except (BadSignature, SignatureExpired):
        return _render_authorization_error("Срок действия запроса на авторизацию истёк.")
    granted_scope = _granted_scope(
        auth_request["scope"],
        {
            "consent_version": consent_version,
            "grant_transcripts": grant_transcripts,
            "grant_phones": grant_phones,
        },
    )
    signed_csrf = auth_request.get("csrf_token")
    if not isinstance(signed_csrf, str) or not secrets.compare_digest(signed_csrf, csrf_token):
        return await _refresh_authorization_form(
            authorization_request=authorization_request,
            db=db,
            settings=settings,
            granted_scope=granted_scope,
            error="Сеанс входа был обновлён. Подтвердите подключение ещё раз.",
        )
    # Клиент и адрес возврата проверяются до похода в OKK: неизвестным — страница, не перенаправление.
    await _load_client(db, auth_request["client_id"], auth_request["redirect_uri"], settings)
    await _check_login_rate_limit(request, settings)
    # Согласие дано — вход на странице OKK; запрос подключения ждёт возврата в Redis.
    verifier = secrets.token_urlsafe(48)
    pending = {
        key: auth_request.get(key)
        for key in ("client_id", "redirect_uri", "state", "scope", "resource", "code_challenge")
    }
    state = await _remember_okk_login(
        settings, {**pending, "granted_scope": granted_scope, "verifier": verifier}
    )
    return _okk_login_redirect(settings, state, verifier)


@router.get("/oauth/okk/callback")
async def okk_login_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Возврат из OKK: одноразовый код OKK → сессия OKK → код клиенту MCP. Запрос подключения —
    по `state` один раз; отказ OKK уходит клиенту ошибкой с его `state`."""

    pending = await _take_okk_login(settings, state) if state else None
    if pending is None:
        return _render_authorization_error("Срок действия входа истёк или он уже завершён.")
    client = await _load_client(db, pending["client_id"], pending["redirect_uri"], settings)
    if error or not code:
        refused = error if error in OKK_LOGIN_ERRORS else "access_denied"
        return _authorization_redirect(
            pending["redirect_uri"], {"error": refused}, pending.get("state"), settings
        )
    try:
        account_session = await platform_client.exchange_login_code(code, pending["verifier"])
    except OKKAuthenticationError:
        return _authorization_redirect(
            pending["redirect_uri"], {"error": "access_denied"}, pending.get("state"), settings
        )
    except OKKUnavailable:
        return _authorization_redirect(
            pending["redirect_uri"], {"error": "temporarily_unavailable"}, pending.get("state"), settings
        )
    return await _issue_authorization_code(
        db,
        client=client,
        account_session=account_session,
        auth_request=pending,
        granted_scope=pending["granted_scope"],
        settings=settings,
    )


def _add_token_pair(
    db: AsyncSession,
    *,
    client_id: str,
    session_id: uuid.UUID,
    scope: str,
    resource: str,
    family_id: uuid.UUID,
    settings: Settings,
) -> tuple[str, str, str]:
    raw_access, raw_refresh = random_token(), random_token()
    access_hash, refresh_hash = token_hash(raw_access), token_hash(raw_refresh)
    now = _now()
    db.add_all(
        [
            OAuthToken(
                token_hash=access_hash,
                token_type="access",
                client_id=client_id,
                session_id=session_id,
                scope=scope,
                resource=resource,
                family_id=family_id,
                expires_at=now + timedelta(minutes=settings.mcp_access_token_minutes),
            ),
            OAuthToken(
                token_hash=refresh_hash,
                token_type="refresh",
                client_id=client_id,
                session_id=session_id,
                scope=scope,
                resource=resource,
                family_id=family_id,
                expires_at=now + timedelta(days=settings.mcp_refresh_token_days),
            ),
        ]
    )
    return raw_access, raw_refresh, refresh_hash


def _token_response(access: str, refresh: str, scope: str, settings: Settings) -> JSONResponse:
    return JSONResponse(
        {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": settings.mcp_access_token_minutes * 60,
            "refresh_token": refresh,
            "scope": scope,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/token")
async def token_endpoint(
    grant_type: str = Form(...),
    client_id: str = Form(...),
    code: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
    scope: str | None = Form(default=None),
    resource: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    client = (
        await db.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id, OAuthClient.deleted_at.is_(None))
        )
    ).scalar_one_or_none()
    if not client:
        return _oauth_error("invalid_client", "Unknown public client", 401)
    if grant_type == "authorization_code":
        if not code or not redirect_uri or not code_verifier:
            return _oauth_error("invalid_request", "code, redirect_uri and code_verifier are required")
        stored = (
            await db.execute(
                select(OAuthAuthorizationCode)
                .where(OAuthAuthorizationCode.code_hash == token_hash(code))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if stored is not None and stored.consumed_at is not None:
            await _revoke_replayed_code_grant(db, stored)
            return _oauth_error("invalid_grant", "Authorization code reuse detected; session revoked")
        if (
            not stored
            or stored.expires_at <= _now()
            or stored.client_id != client_id
            or stored.redirect_uri != redirect_uri
            or not verify_pkce(code_verifier, stored.code_challenge)
            or (resource and resource.rstrip("/") != stored.resource)
        ):
            return _oauth_error("invalid_grant", "Authorization code is invalid or expired")
        stored.consumed_at = _now()
        if client.first_authorized_at is None:
            client.first_authorized_at = stored.consumed_at
        raw_access, raw_refresh, _ = _add_token_pair(
            db,
            client_id=client_id,
            session_id=stored.session_id,
            scope=stored.scope,
            resource=stored.resource,
            family_id=uuid.uuid4(),
            settings=settings,
        )
        await db.flush()
        return _token_response(raw_access, raw_refresh, stored.scope, settings)
    if grant_type == "refresh_token":
        if not refresh_token:
            return _oauth_error("invalid_request", "refresh_token is required")
        stored = (
            await db.execute(
                select(OAuthToken)
                .where(OAuthToken.token_hash == token_hash(refresh_token), OAuthToken.token_type == "refresh")
                .with_for_update()
            )
        ).scalar_one_or_none()
        if not stored or stored.client_id != client_id or stored.expires_at <= _now():
            return _oauth_error("invalid_grant", "Refresh token is invalid or expired")
        if stored.consumed_at is not None:
            retried = await _retried_rotation(db, stored, settings)
            if retried is not None:
                return _token_response(*retried, settings)
            await db.execute(
                update(OAuthToken)
                .where(OAuthToken.family_id == stored.family_id, OAuthToken.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await platform_client.revoke_upstream(stored.session_id)
            return _oauth_error("invalid_grant", "Refresh token reuse detected; session revoked")
        if stored.revoked_at is not None:
            return _oauth_error("invalid_grant", "Refresh token is invalid or expired")
        requested_resource = (resource or stored.resource).rstrip("/")
        if requested_resource != stored.resource:
            return _oauth_error("invalid_target", "Resource cannot change during refresh")
        try:
            requested_scope = validate_scopes(scope or stored.scope)
        except ValueError:
            return _oauth_error("invalid_scope", "Unsupported scope")
        if not set(requested_scope.split()).issubset(set(stored.scope.split())):
            return _oauth_error("invalid_scope", "Refresh cannot expand scope")
        try:
            await platform_client.live_context(stored.session_id)
        except OKKAuthenticationError:
            return _oauth_error("invalid_grant", "OKK account session is unavailable")
        except OKKUnavailable:
            return _oauth_error("temporarily_unavailable", "OKK is temporarily unavailable", 503)
        stored.consumed_at = _now()
        stored.revoked_at = _now()
        raw_access, raw_refresh, next_hash = _add_token_pair(
            db,
            client_id=client_id,
            session_id=stored.session_id,
            scope=requested_scope,
            resource=stored.resource,
            family_id=stored.family_id,
            settings=settings,
        )
        stored.replaced_by_hash = next_hash
        stored.successor_sealed = platform_client.cipher.seal(
            json.dumps({"access": raw_access, "refresh": raw_refresh})
        )
        await db.flush()
        return _token_response(raw_access, raw_refresh, requested_scope, settings)
    return _oauth_error("unsupported_grant_type", "Unsupported grant type")


async def _revoke_replayed_code_grant(db: AsyncSession, code: OAuthAuthorizationCode) -> None:
    """A code used twice may have leaked: revoke what it issued (RFC 6749 §4.1.2).

    Each sign-in creates its own OKK account session and one code, so the tokens of that session
    are exactly the tokens this code issued, including their refreshed successors.
    """

    await db.execute(
        update(OAuthToken)
        .where(OAuthToken.session_id == code.session_id, OAuthToken.revoked_at.is_(None))
        .values(revoked_at=_now())
    )
    await platform_client.revoke_upstream(code.session_id)


async def _retried_rotation(
    db: AsyncSession, stored: OAuthToken, settings: Settings
) -> tuple[str, str, str] | None:
    """The same pair again for a refresh repeated within seconds: the client lost the answer,
    or two of its processes refreshed at once.

    Only while the issued refresh token is unused and not revoked. Later, or once the chain moved
    on, a used refresh token is treated as stolen and the whole family is revoked.
    """

    if not stored.successor_sealed or not stored.replaced_by_hash or stored.consumed_at is None:
        return None
    if _now() - stored.consumed_at > timedelta(seconds=settings.mcp_refresh_retry_seconds):
        return None
    successor = (
        await db.execute(
            select(OAuthToken)
            .where(OAuthToken.token_hash == stored.replaced_by_hash, OAuthToken.token_type == "refresh")
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not successor or successor.consumed_at is not None or successor.revoked_at is not None:
        return None
    pair = json.loads(platform_client.cipher.open(stored.successor_sealed))
    return pair["access"], pair["refresh"], successor.scope


@router.post("/revoke")
async def revoke_token(
    token: str = Form(...), client_id: str = Form(...), db: AsyncSession = Depends(get_db)
):
    stored = (
        await db.execute(
            select(OAuthToken).where(
                OAuthToken.token_hash == token_hash(token), OAuthToken.client_id == client_id
            )
        )
    ).scalar_one_or_none()
    if stored:
        if stored.token_type == "refresh":
            await db.execute(
                update(OAuthToken)
                .where(OAuthToken.family_id == stored.family_id, OAuthToken.revoked_at.is_(None))
                .values(revoked_at=_now())
            )
            await platform_client.revoke_upstream(stored.session_id)
        else:
            stored.revoked_at = _now()
    return Response(status_code=200, headers={"Cache-Control": "no-store"})


def _account_claims(context: AccountContext | None) -> dict[str, Any]:
    """Live OKK account for the request; empty while OKK is unavailable."""

    if context is None:
        return {}
    return {
        "okk_user_id": context.user_id,
        "email": context.email,
        "role": context.role,
        "department_ids": list(context.department_ids),
        # Internal request context only; never serialized into MCP output.
        "_upstream_access_token": context.access_token,
    }


class OKKTokenVerifier(TokenVerifier):
    """Verify opaque MCP tokens and refresh the user's live OKK ACL every request."""

    def __init__(self, resource_url: str):
        self.resource_url = resource_url.rstrip("/")

    async def verify_token(self, token: str) -> AccessToken | None:
        async with session_factory() as db:
            stored = (
                await db.execute(
                    select(OAuthToken).where(
                        OAuthToken.token_hash == token_hash(token), OAuthToken.token_type == "access"
                    )
                )
            ).scalar_one_or_none()
            if (
                not stored
                or stored.revoked_at is not None
                or stored.expires_at <= _now()
                or stored.resource.rstrip("/") != self.resource_url
            ):
                return None
            client_id, scopes, expires_at = stored.client_id, stored.scope.split(), stored.expires_at
            resource, session_id = stored.resource, stored.session_id
        try:
            context = await platform_client.live_context(session_id)
        except OKKAuthenticationError:
            return None
        except OKKUnavailable:
            # Our token is valid; OKK is down. A 401 here would send the client to sign in again,
            # so the request goes on and the tool reports that OKK is temporarily unavailable.
            context = None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(expires_at.timestamp()),
            resource=resource,
            subject=str(session_id),
            claims=_account_claims(context),
        )


def _bearer_token(request: Request) -> str | None:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


@router.get("/userinfo")
async def userinfo(request: Request, settings: Settings = Depends(get_settings)):
    """Return the live verified OKK account email for workspace domain policy."""

    raw_token = _bearer_token(request)
    if raw_token is None:
        return _oauth_error("invalid_token", "Bearer access token is required", 401)
    verified = await OKKTokenVerifier(settings.resource_url).verify_token(raw_token)
    if verified is None:
        return _oauth_error("invalid_token", "Access token is invalid or expired", 401)
    if not {"openid", "email"}.issubset(set(verified.scopes)):
        return _oauth_error("insufficient_scope", "openid and email scopes are required", 403)
    claims = verified.claims or {}
    if not claims:
        return _oauth_error("temporarily_unavailable", "OKK is temporarily unavailable", 503)
    email = str(claims.get("email") or "").strip().lower()
    subject = str(claims.get("okk_user_id") or verified.subject or "")
    if not email or not subject:
        return _oauth_error("invalid_token", "Verified account identity is unavailable", 401)
    return JSONResponse(
        {"sub": subject, "email": email, "email_verified": True},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )
