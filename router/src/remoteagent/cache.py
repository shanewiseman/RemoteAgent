from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class Cache(Protocol):
    async def get_json(self, key: str) -> Any | None: ...

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def publish(self, channel: str, value: dict[str, Any]) -> None: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


class MemoryCache:
    """Small process-local fallback. PostgreSQL remains authoritative."""

    def __init__(self, prefix: str = "remoteagent") -> None:
        self.prefix = prefix
        self._values: dict[str, tuple[float | None, str]] = {}
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, set[asyncio.Queue[str]]] = {}

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get_json(self, key: str) -> Any | None:
        async with self._lock:
            item = self._values.get(self._key(key))
            if item is None:
                return None
            expires_at, payload = item
            if expires_at is not None and expires_at <= time.monotonic():
                self._values.pop(self._key(key), None)
                return None
        return json.loads(payload)

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        payload = json.dumps(value, separators=(",", ":"), default=str)
        async with self._lock:
            self._values[self._key(key)] = (expires_at, payload)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._values.pop(self._key(key), None)

    async def publish(self, channel: str, value: dict[str, Any]) -> None:
        payload = json.dumps(value, separators=(",", ":"), default=str)
        async with self._lock:
            subscribers = tuple(self._subscribers.get(self._key(channel), ()))
        for queue in subscribers:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Activity delivery is best effort; durable job events live in SQL.
                pass

    @asynccontextmanager
    async def subscribe(self, channel: str) -> AsyncIterator[asyncio.Queue[str]]:
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        key = self._key(channel)
        async with self._lock:
            self._subscribers.setdefault(key, set()).add(queue)
        try:
            yield queue
        finally:
            async with self._lock:
                subscribers = self._subscribers.get(key)
                if subscribers is not None:
                    subscribers.discard(queue)
                    if not subscribers:
                        self._subscribers.pop(key, None)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        self._values.clear()


class RedisCache:
    def __init__(self, client: Any, prefix: str = "remoteagent") -> None:
        self.client = client
        self.prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self.prefix}:{key}"

    async def get_json(self, key: str) -> Any | None:
        payload = await self.client.get(self._key(key))
        return None if payload is None else json.loads(payload)

    async def set_json(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        await self.client.set(
            self._key(key),
            json.dumps(value, separators=(",", ":"), default=str),
            ex=ttl_seconds,
        )

    async def delete(self, key: str) -> None:
        await self.client.delete(self._key(key))

    async def publish(self, channel: str, value: dict[str, Any]) -> None:
        await self.client.publish(
            self._key(channel), json.dumps(value, separators=(",", ":"), default=str)
        )

    async def ping(self) -> bool:
        return bool(await self.client.ping())

    async def close(self) -> None:
        await self.client.aclose()


async def create_cache(redis_url: str | None, prefix: str) -> Cache:
    if not redis_url:
        return MemoryCache(prefix)
    client = None
    try:
        from redis.asyncio import Redis

        client = Redis.from_url(redis_url, decode_responses=True)
        cache = RedisCache(client, prefix)
        await cache.ping()
        return cache
    except Exception:  # noqa: BLE001 - Redis failure must degrade to SQL + memory.
        # Redis is an optional accelerator/activity bus. Falling back must not
        # make durable jobs unavailable.
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - cleanup cannot prevent fallback.
                logger.warning("failed to close unavailable Redis client")
        logger.warning("Redis is unavailable; using process-local cache fallback")
        return MemoryCache(prefix)
