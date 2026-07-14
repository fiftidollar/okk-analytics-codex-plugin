"""Bounded cleanup of expired OAuth grants and orphaned upstream sessions."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, exists, or_, select

from okk_mcp.config import Settings
from okk_mcp.db import session_factory
from okk_mcp.models import OAuthAuthorizationCode, OAuthToken, OkkAccountSession
from okk_mcp.platform_client import OKKPlatformClient

logger = logging.getLogger(__name__)


async def cleanup_once(settings: Settings, platform: OKKPlatformClient) -> dict[str, int]:
    now = datetime.now(UTC)
    retention_cutoff = now - timedelta(days=settings.mcp_audit_retention_days)
    live_token = exists().where(
        OAuthToken.session_id == OkkAccountSession.id,
        OAuthToken.expires_at > now,
        OAuthToken.revoked_at.is_(None),
    )
    live_code = exists().where(
        OAuthAuthorizationCode.session_id == OkkAccountSession.id,
        OAuthAuthorizationCode.expires_at > now,
        OAuthAuthorizationCode.consumed_at.is_(None),
    )
    async with session_factory() as db:
        orphan_ids = list(
            (
                await db.scalars(
                    select(OkkAccountSession.id)
                    .where(OkkAccountSession.revoked_at.is_(None), ~live_token, ~live_code)
                    .limit(500)
                )
            ).all()
        )
    for session_id in orphan_ids:
        await platform.revoke_upstream(session_id)

    async with session_factory() as db:
        code_result = await db.execute(
            delete(OAuthAuthorizationCode).where(OAuthAuthorizationCode.expires_at < retention_cutoff)
        )
        token_result = await db.execute(
            delete(OAuthToken).where(
                or_(
                    OAuthToken.expires_at < retention_cutoff,
                    OAuthToken.revoked_at < retention_cutoff,
                )
            )
        )
        referenced_token = exists().where(OAuthToken.session_id == OkkAccountSession.id)
        referenced_code = exists().where(OAuthAuthorizationCode.session_id == OkkAccountSession.id)
        session_result = await db.execute(
            delete(OkkAccountSession).where(
                OkkAccountSession.revoked_at < retention_cutoff,
                ~referenced_token,
                ~referenced_code,
            )
        )
        await db.commit()
    return {
        "orphan_sessions_revoked": len(orphan_ids),
        "codes_deleted": code_result.rowcount or 0,
        "tokens_deleted": token_result.rowcount or 0,
        "sessions_deleted": session_result.rowcount or 0,
    }


async def cleanup_loop(settings: Settings, platform: OKKPlatformClient) -> None:
    while True:
        await asyncio.sleep(settings.mcp_cleanup_interval_minutes * 60)
        try:
            report = await cleanup_once(settings, platform)
            if any(report.values()):
                logger.info("oauth_cleanup_completed", extra=report)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("oauth_cleanup_failed")
