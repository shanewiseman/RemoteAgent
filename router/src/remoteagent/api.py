from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import text

from .agents import AgentConflictError, AgentNotFoundError
from .artifacts import ArtifactNotFoundError, ArtifactPolicyError
from .companions import (
    CompanionCapacityError,
    CompanionConflictError,
    CompanionNotFoundError,
    CompanionPolicyError,
)
from .compose import ComposeValidationError
from .jobs import (
    ConversationConflictError,
    ConversationNotFoundError,
    JobNotFoundError,
)
from .schemas import (
    AgentRegistration,
    AgentSummary,
    AgentView,
    ArtifactView,
    CompanionStageId,
    CompanionStageView,
    ConversationKey,
    ConversationCompanionView,
    GitImportRequest,
    JobStatus,
    JobView,
    PromptAccepted,
    PromptRequest,
    RevisionUpdate,
)


_COMPANION_HTTP_ERRORS = (
    CompanionNotFoundError,
    CompanionConflictError,
    CompanionPolicyError,
    CompanionCapacityError,
)


def _companion_http_exception(exc: Exception) -> HTTPException:
    if isinstance(exc, CompanionNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, CompanionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, CompanionCapacityError):
        return HTTPException(status_code=507, detail=str(exc))
    if isinstance(exc, CompanionPolicyError):
        status_code = exc.status_code if exc.status_code in {413, 422} else 422
        return HTTPException(status_code=status_code, detail=str(exc))
    raise TypeError(f"unsupported companion error: {type(exc).__name__}")


