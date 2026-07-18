"""Configuration for the standalone OKK Analytics MCP gateway."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "staging", "production"] = "development"
    database_url: str = "postgresql+asyncpg://okk_mcp:okk_mcp@localhost:5432/okk_mcp"
    redis_url: str = "redis://localhost:6379/0"
    okk_api_base_url: str = "http://localhost:8002/api/v1"
    mcp_oauth_secret: str = Field(
        default="dev-oauth-secret-change-me-32-characters",
        min_length=32,
    )
    mcp_session_encryption_secret: str = Field(
        default="dev-session-secret-change-me-32-chars",
        min_length=32,
    )
    mcp_issuer_url: str = "http://localhost:8020"
    mcp_resource_url: str = "http://localhost:8020/mcp"
    mcp_service_name: str = "OKK Analytics"
    mcp_access_token_minutes: int = Field(default=60, ge=5, le=1440)
    mcp_refresh_token_days: int = Field(default=30, ge=1, le=365)
    mcp_authorization_code_minutes: int = Field(default=5, ge=1, le=15)
    mcp_http_timeout_seconds: float = Field(default=30.0, ge=2, le=120)
    mcp_login_attempts_per_minute: int = Field(default=8, ge=2, le=30)
    mcp_cleanup_interval_minutes: int = Field(default=60, ge=5, le=1440)
    mcp_audit_retention_days: int = Field(default=30, ge=7, le=365)
    database_pool_size: int = Field(default=15, ge=2, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    analytics_parallel_requests: int = Field(default=12, ge=2, le=40)
    analytics_max_calls: int = Field(default=5000, ge=100, le=25000)
    transcript_search_max_calls: int = Field(default=1000, ge=25, le=5000)
    analytics_max_employees: int = Field(default=2000, ge=100, le=10000)
    analytics_trace_enabled: bool = True

    @model_validator(mode="after")
    def validate_deployment(self) -> Settings:
        issuer = self.mcp_issuer_url.rstrip("/")
        resource = self.mcp_resource_url.rstrip("/")
        api = self.okk_api_base_url.rstrip("/")
        for label, value in (
            ("MCP_ISSUER_URL", issuer),
            ("MCP_RESOURCE_URL", resource),
            ("OKK_API_BASE_URL", api),
        ):
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(f"{label} must be an absolute URL without credentials, query or fragment")
        if not resource.startswith(f"{issuer}/"):
            raise ValueError("MCP_RESOURCE_URL must be hosted below MCP_ISSUER_URL")
        if self.app_env == "production":
            if not issuer.startswith("https://") or not resource.startswith("https://"):
                raise ValueError("Production MCP issuer/resource URLs must use HTTPS")
            if not api.startswith("https://"):
                raise ValueError("Production OKK_API_BASE_URL must use HTTPS")
            if self.mcp_oauth_secret.startswith("dev-"):
                raise ValueError("Production MCP_OAUTH_SECRET must not use the development default")
            if self.mcp_session_encryption_secret.startswith("dev-"):
                raise ValueError(
                    "Production MCP_SESSION_ENCRYPTION_SECRET must not use the development default"
                )
            if self.mcp_oauth_secret == self.mcp_session_encryption_secret:
                raise ValueError("OAuth signing and session encryption secrets must be different")
            database = urlsplit(self.database_url)
            if (
                not database.username
                or not database.password
                or database.password in {"okk_mcp", "change-me"}
            ):
                raise ValueError("Production DATABASE_URL must use a non-default password")
            redis_url = urlsplit(self.redis_url)
            if redis_url.scheme not in {"redis", "rediss"} or not redis_url.password:
                raise ValueError("Production REDIS_URL must use authenticated Redis")
        return self

    @property
    def issuer_url(self) -> str:
        return self.mcp_issuer_url.rstrip("/")

    @property
    def resource_url(self) -> str:
        return self.mcp_resource_url.rstrip("/")

    @property
    def api_base_url(self) -> str:
        return self.okk_api_base_url.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
