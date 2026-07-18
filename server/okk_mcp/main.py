"""ASGI entrypoint combining OKK OAuth endpoints and stateless MCP transport."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress

import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from okk_mcp.backend_client import BackendClient
from okk_mcp.config import get_settings
from okk_mcp.db import close_db, session_factory
from okk_mcp.maintenance import cleanup_loop
from okk_mcp.oauth import router as oauth_router
from okk_mcp.runtime import platform_client
from okk_mcp.server import create_mcp_server

settings = get_settings()
backend_client = BackendClient(settings, platform_client)
mcp = create_mcp_server(settings, backend_client)
mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    cleanup_task = asyncio.create_task(cleanup_loop(settings, platform_client))
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
    await backend_client.close()
    await platform_client.close()
    await close_db()


app = FastAPI(
    title="OKK Analytics MCP",
    version="1.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.include_router(oauth_router)


@app.get("/", response_class=HTMLResponse)
async def service_home():
    return HTMLResponse(
        "<h1>OKK Analytics</h1><p>Read-only MCP service. Connect it from Codex and sign in with your OKK account on this authorization page.</p>",
        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
    )


@app.get("/health")
async def health():
    return {"status": "ok", "service": "okk-analytics-mcp", "version": "1.1.0"}


@app.get("/ready")
async def ready():
    async with session_factory() as db:
        await db.execute(text("SELECT 1"))
    redis_client = redis.from_url(settings.redis_url)
    try:
        await redis_client.ping()
    finally:
        await redis_client.aclose()
    return {"status": "ready"}


# Mounted last so OAuth/metadata/health routes remain owned by FastAPI while the
# MCP app keeps its own authentication middleware and transport route at /mcp.
app.mount("/", mcp_app)
