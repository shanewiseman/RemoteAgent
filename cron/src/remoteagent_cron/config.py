from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Cron service settings. Environment variables use ``REMOTEAGENT_CRON_``."""

    model_config = SettingsConfigDict(
        env_prefix="REMOTEAGENT_CRON_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    version: str = "local"
    host: str = "0.0.0.0"
    port: int = Field(default=8090, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: str = "sqlite+aiosqlite:///./var/cron.db"

    internal_bearer_token: SecretStr = SecretStr("change-me-cron-api")
    internal_bearer_token_file: Path | None = None
    router_mcp_url: str = "http://router:8080/mcp"
    router_mcp_token: SecretStr = SecretStr("change-me-cron-mcp")
    router_mcp_token_file: Path | None = None
    router_request_timeout_seconds: float = Field(default=30, gt=0)

    tick_seconds: float = Field(default=1, gt=0)
    job_poll_seconds: float = Field(default=5, gt=0)
    run_timeout_seconds: int = Field(default=86_400, ge=1)
    retry_initial_seconds: float = Field(default=0.5, gt=0)
    retry_max_seconds: float = Field(default=30, gt=0)

    lease_seconds: int = Field(default=300, ge=1)
    response_retention_seconds: int = Field(default=90 * 24 * 3600, ge=60)
    response_batch_limit: int = Field(default=50, ge=1, le=1_000)
    response_batch_bytes: int = Field(default=4 * 1024 * 1024, ge=1_024)
    ack_tombstone_seconds: int = Field(default=24 * 3600, ge=60)
    execution_retention_seconds: int = Field(default=30 * 24 * 3600, ge=60)
    cleanup_interval_seconds: int = Field(default=3600, ge=10)

    scheduler_enabled: bool = True
    initialize_schema: bool = False

    @field_validator("internal_bearer_token_file", "router_mcp_token_file", mode="before")
    @classmethod
    def expand_paths(cls, value: Any) -> Any:
        if value is None or isinstance(value, Path):
            return value
        return Path(os.path.expandvars(os.path.expanduser(str(value))))

    @field_validator("router_mcp_url")
    @classmethod
    def valid_router_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("router_mcp_url must be an HTTP(S) URL")
        return value

    @model_validator(mode="after")
    def valid_retry_bounds(self) -> Settings:
        if self.retry_max_seconds < self.retry_initial_seconds:
            raise ValueError("retry_max_seconds cannot be less than retry_initial_seconds")
        return self

    @staticmethod
    def _read_secret(file: Path | None, inline: SecretStr, label: str) -> str:
        if file is not None:
            try:
                value = file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"cannot read {label} file: {exc}") from exc
        else:
            value = inline.get_secret_value().strip()
        if not value:
            raise ValueError(f"{label} is empty")
        return value

    def authorization_token(self) -> str:
        return self._read_secret(
            self.internal_bearer_token_file,
            self.internal_bearer_token,
            "internal bearer token",
        )

    def mcp_token(self) -> str:
        return self._read_secret(
            self.router_mcp_token_file,
            self.router_mcp_token,
            "router MCP token",
        )


def load_settings(**overrides: Any) -> Settings:
    aliases = {
        "DATABASE_URL": "database_url",
        "CRON_INTERNAL_BEARER_TOKEN": "internal_bearer_token",
        "CRON_INTERNAL_BEARER_TOKEN_FILE": "internal_bearer_token_file",
        "CRON_ROUTER_MCP_TOKEN": "router_mcp_token",
        "CRON_ROUTER_MCP_TOKEN_FILE": "router_mcp_token_file",
        "ROUTER_MCP_URL": "router_mcp_url",
    }
    alias_values = {
        field: os.environ[name] for name, field in aliases.items() if name in os.environ
    }
    return Settings(**alias_values, **overrides)
