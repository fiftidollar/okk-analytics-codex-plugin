from __future__ import annotations

import pytest

from scripts.smoke_release import EXPECTED_TOOLS, validate_tool_inventory


def _tool(name: str) -> dict:
    return {
        "name": name,
        "annotations": {"readOnlyHint": True, "destructiveHint": False},
    }


def test_release_smoke_requires_exact_safe_inventory():
    validate_tool_inventory({"result": {"tools": [_tool(name) for name in EXPECTED_TOOLS]}})
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": [_tool("unexpected_write")]}})
    unsafe = [_tool(name) for name in EXPECTED_TOOLS]
    unsafe[0]["annotations"]["destructiveHint"] = True
    with pytest.raises(RuntimeError):
        validate_tool_inventory({"result": {"tools": unsafe}})
