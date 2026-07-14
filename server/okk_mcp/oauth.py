"""OAuth 2.1 authorization server backed by native OKK account sessions."""

from __future__ import annotations

import hashlib
import html
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import redis.asyncio as redis
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from okk_mcp.config import Settings, get_settings
from okk_mcp.db import get_db, session_factory
from okk_mcp.models import OAuthAuthorizationCode, OAuthClient, OAuthToken
from okk_mcp.platform_client import OKKAuthenticationError, OKKUnavailable
from okk_mcp.runtime import platform_client
from okk_mcp.security import (
    random_token,
    token_hash,
    valid_pkce_challenge,
    validate_redirect_uri,
    validate_scopes,
    verify_pkce,
)

router = APIRouter()


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


def _authorization_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "issuer": settings.issuer_url,
        "authorization_endpoint": f"{settings.issuer_url}/authorize",
        "token_endpoint": f"{settings.issuer_url}/token",
        "registration_endpoint": f"{settings.issuer_url}/register",
        "revocation_endpoint": f"{settings.issuer_url}/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["okk.statistics.read", "okk.scenarios.read"],
        "service_documentation": f"{settings.issuer_url}/",
    }


@router.get("/.well-known/oauth-authorization-server")
async def authorization_metadata(settings: Settings = Depends(get_settings)):
    return _authorization_metadata(settings)


def _protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    return {
        "resource": settings.resource_url,
        "authorization_servers": [settings.issuer_url],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["okk.statistics.read", "okk.scenarios.read"],
        "resource_documentation": f"{settings.issuer_url}/",
    }


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource_metadata(settings: Settings = Depends(get_settings)):
    return _protected_resource_metadata(settings)


@router.post("/register")
async def register_client(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    try:
        payload = await request.json()
    except Exception:
        return _oauth_error("invalid_client_metadata", "Request body must be valid JSON")
    redirect_uris = payload.get("redirect_uris")
    if not isinstance(redirect_uris, list) or not redirect_uris or len(redirect_uris) > 20:
        return _oauth_error("invalid_redirect_uri", "redirect_uris is required")
    try:
        redirects = list(dict.fromkeys(validate_redirect_uri(str(uri)) for uri in redirect_uris))
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
        {
            "client_id": client.client_id,
            "client_id_issued_at": int(client.created_at.timestamp()),
            "client_name": client.client_name,
            "redirect_uris": client.redirect_uris,
            "grant_types": client.grant_types,
            "response_types": client.response_types,
            "token_endpoint_auth_method": client.token_endpoint_auth_method,
            "scope": client.scope,
            "client_uri": client.client_uri,
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


async def _load_client(db: AsyncSession, client_id: str, redirect_uri: str) -> OAuthClient:
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
    ):
        raise HTTPException(status_code=400, detail="Invalid OAuth client or redirect URI")
    return client


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.mcp_oauth_secret, salt="okk-mcp-authorize-v1")


