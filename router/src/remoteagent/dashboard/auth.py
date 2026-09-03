"""Bearer-token exchange and short-lived browser dashboard sessions."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from fastapi import HTTPException, Request, status

from .util import container_from_app, metrics_from_app, reveal_secret, setting

SESSION_COOKIE = "remoteagent_dashboard_session"


@dataclass(frozen=True, slots=True)
class SessionRecord:
    issued_at: float
    last_seen_at: float
    absolute_expires_at: float
    token_version: str
    csrf_token: str


@dataclass(frozen=True, slots=True)
class AuthContext:
    mechanism: str
    session_id: str | None = None
    csrf_token: str | None = None


class SessionStore(Protocol):
    async def create(
        self, *, token_version: str, idle_seconds: int, absolute_seconds: int
    ) -> tuple[str, SessionRecord]: ...

    async def get(self, session_id: str, *, idle_seconds: int) -> SessionRecord | None: ...

    async def delete(self, session_id: str) -> None: ...

    async def allow_login(
        self, source: str, *, limit: int = 5, window_seconds: int = 60
    ) -> bool: ...


def _session_key(prefix: str, session_id: str) -> str:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{prefix}:dashboard:session:{digest}"


def _rate_key(prefix: str, source: str, bucket: int) -> str:
    digest = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()[:24]
    return f"{prefix}:dashboard:login:{digest}:{bucket}"


class RedisSessionStore:
    """Async Redis session store; only opaque session hashes are keys."""

    def __init__(self, client: Any, *, prefix: str = "remoteagent") -> None:
        self.client = client
        self.prefix = prefix

    async def create(
        self, *, token_version: str, idle_seconds: int, absolute_seconds: int
    ) -> tuple[str, SessionRecord]:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        record = SessionRecord(
            issued_at=now,
            last_seen_at=now,
            absolute_expires_at=now + absolute_seconds,
            token_version=token_version,
            csrf_token=secrets.token_urlsafe(24),
        )
        ttl = max(1, min(idle_seconds, absolute_seconds))
        await self.client.set(
            _session_key(self.prefix, session_id), json.dumps(asdict(record)), ex=ttl
        )
        return session_id, record

    async def get(self, session_id: str, *, idle_seconds: int) -> SessionRecord | None:
        key = _session_key(self.prefix, session_id)
        raw = await self.client.get(key)
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="strict")
        try:
            payload = json.loads(raw)
            record = SessionRecord(**payload)
        except (TypeError, ValueError, json.JSONDecodeError):
            await self.client.delete(key)
            return None
        now = time.time()
        if now >= record.absolute_expires_at or now - record.last_seen_at >= idle_seconds:
            await self.client.delete(key)
            return None
        refreshed = SessionRecord(
            issued_at=record.issued_at,
            last_seen_at=now,
            absolute_expires_at=record.absolute_expires_at,
            token_version=record.token_version,
            csrf_token=record.csrf_token,
        )
        ttl = max(1, min(idle_seconds, int(record.absolute_expires_at - now)))
        await self.client.set(key, json.dumps(asdict(refreshed)), ex=ttl)
        return refreshed

    async def delete(self, session_id: str) -> None:
        await self.client.delete(_session_key(self.prefix, session_id))

    async def allow_login(self, source: str, *, limit: int = 5, window_seconds: int = 60) -> bool:
        bucket = int(time.time() // window_seconds)
        key = _rate_key(self.prefix, source, bucket)
        value = await self.client.incr(key)
        if value == 1:
            await self.client.expire(key, window_seconds + 1)
        return int(value) <= limit


class MemorySessionStore:
    """Process-local test/development fallback, disabled by default."""

    def __init__(self, *, prefix: str = "remoteagent") -> None:
        self.prefix = prefix
        self._sessions: dict[str, SessionRecord] = {}
        self._attempts: dict[tuple[str, int], int] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, *, token_version: str, idle_seconds: int, absolute_seconds: int
    ) -> tuple[str, SessionRecord]:
        session_id = secrets.token_urlsafe(32)
        now = time.time()
        record = SessionRecord(
            now, now, now + absolute_seconds, token_version, secrets.token_urlsafe(24)
        )
        async with self._lock:
            self._sessions[_session_key(self.prefix, session_id)] = record
        return session_id, record

    async def get(self, session_id: str, *, idle_seconds: int) -> SessionRecord | None:
        key = _session_key(self.prefix, session_id)
        async with self._lock:
            record = self._sessions.get(key)
            now = time.time()
            if record is None:
                return None
            if now >= record.absolute_expires_at or now - record.last_seen_at >= idle_seconds:
                self._sessions.pop(key, None)
                return None
            refreshed = SessionRecord(
                record.issued_at,
                now,
                record.absolute_expires_at,
                record.token_version,
                record.csrf_token,
            )
            self._sessions[key] = refreshed
            return refreshed

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(_session_key(self.prefix, session_id), None)

    async def allow_login(self, source: str, *, limit: int = 5, window_seconds: int = 60) -> bool:
        bucket = int(time.time() // window_seconds)
        async with self._lock:
            key = (source, bucket)
            self._attempts[key] = self._attempts.get(key, 0) + 1
            self._attempts = {
                item: count for item, count in self._attempts.items() if item[1] >= bucket - 1
            }
            return self._attempts[key] <= limit


class DashboardAuthManager:
    def __init__(self, app: Any) -> None:
        self.app = app
        self._store: SessionStore | None = None
        self._store_lock = asyncio.Lock()

    def _settings(self) -> Any:
        return getattr(container_from_app(self.app), "settings", None)

    def bearer_token(self) -> str:
        settings = self._settings()
        authorization_token = getattr(settings, "authorization_token", None)
        if callable(authorization_token):
            try:
                value = authorization_token().strip()
            except (OSError, TypeError, ValueError):
                return ""
            return value
        for name in ("bearer_token", "router_bearer_token", "mcp_bearer_token", "auth_token"):
            value = reveal_secret(setting(settings, name))
            if value:
                return value
        return ""

    def token_version(self) -> str:
        return hashlib.sha256(self.bearer_token().encode("utf-8")).hexdigest()[:24]

    def allow_http(self) -> bool:
        return bool(setting(self._settings(), "dashboard_allow_http", False))

    def idle_seconds(self) -> int:
        return max(60, int(setting(self._settings(), "dashboard_session_idle_seconds", 1800)))

    def absolute_seconds(self) -> int:
        return max(
            self.idle_seconds(),
            int(setting(self._settings(), "dashboard_session_absolute_seconds", 28800)),
        )

    def require_secure_transport(self, request: Request) -> None:
        # ProxyHeadersMiddleware, when configured by the core app, is the only
        # trusted place to translate Forwarded/X-Forwarded-Proto into url.scheme.
        if request.url.scheme != "https" and not self.allow_http():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="dashboard requires HTTPS; explicitly enable dashboard_allow_http only on a trusted network",
            )

    async def store(self) -> SessionStore:
        if self._store is not None:
            return self._store
        async with self._store_lock:
            if self._store is not None:
                return self._store
            container = container_from_app(self.app)
            settings = getattr(container, "settings", None)
            injected = getattr(container, "dashboard_session_store", None)
            if injected is not None:
                self._store = injected
                return injected
            redis_client = getattr(container, "redis", None)
            if redis_client is None:
                redis_client = getattr(container, "redis_client", None)
            cache = getattr(container, "cache", None)
            if redis_client is None and cache is not None:
                redis_client = getattr(cache, "client", None)
            if redis_client is None:
                redis_url = setting(settings, "redis_url")
                if redis_url:
                    try:
                        import redis.asyncio as redis
                    except ImportError as exc:  # pragma: no cover - packaging error
                        raise RuntimeError(
                            "redis is required for dashboard browser sessions"
                        ) from exc
                    redis_client = redis.from_url(str(redis_url), decode_responses=True)
                    self.app.state.dashboard_redis_client = redis_client
            prefix = str(setting(settings, "redis_prefix", "remoteagent"))
            if redis_client is not None:
                self._store = RedisSessionStore(redis_client, prefix=prefix)
            elif bool(setting(settings, "dashboard_allow_memory_sessions", False)):
                self._store = MemorySessionStore(prefix=prefix)
            else:
                raise RuntimeError("dashboard browser sessions require Redis")
            return self._store

    async def authenticate(self, request: Request) -> AuthContext | None:
        self.require_secure_transport(request)
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            candidate = authorization[7:].strip()
            expected = self.bearer_token()
            if expected and hmac.compare_digest(candidate.encode(), expected.encode()):
                return AuthContext(mechanism="bearer")

        session_id = request.cookies.get(SESSION_COOKIE)
        if not session_id:
            return None
        try:
            record = await (await self.store()).get(session_id, idle_seconds=self.idle_seconds())
        except Exception:  # noqa: BLE001 - Redis clients expose backend-specific errors
            return None
        if record is None or not hmac.compare_digest(record.token_version, self.token_version()):
            return None
        return AuthContext("session", session_id=session_id, csrf_token=record.csrf_token)

    async def require(self, request: Request) -> AuthContext:
        context = await self.authenticate(request)
        if context is None:
            metrics = metrics_from_app(request.app)
            if metrics is not None and getattr(metrics, "enabled", False):
                metrics.auth_failures.labels(surface="dashboard").inc()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="dashboard authentication required"
            )
        return context

    async def login(self, request: Request, candidate: str) -> tuple[str, SessionRecord]:
        self.require_secure_transport(request)
        store = await self.store()
        source = request.client.host if request.client else "unknown"
        if not await store.allow_login(source):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="too many login attempts"
            )
        expected = self.bearer_token()
        if not expected or not hmac.compare_digest(candidate.encode(), expected.encode()):
            metrics = metrics_from_app(request.app)
            if metrics is not None and getattr(metrics, "enabled", False):
                metrics.auth_failures.labels(surface="dashboard_login").inc()
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token"
            )
        return await store.create(
            token_version=self.token_version(),
            idle_seconds=self.idle_seconds(),
            absolute_seconds=self.absolute_seconds(),
        )

    async def logout(self, request: Request, csrf_token: str) -> None:
        context = await self.require(request)
        if context.mechanism != "session" or not context.session_id or not context.csrf_token:
            raise HTTPException(status_code=400, detail="browser session required")
        if not hmac.compare_digest(csrf_token, context.csrf_token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        await (await self.store()).delete(context.session_id)


__all__ = [
    "SESSION_COOKIE",
    "AuthContext",
    "DashboardAuthManager",
    "MemorySessionStore",
    "RedisSessionStore",
    "SessionRecord",
]
