"""Authenticated, GET-only client for a user's existing OKK account."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from okk_mcp.config import Settings
from okk_mcp.crypto import SessionCipher
from okk_mcp.db import session_factory
from okk_mcp.models import OkkAccountSession


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


class OKKPlatformClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cipher = SessionCipher(settings.mcp_session_encryption_secret)
        self.client = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=httpx.Timeout(settings.mcp_http_timeout_seconds),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=30),
            follow_redirects=False,
            headers={"Accept": "application/json", "X-OKK-Client": "codex-mcp-readonly"},
        )

    async def close(self) -> None:
        await self.client.aclose()

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
        if not user_id or not email or role not in {"admin", "viewer"} or not payload.get("is_active", True):
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

    async def exchange_sso_handoff(self, code: str) -> OkkAccountSession:
        """Exchange the one-time code of an OKK corporate (SSO) login for an OKK session.

        Only this gateway knows the handoff secret, so a code taken from a browser address alone
        opens nothing; OKK accepts each code once and for seconds.
        """

        try:
            response = await self.client.post(
                "/auth/sso/handoff/token",
                json={"code": code},
                headers={"Authorization": f"Bearer {self.settings.okk_sso_handoff_secret}"},
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK login is temporarily unavailable") from exc
        if response.status_code in {400, 401, 403, 404}:
            raise OKKAuthenticationError("OKK rejected the corporate login")
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
                "/auth/refresh",
                cookies={"refresh_token": refresh_token},
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
                return access_token, user
        # Another request renewed the token while this one waited for the lock.
        return current_access, self._me_payload(await self._get_me(current_access))

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
            response = await self.client.get(
                path,
                params=params,
                headers={"Authorization": f"Bearer {context.access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK analytics is temporarily unavailable") from exc
        if response.status_code in {401}:
            raise OKKAuthenticationError("OKK session is no longer valid")
        if response.status_code in {403, 404}:
            raise OKKNotAvailable("Requested OKK data is not available")
        if response.status_code >= 500:
            raise OKKUnavailable("OKK analytics is temporarily unavailable")
        if response.status_code >= 400:
            raise ValueError(f"OKK rejected the analytics filter ({response.status_code})")
        return response.json()

    async def phone_lookup_with_context(
        self,
        context: AccountContext,
        payload: dict[str, Any],
    ) -> Any:
        """Read phone history through the sole approved body-only POST route."""

        try:
            response = await self.client.post(
                "/calls/phone-lookup",
                json=payload,
                headers={"Authorization": f"Bearer {context.access_token}"},
            )
        except httpx.HTTPError as exc:
            raise OKKUnavailable("OKK analytics is temporarily unavailable") from exc
        if response.status_code == 401:
            raise OKKAuthenticationError("OKK session is no longer valid")
        if response.status_code == 404:
            raise OKKUnavailable("OKK phone lookup contract is temporarily unavailable")
        if response.status_code == 403:
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
            await self.client.post("/auth/logout", cookies={"refresh_token": refresh_token})
        except httpx.HTTPError:
            pass
