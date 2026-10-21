from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from okk_mcp.config import Settings
from okk_mcp.server import create_mcp_server
from scripts.smoke_release import (
    validate_connection_confirmation,
    validate_department_employee_grounding,
    validate_oauth_metadata,
    validate_tool_inventory,
)


def _registered_tools() -> list[dict]:
    """The `tools/list` payload of the server this repository builds, as a client receives it."""
    tools = asyncio.run(create_mcp_server(Settings(), AsyncMock()).list_tools())
    return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]


def _tool(tools: list[dict], name: str) -> dict:
    return next(tool for tool in tools if tool["name"] == name)


def test_release_smoke_accepts_the_inventory_the_server_registers():
    # Before: the expected list missed `list_call_phone_records`, so the smoke failed on every
    # real 28-tool gateway while its test compared the list with itself.
    validate_tool_inventory({"result": {"tools": _registered_tools()}})


def _drop_phone_tool(tools: list[dict]) -> None:
    tools.remove(_tool(tools, "list_call_phone_records"))


def _add_unexpected_write(tools: list[dict]) -> None:
    tools.append({**_tool(tools, "get_access_context"), "name": "unexpected_write"})


def _make_destructive(tools: list[dict]) -> None:
    tools[0]["annotations"]["destructiveHint"] = True


def _drop_named_department_filter(tools: list[dict]) -> None:
    _tool(tools, "get_department_statistics")["inputSchema"]["properties"].pop("department")


def _drop_phone_department_filter(tools: list[dict]) -> None:
    _tool(tools, "list_call_phone_records")["inputSchema"]["properties"].pop("department")


def _drop_transcript_scope(tools: list[dict]) -> None:
    _tool(tools, "get_call_transcript")["_meta"]["securitySchemes"][0]["scopes"] = ["okk.statistics.read"]


def _drop_phone_scope(tools: list[dict]) -> None:
    _tool(tools, "list_call_phone_records")["_meta"]["securitySchemes"][0]["scopes"] = ["okk.statistics.read"]


def _drop_supervisor_id(tools: list[dict]) -> None:
    _tool(tools, "get_supervisor_call_statistics")["inputSchema"]["properties"].pop("supervisor_id")


@pytest.mark.parametrize(
    "drift",
    [
        _drop_phone_tool,
        _add_unexpected_write,
        _make_destructive,
        _drop_named_department_filter,
        _drop_phone_department_filter,
        _drop_transcript_scope,
        _drop_phone_scope,
        _drop_supervisor_id,
    ],
)
def test_release_smoke_rejects_drift_from_the_safe_inventory(drift: Callable[[list[dict]], None]):
    tools = _registered_tools()
    drift(tools)

    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": tools}})


def test_release_smoke_requires_a_definitive_connection_confirmation():
    payload = {
        "result": {
            "structuredContent": {
                "status": "ok",
                "data": {
                    "authenticated": True,
                    "connection_status": "connected",
                    "confirmation_message": "OKK подключён. Авторизация подтверждена.",
                    "role": "viewer",
                    "departments": [],
                    "available_sections": {"supervisors": {"available": False, "employee_count": 0}},
                },
            }
        }
    }
    validate_connection_confirmation(payload)

    for key, value in (
        ("authenticated", False),
        ("connection_status", "pending"),
        ("confirmation_message", "Вероятно подключён"),
        ("role", ""),
        ("departments", None),
    ):
        invalid = {
            "result": {
                "structuredContent": {
                    "status": "ok",
                    "data": {**payload["result"]["structuredContent"]["data"], key: value},
                }
            }
        }
        with pytest.raises(RuntimeError):
            validate_connection_confirmation(invalid)

    missing_section = {
        "result": {
            "structuredContent": {
                "status": "ok",
                "data": {
                    **payload["result"]["structuredContent"]["data"],
                    "available_sections": {},
                },
            }
        }
    }
    with pytest.raises(RuntimeError):
        validate_connection_confirmation(missing_section)


def test_release_smoke_requires_portable_identity_metadata():
    base_url = "https://okk-mcp.example"
    authorization_metadata = {
        "code_challenge_methods_supported": ["S256"],
        "userinfo_endpoint": f"{base_url}/userinfo",
        "scopes_supported": ["openid", "email", "okk.transcripts.read"],
    }
    resource_metadata = {
        "resource": f"{base_url}/mcp",
        "scopes_supported": ["openid", "email", "okk.transcripts.read"],
    }
    validate_oauth_metadata(
        authorization_metadata,
        resource_metadata,
        base_url=base_url,
    )

    for metadata, key, value in (
        (authorization_metadata, "userinfo_endpoint", "https://wrong.example/userinfo"),
        (resource_metadata, "resource", "https://wrong.example/mcp"),
        (authorization_metadata, "scopes_supported", ["email", "okk.transcripts.read"]),
        (resource_metadata, "scopes_supported", ["openid", "okk.transcripts.read"]),
    ):
        invalid_authorization = dict(authorization_metadata)
        invalid_resource = dict(resource_metadata)
        target = invalid_authorization if metadata is authorization_metadata else invalid_resource
        target[key] = value
        with pytest.raises(RuntimeError):
            validate_oauth_metadata(
                invalid_authorization,
                invalid_resource,
                base_url=base_url,
            )


def test_release_smoke_requires_department_names_to_match_live_roster():
    department_id = "department-1"
    employee_id = "employee-1"
    structured = {
        "status": "ok",
        "effective_scope": {
            "department_id": department_id,
            "department_code": "ord",
            "department_name": "ОРД",
        },
        "employee_roster_grounding": {
            "source": "live_okk_employee_directory",
            "authoritative": True,
            "department_id": department_id,
            "department_code": "ord",
            "department_name": "ОРД",
            "employee_count": 1,
            "employee_ids": [employee_id],
            "employee_names": ["Сотрудник ОРД"],
            "source_complete": True,
        },
        "data": {
            "complete_employee_ranking": {"employees": [{"id": employee_id, "full_name": "Сотрудник ОРД"}]},
            "plan_fact": {"employees": [{"employee_id": employee_id, "employee_name": "Сотрудник ОРД"}]},
        },
    }
    payload = {"result": {"structuredContent": structured}}

    validate_department_employee_grounding(payload)

    def ranking_of(*rows: dict) -> dict:
        data = {**structured["data"], "complete_employee_ranking": {"employees": list(rows)}}
        return {"result": {"structuredContent": {**structured, "data": data}}}

    with pytest.raises(RuntimeError, match="outside its live roster"):
        validate_department_employee_grounding(
            ranking_of({"id": "foreign-employee", "full_name": "Левый Сотрудник"})
        )
    with pytest.raises(RuntimeError, match="ungrounded employee name"):
        validate_department_employee_grounding(ranking_of({"id": employee_id, "full_name": "Выдуманное Имя"}))
