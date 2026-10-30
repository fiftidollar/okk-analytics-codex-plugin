"""Вход через страницу OKK (фаза 5 мультитенантности OKK) — единственный путь входа шлюза.

Ловит: прод запускается с секретом шлюза для разработки (обмен кода падал бы у каждого человека) или
с HTTP-адресом страницы входа; страница шлюза просит пароль; заголовок безопасности не пускает
браузер на страницу OKK после формы (Chrome проверяет `form-action` и на переходе); учётка-сотрудник
OKK не входит в MCP.
"""

from __future__ import annotations

import pytest

from okk_mcp import oauth
from okk_mcp.config import Settings
from okk_mcp.platform_client import OKKAuthenticationError, OKKPlatformClient

SECRET = "gateway-secret-0123456789"
PRODUCTION = {
    "app_env": "production",
    "mcp_issuer_url": "https://mcp.example",
    "mcp_resource_url": "https://mcp.example/mcp",
    "okk_api_base_url": "https://okk.example/api/v1",
    "mcp_oauth_secret": "o" * 40,
    "mcp_session_encryption_secret": "s" * 40,
    "database_url": "postgresql+asyncpg://mcp:strong-password@db/mcp",
    "redis_url": "redis://:pw@redis:6379/0",
}


def test_production_needs_the_gateway_secret_issued_by_okk_and_an_https_login_page():
    with pytest.raises(ValueError, match="OKK_MCP_CLIENT_SECRET"):
        Settings(**PRODUCTION)
    with pytest.raises(ValueError, match="OKK_PUBLIC_API_URL"):
        Settings(**PRODUCTION, okk_mcp_client_secret=SECRET, okk_public_api_url="http://okk.example/api/v1")
    assert Settings(**PRODUCTION, okk_mcp_client_secret=SECRET).okk_mcp_client_secret == SECRET


def test_consent_page_asks_no_password_and_lets_the_browser_go_to_okk():
    settings = Settings(okk_mcp_client_secret=SECRET, okk_public_api_url="https://okk.example/api/v1")

    page = oauth._render_login(
        settings=settings,
        client_name="Codex",
        signed_request="signed",
        csrf_token="csrf",
        redirect_uri="http://127.0.0.1:40123/callback",
        scope="okk.read okk.transcripts.read",
    )
    body = page.body.decode()

    assert 'name="password"' not in body and 'name="email"' not in body
    assert "Продолжить вход в ОКК" in body and 'name="grant_transcripts"' in body
    assert (
        "https://okk.example"
        in page.headers["content-security-policy"].split("form-action", 1)[1].split(";")[0]
    )


@pytest.mark.parametrize(
    ("role", "accepted"), [("admin", True), ("viewer", True), ("employee", True), ("operator", False)]
)
def test_roles_of_okk_company_accounts_sign_in(role, accepted):
    user = {"id": "u-1", "email": "Anna@Example.com", "role": role, "is_active": True}

    if accepted:
        assert OKKPlatformClient._user_fields(user) == ("u-1", "anna@example.com", role, [])
    else:
        with pytest.raises(OKKAuthenticationError):
            OKKPlatformClient._user_fields(user)
