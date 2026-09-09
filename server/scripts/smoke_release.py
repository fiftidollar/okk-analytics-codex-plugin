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
    "list_supervisors",
    "get_supervisor_call_statistics",
    "get_employee_card",
    "compare_employees",
    "get_call_statistics",
    "list_call_transcripts",
    "get_call_transcript",
    "search_call_transcripts",
    "list_supervisor_call_transcripts",
    "get_supervisor_call_transcript",
    "search_supervisor_call_transcripts",
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
    "list_call_transcripts",
    "get_call_transcript",
    "search_call_transcripts",
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
SUPERVISOR_SCOPED_TOOLS = {
    "get_supervisor_call_statistics",
    "list_supervisor_call_transcripts",
    "get_supervisor_call_transcript",
    "search_supervisor_call_transcripts",
}
IDENTITY_SCOPES = {"openid", "email"}


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
        if tool.get("name") in SUPERVISOR_SCOPED_TOOLS and "supervisor_id" not in properties:
            raise RuntimeError(f"Tool does not require a supervisor identity: {tool.get('name')}")
        if "transcript" in str(tool.get("name")):
            schemes = (tool.get("_meta") or tool.get("meta") or {}).get("securitySchemes") or []
            scopes = set(schemes[0].get("scopes") or []) if schemes else set()
            if "okk.transcripts.read" not in scopes:
                raise RuntimeError(f"Transcript tool has no transcript scope: {tool.get('name')}")


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
    supervisors = (data.get("available_sections") or {}).get("supervisors") or {}
    if not isinstance(supervisors.get("available"), bool) or not isinstance(
        supervisors.get("employee_count"), int
    ):
        raise RuntimeError("Access context did not return the private-supervisor section state")


def validate_department_employee_grounding(payload: dict[str, Any]) -> None:
    structured = payload.get("result", {}).get("structuredContent") or {}
    if structured.get("status") not in {"ok", "partial"}:
        raise RuntimeError("Department report did not return an inspectable result")
    scope = structured.get("effective_scope") or {}
    grounding = structured.get("employee_roster_grounding") or {}
    if grounding.get("source") != "live_okk_employee_directory":
        raise RuntimeError("Department report has no live employee-directory grounding")
    if grounding.get("authoritative") is not True:
        raise RuntimeError("Department employee roster is not authoritative")
    if not isinstance(grounding.get("source_complete"), bool):
        raise RuntimeError("Department employee roster has no completeness marker")
    for field in ("department_id", "department_code", "department_name"):
        if grounding.get(field) != scope.get(field):
            raise RuntimeError(f"Employee roster {field} does not match effective scope")

    employee_ids = grounding.get("employee_ids")
    employee_names = grounding.get("employee_names")
    if not isinstance(employee_ids, list) or not isinstance(employee_names, list):
        raise RuntimeError("Department employee roster has no typed ID/name allowlist")
    if grounding.get("employee_count") != len(employee_ids) or len(employee_ids) != len(employee_names):
        raise RuntimeError("Department employee roster counts do not match")
    if len(set(employee_ids)) != len(employee_ids):
        raise RuntimeError("Department employee roster contains duplicate IDs")
    roster_names = dict(zip(employee_ids, employee_names, strict=True))

    data = structured.get("data") or {}
    authoritative = data.get("authoritative_employee_roster") or {}
    items = authoritative.get("items") or []
    if authoritative.get("employee_count") != len(items):
        raise RuntimeError("Authoritative department roster payload has an invalid count")
    for item in items:
        employee_id = str(item.get("id") or "")
        if roster_names.get(employee_id) != item.get("full_name"):
            raise RuntimeError("Authoritative department roster item conflicts with grounding")
        if str(item.get("department_id") or "") != str(scope.get("department_id") or ""):
            raise RuntimeError("Authoritative department roster contains another department")

    employee_sources = [
        data.get("employee_ranking") or [],
        (data.get("complete_employee_ranking") or {}).get("employees") or [],
        (data.get("department_and_employee_trends") or {}).get("employee_trends") or [],
        (data.get("plan_fact") or {}).get("employees") or [],
    ]
    for rows in employee_sources:
        for row in rows:
            employee_id = str(
                row.get("canonical_employee_id") or row.get("employee_id") or row.get("id") or ""
            )
            if employee_id not in roster_names:
                raise RuntimeError("Department report contains an employee outside its live roster")
            if row.get("canonical_employee_name") != roster_names[employee_id]:
                raise RuntimeError("Department report contains an ungrounded employee name")


