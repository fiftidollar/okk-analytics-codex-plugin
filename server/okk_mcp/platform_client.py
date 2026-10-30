"""Authenticated, GET-only client for a user's existing OKK account."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookiejar import Cookie, CookieJar, DefaultCookiePolicy
from typing import Any
from urllib.request import Request
from uuid import UUID

import httpx
from sqlalchemy import select

from okk_mcp.config import Settings
from okk_mcp.crypto import SessionCipher
from okk_mcp.db import session_factory
from okk_mcp.models import OkkAccountSession

# Pauses before the second and the third attempt of a read that failed in transit.
_READ_RETRY_DELAYS_SECONDS = (0.3, 1.0)
_TRANSIENT_STATUSES = frozenset({502, 503, 504})
_RENEWALS_KEPT = 1000
OKK_ROLES = frozenset({"admin", "viewer", "employee"})


class OKKAuthenticationError(PermissionError):
    pass


class OKKUnavailable(RuntimeError):
    pass


class OKKNotAvailable(LookupError):
    """Neutral 403/404 result: callers must not distinguish the reason."""


@dataclass(frozen=True)
class AccountContext:
    session_id: UUID
    user_id: str
    email: str
    role: str
    department_ids: tuple[str, ...]
    access_token: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class _StoreNoCookies(DefaultCookiePolicy):
    """One HTTP client serves every person: a cookie OKK sets for one must never reach another."""

    def set_ok(self, cookie: Cookie, request: Request) -> bool:
        return False


class OKKPlatformClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.cipher = SessionCipher(settings.mcp_session_encryption_secret)
        self.client = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(settings.mcp_http_timeout_seconds),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            follow_redirects=False,
            headers={"Accept": "application/json", "X-OKK-Client": "codex-mcp-readonly"},
            cookies=CookieJar(policy=_StoreNoCookies()),
            transport=transport,
        )
        # Session id -> (expired, renewed) OKK access token. A tool call keeps the context it started
        # with; its later requests take the renewed token from here instead of meeting a 401 each.
        self._renewed_access: dict[UUID, tuple[str, str]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    @staticmethod
    def _refresh_cookie_header(refresh_token: str) -> dict[str, str]:
        return {"Cookie": f"refresh_token={refresh_token}"}

    @staticmethod
    def _refresh_cookie(response: httpx.Response) -> str:
        for cookie in response.cookies.jar:
            if cookie.name == "refresh_token" and cookie.value:
                return cookie.value
        raise OKKAuthenticationError("OKK did not issue a refresh session")

    @staticmethod
    def _user_fields(payload: dict[str, Any]) -> tuple[str, str, str, list[str]]:
        user_id = str(payload.get("id") or "")
        email = str(payload.get("email") or "").strip().lower()
        role = str(payload.get("role") or "")
        department_ids = [str(value) for value in payload.get("department_ids") or []]
        # Роли учётки компании OKK (фаза 5 мультитенантности): что видит сотрудник, решает сам OKK.
        if not user_id or not email or role not in OKK_ROLES or not payload.get("is_active", True):
            raise OKKAuthenticationError("OKK account is inactive or has an unsupported role")
        return user_id, email, role, department_ids

    async def authenticate(self, email: str, password: str) -> OkkAccountSession:
        """Exchange credentials directly with OKK; the password is never persisted."""

        try:
            response = await self.client.post(
                "/auth/login",
                json={"email": email.strip(), "password": password},
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK login is temporarily unavailable") from exc
        if response.status_code == 401:
            raise OKKAuthenticationError("Invalid OKK login or password")
        return self._account_session(response)

    async def exchange_login_code(self, code: str, code_verifier: str) -> OkkAccountSession:
        """Вход на странице OKK: одноразовый код OKK → сессия OKK сервер-к-серверу (секрет шлюза и
        PKCE). Ответ OKK — тот же, что у входа по паролю."""

        try:
            response = await self.client.post(
                "/auth/mcp/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.settings.okk_login_callback_url,
                    "code_verifier": code_verifier,
                    "client_id": self.settings.okk_mcp_client_id,
                    "client_secret": self.settings.okk_mcp_client_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK login is temporarily unavailable") from exc
        if response.status_code in {400, 401}:
            raise OKKAuthenticationError("OKK rejected the login code")
        return self._account_session(response)

    def _account_session(self, response: httpx.Response) -> OkkAccountSession:
        if response.status_code >= 500:
            raise OKKUnavailable("OKK login is temporarily unavailable")
        if response.status_code != 200:
            raise OKKAuthenticationError("OKK rejected the login request")

        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise OKKAuthenticationError("OKK did not issue an access session")
        refresh_token = self._refresh_cookie(response)
        user_id, _email, role, department_ids = self._user_fields(payload.get("user") or {})
        return OkkAccountSession(
            okk_user_id=user_id,
            encrypted_access_token=self.cipher.seal(access_token),
            encrypted_refresh_token=self.cipher.seal(refresh_token),
            role_snapshot=role,
            department_ids_snapshot=department_ids,
            last_verified_at=datetime.now(UTC),
        )

    async def _get_me(self, access_token: str) -> httpx.Response:
        try:
            return await self.client.get(
                "/auth/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK is temporarily unavailable") from exc

    async def _refresh(self, refresh_token: str) -> tuple[str, str, dict[str, Any]]:
        try:
            response = await self.client.post(
                "/auth/refresh", headers=self._refresh_cookie_header(refresh_token)
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK session refresh is temporarily unavailable") from exc
        if response.status_code == 401:
            raise OKKAuthenticationError("OKK session has expired; reconnect the plugin")
        if response.status_code >= 500:
            raise OKKUnavailable("OKK session refresh is temporarily unavailable")
        if response.status_code != 200:
            raise OKKAuthenticationError("OKK rejected the session refresh")
        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise OKKAuthenticationError("OKK did not refresh the access session")
        return access_token, self._refresh_cookie(response), payload.get("user") or {}

    async def live_context(self, session_id: UUID | str) -> AccountContext:
        """Reload the live OKK user (role, departments) for one account session.

        The usual path holds no database lock and no open transaction while OKK answers, so parallel
        tool calls of one person run in parallel. Only the rotation of the OKK refresh token is
        serialized (`_rotate_session`).
        """

        try:
            parsed_id = UUID(str(session_id))
        except ValueError as exc:
            raise OKKAuthenticationError("Invalid OKK account session") from exc

        async with session_factory() as db:
            row = await db.get(OkkAccountSession, parsed_id)
            if not row or row.revoked_at is not None:
                raise OKKAuthenticationError("OKK account session is no longer active")
            okk_user_id = row.okk_user_id
            access_token = self.cipher.open(row.encrypted_access_token)

        response = await self._get_me(access_token)
        if response.status_code == 401:
            access_token, user = await self._rotate_session(parsed_id, access_token)
        else:
            user = self._me_payload(response)

        user_id, email, role, department_ids = self._user_fields(user)
        async with session_factory() as db:
            row = await db.get(OkkAccountSession, parsed_id)
            if not row or row.revoked_at is not None:
                raise OKKAuthenticationError("OKK account session is no longer active")
            if user_id != okk_user_id:
                row.revoked_at = datetime.now(UTC)
                await db.commit()
                raise OKKAuthenticationError("OKK account identity changed")
            row.role_snapshot = role
            row.department_ids_snapshot = department_ids
            row.last_verified_at = datetime.now(UTC)
            await db.commit()
        return AccountContext(
            session_id=parsed_id,
            user_id=user_id,
            email=email,
            role=role,
            department_ids=tuple(department_ids),
            access_token=access_token,
        )

    @staticmethod
    def _me_payload(response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 200:
            return response.json()
        if response.status_code >= 500:
            raise OKKUnavailable("OKK is temporarily unavailable")
        raise OKKAuthenticationError("OKK rejected the account session")

    async def _rotate_session(self, session_id: UUID, expired_access: str) -> tuple[str, dict[str, Any]]:
        """Renew an expired OKK access token once, even when several requests notice it together.

        OKK rotates its refresh token: using one twice ends the OKK session. So the rotation holds
        the session row lock across the OKK refresh call — the one external call made under a lock.
        A request that waited for the lock finds the token already renewed and only checks it.
        """

        async with session_factory() as db:
            row = (
                await db.execute(
                    select(OkkAccountSession).where(OkkAccountSession.id == session_id).with_for_update()
                )
            ).scalar_one_or_none()
            if not row or row.revoked_at is not None:
                raise OKKAuthenticationError("OKK account session is no longer active")
            current_access = self.cipher.open(row.encrypted_access_token)
            if current_access == expired_access:
                refresh_token = self.cipher.open(row.encrypted_refresh_token)
                try:
                    access_token, rotated_refresh, user = await self._refresh(refresh_token)
                except OKKAuthenticationError:
                    row.revoked_at = datetime.now(UTC)
                    await db.commit()
                    raise
                row.encrypted_access_token = self.cipher.seal(access_token)
                row.encrypted_refresh_token = self.cipher.seal(rotated_refresh)
                await db.commit()
                self._remember_renewal(session_id, expired_access, access_token)
                return access_token, user
        # Another request renewed the token while this one waited for the lock.
        user = self._me_payload(await self._get_me(current_access))
        self._remember_renewal(session_id, expired_access, current_access)
        return current_access, user

    def _remember_renewal(self, session_id: UUID, expired_access: str, renewed_access: str) -> None:
        self._renewed_access.pop(session_id, None)
        self._renewed_access[session_id] = (expired_access, renewed_access)
        if len(self._renewed_access) > _RENEWALS_KEPT:
            del self._renewed_access[next(iter(self._renewed_access))]

    def _current_access(self, context: AccountContext) -> str:
        expired_access, renewed_access = self._renewed_access.get(context.session_id, ("", ""))
        return renewed_access if context.access_token == expired_access else context.access_token

    async def get(
        self,
        session_id: UUID | str,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
    ) -> Any:
        self._validate_read_path(path)
        context = await self.live_context(session_id)
        return await self.get_with_context(context, path, params=params)

    @staticmethod
    def _validate_read_path(path: str) -> None:
        if not path.startswith("/") or ".." in path or path.startswith("/auth/"):
            raise ValueError("Only allowlisted OKK analytics GET paths are permitted")

    async def get_with_context(
        self,
        context: AccountContext,
        path: str,
        *,
        params: list[tuple[str, str]] | dict[str, Any] | None = None,
    ) -> Any:
        """Use a context already revalidated by the MCP token verifier."""

        self._validate_read_path(path)
        try:
            response = await self._read(context, "GET", path, params=params)
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK analytics is temporarily unavailable") from exc
        if response.status_code in {401}:
            raise OKKAuthenticationError("OKK session is no longer valid")
        # 409: OKK closes a restricted employee (a private supervisor) this way.
        if response.status_code in {403, 404, 409}:
            raise OKKNotAvailable("Requested OKK data is not available")
        if response.status_code >= 500:
            raise OKKUnavailable("OKK analytics is temporarily unavailable")
        if response.status_code >= 400:
            raise ValueError(f"OKK rejected the analytics filter ({response.status_code})")
        return response.json()

    async def _read(self, context: AccountContext, method: str, path: str, **request: Any) -> httpx.Response:
        """Send a read; an OKK access token that expired during the tool call is renewed once and the
        read repeated, so the person is not sent to reconnect while the session can still be renewed."""

        access_token = self._current_access(context)
        response = await self._send_read(method, path, access_token, **request)
        if response.status_code != 401:
            return response
        renewed_access, _user = await self._rotate_session(context.session_id, access_token)
        return await self._send_read(method, path, renewed_access, **request)

    async def _send_read(self, method: str, path: str, access_token: str, **request: Any) -> httpx.Response:
        """A dropped connection, a timeout or 502/503/504 is repeated twice after a short pause.

        Only reads go through here, so a repeat cannot change anything in OKK. A 4xx is an answer.
        """

        headers = {"Authorization": f"Bearer {access_token}"}
        for delay in _READ_RETRY_DELAYS_SECONDS:
            try:
                response = await self.client.request(method, path, headers=headers, **request)
            except httpx.TransportError:
                pass
            else:
                if response.status_code not in _TRANSIENT_STATUSES:
                    return response
            await asyncio.sleep(delay)
        return await self.client.request(method, path, headers=headers, **request)

    async def phone_lookup_with_context(
        self,
        context: AccountContext,
        payload: dict[str, Any],
    ) -> Any:
        """Read phone history through the approved body-only POST route."""

        return await self._post_lookup(context, "/calls/phone-lookup", payload, "phone lookup")

    async def transcript_search_with_context(
        self,
        context: AccountContext,
        payload: dict[str, Any],
    ) -> Any:
        """Search shown transcripts through the approved body-only POST route: OKK filters the whole
        ACL-scoped journal and returns the exact number of matching calls."""

        return await self._post_lookup(context, "/calls/transcript-search", payload, "transcript search")

    async def _post_lookup(
        self, context: AccountContext, path: str, payload: dict[str, Any], contract: str
    ) -> Any:
        # Read-only journal reads whose filters (a phone number, words of a conversation) stay out
        # of URLs and access logs. Only these two routes are ever posted to.
        try:
            response = await self._read(context, "POST", path, json=payload)
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK analytics is temporarily unavailable") from exc
        if response.status_code == 401:
            raise OKKAuthenticationError("OKK session is no longer valid")
        if response.status_code == 404:
            raise OKKUnavailable(f"OKK {contract} contract is temporarily unavailable")
        if response.status_code in {403, 409}:
            raise OKKNotAvailable("Requested OKK data is not available")
        if response.status_code >= 500:
            raise OKKUnavailable("OKK analytics is temporarily unavailable")
        if response.status_code >= 400:
            raise ValueError(f"OKK rejected the analytics filter ({response.status_code})")
        return response.json()

    async def revoke_upstream(self, session_id: UUID | str) -> None:
        """Best-effort upstream logout without ever exposing the refresh token."""

        try:
            parsed_id = UUID(str(session_id))
        except ValueError:
            return
        async with session_factory() as db:
            row = await db.get(OkkAccountSession, parsed_id)
            if not row or row.revoked_at is not None:
                return
            refresh_token = self.cipher.open(row.encrypted_refresh_token)
            row.revoked_at = datetime.now(UTC)
            await db.commit()
        try:
            await self.client.post("/auth/logout", headers=self._refresh_cookie_header(refresh_token))
        except httpx.HTTPError:
            pass
