"""Durable activity replay with optional Redis wakeups."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import Request

from .data import DashboardData
from .util import container_from_app, setting

logger = logging.getLogger(__name__)

_EVENT_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_ALLOWED_EVENT_TYPES = {
    "task.updated",
    "agent.updated",
    "conversation.updated",
    "artifact.created",
    "worker.updated",
    "system.degraded",
    "system.recovered",
    "job_event",
    "reset",
}


def _event_type(value: Any) -> str:
    candidate = str(value or "job_event").lower()
    if candidate in _ALLOWED_EVENT_TYPES:
        return candidate
    if candidate.startswith(("turn.", "item.", "job.")):
        return "job_event"
    return candidate if _EVENT_NAME.fullmatch(candidate) else "job_event"


def encode_sse(event: Mapping[str, Any], *, retry_ms: int | None = None) -> bytes:
    event_id = str(event.get("id", ""))
    event_id = event_id if event_id.isdigit() else ""
    event_name = _event_type(event.get("type"))
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str)
    lines = []
    if retry_ms is not None:
        lines.append(f"retry: {retry_ms}")
    if event_id:
        lines.append(f"id: {event_id}")
    lines.extend((f"event: {event_name}", f"data: {payload}", ""))
    return ("\n".join(lines) + "\n").encode("utf-8")


class ActivityStream:
    """Replay from PostgreSQL; use Redis only to avoid waiting for the next poll."""

    def __init__(self, request: Request) -> None:
        self.request = request
        self.data = DashboardData(request.app)
        self.container = container_from_app(request.app)
        settings = getattr(self.container, "settings", None)
        self.poll_seconds = max(0.25, float(setting(settings, "dashboard_sse_poll_seconds", 5.0)))
        self.heartbeat_seconds = max(
            1.0, float(setting(settings, "dashboard_sse_heartbeat_seconds", 15.0))
        )
        self.logical_channel = str(setting(settings, "dashboard_activity_channel", "activity"))
        prefix = str(setting(settings, "redis_prefix", "remoteagent"))
        self.channel = (
            self.logical_channel
            if self.logical_channel.startswith(f"{prefix}:")
            else f"{prefix}:{self.logical_channel}"
        )

    async def _redis_pubsub(self) -> Any | None:
        cache = getattr(self.container, "cache", None)
        redis_client = getattr(cache, "client", None) if cache is not None else None
        if redis_client is None:
            redis_client = getattr(self.container, "redis", None)
        if redis_client is None:
            redis_client = getattr(self.container, "redis_client", None)
        if redis_client is None:
            redis_client = getattr(self.request.app.state, "dashboard_redis_client", None)
        if redis_client is None or not callable(getattr(redis_client, "pubsub", None)):
            return None
        try:
            pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
            await pubsub.subscribe(self.channel)
            return pubsub
        except Exception:
            logger.debug("dashboard Redis subscription unavailable", exc_info=True)
            return None

    async def iter(self, *, after_id: int = 0) -> AsyncIterator[bytes]:
        cursor = max(0, after_id)
        cache = getattr(self.container, "cache", None)
        cache_subscription = None
        cache_queue = None
        subscribe = getattr(cache, "subscribe", None)
        if callable(subscribe):
            try:
                cache_subscription = subscribe(self.logical_channel)
                cache_queue = await cache_subscription.__aenter__()
            except Exception:
                logger.debug("dashboard cache subscription unavailable", exc_info=True)
                cache_subscription = None
                cache_queue = None
        pubsub = None if cache_queue is not None else await self._redis_pubsub()
        last_write = asyncio.get_running_loop().time()
        try:
            while not await self.request.is_disconnected():
                events = await self.data.activity_events(after_id=cursor, limit=1001)
                if len(events) > 1000:
                    try:
                        reset_cursor = max(cursor, int(events[-1].get("id", cursor)))
                    except (TypeError, ValueError):
                        reset_cursor = cursor
                    yield encode_sse(
                        {"id": reset_cursor, "type": "reset", "reason": "replay_limit"},
                        retry_ms=3000,
                    )
                    return
                if events:
                    for event in events:
                        try:
                            event_id = int(event.get("id", cursor))
                        except (TypeError, ValueError):
                            continue
                        if event_id <= cursor:
                            continue
                        cursor = event_id
                        # SSE publishes only status metadata. Detailed payloads
                        # are fetched from authenticated JSON endpoints.
                        thin = {
                            "id": event_id,
                            "type": _event_type(event.get("type")),
                            "job_id": event.get("job_id"),
                            "created_at": event.get("created_at"),
                        }
                        yield encode_sse(thin)
                        last_write = asyncio.get_running_loop().time()
                    continue

                now = asyncio.get_running_loop().time()
                if now - last_write >= self.heartbeat_seconds:
                    yield b": heartbeat\n\n"
                    last_write = now

                if cache_queue is not None:
                    try:
                        await asyncio.wait_for(cache_queue.get(), timeout=self.poll_seconds)
                    except TimeoutError:
                        pass
                    except Exception:
                        logger.debug("dashboard cache wakeup failed", exc_info=True)
                        cache_queue = None
                elif pubsub is not None:
                    try:
                        await pubsub.get_message(timeout=self.poll_seconds)
                    except Exception:
                        logger.debug("dashboard Redis wakeup failed", exc_info=True)
                        try:
                            close = getattr(pubsub, "aclose", None) or getattr(
                                pubsub, "close", None
                            )
                            if callable(close):
                                await close()
                        except Exception:
                            logger.debug("dashboard Redis cleanup failed", exc_info=True)
                        pubsub = None
                else:
                    await asyncio.sleep(self.poll_seconds)
        finally:
            if cache_subscription is not None:
                try:
                    await cache_subscription.__aexit__(None, None, None)
                except Exception:
                    logger.debug("dashboard cache cleanup failed", exc_info=True)
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(self.channel)
                    close = getattr(pubsub, "aclose", None) or getattr(pubsub, "close", None)
                    if callable(close):
                        await close()
                except Exception:
                    logger.debug("dashboard Redis cleanup failed", exc_info=True)


def parse_last_event_id(request: Request) -> int:
    value = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
    try:
        parsed = int(value)
    except ValueError:
        return 0
    return max(0, parsed)


__all__ = ["ActivityStream", "encode_sse", "parse_last_event_id"]
