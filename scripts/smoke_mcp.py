#!/usr/bin/env python3
"""Read-only RemoteAgent MCP smoke; requires Python 3.12+, mcp, httpx, jsonschema."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
from contextlib import AsyncExitStack
from datetime import timedelta
from inspect import signature
from pathlib import Path
import re
import stat
import sys
from typing import Any
from urllib.parse import urlsplit


class SmokeFailure(RuntimeError):
    """A safe diagnostic containing no connection or server-provided content."""


def read_connection(path: Path) -> tuple[str, str]:
    """Read a bounded, current-user-owned file without following its final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise SmokeFailure("connection file must be an owner-only regular file")
        raw = handle.read(16_385)
    if len(raw) > 16_384:
        raise SmokeFailure("connection file exceeds 16 KiB")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SmokeFailure("connection file is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"url", "bearer_token"}:
        raise SmokeFailure("connection file must contain only url and bearer_token")
    url, token = value["url"], value["bearer_token"]
    if not isinstance(url, str) or not isinstance(token, str):
        raise SmokeFailure("connection values must be strings")
    parts = urlsplit(url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise SmokeFailure(
            "url must be HTTP(S), without credentials, query, or fragment"
        )
    if not token or any(not 33 <= ord(character) <= 126 for character in token):
        raise SmokeFailure(
            "bearer_token must contain only visible ASCII without spaces"
        )
    return url, token


async def probe(
    url: str,
    token: str,
    agent_ids: list[str],
    timeout: float,
    *,
    http_transport: Any = None,
) -> dict[str, int]:
    import httpx
    from jsonschema import Draft202012Validator
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    contracts = Path(__file__).resolve().parents[1] / "docs" / "api"
    expected_tools = {
        tool["name"]: tool
        for tool in json.loads((contracts / "mcp-tools-list.json").read_text())["tools"]
    }
    expected_templates = {
        item["uriTemplate"]
        for item in json.loads(
            (contracts / "mcp-resource-templates-list.json").read_text()
        )["resourceTemplates"]
    }
    headers = {"Authorization": f"Bearer {token}"}
    async with asyncio.timeout(timeout), AsyncExitStack() as stack:
        if "http_client" in signature(streamable_http_client).parameters:
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                    transport=http_transport,
                )
            )
            transport = streamable_http_client(url, http_client=client)
        else:  # Same compatibility branch as the cron client (MCP SDK 1.12).
            if http_transport is not None:
                raise SmokeFailure("in-process transport requires a newer MCP SDK")
            transport = streamable_http_client(
                url,
                headers=headers,
                timeout=timedelta(seconds=timeout),
                sse_read_timeout=timedelta(seconds=timeout),
            )
        streams = await stack.enter_async_context(transport)
        session = await stack.enter_async_context(ClientSession(streams[0], streams[1]))
        await session.initialize()
        discovered = await session.list_tools()
        if discovered.nextCursor is not None:
            raise SmokeFailure("unexpected paginated tool discovery")
        tools = {tool.name: tool for tool in discovered.tools}
        if len(tools) != len(discovered.tools) or set(tools) != set(expected_tools):
            raise SmokeFailure("tool discovery differs from checked-in contract")
        for name, expected in expected_tools.items():
            if (
                tools[name].inputSchema != expected["inputSchema"]
                or tools[name].outputSchema != expected["outputSchema"]
            ):
                raise SmokeFailure(
                    "discovered tool schema differs from checked-in contract"
                )
        templates = await session.list_resource_templates()
        uris = {item.uriTemplate for item in templates.resourceTemplates}
        if (
            templates.nextCursor is not None
            or len(uris) != len(templates.resourceTemplates)
            or uris != expected_templates
        ):
            raise SmokeFailure("resource templates differ from checked-in contract")

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            result = await session.call_tool(name, arguments=arguments)
            if result.isError:
                raise SmokeFailure("read-only MCP tool returned an error")
            body = result.structuredContent
            validator = Draft202012Validator(expected_tools[name]["outputSchema"])
            if not isinstance(body, dict) or not validator.is_valid(body):
                raise SmokeFailure("MCP structured result violates checked-in schema")
            return body

        listed = (await call("list_agents", {}))["result"]
        agents = {agent["id"]: agent for agent in listed}
        if len(agents) != len(listed):
            raise SmokeFailure("list_agents returned duplicate identities")
        for agent_id in agent_ids:
            summary = agents.get(agent_id)
            if summary is None or summary["enabled"] is not True:
                raise SmokeFailure("an expected enabled agent is absent")
            detail = await call("get_agent", {"agent_id": agent_id})
            if any(detail[field] != summary[field] for field in summary):
                raise SmokeFailure("agent detail disagrees with discovery")
        return {"tools": len(tools), "templates": len(uris), "agents": len(agent_ids)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify MCP initialization, discovery, and enabled built-in agents; no jobs or writes.",
        epilog=(
            "Run from a Python environment with mcp>=1.12,<2, httpx, and jsonschema: "
            ".venv/bin/python scripts/smoke_mcp.py --connection-file /private/mcp.json. "
            "The current-user-owned file must have mode 0600 (or 0400) and contain "
            '{"url":"http://HOST:8080/mcp","bearer_token":"TOKEN"}. '
            "Keep it outside the repository. Tokens, URLs, and response contents are never printed. "
            "The checked-in docs/api contracts must be present alongside this script."
        ),
    )
    parser.add_argument("--connection-file", type=Path, required=True)
    parser.add_argument(
        "--agent",
        action="append",
        help="expected enabled ID; repeat to replace both built-ins",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="whole-probe timeout in seconds (default: 30)",
    )
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a positive finite number")
    agents = args.agent or ["joke-agent", "repository-critic"]
    if len(set(agents)) != len(agents) or any(
        re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", agent) is None for agent in agents
    ):
        parser.error("--agent values must be unique lowercase agent IDs")
    # Suppress third-party diagnostics that could echo headers or response bodies.
    logging.disable(logging.CRITICAL)
    try:
        url, token = read_connection(args.connection_file)
        counts = asyncio.run(probe(url, token, agents, args.timeout))
    except SmokeFailure as exc:
        print(f"MCP smoke failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Never render third-party exception messages, including ExceptionGroups.
        print(
            f"MCP smoke failed ({type(exc).__name__}); check connection, SDK, and server health",
            file=sys.stderr,
        )
        return 1
    print(
        f"MCP smoke passed: initialized; {counts['tools']} tools; "
        f"{counts['templates']} resource templates; {counts['agents']} expected enabled agents; no jobs submitted"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
