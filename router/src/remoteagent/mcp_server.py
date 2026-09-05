from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field, SkipValidation, TypeAdapter, ValidationError

from .agents import AgentNotFoundError
from .companions import (
    CompanionCapacityError,
    CompanionConflictError,
    CompanionNotFoundError,
    CompanionPolicyError,
)
from .jobs import ConversationConflictError
from .schemas import (
    CONVERSATION_KEY_RE,
    AgentDefinition,
    AgentId,
    AgentSummary,
    AgentView,
    ArtifactView,
    BaseContext,
    CompanionBinding,
    CompanionStageId,
    CompanionStageView,
    ConversationKey,
    ConversationArchived,
    ConversationCompanionView,
    ConversationDeleted,
    CronConversationMode,
    CronExecutionId,
    CronExpression,
    CronLeaseId,
    CronResponseAcknowledgement,
    CronResponseLease,
    CronResponseLeaseRequest,
    CronResponseLimit,
    CronScheduleConfiguration,
    CronScheduleDeleteResult,
    CronScheduleView,
    CronTimezone,
    GitImportRequest,
    JobView,
    IdempotencyKey,
    ModelId,
    PromptAccepted,
    PromptRequest,
    PromptText,
    ReasoningEffort,
    RevisionUpdate,
    ScheduleId,
)
from .security import (
    authorize_mcp_prompt_companions,
    authorize_mcp_resource,
    authorize_mcp_tool,
)


