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
    mcp = json.loads((plugin / ".mcp.codex.json").read_text(encoding="utf-8"))
    portable = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    portable_mcp = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))
    apps = json.loads((plugin / ".app.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "okk-analytics"
    assert manifest["version"] == "1.2.2"
    assert "mcpServers" not in manifest
    assert manifest["apps"] == "./.app.json"
    assert portable["$schema"].endswith("/plugin.schema.json")
    assert portable["name"] == manifest["name"]
    assert portable["version"] == manifest["version"]
    assert portable["extensions"]["com.openai"]["apps"] == "./.app.json"
    assert portable["extensions"]["com.openai"]["interface"] == manifest["interface"]
    assert portable_mcp == {
        "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
        "mcpServers": {
            "okk-analytics": {
                "type": "streamable-http",
                "url": "https://okk-mcp.akfixdev.ru/mcp",
            }
        },
    }
    assert apps == {
        "apps": {
            "okk-analytics": {
                "id": "asdk_app_6a5b29fc5a3c8191ae74cde95f4fec9f",
                "category": "Business analytics",
            }
        }
    }
    assert manifest["repository"].endswith("/okk-analytics-codex-plugin")
    assert manifest["license"] == "MIT"
    assert manifest["interface"]["privacyPolicyURL"].endswith("/PRIVACY.md")
    assert manifest["interface"]["termsOfServiceURL"].endswith("/TERMS.md")
    assert len(manifest["interface"]["defaultPrompt"]) == 3
    for key in ("composerIcon", "logo", "logoDark"):
        asset = plugin / manifest["interface"][key]
        assert asset.is_file()
        assert asset.suffix == ".png"
        assert asset.stat().st_size <= 10_000
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


def test_claude_code_marketplace_reuses_the_shared_skill_and_standard_http_mcp():
    plugin = ROOT / "plugins/okk-analytics"
    manifest = json.loads((plugin / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    mcp = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    command = (plugin / "commands/check-connection.md").read_text(encoding="utf-8")

    assert marketplace["name"] == "alpes-community"
    assert marketplace["owner"] == {"name": "Alpes"}
    assert marketplace["plugins"][0]["name"] == "okk-analytics"
    assert marketplace["plugins"][0]["source"] == "./plugins/okk-analytics"
    assert marketplace["plugins"][0]["version"] == manifest["version"] == "1.2.2"
    assert manifest["name"] == "okk-analytics"
    assert manifest["skills"] == "./skills/"
    assert mcp == {
        "mcpServers": {
            "okk-analytics": {
                "type": "http",
                "url": "https://okk-mcp.akfixdev.ru/mcp",
            }
        }
    }
    assert "get_access_context" in command
    assert "data.authenticated=true" in command
    assert "/mcp" in command
    assert "Never ask for the OKK password" in " ".join(command.split())


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


def test_skill_forbids_credentials_writes_and_routes_people_and_transcripts_to_dedicated_tools():
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
        "okk.transcripts.read",
        "list_call_transcripts",
        "get_call_transcript",
        "search_call_transcripts",
        "list_supervisors",
        "get_supervisor_call_statistics",
        "list_supervisor_call_transcripts",
        "get_supervisor_call_transcript",
        "search_supervisor_call_transcripts",
        "transcription_only",
        "never hardcode",
    ):
        assert required in skill


def test_submission_matrix_has_exactly_five_positive_and_three_negative_cases():
    cases = (ROOT / "docs/submission-test-cases.md").read_text(encoding="utf-8")
    positive, negative = cases.split("## Negative", maxsplit=1)
    assert positive.count("**Prompt:**") == 5
    assert negative.count("**Prompt:**") == 3


def test_chatgpt_submission_bundle_covers_every_tool_and_exact_test_counts():
    submission = json.loads((ROOT / "chatgpt-app-submission.json").read_text(encoding="utf-8"))
    assert submission["schema_version"] == 1
    assert submission["app_info"]["display_name"] == "OKK Analytics"
    assert len(submission["app_info"]["subtitle"]) <= 30
    assert len(submission["tools"]) == 27
    assert len(submission["test_cases"]) == 5
    assert len(submission["negative_test_cases"]) == 3
    for name, tool in submission["tools"].items():
        assert name
        assert tool["annotations"] == {
            "readOnlyHint": True,
            "openWorldHint": False,
            "destructiveHint": False,
        }
        assert all(tool["justifications"].values())
