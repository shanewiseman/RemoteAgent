from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import quote

from fastapi import FastAPI
from fastapi.testclient import TestClient

from remoteagent.dashboard import register_dashboard
from remoteagent.dashboard.auth import MemorySessionStore


class DashboardProjection:
    async def summary(self) -> dict[str, object]:
        return {"agents": {"ready": 1}, "active_agents": 1, "jobs": {"running": 1}}

    async def system(self) -> dict[str, object]:
        return {"postgres": "ok", "redis": "ok"}

    async def debug(self) -> dict[str, object]:
        return {"authorization": "Bearer must-not-leak", "queue": {"running": 1}}


def dashboard_app(*, debug: bool = True) -> FastAPI:
    class Settings:
        dashboard_allow_http = True
        dashboard_debug_enabled = debug
        dashboard_session_idle_seconds = 600
        dashboard_session_absolute_seconds = 3600
        redis_prefix = "remoteagent"

        def authorization_token(self) -> str:
            return "browser-secret"

    app = FastAPI()
    register_dashboard(app)
    app.state.container = SimpleNamespace(
        settings=Settings(),
        dashboard_session_store=MemorySessionStore(),
        dashboard_service=DashboardProjection(),
        telemetry=app.state.dashboard_metrics,
    )
    return app


def test_login_exchange_and_protected_json_api() -> None:
    app = dashboard_app()
    with TestClient(app) as client:
        assert client.get("/dashboard/login").status_code == 200
        assert client.get("/dashboard/api/v1/summary").status_code == 401
        rejected = client.post(
            "/dashboard/session",
            data={"token": "wrong"},
            follow_redirects=False,
        )
        assert rejected.status_code == 401

        response = client.post(
            "/dashboard/session",
            data={"token": "browser-secret"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        cookie = response.headers["set-cookie"]
        assert "HttpOnly" in cookie
        assert "SameSite=strict" in cookie
        assert "Secure" not in cookie

        summary = client.get("/dashboard/api/v1/summary")
        assert summary.status_code == 200
        assert summary.json()["active_agents"] == 1


def test_metrics_requires_bearer_and_uses_shared_registry() -> None:
    app = dashboard_app()
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 401
        assert client.get("/caller-controlled-path").status_code == 404
        response = client.get("/metrics", headers={"Authorization": "Bearer browser-secret"})

    assert response.status_code == 200
    assert "remoteagent_http_requests_total" in response.text
    assert "caller-controlled-path" not in response.text


def test_page_context_is_html_escaped_and_debug_bundle_is_redacted() -> None:
    app = dashboard_app()
    headers = {"Authorization": "Bearer browser-secret"}
    hostile = '<img src=x onerror="alert(1)">'
    with TestClient(app) as client:
        page = client.get(
            f"/dashboard/agents/{quote(hostile, safe='')}",
            headers=headers,
        )
        bundle = client.get("/dashboard/api/v1/debug/bundle", headers=headers)

    assert page.status_code == 200
    assert hostile not in page.text
    assert "&lt;img" in page.text
    assert bundle.status_code == 200
    assert "must-not-leak" not in bundle.text


def test_debug_surface_can_be_disabled() -> None:
    app = dashboard_app(debug=False)
    with TestClient(app) as client:
        response = client.get(
            "/dashboard/api/v1/debug",
            headers={"Authorization": "Bearer browser-secret"},
        )

    assert response.status_code == 404