def _semantic_tool_error(code: str, message: str, *, retryable: bool = False) -> ValueError:
    payload = json.dumps(
        {"code": code, "retryable": retryable, "message": message},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return ValueError(f"REMOTEAGENT_TOOL_ERROR:{payload}")


_COMPANION_TOOL_ERRORS = (
    CompanionNotFoundError,
    CompanionConflictError,
    CompanionPolicyError,
    CompanionCapacityError,
)
_COMPANION_STAGE_ID_ADAPTER = TypeAdapter(CompanionStageId)


def _companion_tool_error(exc: Exception) -> ValueError:
    if isinstance(exc, CompanionNotFoundError):
        return _semantic_tool_error("companion_not_found", str(exc))
    if isinstance(exc, CompanionConflictError):
        return _semantic_tool_error("companion_conflict", str(exc))
    if isinstance(exc, CompanionCapacityError):
        return _semantic_tool_error(
            "companion_capacity_exhausted", str(exc), retryable=True
        )
    if isinstance(exc, CompanionPolicyError):
        code = "companion_limit_exceeded" if exc.status_code == 413 else "companion_invalid"
        return _semantic_tool_error(code, str(exc))
    raise TypeError(f"unsupported companion error: {type(exc).__name__}")


class RoleAwareFastMCP(FastMCP):
    """Apply resource authorization to protocol discovery as well as reads."""

    async def list_resources(self) -> Any:
        authorize_mcp_resource(self.get_context())
        return await super().list_resources()

    async def list_resource_templates(self) -> Any:
        authorize_mcp_resource(self.get_context())
        return await super().list_resource_templates()


def build_mcp(container: Any) -> FastMCP:
    mcp = RoleAwareFastMCP(
        "RemoteAgent Router",
        instructions=(
            "Discover agents, submit asynchronous prompts, poll jobs, and read "
            "artifacts. Stage public HTTPS Git repositories as persistent conversation "
            "companions, then bind returned stage IDs through submit_prompt. Upload file "
            "and archive bytes through authenticated REST because MCP has a 4 MiB transport "
            "ceiling. Configure cron schedules and lease their successful responses. "
            "Preserve the returned conversation_key for continuations. "
            "Optional model and reasoning_effort values select a new conversation; "
            "omit them on continuation or repeat the conversation's stored values."
        ),
        streamable_http_path=container.settings.mcp_mount_path,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=(container.settings.mcp_dns_rebinding_protection),
            allowed_hosts=container.settings.mcp_allowed_hosts,
            allowed_origins=container.settings.mcp_allowed_origins,
        ),
    )

    @mcp.tool(name="list_agents")
    async def list_agents(ctx: Context, include_disabled: bool = False) -> list[AgentSummary]:
        """Discover registered agents and their current configuration revisions."""

        authorize_mcp_tool(ctx, "list_agents")
        return await container.agent_service.list(include_disabled=include_disabled)

    @mcp.tool(name="get_agent")
    async def get_agent(ctx: Context, agent_id: AgentId) -> AgentView:
        """Get one agent, including its effective TOML and base context."""

        authorize_mcp_tool(ctx, "get_agent")
        return await container.agent_service.get(agent_id)

    @mcp.tool(name="register_agent")
    async def register_agent(
        ctx: Context, definition: AgentDefinition, replace: bool = False
    ) -> AgentView:
        """Register a project-local Compose agent after path/service validation."""

        authorize_mcp_tool(ctx, "register_agent")
        return await container.agent_service.register(definition, replace=replace)

    @mcp.tool(name="update_agent_configuration")
    async def update_agent_configuration(
        ctx: Context,
        agent_id: AgentId,
        config_toml: str | None = None,
        base_context: BaseContext | None = None,
    ) -> AgentView:
        """Append an immutable effective configuration/context revision."""

        authorize_mcp_tool(ctx, "update_agent_configuration")
        return await container.agent_service.update_revision(
            agent_id,
            RevisionUpdate(config_toml=config_toml, base_context=base_context),
        )

    @mcp.tool(name="submit_prompt")
    async def submit_prompt(
        ctx: Context,
        agent_id: AgentId,
        prompt: PromptText,
        conversation_key: ConversationKey | None = None,
        idempotency_key: IdempotencyKey | None = None,
        companions: SkipValidation[
            Annotated[
                tuple[CompanionBinding, ...],
                Field(json_schema_extra={"maxItems": 20}),
            ]
        ] = (),
        model: ModelId | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> PromptAccepted:
        """Queue a turn, optionally binding ready companions; model settings start a thread."""

        authorize_mcp_tool(ctx, "submit_prompt")
        authorize_mcp_prompt_companions(ctx, companions)
        if isinstance(companions, (list, tuple)) and len(companions) > 20:
            raise _semantic_tool_error(
                "companion_limit_exceeded",
                "too many companion additions for one turn",
            )
        try:
            request = PromptRequest(
                agent_id=agent_id,
                prompt=prompt,
                conversation_key=conversation_key,
                idempotency_key=idempotency_key,
                companions=companions,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            return await container.job_service.submit(request)
        except ValidationError as exc:
            raise _semantic_tool_error(
                "companion_invalid", "invalid companion binding"
            ) from exc
        except AgentNotFoundError as exc:
            raise _semantic_tool_error("agent_unavailable", "agent not found or disabled") from exc
        except ConversationConflictError as exc:
            raise _semantic_tool_error("conversation_conflict", str(exc)) from exc
        except _COMPANION_TOOL_ERRORS as exc:
            raise _companion_tool_error(exc) from exc

    @mcp.tool(name="stage_git_repository")
    async def stage_git_repository(
        ctx: Context,
        url: SkipValidation[Annotated[str, Field(min_length=1, max_length=2_048)]],
        ref: SkipValidation[
            Annotated[str | None, Field(min_length=1, max_length=1_024)]
        ] = None,
    ) -> CompanionStageView:
        """Queue a full-history import of a credential-free public HTTPS Git repository."""

        authorize_mcp_tool(ctx, "stage_git_repository")
        try:
            return await container.companion_service.queue_git_import(
                GitImportRequest(url=url, ref=ref)
            )
        except ValidationError as exc:
            raise _semantic_tool_error(
                "companion_invalid", "invalid Git repository import request"
            ) from exc
        except _COMPANION_TOOL_ERRORS as exc:
            raise _companion_tool_error(exc) from exc

    @mcp.tool(name="get_companion_stage")
    async def get_companion_stage(
        ctx: Context, stage_id: SkipValidation[CompanionStageId]
    ) -> CompanionStageView:
        """Poll a companion stage until it is ready, failed, claimed, or expired."""

        authorize_mcp_tool(ctx, "get_companion_stage")
        try:
            validated_stage_id = _COMPANION_STAGE_ID_ADAPTER.validate_python(stage_id)
            return await container.companion_service.get_stage(validated_stage_id)
        except ValidationError as exc:
            raise _semantic_tool_error(
                "companion_invalid", "invalid companion stage ID"
            ) from exc
        except _COMPANION_TOOL_ERRORS as exc:
            raise _companion_tool_error(exc) from exc

    @mcp.tool(name="list_conversation_companions")
    async def list_conversation_companions(
        ctx: Context,
        conversation_key: SkipValidation[ConversationKey],
        include_history: bool = False,
    ) -> list[ConversationCompanionView]:
        """List active and pending companions, optionally including superseded versions."""

        authorize_mcp_tool(ctx, "list_conversation_companions")
        try:
            if not isinstance(conversation_key, str) or not CONVERSATION_KEY_RE.fullmatch(
                conversation_key
            ):
                raise _semantic_tool_error(
                    "companion_invalid", "invalid conversation key"
                )
            return await container.companion_service.list_conversation(
                conversation_key, include_history=include_history
            )
        except _COMPANION_TOOL_ERRORS as exc:
            raise _companion_tool_error(exc) from exc

    @mcp.tool(name="get_prompt_status")
    async def get_prompt_status(ctx: Context, job_id: str) -> JobView:
        """Poll a queued/running/completed prompt without holding a connection."""

        authorize_mcp_tool(ctx, "get_prompt_status")
        return await container.job_service.get(job_id)

    @mcp.tool(name="cancel_prompt")
    async def cancel_prompt(ctx: Context, job_id: str) -> JobView:
        """Cancel a queued prompt or request cancellation of a running one."""

        authorize_mcp_tool(ctx, "cancel_prompt")
        return await container.job_service.cancel(job_id)

    @mcp.tool(name="list_artifacts")
    async def list_artifacts(
        ctx: Context, job_id: str | None = None, conversation_key: str | None = None
    ) -> list[ArtifactView]:
        """List immutable artifacts and their artifact:// resource URIs."""

        authorize_mcp_tool(ctx, "list_artifacts")
        return await container.artifact_service.list(
            job_id=job_id, conversation_key=conversation_key
        )

    @mcp.tool(name="archive_conversation")
    async def archive_conversation(
        ctx: Context, conversation_key: ConversationKey
    ) -> ConversationArchived:
        """Archive a conversation after all of its jobs are terminal."""

        authorize_mcp_tool(ctx, "archive_conversation")
        await container.job_service.archive_conversation(conversation_key)
        return ConversationArchived(conversation_key=conversation_key)

    @mcp.tool(name="delete_conversation")
    async def delete_conversation(
        ctx: Context, conversation_key: ConversationKey
    ) -> ConversationDeleted:
        """Delete an inactive conversation and its isolated runtime data."""

        authorize_mcp_tool(ctx, "delete_conversation")
        await container.job_service.delete_conversation(
            conversation_key,
            artifact_cleanup=container.artifact_service.delete_storage,
        )
        return ConversationDeleted(conversation_key=conversation_key)

    @mcp.tool(name="configure_cron_schedule")
    async def configure_cron_schedule(
        ctx: Context,
        schedule_id: ScheduleId,
        cron_expression: CronExpression,
        agent_id: AgentId,
        prompt: PromptText,
        timezone: CronTimezone = "UTC",
        model: ModelId | None = None,
        reasoning_effort: ReasoningEffort | None = None,
        conversation_mode: CronConversationMode = CronConversationMode.FRESH,
    ) -> CronScheduleView:
        """Create or atomically replace a cron schedule for an enabled agent."""

        authorize_mcp_tool(ctx, "configure_cron_schedule")
        configuration = CronScheduleConfiguration(
            cron_expression=cron_expression,
            timezone=timezone,
            agent_id=agent_id,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            conversation_mode=conversation_mode,
        )
        agent = await container.agent_service.get(configuration.agent_id)
        if not agent.enabled:
            raise ValueError("agent is disabled")
        return await container.cron_service.configure_schedule(schedule_id, configuration)

    @mcp.tool(name="list_cron_schedules")
    async def list_cron_schedules(
        ctx: Context, include_disabled: bool = False
    ) -> list[CronScheduleView]:
        """List configured cron schedules and their current execution state."""

        authorize_mcp_tool(ctx, "list_cron_schedules")
        return await container.cron_service.list_schedules(include_disabled=include_disabled)

    @mcp.tool(name="get_cron_schedule")
    async def get_cron_schedule(ctx: Context, schedule_id: ScheduleId) -> CronScheduleView:
        """Get one cron schedule, including timing and response queue state."""

        authorize_mcp_tool(ctx, "get_cron_schedule")
        return await container.cron_service.get_schedule(schedule_id)

    @mcp.tool(name="set_cron_schedule_enabled")
    async def set_cron_schedule_enabled(
        ctx: Context, schedule_id: ScheduleId, enabled: bool
    ) -> CronScheduleView:
        """Pause or resume future occurrences without cancelling an active run."""

        authorize_mcp_tool(ctx, "set_cron_schedule_enabled")
        return await container.cron_service.set_schedule_enabled(schedule_id, enabled)

    @mcp.tool(name="delete_cron_schedule")
    async def delete_cron_schedule(
        ctx: Context, schedule_id: ScheduleId
    ) -> CronScheduleDeleteResult:
        """Delete a schedule now or mark it deleting until its active run ends."""

        authorize_mcp_tool(ctx, "delete_cron_schedule")
        return await container.cron_service.delete_schedule(schedule_id)

    @mcp.tool(name="retrieve_cron_responses")
    async def retrieve_cron_responses(
        ctx: Context,
        schedule_id: ScheduleId | None = None,
        execution_id: CronExecutionId | None = None,
        limit: CronResponseLimit = 50,
    ) -> CronResponseLease:
        """Lease successful responses; exactly one schedule or execution ID is required."""

        authorize_mcp_tool(ctx, "retrieve_cron_responses")
        request = CronResponseLeaseRequest(
            schedule_id=schedule_id,
            execution_id=execution_id,
            limit=limit,
        )
        return await container.cron_service.lease_responses(request)

    @mcp.tool(name="acknowledge_cron_responses")
    async def acknowledge_cron_responses(
        ctx: Context, lease_id: CronLeaseId
    ) -> CronResponseAcknowledgement:
        """Delete the responses in a valid lease; repeated acknowledgement is safe."""

        authorize_mcp_tool(ctx, "acknowledge_cron_responses")
        return await container.cron_service.acknowledge_responses(lease_id)

    @mcp.resource(
        "agent://{agent_id}/configuration",
        name="agent_configuration",
        mime_type="application/json",
    )
    async def agent_configuration(agent_id: str, ctx: Context) -> str:
        authorize_mcp_resource(ctx)
        value = await container.agent_service.get(agent_id)
        return json.dumps(value.model_dump(mode="json"), ensure_ascii=False)

    @mcp.resource(
        "artifact://{artifact_id}",
        name="agent_artifact",
        mime_type="application/octet-stream",
    )
    async def agent_artifact(artifact_id: str, ctx: Context) -> bytes:
        authorize_mcp_resource(ctx)
        path, metadata = await container.artifact_service.path(artifact_id)
        if metadata.size_bytes > 16 * 1024 * 1024:
            raise ValueError(
                "artifact exceeds the 16 MiB MCP resource limit; use authenticated "
                f"HTTP GET /api/v1/artifacts/{artifact_id}/content with Range"
            )
        return await asyncio.to_thread(path.read_bytes)

    return mcp
