from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Router settings. Environment variables use the ``REMOTEAGENT_`` prefix."""

    model_config = SettingsConfigDict(
        env_prefix="REMOTEAGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    version: str = "local"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)
    bearer_token: SecretStr = SecretStr("change-me")
    bearer_token_file: Path | None = None
    cron_mcp_bearer_token: SecretStr | None = None
    cron_mcp_bearer_token_file: Path | None = None
    cron_api_bearer_token: SecretStr | None = None
    cron_api_bearer_token_file: Path | None = None
    cron_api_url: str = "http://cron:8090"
    cron_api_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    auth_mode: Literal["chatgpt", "api"] = "chatgpt"

    repository_root: Path = Field(default_factory=lambda: Path.cwd())
    data_dir: Path = Path("./var")
    agents_root: Path = Path(".")
    phonebook_path: Path = Path("./phonebook.toml")
    skills_path: Path | None = None
    canonical_auth_file: Path | None = None

    database_url: str = "sqlite+aiosqlite:///./var/router.db"
    redis_url: str | None = None
    redis_prefix: str = "remoteagent"

    scheduler_enabled: bool = True
    scheduler_poll_seconds: float = Field(default=0.5, gt=0)
    scheduler_concurrency: int = Field(default=1, ge=1, le=128)
    subscription_lease_name: str = "codex-subscription"
    subscription_lease_ttl_seconds: int = Field(default=21_600, ge=30)
    subscription_lease_retry_seconds: float = Field(default=1.0, gt=0)
    job_timeout_seconds: int = Field(default=14_400, ge=1)

    compose_binary: str = "docker"
    compose_wait_timeout_seconds: int = Field(default=120, ge=1)
    compose_stop_timeout_seconds: int = Field(default=30, ge=1)
    dependency_warm_seconds: int = Field(default=900, ge=0)

    workspace_container_path: str = "/workspace"
    codex_home_container_path: str = "/home/agent/.codex"
    artifacts_container_path: str = "/workspace/artifacts"
    job_output_container_path: str = "/run/remoteagent"
    codex_sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"

    artifact_max_file_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    artifact_max_files_per_job: int = Field(default=1_000, ge=1)
    companion_max_object_bytes: int = Field(default=100 * 1024 * 1024, ge=1)
    companion_max_files_per_item: int = Field(default=20_000, ge=1)
    companion_max_per_turn: int = Field(default=20, ge=1, le=20)
    companion_max_active_names: int = Field(default=200, ge=1, le=10_000)
    companion_max_conversation_bytes: int = Field(default=1024 * 1024 * 1024, ge=1)
    companion_staging_max_bytes: int = Field(default=5 * 1024 * 1024 * 1024, ge=1)
    companion_stage_ttl_seconds: int = Field(default=24 * 3600, ge=60)
    companion_git_workers: int = Field(default=2, ge=1, le=32)
    companion_git_timeout_seconds: int = Field(default=300, ge=1)
    companion_cleanup_interval_seconds: int = Field(default=300, ge=10)
    artifact_retention_seconds: int = Field(default=30 * 24 * 3600, ge=60)
    job_retention_seconds: int = Field(default=90 * 24 * 3600, ge=60)
    conversation_retention_seconds: int = Field(default=90 * 24 * 3600, ge=60)
    retention_interval_seconds: int = Field(default=3600, ge=10)

    dashboard_enabled: bool = True
    dashboard_allow_http: bool = False
    dashboard_debug_enabled: bool = False
    dashboard_session_idle_seconds: int = Field(default=1_800, ge=60)
    dashboard_session_absolute_seconds: int = Field(default=28_800, ge=300)
    dashboard_allow_memory_sessions: bool = False
    dashboard_activity_channel: str = "activity"
    dashboard_sse_heartbeat_seconds: float = Field(default=15, gt=0)
    dashboard_sse_poll_seconds: float = Field(default=5, gt=0)
    dashboard_history_days: int = Field(default=30, ge=1, le=3_650)
    dashboard_history_default_turns: int = Field(default=100, ge=1, le=500)
    initialize_schema: bool = True
    mcp_mount_path: str = "/mcp"
    mcp_dns_rebinding_protection: bool = True
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "[::1]:*", "router:8080"]
    )
    mcp_allowed_origins: list[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:*",
            "http://localhost:*",
            "http://[::1]:*",
        ]
    )

    @field_validator(
        "repository_root",
        "data_dir",
        "agents_root",
        "phonebook_path",
        "skills_path",
        "canonical_auth_file",
        "bearer_token_file",
        "cron_mcp_bearer_token_file",
        "cron_api_bearer_token_file",
        mode="before",
    )
    @classmethod
    def expand_paths(cls, value: Any) -> Any:
        if value is None or isinstance(value, Path):
            return value
        return Path(os.path.expandvars(os.path.expanduser(str(value))))

    @field_validator("mcp_mount_path")
    @classmethod
    def valid_mount_path(cls, value: str) -> str:
        if not value.startswith("/") or value.endswith("/"):
            raise ValueError("mcp_mount_path must start with '/' and not end with '/'")
        return value

    @field_validator("cron_api_url")
    @classmethod
    def valid_cron_api_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("cron_api_url must be an absolute HTTP(S) URL")
        if parsed.query or parsed.fragment:
            raise ValueError("cron_api_url cannot contain a query or fragment")
        return value.rstrip("/")

    def resolved(self) -> Settings:
        root = self.repository_root.resolve()

        def rooted(path: Path | None) -> Path | None:
            if path is None:
                return None
            return (path if path.is_absolute() else root / path).resolve()

        return self.model_copy(
            update={
                "repository_root": root,
                "data_dir": rooted(self.data_dir),
                "agents_root": rooted(self.agents_root),
                "phonebook_path": rooted(self.phonebook_path),
                "skills_path": rooted(self.skills_path),
                "canonical_auth_file": rooted(self.canonical_auth_file),
                "bearer_token_file": rooted(self.bearer_token_file),
                "cron_mcp_bearer_token_file": rooted(self.cron_mcp_bearer_token_file),
                "cron_api_bearer_token_file": rooted(self.cron_api_bearer_token_file),
            }
        )

    def authorization_token(self) -> str:
        """Return the bearer token, preferring the Docker-secret file form."""

        value = self._service_token(
            self.bearer_token_file, self.bearer_token, label="bearer token", required=True
        )
        assert value is not None
        return value

    def cron_mcp_authorization_token(self) -> str | None:
        """Return the optional scoped token used by cron when calling router MCP."""

        return self._service_token(
            self.cron_mcp_bearer_token_file,
            self.cron_mcp_bearer_token,
            label="cron MCP bearer token",
            required=False,
        )

    def cron_api_authorization_token(self) -> str | None:
        """Return the optional token used by the router's private cron API client."""

        return self._service_token(
            self.cron_api_bearer_token_file,
            self.cron_api_bearer_token,
            label="cron API bearer token",
            required=False,
        )

    @staticmethod
    def _service_token(
        token_file: Path | None,
        token_value: SecretStr | None,
        *,
        label: str,
        required: bool,
    ) -> str | None:
        if token_file is not None:
            try:
                token = token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(f"cannot read {label} file: {exc}") from exc
            if not token:
                raise ValueError(f"{label} file is empty")
            return token
        if token_value is None:
            if required:
                raise ValueError(f"{label} is empty")
            return None
        token = token_value.get_secret_value().strip()
        if not token:
            raise ValueError(f"{label} is empty")
        return token

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.data_dir / "workspaces",
            self.data_dir / "codex-homes",
            self.data_dir / "artifacts",
            self.data_dir / "jobs",
            self.data_dir / "companion-staging",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)


