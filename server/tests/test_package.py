"""Community plugin/repository packaging contracts."""

from __future__ import annotations

import json
from pathlib import Path

from okk_mcp.config import Settings

ROOT = Path(__file__).resolve().parents[2]


def test_plugin_and_marketplace_point_to_the_standalone_package():
    plugin = ROOT / "plugins/okk-analytics"
    manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    mcp = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "okk-analytics"
    assert manifest["version"] == "1.1.0"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert manifest["repository"].endswith("/okk-analytics-codex-plugin")
    assert manifest["license"] == "MIT"
    assert manifest["interface"]["privacyPolicyURL"].endswith("/PRIVACY.md")
    assert manifest["interface"]["termsOfServiceURL"].endswith("/TERMS.md")
    assert len(manifest["interface"]["defaultPrompt"]) == 3
    assert all(len(prompt) <= 128 for prompt in manifest["interface"]["defaultPrompt"])
    assert manifest["interface"]["defaultPrompt"][0] == ("Проверить подключение OKK и показать мой доступ.")
    assert manifest["interface"]["defaultPrompt"][1] == (
        "Покажи доступные мне отделы и краткую статистику по каждому."
    )
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/okk-analytics"
    assert marketplace["plugins"][0]["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert mcp["mcpServers"]["okk-analytics"] == {
        "type": "http",
        "url": "https://okk-mcp.akfixdev.ru/mcp",
        "oauth_resource": "https://okk-mcp.akfixdev.ru/mcp",
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


def test_published_connector_is_wired_for_production_not_test_stand():
    production_env = (ROOT / ".env.production.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "APP_ENV=production" in production_env
    assert "OKK_API_BASE_URL=https://okk-backend.akfixdev.ru/api/v1" in production_env
    assert "MCP_RESOURCE_URL=https://okk-mcp.akfixdev.ru/mcp" in production_env
    assert "REDIS_PASSWORD=REPLACE_WITH_REDIS_PASSWORD" in production_env
    assert "test-stand connector" in readme
    assert "ready for a test environment" not in readme
    settings = Settings(_env_file=ROOT / ".env.production.example")
    assert settings.app_env == "production"
    assert settings.api_base_url == "https://okk-backend.akfixdev.ru/api/v1"


def test_skill_forbids_credentials_writes_and_excluded_surfaces():
    skill = (ROOT / "plugins/okk-analytics/skills/okk-analytics/SKILL.md").read_text(encoding="utf-8").lower()
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
        "authenticated=true",
        "okk подключён",
        "browser redirect alone",
    ):
        assert required in skill


def test_submission_matrix_has_exactly_five_positive_and_three_negative_cases():
    cases = (ROOT / "docs/submission-test-cases.md").read_text(encoding="utf-8")
    positive, negative = cases.split("## Negative", maxsplit=1)
    assert positive.count("**Prompt:**") == 5
    assert negative.count("**Prompt:**") == 3
