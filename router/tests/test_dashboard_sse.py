from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.requests import Request

from remoteagent.dashboard.sse import ActivityStream, encode_sse, parse_last_event_id


def make_request(*, last_event_id: str = "0") -> Request:
    app = FastAPI()
    app.state.container = SimpleNamespace(
        settings=SimpleNamespace(
            redis_prefix="tenant",
            dashboard_activity_channel="activity",
            dashboard_sse_poll_seconds=1,
            dashboard_sse_heartbeat_seconds=15,
        )
    )

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/dashboard/events",
            "raw_path": b"/dashboard/events",
            "query_string": b"",
            "headers": [(b"last-event-id", last_event_id.encode())],
            "client": ("127.0.0.1", 1),
            "server": ("test", 443),
            "app": app,
        },
        receive,
    )


def test_sse_encoding_rejects_event_name_injection() -> None:
    encoded = encode_sse(
        {"id": "12", "type": "bad\nevent", "message": "line one\nline two"},
        retry_ms=3000,
    ).decode()

    assert "event: job_event\n" in encoded
    assert "id: 12\n" in encoded
    assert "retry: 3000\n" in encoded
    assert "line one\\nline two" in encoded


def test_activity_channel_matches_core_cache_prefixing() -> None:
    stream = ActivityStream(make_request(last_event_id="17"))

    assert stream.logical_channel == "activity"
    assert stream.channel == "tenant:activity"
    assert parse_last_event_id(stream.request) == 17


@pytest.mark.asyncio
async def test_replay_limit_advances_cursor_instead_of_reset_loop() -> None:
    request = make_request()
    stream = ActivityStream(request)

    class Events:
        async def activity_events(self, *, after_id: int, limit: int) -> list[dict[str, object]]:
            assert after_id == 0
            assert limit == 1001
            return [{"id": value, "type": "job.running"} for value in range(1, 1002)]

    stream.data = Events()  # type: ignore[assignment]
    chunks = [chunk async for chunk in stream.iter()]

    assert len(chunks) == 1
    assert b"id: 1001\n" in chunks[0]
    assert b'"reason":"replay_limit"' in chunks[0]
