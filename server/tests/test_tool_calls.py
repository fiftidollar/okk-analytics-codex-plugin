"""Tool calls through the MCP server: arguments that reach OKK, result shape, errors."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp.exceptions import ToolError

from okk_mcp.backend_client import BackendUnavailable, _segment_chunk
from okk_mcp.config import Settings
from okk_mcp.security import DEFAULT_SCOPES
from okk_mcp.server import create_mcp_server

ENVELOPE = {"status": "ok", "effective_scope": {}, "data": {"total": 3, "name": "Отдел"}}


def _call(tool: str, arguments: dict, client: AsyncMock, scopes: str = DEFAULT_SCOPES):
    token = AccessToken(token="t", client_id="c", scopes=scopes.split(), expires_at=None)
    mcp = create_mcp_server(Settings(), client)

    async def run():
        marker = auth_context_var.set(AuthenticatedUser(token))
        try:
            return await mcp._tool_manager.call_tool(tool, arguments, convert_result=True)
        finally:
            auth_context_var.reset(marker)

    return asyncio.run(run())


def _client(result: dict | Exception = ENVELOPE) -> AsyncMock:
    client = AsyncMock()
    if isinstance(result, Exception):
        client.get.side_effect = result
    else:
        client.get.return_value = result
    return client


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        # Dates alone mean a custom range; before 1.3.2 they were dropped for the current month.
        (
            {"start_date": "2026-09-01", "end_date": "2026-09-30"},
            {"period": "custom", "start_date": "2026-09-01", "end_date": "2026-09-30"},
        ),
        (
            {"period": "custom", "start_date": "2026-09-01", "end_date": "2026-09-30"},
            {"period": "custom", "start_date": "2026-09-01", "end_date": "2026-09-30"},
        ),
        ({}, {"period": "month", "start_date": None, "end_date": None}),
        ({"period": "prev_month"}, {"period": "prev_month", "start_date": None, "end_date": None}),
    ],
)
def test_requested_period_reaches_okk(arguments, expected):
    client = _client()
    _call("get_call_statistics", arguments, client)
    sent = client.get.await_args.kwargs
    assert {key: sent[key] for key in expected} == expected


def test_phone_history_defaults_to_the_whole_journal():
    client = _client()
    _call("list_call_phone_records", {"phone_number": "79001234567"}, client)
    assert client.get.await_args.kwargs["period"] == "all"


@pytest.mark.parametrize(
    "arguments",
    [
        {"period": "month", "start_date": "2026-09-01", "end_date": "2026-09-30"},
        {"start_date": "2026-09-01"},
        {"period": "custom"},
        {"start_date": "2026-09-30", "end_date": "2026-09-01"},
        {"start_date": "01.09.2026", "end_date": "30.09.2026"},
    ],
)
def test_ambiguous_or_invalid_period_is_an_error_not_another_period(arguments):
    client = _client()
    with pytest.raises(ToolError):
        _call("get_call_statistics", arguments, client)
    client.get.assert_not_awaited()


def test_result_text_is_the_compact_copy_of_the_structured_envelope():
    result = _call("get_call_statistics", {}, _client())
    text = result.content[0].text
    assert json.loads(text) == result.structuredContent
    assert "\n" not in text and "Отдел" in text


def test_okk_outage_is_a_tool_error_not_an_empty_result():
    with pytest.raises(ToolError, match="temporarily unavailable"):
        _call("get_call_statistics", {}, _client(BackendUnavailable("down")))


def test_transcript_segments_chunk_stops_at_the_character_budget():
    segments = [{"text": "x" * 400} for _ in range(10)]
    assert len(_segment_chunk(segments, 0, 500, 1000)) == 2
    assert len(_segment_chunk(segments, 8, 500, 1000)) == 2
    # One oversized segment still comes back, so reading always moves forward.
    assert len(_segment_chunk([{"text": "y" * 5000}], 0, 500, 1000)) == 1