def _render_login(
    *,
    settings: Settings,
    client_name: str,
    signed_request: str,
    csrf_token: str,
    error: str | None = None,
) -> HTMLResponse:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    body = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход в ОКК</title>
<style>:root{{--bg:#f4f6f8;--card:#fff;--text:#17202a;--muted:#637083;--brand:#1769e0;--danger:#b42318}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);font:16px/1.45 system-ui;color:var(--text)}}
main{{min-height:100vh;display:grid;place-items:center;padding:24px}}.card{{width:min(440px,100%);background:var(--card);padding:32px;border-radius:18px;box-shadow:0 16px 48px #17202a18}}
h1{{margin:0 0 8px}}p{{color:var(--muted)}}label{{display:block;font-weight:650;margin:14px 0 6px}}input{{width:100%;padding:12px 14px;border:1px solid #cbd3dc;border-radius:10px;font:inherit}}
button{{width:100%;margin-top:22px;padding:13px;border:0;border-radius:10px;background:var(--brand);color:#fff;font:700 16px system-ui}}.scope{{font-size:14px;background:#f7f9fb;padding:12px 14px;border-radius:10px;margin-top:18px}}.error{{color:var(--danger);background:#fef3f2;padding:10px 12px;border-radius:10px}}</style></head>
<body><main><section class="card"><h1>Вход в ОКК</h1><p>Подключение приложения <strong>{html.escape(client_name)}</strong></p>{error_html}
<form method="post" action="/authorize"><input type="hidden" name="authorization_request" value="{html.escape(signed_request, quote=True)}"><input type="hidden" name="csrf_token" value="{html.escape(csrf_token, quote=True)}">
<label for="email">Логин (email)</label><input id="email" name="email" type="email" autocomplete="username" maxlength="200" required autofocus>
<label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" maxlength="128" required>
<div class="scope">Только чтение: статистика, карточки сотрудников, наставничество, сценарии и критерии — строго в пределах прав аккаунта.</div>
<button type="submit">Войти и разрешить доступ</button></form><p><small>MCP-шлюз сразу передаёт пароль в штатный API ОКК, не сохраняет его и никогда не передаёт Codex.</small></p></section></main></body></html>"""
    response = HTMLResponse(body)
    response.headers.update(
        {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
    )
    response.set_cookie(
        "okk_mcp_csrf",
        csrf_token,
        max_age=600,
        httponly=True,
        secure=settings.issuer_url.startswith("https://"),
        samesite="lax",
        path="/authorize",
    )
    return response


@router.get("/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    response_type: str,
    code_challenge: str,
    code_challenge_method: str,
    state: str | None = None,
    scope: str | None = None,
    resource: str | None = None,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if response_type != "code" or not valid_pkce_challenge(code_challenge, code_challenge_method):
        raise HTTPException(status_code=400, detail="Authorization Code with PKCE S256 is required")
    client = await _load_client(db, client_id, redirect_uri)
    try:
        requested_scope = validate_scopes(scope or client.scope)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid scope") from None
    requested_resource = (resource or settings.resource_url).rstrip("/")
    if requested_resource != settings.resource_url:
        raise HTTPException(status_code=400, detail="Invalid resource")
    signed = _serializer(settings).dumps(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": requested_scope,
            "resource": requested_resource,
            "code_challenge": code_challenge,
        }
    )
    csrf_token = secrets.token_urlsafe(32)
    return _render_login(
        settings=settings,
        client_name=client.client_name,
        signed_request=signed,
        csrf_token=csrf_token,
    )


async def _check_login_rate_limit(request: Request, email: str, settings: Settings) -> None:
    host = request.client.host if request.client else "unknown"
    account = hashlib.sha256(email.strip().lower().encode()).hexdigest()[:24]
    minute = int(time.time() // 60)
    keys = [f"okk:mcp:login:ip:{host}:{minute}", f"okk:mcp:login:account:{account}:{minute}"]
    client = redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with client.pipeline(transaction=True) as pipe:
            for key in keys:
                pipe.incr(key)
                pipe.expire(key, 120)
            values = await pipe.execute()
        if any(int(value) > settings.mcp_login_attempts_per_minute for value in values[0::2]):
            raise HTTPException(status_code=429, detail="Too many login attempts")
    except RedisError as exc:
        raise HTTPException(status_code=503, detail="Login protection is temporarily unavailable") from exc
    finally:
        await client.aclose()


@router.post("/authorize")
async def authorize_login(
    request: Request,
    authorization_request: str = Form(...),
    csrf_token: str = Form(...),
    email: str = Form(..., max_length=200),
    password: str = Form(..., max_length=128),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    cookie_csrf = request.cookies.get("okk_mcp_csrf")
    if not cookie_csrf or not secrets.compare_digest(cookie_csrf, csrf_token):
        raise HTTPException(status_code=400, detail="Invalid authorization session")
    try:
        auth_request = _serializer(settings).loads(authorization_request, max_age=600)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status_code=400, detail="Authorization request expired") from None
    client = await _load_client(db, auth_request["client_id"], auth_request["redirect_uri"])
    await _check_login_rate_limit(request, email, settings)
    try:
        account_session = await platform_client.authenticate(email, password)
    except OKKAuthenticationError:
        return _render_login(
            settings=settings,
            client_name=client.client_name,
            signed_request=authorization_request,
            csrf_token=csrf_token,
            error="Неверный логин или пароль",
        )
    except OKKUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    db.add(account_session)
    await db.flush()
    raw_code = secrets.token_urlsafe(48)
    db.add(
        OAuthAuthorizationCode(
            code_hash=token_hash(raw_code),
            client_id=client.client_id,
            session_id=account_session.id,
            redirect_uri=auth_request["redirect_uri"],
            scope=auth_request["scope"],
            code_challenge=auth_request["code_challenge"],
            resource=auth_request["resource"],
            expires_at=_now() + timedelta(minutes=settings.mcp_authorization_code_minutes),
        )
    )
    await db.flush()
    query = {"code": raw_code}
    if auth_request.get("state") is not None:
        query["state"] = auth_request["state"]
    response = RedirectResponse(_append_redirect_query(auth_request["redirect_uri"], query), status_code=303)
    response.delete_cookie("okk_mcp_csrf", path="/authorize")
    response.headers["Cache-Control"] = "no-store"
    return response


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
        if (
            not stored
            or stored.consumed_at is not None
            or stored.expires_at <= _now()
            or stored.client_id != client_id
            or stored.redirect_uri != redirect_uri
            or not verify_pkce(code_verifier, stored.code_challenge)
            or (resource and resource.rstrip("/") != stored.resource)
        ):
            return _oauth_error("invalid_grant", "Authorization code is invalid or expired")
        stored.consumed_at = _now()
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
        await db.flush()
        return _token_response(raw_access, raw_refresh, requested_scope, settings)
    return _oauth_error("unsupported_grant_type", "Unsupported grant type")


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
        except (OKKAuthenticationError, OKKUnavailable):
            return None
        return AccessToken(
            token=token,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(expires_at.timestamp()),
            resource=resource,
            subject=str(session_id),
            claims={
                "okk_user_id": context.user_id,
                "role": context.role,
                "department_ids": list(context.department_ids),
                # Internal request context only; never serialized into MCP output.
                "_upstream_access_token": context.access_token,
            },
        )
