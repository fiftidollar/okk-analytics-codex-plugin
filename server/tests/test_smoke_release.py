from __future__ import annotations

import pytest

from scripts.smoke_release import (
    DEPARTMENT_SCOPED_TOOLS,
    EXPECTED_TOOLS,
    SUPERVISOR_SCOPED_TOOLS,
    validate_connection_confirmation,
    validate_department_employee_grounding,
    validate_oauth_metadata,
    validate_tool_inventory,
)


def _tool(name: str) -> dict:
    properties = {}
    if name in DEPARTMENT_SCOPED_TOOLS:
        properties["department_ref"] = {"type": ["string", "null"]}
    if name == "compare_departments":
        properties["department_refs"] = {"type": ["array", "null"]}
    if name in SUPERVISOR_SCOPED_TOOLS:
        properties["supervisor_id"] = {"type": "string"}
    return {
        "name": name,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": properties},
        "_meta": {
            "securitySchemes": [
                {
                    "type": "oauth2",
                    "scopes": ["okk.transcripts.read"] if "transcript" in name else ["okk.statistics.read"],
                }
            ]
        },
    }


def test_release_smoke_requires_exact_safe_inventory():
    validate_tool_inventory({"result": {"tools": [_tool(name) for name in EXPECTED_TOOLS]}})
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": [_tool("unexpected_write")]}})
    unsafe = [_tool(name) for name in EXPECTED_TOOLS]
    unsafe[0]["annotations"]["destructiveHint"] = True
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": unsafe}})
    missing_named_filter = [_tool(name) for name in EXPECTED_TOOLS]
    next(tool for tool in missing_named_filter if tool["name"] == "get_department_statistics")["inputSchema"][
        "properties"
    ].pop("department_ref")
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": missing_named_filter}})
    missing_transcript_scope = [_tool(name) for name in EXPECTED_TOOLS]
    next(tool for tool in missing_transcript_scope if tool["name"] == "get_call_transcript")["_meta"][
        "securitySchemes"
    ][0]["scopes"] = ["okk.statistics.read"]
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": missing_transcript_scope}})
    missing_supervisor_id = [_tool(name) for name in EXPECTED_TOOLS]
    next(tool for tool in missing_supervisor_id if tool["name"] == "get_supervisor_call_statistics")[
        "inputSchema"
    ]["properties"].pop("supervisor_id")
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": missing_supervisor_id}})


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
            "authoritative_employee_roster": {
                "employee_count": 1,
                "items": [
                    {
                        "id": employee_id,
                        "full_name": "Сотрудник ОРД",
                        "department_id": department_id,
                    }
                ],
            },
            "employee_ranking": [
                {
                    "employee_id": employee_id,
                    "canonical_employee_id": employee_id,
                    "canonical_employee_name": "Сотрудник ОРД",
                }
            ],
            "complete_employee_ranking": {"employees": []},
            "department_and_employee_trends": {"employee_trends": []},
            "plan_fact": {"employees": []},
        },
    }
    payload = {"result": {"structuredContent": structured}}

    validate_department_employee_grounding(payload)

    foreign = {"result": {"structuredContent": {**structured, "data": dict(structured["data"])}}}
    foreign["result"]["structuredContent"]["data"]["employee_ranking"] = [
        {
            "employee_id": "foreign-employee",
            "canonical_employee_id": "foreign-employee",
            "canonical_employee_name": "Левый Сотрудник",
        }
    ]
    with pytest.raises(RuntimeError, match="outside its live roster"):
        validate_department_employee_grounding(foreign)

    wrong_name = {"result": {"structuredContent": {**structured, "data": dict(structured["data"])}}}
    wrong_name["result"]["structuredContent"]["data"]["employee_ranking"] = [
        {
            "employee_id": employee_id,
            "canonical_employee_id": employee_id,
            "canonical_employee_name": "Выдуманное Имя",
        }
    ]
    with pytest.raises(RuntimeError, match="ungrounded employee name"):
        validate_department_employee_grounding(wrong_name)
