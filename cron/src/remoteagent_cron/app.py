from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from sqlalchemy import inspect, text

from .api import build_api_router
from .clock import Clock, SystemClock
from .config import Settings, load_settings
from .db import create_engine, create_session_factory, initialize_schema
from .mcp_client import RouterMCPClient, StreamableHTTPRouterClient
from .schemas import ReadinessView
from .security import BearerAuthMiddleware
from .service import CronService
from .worker import CronWorker

logger = logging.getLogger(__name__)

SCHEMA_REVISION = "20260902_0001"


def configure_openapi_security(app: FastAPI) -> None:
    original = app.openapi

    def secured_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = original()
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["CronBearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "description": "Router-to-cron internal service token",
        }
        requirement = [{"CronBearerAuth": []}]
        schema["security"] = requirement
        for path in schema.get("paths", {}).values():
            for operation in path.values():
                if isinstance(operation, dict) and "operationId" in operation:
                    operation["security"] = requirement
        app.openapi_schema = schema
        return schema

    app.openapi = secured_openapi  # type: ignore[method-assign]


@dataclass(slots=True)
class Container:
    settings: Settings
    engine: Any
    session_factory: Any
    router: RouterMCPClient
    service: CronService
    worker: CronWorker

    async def _schema_ready(self) -> bool:
        if self.settings.initialize_schema:
            async with self.engine.connect() as connection:
                tables = await connection.run_sync(
                    lambda sync_connection: set(inspect(sync_connection).get_table_names())
                )
            return {
                "cron_schedules",
                "cron_schedule_revisions",
                "cron_executions",
                "cron_responses",
                "cron_response_leases",
            } <= tables
        try:
            async with self.engine.connect() as connection:
                revision = await connection.scalar(
                    text("SELECT version_num FROM cron_alembic_version")
                )
            return revision == SCHEMA_REVISION
        except Exception:
            return False

    async def readiness(self) -> ReadinessView:
        database = False
        schema = False
        router_mcp = False
        failures: list[str] = []
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            database = True
            schema = await self._schema_ready()
            if not schema:
                failures.append("schema is not current")
        except Exception:
            failures.append("database is unavailable")
        try:
            router_mcp = await self.router.check_ready()
            if not router_mcp:
                failures.append("router MCP is unavailable or missing required tools")
        except Exception:
            failures.append("router MCP is unavailable")
        scheduler = self.worker.running if self.settings.scheduler_enabled else True
        if not scheduler:
            failures.append("scheduler is not running")
        ready = database and schema and router_mcp and scheduler
        return ReadinessView(
            status="ready" if ready else "not_ready",
            database=database,
            schema=schema,
            router_mcp=router_mcp,
            scheduler=scheduler,
            detail="; ".join(failures) or None,
        )


def create_app(
    settings: Settings | None = None,
    *,
    router_client: RouterMCPClient | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    if settings.environment == "production":
        if settings.authorization_token() == "change-me-cron-api":
            raise ValueError("the default internal bearer token cannot be used in production")
        if settings.mcp_token() == "change-me-cron-mcp":
            raise ValueError("the default router MCP token cannot be used in production")

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    selected_clock = clock or SystemClock()
    selected_router = router_client or StreamableHTTPRouterClient(
        settings.router_mcp_url,
        settings.mcp_token(),
        timeout_seconds=settings.router_request_timeout_seconds,
    )
    service = CronService(settings, session_factory, selected_router, clock=selected_clock)
    worker = CronWorker(service, clock=selected_clock)
    container = Container(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        router=selected_router,
        service=service,
        worker=worker,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if settings.initialize_schema:
                await initialize_schema(engine)
            if settings.scheduler_enabled:
                await worker.start()
            yield
        finally:
            await worker.stop()
            await selected_router.close()
            await engine.dispose()

    app = FastAPI(
        title="RemoteAgent Cron Internal API",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.container = container
    app.include_router(build_api_router())
    configure_openapi_security(app)
    app.add_middleware(
        BearerAuthMiddleware,
        token=settings.authorization_token(),
    )
    return app
