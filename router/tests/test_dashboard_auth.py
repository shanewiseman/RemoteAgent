from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.requests import Request

from remoteagent.dashboard import auth as auth_module
from remoteagent.dashboard.auth import (
    SESSION_COOKIE,
    DashboardAuthManager,
    MemorySessionStore,
    RedisSessionStore,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.expirations: dict[str, int] = {}

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.values[key] = value
        self.expirations[key] = ex

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)

    async def incr(self, key: str) -> int:
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def expire(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds


def request_for(
    app: FastAPI,
    *,
    scheme: str = "http",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": scheme,
            "path": "/dashboard/",
            "raw_path": b"/dashboard/",
            "query_string": b"",
            "headers": headers or [],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "app": app,
        }
    )


@pytest.mark.asyncio
async def test_redis_store_never_uses_raw_session_id_as_key() -> None:
    client = FakeRedis()
    store = RedisSessionStore(client, prefix="ra")

    session_id, record = await store.create(
        token_version="version", idle_seconds=600, absolute_seconds=3600
    )

    assert session_id not in next(iter(client.values))
    assert await store.get(session_id, idle_seconds=600) is not None
    await store.delete(session_id)
    assert not client.values
    assert record.csrf_token


@pytest.mark.asyncio
async def test_memory_session_enforces_idle_and_absolute_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(auth_module.time, "time", lambda: clock[0])
    store = MemorySessionStore()
    session_id, _record = await store.create(
        token_version="version", idle_seconds=60, absolute_seconds=120
    )

    clock[0] = 1050.0
    assert await store.get(session_id, idle_seconds=60) is not None
    clock[0] = 1115.0
    assert await store.get(session_id, idle_seconds=60) is None


def test_authorization_token_method_precedes_bearer_field() -> None:
    class Settings:
        bearer_token = "wrong"
        dashboard_allow_http = True

        def authorization_token(self) -> str:
            return "from-secret-file"

    app = FastAPI()
    app.state.container = SimpleNamespace(settings=Settings())

    assert DashboardAuthManager(app).bearer_token() == "from-secret-file"


def test_authorization_token_file_failure_never_falls_back_to_default_field() -> None:
    class Settings:
        bearer_token = "change-me"

        def authorization_token(self) -> str:
            raise ValueError("secret file disappeared")

    app = FastAPI()
    app.state.container = SimpleNamespace(settings=Settings())

    assert DashboardAuthManager(app).bearer_token() == ""


@pytest.mark.asyncio
async def test_http_requires_explicit_opt_in() -> None:
    app = FastAPI()
    app.state.container = SimpleNamespace(
        settings=SimpleNamespace(bearer_token="secret", dashboard_allow_http=False)
    )
    manager = DashboardAuthManager(app)

    with pytest.raises(HTTPException) as error:
        await manager.authenticate(request_for(app))

    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_browser_session_is_bound_to_bearer_token_version() -> None:
    settings = SimpleNamespace(
        bearer_token="first",
        dashboard_allow_http=True,
        dashboard_session_idle_seconds=600,
        dashboard_session_absolute_seconds=3600,
    )
    store = MemorySessionStore()
    app = FastAPI()
    app.state.container = SimpleNamespace(settings=settings, dashboard_session_store=store)
    manager = DashboardAuthManager(app)
    app.state.dashboard_metrics = None
    session_id, _record = await manager.login(request_for(app), "first")
    cookie = [(b"cookie", f"{SESSION_COOKIE}={session_id}".encode())]

    assert (await manager.authenticate(request_for(app, headers=cookie))).mechanism == "session"
    settings.bearer_token = "rotated"
    assert await manager.authenticate(request_for(app, headers=cookie)) is None
