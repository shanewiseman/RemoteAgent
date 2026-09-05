from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .agents import AgentService
from .api import build_api_router
from .artifacts import ArtifactService
from .cache import Cache, MemoryCache, create_cache
from .compose import ComposeProjectValidator
from .companions import CompanionService
from .config import Settings, load_settings
from .contracts import API_VERSION, configure_openapi
from .cron_client import CronServiceClient
from .db import create_engine, create_session_factory, initialize_schema
from .jobs import JobService
from .lease import LeaseManager
from .mcp_server import build_mcp
from .phonebook import load_phonebook_partial
from .retention import RetentionWorker
from .runtime import AgentRuntime, DockerComposeRuntime
from .scheduler import Scheduler
from .security import BearerAuthMiddleware
from .telemetry import DashboardMetrics, HTTPMetricsMiddleware
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: Any
    session_factory: Any
    cache: Cache
    agent_service: AgentService
    job_service: JobService
    companion_service: CompanionService
    artifact_service: ArtifactService
    cron_service: CronServiceClient
    telemetry: DashboardMetrics
    scheduler: Scheduler
    retention: RetentionWorker
    runtime: AgentRuntime
    workspaces: WorkspaceManager
    phonebook_errors: dict[str, str]


def create_app(
    settings: Settings | None = None,
    *,
    runtime: AgentRuntime | None = None,
    validate_compose: bool | None = None,
) -> FastAPI:
    settings = (settings or load_settings()).resolved()
    settings.ensure_directories()
    router_token = settings.authorization_token()
    cron_mcp_token = settings.cron_mcp_authorization_token()
    cron_api_token = settings.cron_api_authorization_token()
    if settings.environment == "production" and router_token == "change-me":
        raise ValueError("the default bearer token cannot be used in production")
    if settings.environment == "production" and (cron_mcp_token is None or cron_api_token is None):
        raise ValueError("cron MCP and API bearer tokens are required in production")
    configured_tokens = [
        token for token in (router_token, cron_mcp_token, cron_api_token) if token is not None
    ]
    if len(configured_tokens) != len(set(configured_tokens)):
        raise ValueError("router and cron bearer tokens must be independent")

    app = FastAPI(title="RemoteAgent Router", version=API_VERSION)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path == "/api/v1/jobs" and any(
            error.get("type") == "too_long"
            and tuple(error.get("loc", ())) == ("body", "companions")
            and error.get("ctx", {}).get("max_length") == 20
            for error in exc.errors()
        ):
            return JSONResponse(
                status_code=413,
                content={"detail": "too many companion additions for one turn"},
            )
        return await request_validation_exception_handler(request, exc)

    if settings.dashboard_enabled:
        from .dashboard import register_dashboard

        register_dashboard(app)
        telemetry = app.state.dashboard_metrics
    else:
        telemetry = DashboardMetrics()
        app.state.dashboard_metrics = telemetry
        app.add_middleware(HTTPMetricsMiddleware, metrics=telemetry)

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    workspaces = WorkspaceManager(settings.data_dir)
    selected_runtime = runtime or DockerComposeRuntime(settings)
    if validate_compose is None:
        validate_compose = isinstance(selected_runtime, DockerComposeRuntime)
    compose_validator = (
        ComposeProjectValidator(
            settings.compose_binary,
            settings.data_dir,
            wait_timeout_seconds=settings.compose_wait_timeout_seconds,
        )
        if validate_compose
        else None
    )
    cache: Cache = MemoryCache(settings.redis_prefix)
    agent_service = AgentService(
        session_factory, settings.agents_root, compose_validator=compose_validator
    )
    companion_service = CompanionService(
        session_factory,
        settings.data_dir / "companion-staging",
        workspaces.root,
        max_upload_bytes=settings.companion_max_object_bytes,
        max_archive_bytes=settings.companion_max_object_bytes,
        max_git_mirror_bytes=settings.companion_max_object_bytes,
        max_git_checkout_bytes=settings.companion_max_object_bytes,
        max_files=settings.companion_max_files_per_item,
        max_additions_per_turn=settings.companion_max_per_turn,
        max_active_names=settings.companion_max_active_names,
        max_conversation_bytes=settings.companion_max_conversation_bytes,
        max_staging_bytes=settings.companion_staging_max_bytes,
        stage_ttl=timedelta(seconds=settings.companion_stage_ttl_seconds),
        git_workers=settings.companion_git_workers,
        git_timeout_seconds=settings.companion_git_timeout_seconds,
        cleanup_interval_seconds=settings.companion_cleanup_interval_seconds,
        telemetry=telemetry,
    )
    job_service = JobService(
        session_factory,
        workspaces,
        cache,
        activity_channel=settings.dashboard_activity_channel,
        companion_service=companion_service,
    )
    artifact_service = ArtifactService(
        session_factory,
        settings.data_dir / "artifact-store",
        max_file_bytes=settings.artifact_max_file_bytes,
        max_files_per_job=settings.artifact_max_files_per_job,
    )
    cron_service = CronServiceClient(
        settings.cron_api_url,
        cron_api_token,
        timeout_seconds=settings.cron_api_timeout_seconds,
    )
    lease_manager = LeaseManager(
        session_factory,
        ttl_seconds=settings.subscription_lease_ttl_seconds,
        retry_seconds=settings.subscription_lease_retry_seconds,
        cleanup_timeout_seconds=settings.job_cleanup_timeout_seconds,
    )
    scheduler = Scheduler(
        settings,
        agent_service=agent_service,
        job_service=job_service,
        artifact_service=artifact_service,
        workspaces=workspaces,
        runtime=selected_runtime,
        lease_manager=lease_manager,
        telemetry=telemetry,
        companion_service=companion_service,
    )
    retention = RetentionWorker(settings, session_factory, artifact_service)
    container = Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        cache=cache,
        agent_service=agent_service,
        job_service=job_service,
        companion_service=companion_service,
        artifact_service=artifact_service,
        cron_service=cron_service,
        telemetry=telemetry,
        scheduler=scheduler,
        retention=retention,
        runtime=selected_runtime,
        workspaces=workspaces,
        phonebook_errors={},
    )
    app.state.container = container
    app.include_router(build_api_router())
    configure_openapi(app)
    mcp = build_mcp(container)
    mcp_app = mcp.streamable_http_app()
    app.mount("/", mcp_app, name="mcp")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if settings.initialize_schema:
                await initialize_schema(engine)
            definitions, phonebook_errors = load_phonebook_partial(
                settings.phonebook_path, settings.agents_root
            )
            container.phonebook_errors = phonebook_errors
            if phonebook_errors:
                logger.warning("ignored %d invalid phonebook entries", len(phonebook_errors))
            sync_errors = await agent_service.synchronize(definitions)
            container.phonebook_errors.update(sync_errors)
            if sync_errors:
                logger.warning("ignored %d agents with invalid Compose projects", len(sync_errors))
            recovered_jobs = await job_service.recover_interrupted()
            await selected_runtime.recover(recovered_jobs)
            # Reconcile mutable companion working trees only after any stale
            # agent runtimes have been stopped and recovered.
            await companion_service.start()
            actual_cache = await create_cache(settings.redis_url, settings.redis_prefix)
            old_cache = container.cache
            container.cache = actual_cache
            job_service.cache = actual_cache
            if old_cache is not actual_cache:
                await old_cache.close()
            if settings.scheduler_enabled:
                await scheduler.start()
            await retention.start()
            async with mcp.session_manager.run():
                yield
        finally:
            await scheduler.stop()
            await companion_service.stop()
            await retention.stop()
            await selected_runtime.close()
            await cron_service.close()
            await container.cache.close()
            await engine.dispose()

    app.router.lifespan_context = lifespan
    app.add_middleware(
        BearerAuthMiddleware,
        token=router_token,
        cron_token=cron_mcp_token,
        mcp_path=settings.mcp_mount_path,
        public_paths=("/health", "/healthz"),
        public_prefixes=("/dashboard",) if settings.dashboard_enabled else (),
    )
    return app
