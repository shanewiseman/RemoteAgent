from __future__ import annotations

import hashlib
import json
import re
import tomllib
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .environment import RESERVED_AGENT_ENVIRONMENT

IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$")
CONVERSATION_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
CRON_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
COMPANION_STAGE_ID_RE = re.compile(r"^cs_[0-9a-f]{32}$")
COMPANION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

AgentId = Annotated[str, Field(json_schema_extra={"pattern": IDENTIFIER_RE.pattern})]
ScheduleId = Annotated[str, Field(pattern=IDENTIFIER_RE.pattern)]
ConversationKey = Annotated[str, Field(json_schema_extra={"pattern": CONVERSATION_KEY_RE.pattern})]
CronExecutionId = Annotated[str, Field(min_length=36, max_length=36, pattern=CRON_UUID_RE.pattern)]
CronLeaseId = Annotated[str, Field(min_length=36, max_length=36, pattern=CRON_UUID_RE.pattern)]
CronExpression = Annotated[str, Field(min_length=9, max_length=255)]
CronTimezone = Annotated[str, Field(min_length=1, max_length=128)]
CronResponseLimit = Annotated[int, Field(ge=1, le=1_000)]
PromptText = Annotated[str, Field(min_length=1, max_length=2_000_000)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=256)]
BaseContext = Annotated[str, Field(max_length=1_000_000)]
ModelId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=MODEL_ID_RE.pattern,
        description="OpenAI model identifier selected for a conversation.",
    ),
]
CompanionStageId = Annotated[
    str,
    Field(
        min_length=35,
        max_length=35,
        pattern=COMPANION_STAGE_ID_RE.pattern,
        description="Opaque identifier for a single-use companion stage.",
    ),
]
CompanionName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=128,
        pattern=COMPANION_NAME_RE.pattern,
        description="One safe path component used below /workspace/companions.",
    ),
]
Sha256Digest = Annotated[
    str,
    Field(min_length=64, max_length=64, pattern=SHA256_RE.pattern),
]


class ReasoningEffort(StrEnum):
    """Codex reasoning effort selected for a conversation."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class CronConversationMode(StrEnum):
    """Conversation continuity used by a cron schedule."""

    FRESH = "fresh"
    PERSISTENT = "persistent"


class CronScheduleStatus(StrEnum):
    """Lifecycle state reported by the cron service."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    DELETING = "deleting"


# Agent-managed config is consumed by the Codex parent process, not solely by
# commands inside its sandbox. Keep this V1 surface deliberately fail-closed:
# provider/auth endpoints, executable notifications/hooks, MCP/plugins/apps,
# profiles/permissions, writable-root grants, and host path readers are not
# safe for bearer-authorized runtime mutation.
SAFE_CODEX_CONFIG_KEYS = frozenset(
    {
        "approval_policy",
        "cli_auth_credentials_store",
        "compact_prompt",
        "developer_instructions",
        "disable_response_storage",
        "hide_agent_reasoning",
        "model",
        "model_auto_compact_token_limit",
        "model_context_window",
        "model_reasoning_effort",
        "model_reasoning_summary",
        "model_verbosity",
        "personality",
        "plan_mode_reasoning_effort",
        "review_model",
        "sandbox_mode",
        "service_tier",
        "show_raw_agent_reasoning",
        "tool_output_token_limit",
        "tool_output_token_max_bytes",
        "web_search",
    }
)


def validate_codex_config_toml(value: str) -> str:
    if not value:
        return value
    try:
        document = tomllib.loads(value)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid Codex config TOML: {exc}") from exc
    unsafe = set(document) - SAFE_CODEX_CONFIG_KEYS
    if unsafe:
        raise ValueError(f"unsupported or unsafe Codex config keys: {sorted(unsafe)}")
    nested = sorted(key for key, item in document.items() if isinstance(item, (dict, list)))
    if nested:
        raise ValueError(f"unsupported structured Codex config keys: {nested}")
    credentials_store = document.get("cli_auth_credentials_store")
    if credentials_store not in (None, "file"):
        raise ValueError("cli_auth_credentials_store must be 'file'")
    approval = document.get("approval_policy")
    if approval not in (None, "never"):
        raise ValueError("approval_policy must be 'never'")
    sandbox = document.get("sandbox_mode")
    if sandbox not in (
        None,
        "read-only",
        "workspace-write",
    ):
        raise ValueError("invalid sandbox_mode")
    _execution_profile_from_document(document)
    return value


