from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx

from remoteagent_cron.app import create_app
from remoteagent_cron.config import Settings
from remoteagent_cron.db import initialize_schema
from remoteagent_cron.mcp_client import (
    RouterMCPProtocolError,
    RouterMCPRejectedError,
    RouterMCPToolError,
    StreamableHTTPRouterClient,
)


async def test_internal_api_auth_liveness_and_crud(tmp_path, router, schedule_body) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path}/api.db",
        internal_bearer_token="secret",
        scheduler_enabled=False,
        initialize_schema=True,
    )
    app = create_app(settings, router_client=router)
    await initialize_schema(app.state.container.engine)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://cron") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/internal/v1/schedules")).status_code == 401
        assert (await client.get("/readyz")).status_code == 401
        headers = {"Authorization": "Bearer secret"}
        configured = await client.put(
            "/internal/v1/schedules/api-test",
            headers=headers,
            json=schedule_body.model_dump(mode="json"),
        )
        assert configured.status_code == 200, configured.text
        assert configured.json()["revision"] == 1
        listed = await client.get("/internal/v1/schedules", headers=headers)
        assert [item["schedule_id"] for item in listed.json()] == ["api-test"]
        ready = await client.get("/readyz", headers=headers)
        assert ready.status_code == 200
        assert ready.json()["status"] == "ready"
        router.ready = False
        degraded = await client.get("/readyz", headers=headers)
        assert degraded.status_code == 503
        assert degraded.json()["status"] == "not_ready"
        assert degraded.json()["schema"] is True
        assert degraded.json()["router_mcp"] is False
    await app.state.container.engine.dispose()


def test_openapi_contract_is_checked_in_and_current() -> None:
    package_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(package_root / "scripts" / "export_contract.py"), "--check"],
        cwd=package_root.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    contract = json.loads((package_root / "openapi.json").read_text(encoding="utf-8"))
    assert set(contract["paths"]) == {
        "/internal/v1/schedules/{schedule_id}",
        "/internal/v1/schedules",
        "/internal/v1/schedules/{schedule_id}/enabled",
        "/internal/v1/responses/lease",
        "/internal/v1/responses/acknowledge",
    }
    assert contract["components"]["securitySchemes"]["CronBearerAuth"]["scheme"] == "bearer"
    assert contract["security"] == [{"CronBearerAuth": []}]
    assert all(
        operation["security"] == [{"CronBearerAuth": []}]
        for path in contract["paths"].values()
        for operation in path.values()
    )


def test_mcp_client_requires_structured_content() -> None:
    class Result:
        isError = False
        content = []
        structuredContent = None

    try:
        StreamableHTTPRouterClient._structured(Result(), "get_agent")
    except RouterMCPProtocolError as exc:
        assert "structuredContent" in str(exc)
    else:
        raise AssertionError("missing structuredContent was accepted")


def test_mcp_tool_internal_error_is_retryable() -> None:
    class Text:
        text = "Error executing tool submit_prompt: database temporarily unavailable"

    class Result:
        isError = True
        content = [Text()]
        structuredContent = None

    try:
        StreamableHTTPRouterClient._structured(Result(), "submit_prompt")
    except RouterMCPToolError as exc:
        assert not isinstance(exc, RouterMCPRejectedError)
        assert "database temporarily unavailable" in str(exc)
    else:
        raise AssertionError("transient MCP tool error was accepted")


def test_mcp_tool_known_semantic_error_has_stable_code() -> None:
    class Text:
        text = (
            "Error executing tool submit_prompt: REMOTEAGENT_TOOL_ERROR:"
            '{"code":"agent_unavailable","retryable":false,'
            '"message":"agent is disabled"}'
        )

    class Result:
        isError = True
        content = [Text()]
        structuredContent = None

    try:
        StreamableHTTPRouterClient._structured(Result(), "submit_prompt")
    except RouterMCPRejectedError as exc:
        assert exc.code == "agent_unavailable"
        assert str(exc) == "agent is disabled"
    else:
        raise AssertionError("semantic MCP tool rejection was not classified")


def test_unknown_or_malformed_error_envelope_remains_retryable() -> None:
    class Text:
        text = (
            'REMOTEAGENT_TOOL_ERROR:{"code":"unexpected_internal_code",'
            '"retryable":false,"message":"do not trust unknown codes"}'
        )

    class Result:
        isError = True
        content = [Text()]
        structuredContent = None

    try:
        StreamableHTTPRouterClient._structured(Result(), "submit_prompt")
    except RouterMCPToolError as exc:
        assert not isinstance(exc, RouterMCPRejectedError)
    else:
        raise AssertionError("unknown MCP tool error code was treated as semantic")
