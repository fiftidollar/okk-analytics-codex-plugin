"""Community plugin/repository packaging contracts."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_and_marketplace_point_to_the_standalone_package():
    manifest = json.loads((ROOT / "plugin/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    mcp = json.loads((ROOT / "plugin/.mcp.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "okk-analytics"
    assert manifest["version"] == "1.0.0"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["repository"].endswith("/okk-analytics-codex-plugin")
    assert marketplace["plugins"][0]["source"]["path"] == "./plugin"
    assert marketplace["plugins"][0]["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_USE",
    }
    assert mcp["mcpServers"]["okk-analytics"] == {
        "type": "http",
        "url": "https://okk-mcp.akfixdev.ru/mcp",
    }


def test_standalone_server_has_no_private_backend_import_or_copy():
    python_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "server/okk_mcp").glob("*.py")
    )
    dockerfile = (ROOT / "server/Dockerfile").read_text(encoding="utf-8")
    requirements = (ROOT / "server/requirements.txt").read_text(encoding="utf-8")
    assert "from app." not in python_source
    assert "import app." not in python_source
    assert "COPY backend" not in dockerfile
    assert "mcp==1.27.2" in requirements


def test_skill_forbids_credentials_writes_and_excluded_surfaces():
    skill = (ROOT / "plugin/skills/okk-analytics/SKILL.md").read_text(encoding="utf-8").lower()
    for required in (
        "never ask",
        "password",
        "read-only",
        "not_available",
        "audio",
        "transcripts",
        "raw prompts",
        "raw ai",
        "scripts",
        "megafon",
        "pipeline",
        "routing",
        "write action",
    ):
        assert required in skill