def _execution_profile_from_document(
    document: dict[str, Any],
) -> tuple[str | None, ReasoningEffort | None]:
    model = document.get("model")
    if model is not None:
        if not isinstance(model, str) or not MODEL_ID_RE.fullmatch(model):
            raise ValueError(
                "model must be a lowercase identifier containing only letters, "
                "digits, dots, underscores, or hyphens (maximum 128 characters)"
            )

    raw_reasoning = document.get("model_reasoning_effort")
    if raw_reasoning is None:
        reasoning_effort = None
    else:
        if not isinstance(raw_reasoning, str):
            raise ValueError("model_reasoning_effort must be a string")
        try:
            reasoning_effort = ReasoningEffort(raw_reasoning)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ReasoningEffort)
            raise ValueError(f"model_reasoning_effort must be one of: {allowed}") from exc
    return model, reasoning_effort


def execution_profile_from_config(
    config_toml: str,
) -> tuple[str | None, ReasoningEffort | None]:
    """Return the explicit model settings in an already validated Codex config."""

    document = tomllib.loads(config_toml or "")
    return _execution_profile_from_document(document)


class JobStatus(StrEnum):
    QUEUED = "queued"
    PROVISIONING = "provisioning"
    WAITING_FOR_LEASE = "waiting_for_lease"
    RUNNING = "running"
    COLLECTING = "collecting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    EXPIRED = "expired"

    @property
    def terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
            self.INTERRUPTED,
            self.EXPIRED,
        }


class ConversationStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DELETED = "deleted"


class CompanionStageKind(StrEnum):
    FILE = "file"
    ARCHIVE = "archive"
    GIT = "git"


class CompanionStageStatus(StrEnum):
    QUEUED = "queued"
    IMPORTING = "importing"
    READY = "ready"
    FAILED = "failed"
    CLAIMED = "claimed"
    EXPIRED = "expired"


class ConversationCompanionStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    SUPERSEDED = "superseded"


class AgentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(json_schema_extra={"pattern": IDENTIFIER_RE.pattern})
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=4_096)
    compose_file: Path
    project_name: str | None = Field(
        default=None, json_schema_extra={"pattern": IDENTIFIER_RE.pattern}
    )
    runner_service: str = Field(default="agent", json_schema_extra={"pattern": SERVICE_RE.pattern})
    dependency_services: tuple[str, ...] = ()
    enabled: bool = True
    config_toml: str = ""
    base_context: str = Field(default="", max_length=1_000_000)
    environment: dict[str, str] = Field(default_factory=dict)
    labels: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("agent id must be a lowercase slug")
        return value

    @field_validator("runner_service")
    @classmethod
    def valid_runner_service(cls, value: str) -> str:
        if not SERVICE_RE.fullmatch(value):
            raise ValueError("invalid Compose service name")
        return value

    @field_validator("dependency_services")
    @classmethod
    def valid_dependencies(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("dependency_services must not contain duplicates")
        for value in values:
            if not SERVICE_RE.fullmatch(value):
                raise ValueError(f"invalid Compose service name: {value}")
        return values

    @field_validator("project_name")
    @classmethod
    def valid_project_name(cls, value: str | None) -> str | None:
        if value is not None and not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("project_name must be a lowercase Compose project slug")
        return value

    @field_validator("environment")
    @classmethod
    def safe_environment(cls, value: dict[str, str]) -> dict[str, str]:
        forbidden = set(value) & RESERVED_AGENT_ENVIRONMENT
        forbidden.update(name for name in value if name.startswith("DOCKER_"))
        if forbidden:
            raise ValueError(
                f"agent environment uses controller-reserved keys: {sorted(forbidden)}"
            )
        return value

    @field_validator("config_toml")
    @classmethod
    def valid_toml(cls, value: str) -> str:
        return validate_codex_config_toml(value)

    @model_validator(mode="after")
    def populate_project_name(self) -> AgentDefinition:
        expected_project = f"remoteagent-{self.id}"
        if self.project_name is None:
            self.project_name = expected_project
        elif self.project_name != expected_project:
            raise ValueError(f"project_name must be {expected_project!r}")
        if self.runner_service in self.dependency_services:
            raise ValueError("runner_service cannot also be a dependency service")
        return self

    def checksum(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class Phonebook(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agents: list[AgentDefinition] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_agents(self) -> Phonebook:
        ids = [agent.id for agent in self.agents]
        if len(set(ids)) != len(ids):
            raise ValueError("phonebook contains duplicate agent ids")
        return self


class AgentSummary(BaseModel):
    id: str
    name: str
    description: str
    enabled: bool
    revision: int


class AgentView(AgentSummary):
    compose_file: str
    project_name: str
    runner_service: str
    dependency_services: list[str]
    environment: dict[str, str]
    labels: dict[str, str]
    metadata: dict[str, Any]
    config_toml: str
    base_context: str


class RevisionUpdate(BaseModel):
    config_toml: str | None = None
    base_context: BaseContext | None = None

    @field_validator("config_toml")
    @classmethod
    def valid_toml(cls, value: str | None) -> str | None:
        return None if value is None else validate_codex_config_toml(value)


class CompanionBinding(BaseModel):
    """Bind one ready, single-use stage to a conversation under a stable name."""

    model_config = ConfigDict(extra="forbid")

    stage_id: CompanionStageId
    name: CompanionName


class GitImportRequest(BaseModel):
    """Request a public Git repository import.

    URL scheme, address, credential, and ref policy require network-aware checks
    and are therefore enforced by the staging service.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=2_048)
    ref: str | None = Field(default=None, min_length=1, max_length=1_024)


class CompanionStageView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: CompanionStageId
    kind: CompanionStageKind
    status: CompanionStageStatus
    source_metadata: dict[str, Any]
    size_bytes: int | None = Field(default=None, ge=0)
    file_count: int | None = Field(default=None, ge=0)
    sha256: Sha256Digest | None = None
    resolved_git_commit: str | None = Field(default=None, min_length=40, max_length=64)
    error: str | None = None
    expires_at: datetime
    claimed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ConversationCompanionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    stage_id: CompanionStageId
    conversation_key: ConversationKey
    introduced_job_id: str | None
    introducing_sequence: int = Field(ge=1)
    name: CompanionName
    version: int = Field(ge=1)
    kind: CompanionStageKind
    status: ConversationCompanionStatus
    path: str
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)
    sha256: Sha256Digest
    resolved_git_commit: str | None = Field(default=None, min_length=40, max_length=64)
    activated_at: datetime | None = None
    superseded_at: datetime | None = None
    last_activation_error: str | None = None
    created_at: datetime
    updated_at: datetime


class PromptRequest(BaseModel):
    agent_id: ScheduleId
    prompt: PromptText
    conversation_key: ConversationKey | None = None
    idempotency_key: IdempotencyKey | None = None
    companions: list[CompanionBinding] = Field(default_factory=list, max_length=20)
    model: ModelId | None = Field(
        default=None,
        description=(
            "Optional model for a new conversation. On continuation, an explicit value "
            "must equal the conversation's stored value."
        ),
    )
    reasoning_effort: ReasoningEffort | None = Field(
        default=None,
        description=(
            "Optional reasoning effort for a new conversation. On continuation, an explicit "
            "value must equal the conversation's stored value."
        ),
    )

    @field_validator("agent_id")
    @classmethod
    def valid_agent_id(cls, value: str) -> str:
        if not IDENTIFIER_RE.fullmatch(value):
            raise ValueError("invalid agent id")
        return value

    @field_validator("conversation_key")
    @classmethod
    def valid_conversation_key(cls, value: str | None) -> str | None:
        if value is not None and not CONVERSATION_KEY_RE.fullmatch(value):
            raise ValueError("invalid conversation key")
        return value

    @field_validator("companions")
    @classmethod
    def unique_companions(cls, value: list[CompanionBinding]) -> list[CompanionBinding]:
        stage_ids = [binding.stage_id for binding in value]
        if len(stage_ids) != len(set(stage_ids)):
            raise ValueError("companions must not contain duplicate stage ids")
        names = [binding.name for binding in value]
        if len(names) != len(set(names)):
            raise ValueError("companions must not contain duplicate names")
        return value


class AgentRegistration(BaseModel):
    definition: AgentDefinition
    replace: bool = False


class PromptAccepted(BaseModel):
    job_id: str
    conversation_key: str
    status: JobStatus
    model: ModelId | None = Field(
        description="Stored conversation model, or null when model selection is inherited."
    )
    reasoning_effort: ReasoningEffort | None = Field(
        description=(
            "Stored conversation reasoning effort, or null when effort selection is inherited."
        )
    )
    companion_additions: list[ConversationCompanionView] = Field(default_factory=list)


class ConversationArchived(BaseModel):
    conversation_key: str
    status: Literal["archived"] = "archived"


class ConversationDeleted(BaseModel):
    conversation_key: str
    deleted: Literal[True] = True


class UsageTotals(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_subtotals(self) -> UsageTotals:
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached input tokens exceed total input tokens")
        if self.reasoning_output_tokens > self.output_tokens:
            raise ValueError("reasoning tokens exceed total output tokens")
        return self


class CronScheduleConfiguration(BaseModel):
    """Configuration forwarded to the private cron service API."""

    model_config = ConfigDict(extra="forbid")

    cron_expression: CronExpression
    timezone: CronTimezone = "UTC"
    agent_id: AgentId
    prompt: PromptText
    model: ModelId | None = None
    reasoning_effort: ReasoningEffort | None = None
    conversation_mode: CronConversationMode = CronConversationMode.FRESH


class CronScheduleEnabledUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class CronLastFailure(BaseModel):
    execution_id: str
    status: str
    error: str
    completed_at: datetime


class CronScheduleView(BaseModel):
    schedule_id: str
    generation_id: str
    status: CronScheduleStatus
    enabled: bool
    cron_expression: str
    timezone: str
    agent_id: str
    prompt: str
    model: ModelId | None
    reasoning_effort: ReasoningEffort | None
    conversation_mode: CronConversationMode
    revision: int = Field(ge=1)
    revision_id: str
    next_fire_at: datetime | None
    active_execution_id: str | None
    last_execution_id: str | None
    pending_response_count: int = Field(ge=0)
    skipped_occurrences: int = Field(ge=0)
    last_failure: CronLastFailure | None
    created_at: datetime
    updated_at: datetime


class CronScheduleDeleteResult(BaseModel):
    schedule_id: str
    status: Literal["deleted", "deleting"]


class CronUsageTotals(UsageTotals):
    """Usage returned by cron, preserving forward-compatible integer counters."""

    model_config = ConfigDict(extra="allow")


class CronResponse(BaseModel):
    response_id: str
    schedule_id: str
    execution_id: str
    revision_id: str
    revision: int = Field(ge=1)
    agent_id: str
    router_job_id: str
    conversation_key: str
    scheduled_for: datetime
    completed_at: datetime
    model: ModelId | None
    reasoning_effort: ReasoningEffort | None
    usage: CronUsageTotals | None
    result: str


class CronResponseLeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: ScheduleId | None = None
    execution_id: CronExecutionId | None = None
    limit: CronResponseLimit = 50

    @model_validator(mode="after")
    def exactly_one_lookup_id(self) -> CronResponseLeaseRequest:
        if (self.schedule_id is None) == (self.execution_id is None):
            raise ValueError("exactly one of schedule_id or execution_id is required")
        return self


class CronResponseLease(BaseModel):
    lease_id: str | None
    expires_at: datetime | None
    responses: list[CronResponse]
    more_available: bool


class CronResponseAcknowledgement(BaseModel):
    lease_id: str
    status: Literal["acknowledged", "already_acknowledged", "stale"]
    deleted_count: int = Field(ge=0)


class CronServiceReadiness(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["ready", "not_ready"]
    database: bool
    schema_current: bool = Field(alias="schema")
    router_mcp: bool
    scheduler: bool
    detail: str | None = None


class JobView(BaseModel):
    id: str
    agent_id: str
    conversation_key: str
    sequence: int
    status: JobStatus
    model: ModelId | None = Field(
        description="Model selection snapshotted for this job, or null when inherited."
    )
    reasoning_effort: ReasoningEffort | None = Field(
        description="Reasoning effort snapshotted for this job, or null when inherited."
    )
    result: str | None = None
    error: str | None = None
    usage: UsageTotals | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    companion_additions: list[ConversationCompanionView] = Field(default_factory=list)


class ArtifactView(BaseModel):
    id: str
    job_id: str
    conversation_key: str
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str
    resource_uri: str


def validate_artifact_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("artifact path must be relative and cannot traverse parents")
    return str(path)
