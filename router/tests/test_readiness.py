from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from remoteagent.app import create_app
from remoteagent.config import Settings
from remoteagent.runtime import FakeRuntime


class ReadyCron:
    async def readiness(self) -> dict[str, Any]:
        return {"status": "ready", "database": True, "scheduler": True}


class DegradedCron:
    async def readiness(self) -> dict[str, Any]:
        return {"status": "not_ready", "detail": "cron database is unavailable"}


class UnavailableCache:
    backend = "redis"

    def __init__(self, *, raises: bool) -> None:
        self.raises = raises

    async def ping(self) -> bool:
        if self.raises:
            raise OSError("cache unavailable")
        return False


def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        bearer_token="secret",
        dashboard_enabled=False,
        scheduler_enabled=False,
    ).resolved()


def test_readyz_reports_structured_healthy_and_optional_degraded_states(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        app.state.container.cron_service = ReadyCron()
        response = client.get("/readyz", headers=headers)
        assert response.status_code == 200
        assert response.json() == {
            "status": "ready",
            "database": {"status": "ready", "mandatory": True},
            "cache": {
                "status": "ready",
                "mandatory": False,
                "backend": "memory",
            },
            "scheduler": {
                "status": "disabled",
                "mandatory": False,
                "configured_workers": 1,
                "live_workers": 0,
            },
            "cron": {
                "status": "ready",
                "database": True,
                "scheduler": True,
                "mandatory": False,
            },
            "dashboard": {"status": "disabled", "mandatory": False},
        }

        app.state.container.cron_service = DegradedCron()
        response = client.get("/readyz", headers=headers)
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["cron"] == {
            "status": "degraded",
            "detail": "cron database is unavailable",
            "mandatory": False,
        }


def test_readyz_fails_when_mandatory_database_or_scheduler_is_unavailable(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        app.state.container.cron_service = ReadyCron()
        app.state.container.settings.scheduler_enabled = True
        response = client.get("/readyz", headers=headers)
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert response.json()["scheduler"] == {
            "status": "not_ready",
            "mandatory": True,
            "configured_workers": 1,
            "live_workers": 0,
            "detail": "one or more scheduler workers are unavailable",
        }

        app.state.container.settings.scheduler_enabled = False

        @asynccontextmanager
        async def unavailable_database():
            raise OSError("database unavailable")
            yield

        app.state.container.session_factory = unavailable_database
        response = client.get("/readyz", headers=headers)
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        assert response.json()["database"] == {
            "status": "not_ready",
            "mandatory": True,
            "detail": "database is unavailable",
        }


def test_readyz_identifies_configured_redis_memory_fallback(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        app.state.container.cron_service = ReadyCron()
        app.state.container.settings.redis_url = "redis://redis:6379/0"
        app.state.container.settings.dashboard_enabled = True
        response = client.get("/readyz", headers=headers)

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["cache"] == {
        "status": "degraded",
        "mandatory": False,
        "backend": "memory",
        "detail": "configured Redis is unavailable; using process-local memory",
    }
    assert response.json()["dashboard"] == {
        "status": "degraded",
        "mandatory": False,
        "detail": "dashboard login requires Redis",
    }


def test_readyz_treats_cache_false_or_exception_as_optional_degradation(
    tmp_path: Path,
) -> None:
    app = create_app(settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    headers = {"Authorization": "Bearer secret"}
    with TestClient(app) as client:
        app.state.container.cron_service = ReadyCron()
        original_cache = app.state.container.cache
        for raises in (False, True):
            app.state.container.cache = UnavailableCache(raises=raises)
            response = client.get("/readyz", headers=headers)
            assert response.status_code == 200
            assert response.json()["status"] == "degraded"
            assert response.json()["cache"] == {
                "status": "degraded",
                "mandatory": False,
                "backend": "redis",
                "detail": "cache is unavailable",
            }
        app.state.container.cache = original_cache
