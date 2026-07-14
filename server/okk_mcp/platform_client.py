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
    def _user_fields(payload: dict[str, Any]) -> tuple[str, str, list[str]]:
        user_id = str(payload.get("id") or "")
        role = str(payload.get("role") or "")
        department_ids = [str(value) for value in payload.get("department_ids") or []]
        if not user_id or role not in {"admin", "viewer"} or not payload.get("is_active", True):
            raise OKKAuthenticationError("OKK account is inactive or has an unsupported role")
        return user_id, role, department_ids

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
        if response.status_code >= 500:
            raise OKKUnavailable("OKK login is temporarily unavailable")
        if response.status_code != 200:
            raise OKKAuthenticationError("OKK rejected the login request")

        payload = response.json()
        access_token = str(payload.get("access_token") or "")
        if not access_token:
            raise OKKAuthenticationError("OKK did not issue an access session")
        refresh_token = self._refresh_cookie(response)
        user_id, role, department_ids = self._user_fields(payload.get("user") or {})
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
        """Reload live user state and serialize refresh-token rotation per account session."""

        try:
            parsed_id = UUID(str(session_id))
        except ValueError as exc:
            raise OKKAuthenticationError("Invalid OKK account session") from exc

        async with session_factory() as db:
            row = (
                await db.execute(
                    select(OkkAccountSession).where(OkkAccountSession.id == parsed_id).with_for_update()
                )
            ).scalar_one_or_none()
            if not row or row.revoked_at is not None:
                raise OKKAuthenticationError("OKK account session is no longer active")

            access_token = self.cipher.open(row.encrypted_access_token)
            response = await self._get_me(access_token)
            if response.status_code == 401:
                refresh_token = self.cipher.open(row.encrypted_refresh_token)
                try:
                    access_token, rotated_refresh, user = await self._refresh(refresh_token)
                except OKKAuthenticationError:
                    row.revoked_at = datetime.now(UTC)
                    await db.commit()
                    raise
                row.encrypted_access_token = self.cipher.seal(access_token)
                row.encrypted_refresh_token = self.cipher.seal(rotated_refresh)
            elif response.status_code == 200:
                user = response.json()
            elif response.status_code >= 500:
                raise OKKUnavailable("OKK is temporarily unavailable")
            else:
                raise OKKAuthenticationError("OKK rejected the account session")

            user_id, role, department_ids = self._user_fields(user)
            if user_id != row.okk_user_id:
                row.revoked_at = datetime.now(UTC)
                await db.commit()
                raise OKKAuthenticationError("OKK account identity changed")
            row.role_snapshot = role
            row.department_ids_snapshot = department_ids
            row.last_verified_at = datetime.now(UTC)
            await db.commit()
            return AccountContext(
                session_id=row.id,
                user_id=user_id,
                role=role,
                department_ids=tuple(department_ids),
                access_token=access_token,
            )

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