def build_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health", include_in_schema=False)
    @router.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz", include_in_schema=False)
    async def ready(request: Request) -> dict[str, Any]:
        container = request.app.state.container
        try:
            async with container.session_factory() as session:
                await session.execute(text("SELECT 1"))
            cache_ready = await container.cache.ping()
        except Exception as exc:
            raise HTTPException(
                status_code=503, detail="router dependencies are unavailable"
            ) from exc
        try:
            cron_readiness = await container.cron_service.readiness()
            cron = dict(cron_readiness)
            if cron.get("status") != "ready":
                cron["status"] = "degraded"
        except Exception:
            cron = {"status": "degraded", "detail": "cron service is unavailable"}
        return {
            "status": "ready",
            "database": True,
            "cache": cache_ready,
            "scheduler": container.scheduler.running
            if container.settings.scheduler_enabled
            else False,
            "cron": cron,
        }

    @router.get(
        "/api/v1/agents",
        response_model=list[AgentSummary],
        tags=["Agents"],
        operation_id="agentsList",
    )
    async def list_agents(request: Request, include_disabled: bool = False) -> list[AgentSummary]:
        return await request.app.state.container.agent_service.list(
            include_disabled=include_disabled
        )

    @router.get(
        "/api/v1/agents/{agent_id}",
        response_model=AgentView,
        tags=["Agents"],
        operation_id="agentsGet",
    )
    async def get_agent(request: Request, agent_id: str) -> AgentView:
        try:
            return await request.app.state.container.agent_service.get(agent_id)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="agent not found") from exc

    @router.post(
        "/api/v1/agents",
        response_model=AgentView,
        status_code=201,
        tags=["Agents"],
        operation_id="agentsRegister",
    )
    async def register_agent(request: Request, body: AgentRegistration) -> AgentView:
        try:
            return await request.app.state.container.agent_service.register(
                body.definition, replace=body.replace
            )
        except AgentConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, ComposeValidationError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.patch(
        "/api/v1/agents/{agent_id}/configuration",
        response_model=AgentView,
        tags=["Agents"],
        operation_id="agentConfigurationUpdate",
    )
    async def update_agent(request: Request, agent_id: str, body: RevisionUpdate) -> AgentView:
        try:
            return await request.app.state.container.agent_service.update_revision(agent_id, body)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="agent not found") from exc

    @router.post(
        "/api/v1/jobs",
        response_model=PromptAccepted,
        status_code=202,
        tags=["Jobs"],
        operation_id="jobsSubmit",
    )
    async def submit_job(request: Request, body: PromptRequest) -> PromptAccepted:
        try:
            return await request.app.state.container.job_service.submit(body)
        except AgentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="agent not found or disabled") from exc
        except ConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except _COMPANION_HTTP_ERRORS as exc:
            raise _companion_http_exception(exc) from exc

    @router.post(
        "/api/v1/companion-stages/uploads",
        response_model=CompanionStageView,
        status_code=201,
        tags=["Companions"],
        operation_id="companionStagesUpload",
    )
    async def upload_companion_stage(
        request: Request,
        filename: str = Query(..., min_length=1, max_length=255),
        kind: Literal["file", "archive"] = Query(...),
        sha256: str | None = Query(
            default=None,
            min_length=64,
            max_length=64,
            pattern=r"^[0-9a-f]{64}$",
        ),
    ) -> CompanionStageView:
        """Stream an uploaded file or archive into a ready, single-use stage."""

        try:
            return await request.app.state.container.companion_service.stage_upload(
                request.stream(),
                filename=filename,
                kind=kind,
                expected_sha256=sha256,
            )
        except _COMPANION_HTTP_ERRORS as exc:
            raise _companion_http_exception(exc) from exc

    @router.post(
        "/api/v1/companion-stages/git-imports",
        response_model=CompanionStageView,
        status_code=202,
        tags=["Companions"],
        operation_id="companionStagesGitImport",
    )
    async def import_git_companion_stage(
        request: Request, body: GitImportRequest
    ) -> CompanionStageView:
        """Queue a credential-free public HTTPS Git repository import."""

        try:
            return await request.app.state.container.companion_service.queue_git_import(body)
        except _COMPANION_HTTP_ERRORS as exc:
            raise _companion_http_exception(exc) from exc

    @router.get(
        "/api/v1/companion-stages/{stage_id}",
        response_model=CompanionStageView,
        tags=["Companions"],
        operation_id="companionStagesGet",
    )
    async def get_companion_stage(
        request: Request, stage_id: CompanionStageId
    ) -> CompanionStageView:
        try:
            return await request.app.state.container.companion_service.get_stage(stage_id)
        except _COMPANION_HTTP_ERRORS as exc:
            raise _companion_http_exception(exc) from exc

    @router.get(
        "/api/v1/conversations/{conversation_key}/companions",
        response_model=list[ConversationCompanionView],
        tags=["Companions", "Conversations"],
        operation_id="conversationCompanionsList",
    )
    async def list_conversation_companions(
        request: Request,
        conversation_key: ConversationKey,
        include_history: bool = False,
    ) -> list[ConversationCompanionView]:
        try:
            return await request.app.state.container.companion_service.list_conversation(
                conversation_key, include_history=include_history
            )
        except _COMPANION_HTTP_ERRORS as exc:
            raise _companion_http_exception(exc) from exc

    @router.get(
        "/api/v1/jobs",
        response_model=list[JobView],
        tags=["Jobs"],
        operation_id="jobsList",
    )
    async def list_jobs(
        request: Request,
        conversation_key: str | None = None,
        status: JobStatus | None = None,
        limit: int = Query(100, ge=1, le=1_000),
    ) -> list[JobView]:
        return await request.app.state.container.job_service.list(
            conversation_key=conversation_key, status=status, limit=limit
        )

    @router.get(
        "/api/v1/jobs/{job_id}",
        response_model=JobView,
        tags=["Jobs"],
        operation_id="jobsGet",
    )
    async def get_job(request: Request, job_id: str) -> JobView:
        try:
            return await request.app.state.container.job_service.get(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @router.post(
        "/api/v1/jobs/{job_id}/cancel",
        response_model=JobView,
        tags=["Jobs"],
        operation_id="jobsCancel",
    )
    async def cancel_job(request: Request, job_id: str) -> JobView:
        try:
            return await request.app.state.container.job_service.cancel(job_id)
        except JobNotFoundError as exc:
            raise HTTPException(status_code=404, detail="job not found") from exc

    @router.get(
        "/api/v1/jobs/{job_id}/artifacts",
        response_model=list[ArtifactView],
        tags=["Artifacts"],
        operation_id="jobArtifactsList",
    )
    async def job_artifacts(request: Request, job_id: str) -> list[ArtifactView]:
        return await request.app.state.container.artifact_service.list(job_id=job_id)

    @router.post(
        "/api/v1/conversations/{conversation_key}/archive",
        status_code=204,
        tags=["Conversations"],
        operation_id="conversationArchive",
    )
    async def archive_conversation(request: Request, conversation_key: str) -> None:
        try:
            await request.app.state.container.job_service.archive_conversation(conversation_key)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete(
        "/api/v1/conversations/{conversation_key}",
        status_code=204,
        tags=["Conversations"],
        operation_id="conversationDelete",
    )
    async def delete_conversation(request: Request, conversation_key: str) -> None:
        container = request.app.state.container
        try:
            job_ids = await container.job_service.delete_conversation(conversation_key)
        except ConversationNotFoundError as exc:
            raise HTTPException(status_code=404, detail="conversation not found") from exc
        except ConversationConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        for job_id in job_ids:
            await container.artifact_service.delete_storage(job_id)

    @router.get(
        "/api/v1/artifacts",
        response_model=list[ArtifactView],
        tags=["Artifacts"],
        operation_id="artifactsList",
    )
    async def list_artifacts(
        request: Request,
        job_id: str | None = None,
        conversation_key: str | None = None,
    ) -> list[ArtifactView]:
        return await request.app.state.container.artifact_service.list(
            job_id=job_id, conversation_key=conversation_key
        )

    @router.get(
        "/api/v1/artifacts/{artifact_id}",
        response_model=ArtifactView,
        tags=["Artifacts"],
        operation_id="artifactsGet",
    )
    async def artifact_metadata(request: Request, artifact_id: str) -> ArtifactView:
        try:
            return await request.app.state.container.artifact_service.get(artifact_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc

    @router.get(
        "/api/v1/artifacts/{artifact_id}/content",
        response_class=FileResponse,
        tags=["Artifacts"],
        operation_id="artifactContentGet",
    )
    async def artifact_content(request: Request, artifact_id: str) -> FileResponse:
        try:
            path, metadata = await request.app.state.container.artifact_service.path(artifact_id)
        except ArtifactNotFoundError as exc:
            raise HTTPException(status_code=404, detail="artifact not found") from exc
        except ArtifactPolicyError as exc:
            raise HTTPException(status_code=410, detail=str(exc)) from exc
        return FileResponse(
            path,
            media_type=metadata.media_type,
            filename=metadata.relative_path.rsplit("/", 1)[-1],
            headers={
                "ETag": f'"{metadata.sha256}"',
                "Accept-Ranges": "bytes",
                "Cache-Control": "private, immutable",
            },
        )

    return router
