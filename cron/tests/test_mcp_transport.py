from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import mcp
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import PlainTextResponse

from remoteagent_cron.mcp_client import (
    RouterMCPError,
    RouterMCPToolError,
    StreamableHTTPRouterClient,
)
from remoteagent_cron.schemas import RouterAgent, RouterJobView, RouterPromptAccepted


@dataclass
class RouterState:
    submit_calls: list[dict[str, Any]] = field(default_factory=list)
    accepted: dict[str, RouterPromptAccepted] = field(default_factory=dict)
    jobs: dict[str, RouterJobView] = field(default_factory=dict)
    ambiguous_keys: set[str] = field(default_factory=set)
    cancel_calls: list[str] = field(default_factory=list)


class BearerAuthApp:
    def __init__(self, app: Callable[..., Awaitable[None]], token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", ()))
            if headers.get(b"authorization") != self.expected:
                response = PlainTextResponse("unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


@asynccontextmanager
async def router_mcp_app() -> AsyncIterator[tuple[BearerAuthApp, RouterState]]:
    state = RouterState()
    server = FastMCP(
        "cron-router-test",
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(name="list_agents")
    async def list_agents() -> list[RouterAgent]:
        return [RouterAgent(id="alpha", enabled=True)]

    @server.tool(name="get_agent")
    async def get_agent(agent_id: str) -> RouterAgent:
        return RouterAgent(id=agent_id, enabled=agent_id == "alpha")

    @server.tool(name="submit_prompt")
    async def submit_prompt(
        agent_id: str,
        prompt: str,
        idempotency_key: str,
        conversation_key: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> RouterPromptAccepted:
        arguments = {
            "agent_id": agent_id,
            "prompt": prompt,
            "idempotency_key": idempotency_key,
            "conversation_key": conversation_key,
            "model": model,
            "reasoning_effort": reasoning_effort,
        }
        state.submit_calls.append(arguments)
        accepted = state.accepted.get(idempotency_key)
        if accepted is None:
            number = len(state.accepted) + 1
            accepted = RouterPromptAccepted(
                job_id=f"job-{number}",
                conversation_key=conversation_key or f"conversation-{number}",
                status="queued",
                model=model,
                reasoning_effort=reasoning_effort,
            )
            state.accepted[idempotency_key] = accepted
            state.jobs[accepted.job_id] = RouterJobView(
                id=accepted.job_id,
                agent_id=agent_id,
                conversation_key=accepted.conversation_key,
                status="queued",
                model=model,
                reasoning_effort=reasoning_effort,
            )
        if prompt == "lose response" and idempotency_key not in state.ambiguous_keys:
            state.ambiguous_keys.add(idempotency_key)
            raise RuntimeError("connection lost after acceptance")
        return accepted

    @server.tool(name="get_prompt_status")
    async def get_prompt_status(job_id: str) -> RouterJobView:
        if job_id == "malformed":
            return {"unexpected": True}  # type: ignore[return-value]
        return state.jobs[job_id]

    @server.tool(name="cancel_prompt")
    async def cancel_prompt(job_id: str) -> RouterJobView:
        state.cancel_calls.append(job_id)
        job = state.jobs[job_id].model_copy(
            update={"status": "cancelled", "completed_at": datetime.now(UTC)}
        )
        state.jobs[job_id] = job
        return job

    app = BearerAuthApp(server.streamable_http_app(), "cron-secret")
    async with server.session_manager.run():
        yield app, state


def _client(app: BearerAuthApp, *, token: str = "cron-secret") -> StreamableHTTPRouterClient:
    return StreamableHTTPRouterClient(
        "http://router/mcp",
        token,
        timeout_seconds=2,
        http_transport=httpx.ASGITransport(app=app),
    )


async def test_real_streamable_http_auth_initialization_and_typed_calls() -> None:
    async with router_mcp_app() as (app, state):
        client = _client(app)
        assert await client.check_ready() is True

        agent = await client.get_agent("alpha")
        assert agent == RouterAgent(id="alpha", enabled=True)

        accepted = await client.submit_prompt(
            agent_id="alpha",
            prompt="run",
            conversation_key="conversation-existing",
            idempotency_key="occurrence-1",
            model="gpt-test",
            reasoning_effort="high",
        )
        assert accepted.job_id == "job-1"
        assert accepted.conversation_key == "conversation-existing"
        assert state.submit_calls == [
            {
                "agent_id": "alpha",
                "prompt": "run",
                "idempotency_key": "occurrence-1",
                "conversation_key": "conversation-existing",
                "model": "gpt-test",
                "reasoning_effort": "high",
            }
        ]

        queued = await client.get_prompt_status(accepted.job_id)
        assert queued.status == "queued"
        cancelled = await client.cancel_prompt(accepted.job_id)
        assert cancelled.status == "cancelled"
        assert cancelled.terminal
        assert state.cancel_calls == [accepted.job_id]

        invalid = _client(app, token="wrong-secret")
        assert await invalid.check_ready() is False
        with pytest.raises(RouterMCPError, match="router MCP transport failed"):
            await invalid.get_agent("alpha")


async def test_real_streamable_http_preserves_ambiguous_submission_identity() -> None:
    async with router_mcp_app() as (app, state):
        client = _client(app)
        arguments = {
            "agent_id": "alpha",
            "prompt": "lose response",
            "conversation_key": None,
            "idempotency_key": "occurrence-ambiguous",
            "model": None,
            "reasoning_effort": None,
        }

        with pytest.raises(RouterMCPToolError, match="connection lost after acceptance"):
            await client.submit_prompt(**arguments)
        accepted = await client.submit_prompt(**arguments)

        assert accepted.job_id == "job-1"
        assert len(state.accepted) == 1
        assert [call["idempotency_key"] for call in state.submit_calls] == [
            "occurrence-ambiguous",
            "occurrence-ambiguous",
        ]


async def test_real_streamable_http_rejects_malformed_typed_payload() -> None:
    async with router_mcp_app() as (app, _state):
        with pytest.raises(RouterMCPToolError, match="validation errors for RouterJobView"):
            await _client(app).get_prompt_status("malformed")


async def test_mcp_sdk_legacy_transport_branch(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    observed: dict[str, Any] = {}

    @asynccontextmanager
    async def legacy_transport(
        url: str,
        *,
        headers: dict[str, str],
        timeout: timedelta,
        sse_read_timeout: timedelta,
    ) -> AsyncIterator[tuple[object, object, Callable[[], None]]]:
        observed.update(
            url=url,
            headers=headers,
            timeout=timeout,
            sse_read_timeout=sse_read_timeout,
        )
        yield object(), object(), lambda: None

    class LegacySession:
        def __init__(self, _read: object, _write: object) -> None:
            observed["constructed"] = True

        async def __aenter__(self) -> LegacySession:
            return self

        async def __aexit__(self, *_arguments: object) -> None:
            return None

        async def initialize(self) -> None:
            observed["initialized"] = True

        async def call_tool(self, name: str, *, arguments: dict[str, Any]) -> SimpleNamespace:
            observed["call"] = (name, arguments)
            return SimpleNamespace(
                isError=False,
                content=[],
                structuredContent={"id": "alpha", "enabled": True},
            )

    import mcp.client.streamable_http as streamable_http_module

    monkeypatch.setattr(mcp, "ClientSession", LegacySession)
    monkeypatch.setattr(streamable_http_module, "streamable_http_client", legacy_transport)

    client = StreamableHTTPRouterClient("http://legacy/mcp", "legacy-token", timeout_seconds=3)
    assert await client.get_agent("alpha") == RouterAgent(id="alpha", enabled=True)
    assert observed == {
        "url": "http://legacy/mcp",
        "headers": {"Authorization": "Bearer legacy-token"},
        "timeout": timedelta(seconds=3),
        "sse_read_timeout": timedelta(seconds=3),
        "constructed": True,
        "initialized": True,
        "call": ("get_agent", {"agent_id": "alpha"}),
    }
