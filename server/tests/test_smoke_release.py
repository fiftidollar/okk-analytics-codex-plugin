from __future__ import annotations

import pytest

from scripts.smoke_release import DEPARTMENT_SCOPED_TOOLS, EXPECTED_TOOLS, validate_tool_inventory


def _tool(name: str) -> dict:
    properties = {}
    if name in DEPARTMENT_SCOPED_TOOLS:
        properties["department_ref"] = {"type": ["string", "null"]}
    if name == "compare_departments":
        properties["department_refs"] = {"type": ["array", "null"]}
    return {
        "name": name,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
        "inputSchema": {"type": "object", "properties": properties},
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