def load_settings(path: Path | str | None = None, **overrides: Any) -> Settings:
    """Load optional ``[router]`` TOML defaults, then environment and overrides.

    Environment variables intentionally win over TOML. Explicit keyword overrides
    win over both and are primarily useful for tests and embedding.
    """

    config_path = Path(
        path or os.environ.get("REMOTEAGENT_CONFIG_FILE", "./remoteagent.toml")
    ).expanduser()
    toml_values: dict[str, Any] = {}
    if config_path.is_file():
        with config_path.open("rb") as handle:
            loaded = tomllib.load(handle)
        router_values = loaded.get("router", loaded)
        if not isinstance(router_values, dict):
            raise ValueError("router configuration must be a TOML table")
        toml_values = dict(router_values)

    # Deployment aliases are deliberately handled here instead of adding field
    # aliases that would suppress the normal REMOTEAGENT_* names in
    # pydantic-settings.
    aliases = {
        "REMOTEAGENT_REPO_ROOT": "repository_root",
        "REMOTEAGENT_STATE_ROOT": "data_dir",
        "DATABASE_URL": "database_url",
        "ROUTER_BEARER_TOKEN": "bearer_token",
        "ROUTER_BEARER_TOKEN_FILE": "bearer_token_file",
    }
    alias_values = {
        field: os.environ[environment]
        for environment, field in aliases.items()
        if environment in os.environ
    }
    env_settings = Settings()
    env_fields = {
        key.removeprefix("REMOTEAGENT_").lower()
        for key in os.environ
        if key.startswith("REMOTEAGENT_")
    }
    merged = {
        **toml_values,
        **alias_values,
        **{
            name: getattr(env_settings, name)
            for name in env_fields
            if name in Settings.model_fields
        },
        **overrides,
    }
    return Settings(**merged).resolved()
