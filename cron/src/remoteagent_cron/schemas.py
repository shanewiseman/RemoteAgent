from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

ScheduleId = Annotated[
    str,
    Field(min_length=1, max_length=63, pattern=IDENTIFIER_RE.pattern),
]
ExecutionId = Annotated[str, Field(min_length=36, max_length=36, pattern=UUID_RE.pattern)]
LeaseId = Annotated[str, Field(min_length=36, max_length=36, pattern=UUID_RE.pattern)]
ModelId = Annotated[str, Field(min_length=1, max_length=128, pattern=MODEL_ID_RE.pattern)]


class ConversationMode(StrEnum):
    FRESH = "fresh"
    PERSISTENT = "persistent"


class ReasoningEffort(StrEnum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"
    ULTRA = "ultra"


class ConfigureScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron_expression: str = Field(min_length=9, max_length=255)
    timezone: str = Field(default="UTC", min_length=1, max_length=128)
    agent_id: ScheduleId
    prompt: str = Field(min_length=1, max_length=2_000_000)
    model: ModelId | None = None
    reasoning_effort: ReasoningEffort | None = None
    conversation_mode: ConversationMode = ConversationMode.FRESH


class SetScheduleEnabledRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class LastFailure(BaseModel):
    execution_id: str
    status: str
    error: str
    completed_at: datetime


class ScheduleView(BaseModel):
    schedule_id: str
    generation_id: str
    status: Literal["enabled", "disabled", "deleting"]
    enabled: bool
    cron_expression: str
    timezone: str
    agent_id: str
    prompt: str
    model: str | None
    reasoning_effort: ReasoningEffort | None
    conversation_mode: ConversationMode
    revision: int
    revision_id: str
    next_fire_at: datetime | None
    active_execution_id: str | None
    last_execution_id: str | None
    pending_response_count: int = Field(ge=0)
    skipped_occurrences: int = Field(ge=0)
    last_failure: LastFailure | None
    created_at: datetime
    updated_at: datetime


class DeleteScheduleResult(BaseModel):
    schedule_id: str
    status: Literal["deleted", "deleting"]


class LeaseResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule_id: ScheduleId | None = None
    execution_id: ExecutionId | None = None
    limit: int = Field(default=50, ge=1, le=1_000)

    @model_validator(mode="after")
    def exactly_one_identifier(self) -> LeaseResponsesRequest:
        if (self.schedule_id is None) == (self.execution_id is None):
            raise ValueError("exactly one of schedule_id or execution_id is required")
        return self


class UsageTotals(BaseModel):
    model_config = ConfigDict(extra="allow")
    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    reasoning_output_tokens: int = Field(default=0, ge=0)


class CronResponse(BaseModel):
    response_id: str
    schedule_id: str
    execution_id: str
    revision_id: str
    revision: int
    agent_id: str
    router_job_id: str
    conversation_key: str
    scheduled_for: datetime
    completed_at: datetime
    model: str | None
    reasoning_effort: ReasoningEffort | None
    usage: UsageTotals | None
    result: str


class ResponseLease(BaseModel):
    lease_id: str | None
    expires_at: datetime | None
    responses: list[CronResponse]
    more_available: bool


class AcknowledgeResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_id: LeaseId


class AcknowledgeResponsesResult(BaseModel):
    lease_id: str
    status: Literal["acknowledged", "already_acknowledged", "stale"]
    deleted_count: int = Field(ge=0)


class RouterAgent(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    enabled: bool


class RouterPromptAccepted(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str
    conversation_key: str
    status: str
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None


class RouterJobView(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    agent_id: str
    conversation_key: str
    status: str
    model: str | None = None
    reasoning_effort: ReasoningEffort | None = None
    result: str | None = None
    error: str | None = None
    usage: UsageTotals | None = None
    completed_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {"succeeded", "failed", "cancelled", "interrupted", "expired"}


class ReadinessView(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    status: Literal["ready", "not_ready"]
    database: bool
    schema_ready: bool = Field(alias="schema", serialization_alias="schema")
    router_mcp: bool
    scheduler: bool
    detail: str | None = None
