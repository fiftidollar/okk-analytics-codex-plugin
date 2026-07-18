"""Non-mutating OAuth/MCP release smoke for a deployed OKK Analytics gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

EXPECTED_TOOLS = [
    "get_access_context",
    "get_statistics_catalog",
    "get_overview_statistics",
    "list_departments",
    "get_department_statistics",
    "compare_departments",
    "list_employees",
    "get_employee_card",
    "compare_employees",
    "get_call_statistics",
    "get_plan_fact_statistics",
    "get_client_statistics",
    "get_crm_statistics",
    "get_growth_insights",
    "get_mentoring_statistics",
    "list_scenarios",
    "get_scenario_criteria",
    "get_scenario_performance",
    "get_criterion_performance",
]
DEPARTMENT_SCOPED_TOOLS = {
    "get_overview_statistics",
    "get_department_statistics",
    "list_employees",
    "get_employee_card",
    "compare_employees",
    "get_call_statistics",
    "get_plan_fact_statistics",
    "get_client_statistics",
    "get_crm_statistics",
    "get_growth_insights",
    "get_mentoring_statistics",
    "list_scenarios",
    "get_scenario_criteria",
    "get_scenario_performance",
    "get_criterion_performance",
}


def _rpc(method: str, params: dict[str, Any], request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def validate_tool_inventory(payload: dict[str, Any]) -> None:
    tools = payload.get("result", {}).get("tools", [])
    names = [tool.get("name") for tool in tools]
    if names != EXPECTED_TOOLS:
        raise RuntimeError(f"Unexpected MCP tool inventory: {names}")
    for tool in tools:
        annotations = tool.get("annotations") or {}
        if annotations.get("readOnlyHint") is not True:
            raise RuntimeError(f"Tool is not read-only: {tool.get('name')}")
        if annotations.get("destructiveHint") is not False:
            raise RuntimeError(f"Tool is destructive: {tool.get('name')}")
        properties = (tool.get("inputSchema") or {}).get("properties") or {}
        if tool.get("name") in DEPARTMENT_SCOPED_TOOLS and "department_ref" not in properties:
            raise RuntimeError(f"Tool cannot resolve a named department: {tool.get('name')}")
        if tool.get("name") == "compare_departments" and "department_refs" not in properties:
            raise RuntimeError("compare_departments cannot resolve named departments")


def validate_connection_confirmation(payload: dict[str, Any]) -> None:
    structured = payload.get("result", {}).get("structuredContent") or {}
    data = structured.get("data") or {}
    if structured.get("status") != "ok":
        raise RuntimeError("Authenticated access-context call did not succeed")
    if data.get("authenticated") is not True or data.get("connection_status") != "connected":
        raise RuntimeError("Access context did not confirm an authenticated OKK connection")
    if data.get("confirmation_message") != "OKK подключён. Авторизация подтверждена.":
        raise RuntimeError("Access context did not return the canonical connection confirmation")
    if not isinstance(data.get("role"), str) or not data["role"]:
        raise RuntimeError("Access context did not return the connected account role")
    if not isinstance(data.get("departments"), list):
        raise RuntimeError("Access context did not return the visible department list")


async def run(base_url: str, token: str | None) -> dict[str, Any]:
    base = base_url.rstrip("/")
    mcp_url = f"{base}/mcp"
    headers = {"Accept": "application/json, text/event-stream"}
    report: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        health, auth_meta, resource_meta = await asyncio.gather(
            client.get(f"{base}/health"),
            client.get(f"{base}/.well-known/oauth-authorization-server"),
            client.get(f"{base}/.well-known/oauth-protected-resource/mcp"),
        )
        for name, response in (
            ("health", health),
            ("authorization_metadata", auth_meta),
            ("resource_metadata", resource_meta),
        ):
            response.raise_for_status()
            report[name] = response.json()
        if report["authorization_metadata"].get("code_challenge_methods_supported") != ["S256"]:
            raise RuntimeError("OAuth metadata does not require PKCE S256")
        if report["resource_metadata"].get("resource") != mcp_url:
            raise RuntimeError("Protected resource metadata points to another MCP URL")

        initialize = _rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "okk-release-smoke", "version": "1"},
            },
            1,
        )
        challenge = await client.post(mcp_url, headers=headers, json=initialize)
        if challenge.status_code != 401 or "resource_metadata=" not in challenge.headers.get(
            "www-authenticate", ""
        ):
            raise RuntimeError("Unauthenticated MCP request did not return the OAuth resource challenge")
        report["unauthenticated_challenge"] = "ok"

        if token:
            authenticated = {**headers, "Authorization": f"Bearer {token}"}
            initialized = await client.post(mcp_url, headers=authenticated, json=initialize)
            initialized.raise_for_status()
            report["initialize"] = initialized.json()
            listed = await client.post(mcp_url, headers=authenticated, json=_rpc("tools/list", {}, 2))
            listed.raise_for_status()
            tools_payload = listed.json()
            validate_tool_inventory(tools_payload)
            report["tools"] = EXPECTED_TOOLS
            access = await client.post(
                mcp_url,
                headers=authenticated,
                json=_rpc("tools/call", {"name": "get_access_context", "arguments": {}}, 3),
            )
            access.raise_for_status()
            access_payload = access.json()
            validate_connection_confirmation(access_payload)
            report["access_context_call"] = access_payload
            inaccessible = await client.post(
                mcp_url,
                headers=authenticated,
                json=_rpc(
                    "tools/call",
                    {
                        "name": "get_department_statistics",
                        "arguments": {
                            "department_ref": "__mcp_acl_smoke_inaccessible_department__",
                            "period": "today",
                        },
                    },
                    4,
                ),
            )
            inaccessible.raise_for_status()
            inaccessible_payload = inaccessible.json()
            structured = inaccessible_payload.get("result", {}).get("structuredContent") or {}
            if structured.get("status") != "not_available":
                raise RuntimeError("Unknown named department did not fail closed")
            if structured.get("data") != {"reason": "department_not_in_access_scope"}:
                raise RuntimeError("Unknown named department returned business data")
            report["named_department_fail_closed"] = inaccessible_payload
        else:
            report["authenticated_checks"] = "skipped: set OKK_MCP_SMOKE_ACCESS_TOKEN"
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.getenv("OKK_MCP_SMOKE_URL"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.url:
        parser.error("--url or OKK_MCP_SMOKE_URL is required")
    report = asyncio.run(run(args.url, os.getenv("OKK_MCP_SMOKE_ACCESS_TOKEN")))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