def validate_department_report_semantics(payload: dict[str, Any]) -> None:
    structured = payload.get("result", {}).get("structuredContent") or {}
    data = structured.get("data") or {}
    definitions = (data.get("reporting_contract") or {}).get("metric_definitions") or {}
    for key in (
        "summary.calls_total",
        "summary.total_calls",
        "summary.calls_evaluated",
        "summary.avg_duration",
        "summary.average_score",
        "plan_fact.totals",
    ):
        if not isinstance(definitions.get(key), str) or not definitions[key]:
            raise RuntimeError(f"Department report has no metric definition: {key}")
    plan = data.get("plan_fact") or {}
    rows = plan.get("employees") or []
    expected = (structured.get("employee_roster_grounding") or {}).get("employee_count", 0)
    for key in ("plan_total", "plan_outbound", "plan_inbound", "plan_outbound_new", "plan_outbound_regular"):
        if key not in (plan.get("totals") or {}):
            raise RuntimeError("Plan totals omit an availability value")
        values = [row[key] for row in rows if row.get(key) is not None]
        if plan["totals"][key] != (sum(values) if values else None):
            raise RuntimeError("Plan totals replace unset values or disagree with source rows")
        coverage = (plan.get("coverage") or {}).get(key) or {}
        if coverage.get("employees_with_plan") != len(values) or coverage.get(
            "employees_without_plan"
        ) != expected - len(values):
            raise RuntimeError("Plan coverage disagrees with the live roster")


def validate_oauth_metadata(
    authorization_metadata: dict[str, Any],
    resource_metadata: dict[str, Any],
    *,
    base_url: str,
) -> None:
    if authorization_metadata.get("code_challenge_methods_supported") != ["S256"]:
        raise RuntimeError("OAuth metadata does not require PKCE S256")
    if authorization_metadata.get("userinfo_endpoint") != f"{base_url}/userinfo":
        raise RuntimeError("OAuth metadata does not advertise the canonical UserInfo endpoint")
    if resource_metadata.get("resource") != f"{base_url}/mcp":
        raise RuntimeError("Protected resource metadata points to another MCP URL")
    for metadata in (authorization_metadata, resource_metadata):
        scopes = set(metadata.get("scopes_supported", []))
        if not IDENTITY_SCOPES.issubset(scopes):
            raise RuntimeError("OAuth metadata does not advertise openid and email scopes")
        if "okk.transcripts.read" not in scopes:
            raise RuntimeError("OAuth metadata does not advertise the transcript read scope")


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
        validate_oauth_metadata(
            report["authorization_metadata"],
            report["resource_metadata"],
            base_url=base,
        )

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
            userinfo = await client.get(f"{base}/userinfo", headers=authenticated)
            userinfo.raise_for_status()
            userinfo_payload = userinfo.json()
            if (
                not isinstance(userinfo_payload.get("sub"), str)
                or not isinstance(userinfo_payload.get("email"), str)
                or userinfo_payload.get("email_verified") is not True
            ):
                raise RuntimeError("OAuth UserInfo did not return verified account identity")
            report["userinfo"] = "ok"
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
            departments = (
                access_payload.get("result", {})
                .get("structuredContent", {})
                .get("data", {})
                .get("departments", [])
            )
            if departments:
                first_department = departments[0]
                department_ref = (
                    first_department.get("code") or first_department.get("name") or first_department.get("id")
                )
                department_report = await client.post(
                    mcp_url,
                    headers=authenticated,
                    json=_rpc(
                        "tools/call",
                        {
                            "name": "get_department_statistics",
                            "arguments": {"department_ref": department_ref, "period": "month"},
                        },
                        5,
                    ),
                )
                department_report.raise_for_status()
                validate_department_employee_grounding(department_report.json())
                validate_department_report_semantics(department_report.json())
                report["department_employee_grounding"] = "ok"
                report["department_report_semantics"] = "ok"
            else:
                report["department_employee_grounding"] = "skipped: account has no departments"
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
