from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from inspect import signature
from typing import Any, AsyncIterator, Protocol

import httpx

from .schemas import RouterAgent, RouterJobView, RouterPromptAccepted

REQUIRED_ROUTER_TOOLS = frozenset(
    {"list_agents", "get_agent", "submit_prompt", "get_prompt_status", "cancel_prompt"}
)


class RouterMCPError(RuntimeError):
    pass


class RouterMCPProtocolError(RouterMCPError):
    pass


class RouterMCPToolError(RouterMCPError):
    """A retryable or unclassified error returned by an MCP tool handler."""


class RouterMCPRejectedError(RouterMCPError):
    """The router completed a tool call and explicitly rejected it."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


_ERROR_MARKER = "REMOTEAGENT_TOOL_ERROR:"
_DEFINITIVE_SUBMISSION_REJECTIONS = frozenset({"agent_unavailable", "conversation_conflict"})


def _semantic_error(message: str, tool_name: str) -> tuple[str, str] | None:
    """Decode a stable error envelope embedded in FastMCP text content.

    FastMCP prefixes exception messages before placing them in an ``isError``
    result. Unknown, malformed, and explicitly retryable envelopes remain
    retryable so a new server-side error can never silently drop an occurrence.
    """

    if tool_name != "submit_prompt":
        return None
    fastmcp_prefix = f"Error executing tool {tool_name}: "
    unwrapped = message.removeprefix(fastmcp_prefix)
    if not unwrapped.startswith(_ERROR_MARKER):
        return None
    encoded = unwrapped[len(_ERROR_MARKER) :].lstrip()
    try:
        payload, _remainder = json.JSONDecoder().raw_decode(encoded)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("code")
    retryable = payload.get("retryable")
    detail = payload.get("message")
    if (
        not isinstance(code, str)
        or code not in _DEFINITIVE_SUBMISSION_REJECTIONS
        or retryable is not False
        or not isinstance(detail, str)
        or not detail.strip()
    ):
        return None
    return code, detail.strip()


class RouterMCPClient(Protocol):
    async def get_agent(self, agent_id: str) -> RouterAgent: ...

    async def submit_prompt(
        self,
        *,
        agent_id: str,
        prompt: str,
        conversation_key: str | None,
        idempotency_key: str,
        model: str | None,
        reasoning_effort: str | None,
    ) -> RouterPromptAccepted: ...

    async def get_prompt_status(self, job_id: str) -> RouterJobView: ...

    async def cancel_prompt(self, job_id: str) -> RouterJobView: ...

    async def check_ready(self) -> bool: ...

    async def close(self) -> None: ...


class StreamableHTTPRouterClient:
    """Small typed wrapper around the router's real Streamable HTTP MCP endpoint."""

    def __init__(self, url: str, token: str, *, timeout_seconds: float = 30) -> None:
        self.url = url
        self.token = token
        self.timeout = timedelta(seconds=timeout_seconds)

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[Any]:
        # Imports stay local so schema, migration and unit-test commands do not
        # initialize the MCP networking stack.
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            async with AsyncExitStack() as stack:
                if "http_client" in signature(streamable_http_client).parameters:
                    http_client = await stack.enter_async_context(
                        httpx.AsyncClient(
                            headers=headers,
                            timeout=self.timeout.total_seconds(),
                            follow_redirects=False,
                        )
                    )
                    transport = streamable_http_client(self.url, http_client=http_client)
                else:  # MCP SDK 1.12 compatibility
                    transport = streamable_http_client(
                        self.url,
                        headers=headers,
                        timeout=self.timeout,
                        sse_read_timeout=self.timeout,
                    )
                streams = await stack.enter_async_context(transport)
                read_stream, write_stream = streams[0], streams[1]
                session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
                await session.initialize()
                yield session
        except RouterMCPError:
            raise
        except Exception as exc:
            raise RouterMCPError(f"router MCP transport failed: {exc}") from exc

    @staticmethod
    def _structured(result: Any, tool_name: str) -> Mapping[str, Any]:
        if bool(getattr(result, "isError", False)):
            messages = [getattr(item, "text", "") for item in getattr(result, "content", ())]
            message = " ".join(filter(None, messages)).strip()
            semantic = _semantic_error(message, tool_name)
            if semantic is not None:
                code, detail = semantic
                raise RouterMCPRejectedError(code, detail)
            raise RouterMCPToolError(
                f"router MCP tool {tool_name} failed: {message or 'unclassified tool error'}"
            )
        structured = getattr(result, "structuredContent", None)
        if not isinstance(structured, Mapping):
            raise RouterMCPProtocolError(
                f"router MCP tool {tool_name} did not return structuredContent"
            )
        return structured

    async def _call(self, name: str, arguments: dict[str, Any]) -> Mapping[str, Any]:
        async with self._session() as session:
            result = await session.call_tool(name, arguments=arguments)
        return self._structured(result, name)

    async def get_agent(self, agent_id: str) -> RouterAgent:
        return RouterAgent.model_validate(await self._call("get_agent", {"agent_id": agent_id}))

    async def submit_prompt(
        self,
        *,
        agent_id: str,
        prompt: str,
        conversation_key: str | None,
        idempotency_key: str,
        model: str | None,
        reasoning_effort: str | None,
    ) -> RouterPromptAccepted:
        arguments: dict[str, Any] = {
            "agent_id": agent_id,
            "prompt": prompt,
            "idempotency_key": idempotency_key,
        }
        if conversation_key is not None:
            arguments["conversation_key"] = conversation_key
        if model is not None:
            arguments["model"] = model
        if reasoning_effort is not None:
            arguments["reasoning_effort"] = reasoning_effort
        return RouterPromptAccepted.model_validate(await self._call("submit_prompt", arguments))

    async def get_prompt_status(self, job_id: str) -> RouterJobView:
        return RouterJobView.model_validate(
            await self._call("get_prompt_status", {"job_id": job_id})
        )

    async def cancel_prompt(self, job_id: str) -> RouterJobView:
        return RouterJobView.model_validate(await self._call("cancel_prompt", {"job_id": job_id}))

    async def check_ready(self) -> bool:
        try:
            async with self._session() as session:
                result = await session.list_tools()
            names = {tool.name for tool in result.tools}
            return REQUIRED_ROUTER_TOOLS <= names
        except Exception:
            return False

    async def close(self) -> None:
        return None
