"""FastAPI registration for the operational dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from remoteagent.artifacts import ArtifactNotFoundError, ArtifactPolicyError
from remoteagent.telemetry import DashboardMetrics, HTTPMetricsMiddleware

from .artifacts import (
    TEXT_PREVIEW_BYTES,
    download_response,
    normalize_payload,
    preview_response,
)
from .auth import SESSION_COOKIE, DashboardAuthManager
from .data import DashboardData
from .diagnostics import diagnostic_bundle
from .sse import ActivityStream, parse_last_event_id
from .util import container_from_app, maybe_await, metrics_from_app, setting

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATES = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(
        enabled_extensions=("html", "xml"), default_for_string=True, default=True
    ),
    enable_async=False,
)


SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "X-Frame-Options": "DENY",
}


class DashboardSecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http" or not str(scope.get("path", "")).startswith("/dashboard"):
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {key.lower() for key, _ in headers}
                for key, value in SECURITY_HEADERS.items():
                    encoded = key.lower().encode("latin-1")
                    if encoded not in existing:
                        headers.append((encoded, value.encode("latin-1")))
                if b"cache-control" not in existing and not str(scope.get("path", "")).startswith(
                    "/dashboard/static/"
                ):
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


router = APIRouter(prefix="/dashboard", include_in_schema=False)


def _auth(request: Request) -> DashboardAuthManager:
    return request.app.state.dashboard_auth_manager


def _data(request: Request) -> DashboardData:
    return DashboardData(request.app)


def _debug_enabled(request: Request) -> bool:
    settings = getattr(container_from_app(request.app), "settings", None)
    return bool(setting(settings, "dashboard_debug_enabled", False))


def _render(template: str, *, status_code: int = 200, **context: Any) -> HTMLResponse:
    body = TEMPLATES.get_template(template).render(**context)
    return HTMLResponse(
        body,
        status_code=status_code,
        headers={"Cache-Control": "no-store", **SECURITY_HEADERS},
    )


async def _page_auth(request: Request) -> Any:
    context = await _auth(request).authenticate(request)
    if context is None:
        return RedirectResponse("/dashboard/login", status_code=303)
    return context


async def _page(
    request: Request,
    *,
    title: str,
    page: str,
    entity_id: str = "",
    debug: bool = False,
) -> Response:
    context = await _page_auth(request)
    if isinstance(context, Response):
        return context
    if debug and not _debug_enabled(request):
        raise HTTPException(status_code=404, detail="debug dashboard is disabled")
    return _render(
        "dashboard.html",
        request=request,
        title=title,
        page=page,
        entity_id=entity_id,
        csrf_token=context.csrf_token or "",
        debug_enabled=_debug_enabled(request),
    )


async def _api_auth(request: Request) -> None:
    await _auth(request).require(request)


def _json(value: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        value,
        status_code=status_code,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


async def _artifact_content(
    data: DashboardData,
    artifact_id: str,
    *,
    preview: bool = False,
    max_bytes: int | None = None,
) -> Any:
    try:
        return await data.artifact_content(
            artifact_id,
            preview=preview,
            max_bytes=max_bytes,
        )
    except ArtifactNotFoundError as exc:
        raise HTTPException(status_code=404, detail="artifact content not found") from exc
    except (ArtifactPolicyError, OSError) as exc:
        raise HTTPException(status_code=410, detail="artifact storage is unavailable") from exc


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    try:
        existing = await _auth(request).authenticate(request)
    except HTTPException as exc:
        return _render(
            "login.html",
            title="Dashboard sign in",
            error=exc.detail,
            allow_form=False,
            status_code=exc.status_code,
        )
    if existing is not None:
        return RedirectResponse("/dashboard/", status_code=303)
    return _render("login.html", title="Dashboard sign in", error=None, allow_form=True)


@router.post("/session")
async def create_session(request: Request) -> Response:
    raw = await request.body()
    if len(raw) > 16_384:
        raise HTTPException(status_code=413, detail="login request too large")
    values = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    candidate = values.get("token", [""])[0]
    try:
        session_id, _record = await _auth(request).login(request, candidate)
    except HTTPException as exc:
        return _render(
            "login.html",
            title="Dashboard sign in",
            error=exc.detail,
            allow_form=True,
            status_code=exc.status_code,
        )
    except RuntimeError:
        return _render(
            "login.html",
            title="Dashboard sign in",
            error="Browser sessions are temporarily unavailable.",
            allow_form=True,
            status_code=503,
        )
    response = RedirectResponse("/dashboard/", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=_auth(request).absolute_seconds(),
        httponly=True,
        secure=not _auth(request).allow_http(),
        samesite="strict",
        path="/dashboard",
    )
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    raw = await request.body()
    values = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    await _auth(request).logout(request, values.get("csrf_token", [""])[0])
    response = RedirectResponse("/dashboard/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/dashboard")
    return response


@router.get("/")
async def overview_page(request: Request) -> Response:
    return await _page(request, title="Overview", page="overview")


@router.get("/agents")
async def agents_page(request: Request) -> Response:
    return await _page(request, title="Agents", page="agents")


@router.get("/agents/{agent_id}")
async def agent_page(request: Request, agent_id: str) -> Response:
    return await _page(request, title="Agent", page="agent", entity_id=agent_id)


@router.get("/jobs")
async def jobs_page(request: Request) -> Response:
    return await _page(request, title="Jobs", page="jobs")


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str) -> Response:
    return await _page(request, title="Job", page="job", entity_id=job_id)


@router.get("/history")
async def history_page(request: Request) -> Response:
    return await _page(request, title="Conversation history", page="conversations")


@router.get("/history/{conversation_key}")
async def conversation_page(request: Request, conversation_key: str) -> Response:
    return await _page(
        request, title="Conversation", page="conversation", entity_id=conversation_key
    )


@router.get("/artifacts")
async def artifacts_page(request: Request) -> Response:
    return await _page(request, title="Artifacts", page="artifacts")


@router.get("/artifacts/{artifact_id}")
async def artifact_page(request: Request, artifact_id: str) -> Response:
    return await _page(request, title="Artifact", page="artifact", entity_id=artifact_id)


@router.get("/system")
async def system_page(request: Request) -> Response:
    return await _page(request, title="System", page="system")


@router.get("/debug")
async def debug_page(request: Request) -> Response:
    return await _page(request, title="Read-only diagnostics", page="debug", debug=True)


@router.get("/api/v1/summary")
async def api_summary(request: Request) -> JSONResponse:
    await _api_auth(request)
    return _json(await _data(request).summary())


@router.get("/api/v1/agents")
async def api_agents(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    status: str | None = None,
) -> JSONResponse:
    await _api_auth(request)
    return _json(await _data(request).list_agents(cursor=cursor, limit=limit, status=status))


@router.get("/api/v1/agents/{agent_id}")
async def api_agent(request: Request, agent_id: str) -> JSONResponse:
    await _api_auth(request)
    value = await _data(request).get_agent(agent_id)
    if value is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _json(value)


@router.get("/api/v1/agents/{agent_id}/revisions")
async def api_agent_revisions(
    request: Request, agent_id: str, cursor: str | None = None, limit: int = Query(50, ge=1, le=100)
) -> JSONResponse:
    await _api_auth(request)
    return _json(await _data(request).agent_revisions(agent_id, cursor=cursor, limit=limit))


@router.get("/api/v1/jobs")
async def api_jobs(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    status: str | None = None,
    agent_id: str | None = None,
    conversation_key: str | None = None,
) -> JSONResponse:
    await _api_auth(request)
    return _json(
        await _data(request).list_jobs(
            cursor=cursor,
            limit=limit,
            status=status,
            agent_id=agent_id,
            conversation_key=conversation_key,
        )
    )


@router.get("/api/v1/jobs/{job_id}")
async def api_job(request: Request, job_id: str) -> JSONResponse:
    await _api_auth(request)
    value = await _data(request).get_job(job_id)
    if value is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _json(value)


@router.get("/api/v1/jobs/{job_id}/events")
async def api_job_events(
    request: Request,
    job_id: str,
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> JSONResponse:
    await _api_auth(request)
    return _json(await _data(request).job_events(job_id, after=after, limit=limit))


@router.get("/api/v1/conversations")
async def api_conversations(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    agent_id: str | None = None,
    status: str | None = None,
) -> JSONResponse:
    await _api_auth(request)
    return _json(
        await _data(request).list_conversations(
            cursor=cursor, limit=limit, agent_id=agent_id, status=status
        )
    )


@router.get("/api/v1/conversations/{conversation_key}")
async def api_conversation(request: Request, conversation_key: str) -> JSONResponse:
    await _api_auth(request)
    value = await _data(request).get_conversation(conversation_key)
    if value is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _json(value)


@router.get("/api/v1/conversations/{conversation_key}/turns")
async def api_conversation_turns(
    request: Request, conversation_key: str, limit: int | None = Query(None, ge=1, le=500)
) -> JSONResponse:
    await _api_auth(request)
    return _json(await _data(request).conversation_turns(conversation_key, limit=limit))


@router.get("/api/v1/conversations/{conversation_key}/companions")
async def api_conversation_companions(
    request: Request,
    conversation_key: str,
    include_history: bool = False,
    limit: int = Query(200, ge=1, le=500),
) -> JSONResponse:
    """Expose bounded metadata only; companion files are not downloadable here."""

    await _api_auth(request)
    data = _data(request)
    if await data.get_conversation(conversation_key) is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    return _json(
        await data.conversation_companions(
            conversation_key, include_history=include_history, limit=limit
        )
    )


@router.get("/api/v1/artifacts")
async def api_artifacts(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    job_id: str | None = None,
    conversation_key: str | None = None,
    media_type: str | None = None,
) -> JSONResponse:
    await _api_auth(request)
    return _json(
        await _data(request).list_artifacts(
            cursor=cursor,
            limit=limit,
            job_id=job_id,
            conversation_key=conversation_key,
            media_type=media_type,
        )
    )


@router.get("/api/v1/artifacts/{artifact_id}")
async def api_artifact(request: Request, artifact_id: str) -> JSONResponse:
    await _api_auth(request)
    value = await _data(request).get_artifact(artifact_id)
    if value is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return _json(value)


@router.get("/api/v1/artifacts/{artifact_id}/preview")
async def api_artifact_preview(request: Request, artifact_id: str) -> Response:
    await _api_auth(request)
    data = _data(request)
    metadata = await data.get_artifact(artifact_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    raw = await _artifact_content(
        data,
        artifact_id,
        preview=True,
        max_bytes=TEXT_PREVIEW_BYTES,
    )
    payload = normalize_payload(raw, metadata, safe_derivative=True)
    if payload is None and metadata.get("media_type") in {
        "text/plain",
        "text/markdown",
        "application/json",
        "application/problem+json",
    }:
        raw = await _artifact_content(data, artifact_id, max_bytes=TEXT_PREVIEW_BYTES)
        payload = normalize_payload(raw, metadata)
    return preview_response(metadata, payload)


@router.get("/api/v1/artifacts/{artifact_id}/download")
async def api_artifact_download(request: Request, artifact_id: str) -> Response:
    await _api_auth(request)
    data = _data(request)
    metadata = await data.get_artifact(artifact_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    raw = await _artifact_content(data, artifact_id)
    return download_response(metadata, normalize_payload(raw, metadata))


@router.get("/api/v1/system")
async def api_system(request: Request) -> JSONResponse:
    await _api_auth(request)
    return _json(await _data(request).system())


@router.get("/api/v1/debug")
async def api_debug(request: Request) -> JSONResponse:
    await _api_auth(request)
    if not _debug_enabled(request):
        raise HTTPException(status_code=404, detail="debug dashboard is disabled")
    return _json(await _data(request).debug())


@router.get("/api/v1/debug/bundle")
async def api_debug_bundle(request: Request) -> Response:
    await _api_auth(request)
    if not _debug_enabled(request):
        raise HTTPException(status_code=404, detail="debug dashboard is disabled")
    payload = diagnostic_bundle(await _data(request).debug())
    return Response(
        payload,
        media_type="application/json",
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": 'attachment; filename="remoteagent-diagnostics.json"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/events")
async def dashboard_events(request: Request) -> StreamingResponse:
    await _api_auth(request)
    metrics = metrics_from_app(request.app)

    async def generate() -> Any:
        if metrics is not None and metrics.enabled:
            metrics.sse_connections.inc()
        try:
            yield b"retry: 3000\n\n"
            async for chunk in ActivityStream(request).iter(after_id=parse_last_event_id(request)):
                if (
                    metrics is not None
                    and metrics.enabled
                    and (chunk.startswith(b"event:") or b"\nevent:" in chunk)
                ):
                    metrics.sse_events.labels(type="activity").inc()
                yield chunk
        finally:
            if metrics is not None and metrics.enabled:
                metrics.sse_connections.dec()

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _metrics_endpoint(request: Request) -> Response:
    # Metrics are protected too. Browser sessions are path-scoped and normally
    # will not reach /metrics, so Prometheus should send the existing bearer.
    await _auth(request).require(request)
    metrics = metrics_from_app(request.app)
    renderer = getattr(metrics, "render", None)
    if not callable(renderer):
        raise HTTPException(status_code=503, detail="metrics are unavailable")
    payload, content_type = await maybe_await(renderer())
    return Response(
        payload,
        media_type=None,
        headers={"Content-Type": content_type, "Cache-Control": "no-store"},
    )


def register_dashboard(app: FastAPI) -> None:
    """Register dashboard routes without requiring the lifespan container yet."""

    if getattr(app.state, "remoteagent_dashboard_registered", False):
        return
    app.state.remoteagent_dashboard_registered = True
    app.state.dashboard_auth_manager = DashboardAuthManager(app)
    app.state.dashboard_metrics = DashboardMetrics()
    app.add_middleware(DashboardSecurityHeadersMiddleware)
    app.add_middleware(
        HTTPMetricsMiddleware,
        metrics=app.state.dashboard_metrics,
        metrics_resolver=lambda: metrics_from_app(app),
    )
    app.mount(
        "/dashboard/static",
        StaticFiles(directory=str(STATIC_DIR)),
        name="remoteagent-dashboard-static",
    )
    app.include_router(router)
    if not any(getattr(route, "path", None) == "/metrics" for route in app.routes):
        app.add_api_route("/metrics", _metrics_endpoint, methods=["GET"], include_in_schema=False)


__all__ = ["register_dashboard", "router"]
