from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

from remoteagent.app import create_app
from remoteagent.config import Settings
from remoteagent.cron_client import CronServiceClient, CronServiceError
from remoteagent.mcp_server import build_mcp
from remoteagent.runtime import FakeRuntime
from remoteagent.security import CRON_MCP_ALLOWED_TOOLS
from remoteagent.schemas import (
    CronResponseLeaseRequest,
    CronScheduleConfiguration,
    CronScheduleView,
)


SCHEDULE = {
    "schedule_id": "nightly",
    "generation_id": "11111111-1111-4111-8111-111111111111",
    "status": "enabled",
    "enabled": True,
    "cron_expression": "0 2 * * *",
    "timezone": "UTC",
    "agent_id": "alpha",
    "prompt": "Run the nightly report.",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "high",
    "conversation_mode": "persistent",
    "revision": 1,
    "revision_id": "22222222-2222-4222-8222-222222222222",
    "next_fire_at": "2026-09-03T02:00:00Z",
    "active_execution_id": None,
    "last_execution_id": "33333333-3333-4333-8333-333333333333",
    "pending_response_count": 1,
    "skipped_occurrences": 0,
    "last_failure": None,
    "created_at": "2026-09-02T10:00:00Z",
    "updated_at": "2026-09-02T10:00:00Z",
}

RESPONSE = {
    "response_id": "44444444-4444-4444-8444-444444444444",
    "schedule_id": "nightly",
    "execution_id": "33333333-3333-4333-8333-333333333333",
    "revision_id": "22222222-2222-4222-8222-222222222222",
    "revision": 1,
    "agent_id": "alpha",
    "router_job_id": "j_one",
    "conversation_key": "c_one",
    "scheduled_for": "2026-09-02T02:00:00Z",
    "completed_at": "2026-09-02T02:01:00Z",
    "model": "gpt-5.6-terra",
    "reasoning_effort": "high",
    "usage": {
        "input_tokens": 10,
        "cached_input_tokens": 2,
        "output_tokens": 5,
        "reasoning_output_tokens": 1,
        "future_counter": 3,
    },
    "result": "complete response",
}


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "repository_root": tmp_path,
        "data_dir": tmp_path / "state",
        "agents_root": tmp_path,
        "phonebook_path": tmp_path / "phonebook.toml",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        "bearer_token": "router-secret",
        "cron_mcp_bearer_token": "cron-mcp-secret",
        "cron_api_bearer_token": "cron-api-secret",
        "dashboard_enabled": False,
        "scheduler_enabled": False,
    }
    values.update(overrides)
    return Settings(**values).resolved()


@pytest.mark.asyncio
async def test_cron_client_uses_private_api_and_bearer_auth() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer cron-api-secret"
        path = request.url.path
        if path == "/readyz":
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "database": True,
                    "schema": True,
                    "router_mcp": True,
                    "scheduler": True,
                },
            )
        if path == "/internal/v1/schedules/nightly" and request.method == "DELETE":
            return httpx.Response(200, json={"schedule_id": "nightly", "status": "deleted"})
        if path == "/internal/v1/schedules/nightly/enabled":
            return httpx.Response(200, json={**SCHEDULE, "enabled": False, "status": "disabled"})
        if path == "/internal/v1/schedules/nightly":
            return httpx.Response(200, json=SCHEDULE)
        if path == "/internal/v1/schedules":
            return httpx.Response(200, json=[SCHEDULE])
        if path == "/internal/v1/responses/lease":
            return httpx.Response(
                200,
                json={
                    "lease_id": "55555555-5555-4555-8555-555555555555",
                    "expires_at": "2026-09-02T10:05:00Z",
                    "responses": [RESPONSE],
                    "more_available": False,
                },
            )
        if path == "/internal/v1/responses/acknowledge":
            return httpx.Response(
                200,
                json={
                    "lease_id": "55555555-5555-4555-8555-555555555555",
                    "status": "acknowledged",
                    "deleted_count": 1,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CronServiceClient("http://cron:8090", "cron-api-secret", client=http)
    configuration = CronScheduleConfiguration(
        cron_expression="0 2 * * *",
        agent_id="alpha",
        prompt="Run the nightly report.",
        model="gpt-5.6-terra",
        reasoning_effort="high",
        conversation_mode="persistent",
    )

    assert (await client.readiness())["status"] == "ready"
    assert (await client.configure_schedule("nightly", configuration)).revision == 1
    assert (await client.list_schedules())[0].schedule_id == "nightly"
    assert (await client.get_schedule("nightly")).next_fire_at is not None
    assert not (await client.set_schedule_enabled("nightly", False)).enabled
    assert (await client.delete_schedule("nightly")).status == "deleted"
    lease = await client.lease_responses(CronResponseLeaseRequest(schedule_id="nightly", limit=50))
    assert lease.responses[0].result == "complete response"
    assert lease.responses[0].usage is not None
    assert lease.responses[0].usage.model_extra == {"future_counter": 3}
    acknowledgement = await client.acknowledge_responses("55555555-5555-4555-8555-555555555555")
    assert acknowledgement.deleted_count == 1
    assert len(requests) == 8
    await http.aclose()


@pytest.mark.asyncio
async def test_cron_client_maps_private_api_errors_without_leaking_tokens() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/readyz":
            return httpx.Response(
                503,
                json={
                    "status": "not_ready",
                    "database": True,
                    "schema": True,
                    "router_mcp": False,
                    "scheduler": True,
                    "detail": "router MCP is unavailable",
                },
            )
        return httpx.Response(404, json={"detail": "schedule not found"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = CronServiceClient("http://cron:8090/internal/v1", "secret", client=http)

    readiness = await client.readiness()
    assert readiness["status"] == "not_ready"
    assert readiness["router_mcp"] is False
    with pytest.raises(CronServiceError, match="schedule not found") as error:
        await client.get_schedule("missing")
    assert "secret" not in str(error.value)
    await http.aclose()


@pytest.mark.asyncio
async def test_cron_mcp_tools_validate_agent_and_forward_typed_configuration() -> None:
    class Agents:
        enabled = True

        async def get(self, agent_id: str) -> Any:
            assert agent_id == "alpha"
            return SimpleNamespace(enabled=self.enabled)

    class Cron:
        configuration: CronScheduleConfiguration | None = None

        async def configure_schedule(
            self, schedule_id: str, configuration: CronScheduleConfiguration
        ) -> CronScheduleView:
            assert schedule_id == "nightly"
            self.configuration = configuration
            return CronScheduleView.model_validate(SCHEDULE)

    cron = Cron()
    agents = Agents()
    server = build_mcp(
        SimpleNamespace(
            settings=SimpleNamespace(
                mcp_mount_path="/mcp",
                mcp_dns_rebinding_protection=True,
                mcp_allowed_hosts=["testserver"],
                mcp_allowed_origins=[],
            ),
            agent_service=agents,
            job_service=None,
            artifact_service=None,
            cron_service=cron,
        )
    )

    _content, structured = await server.call_tool(
        "configure_cron_schedule",
        {
            "schedule_id": "nightly",
            "cron_expression": "0 2 * * *",
            "agent_id": "alpha",
            "prompt": "Run the nightly report.",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
            "conversation_mode": "persistent",
        },
    )

    assert structured["schedule_id"] == "nightly"
    assert cron.configuration is not None
    assert cron.configuration.timezone == "UTC"
    assert cron.configuration.conversation_mode == "persistent"

    agents.enabled = False
    with pytest.raises(ToolError, match="agent is disabled"):
        await server.call_tool(
            "configure_cron_schedule",
            {
                "schedule_id": "nightly",
                "cron_expression": "0 2 * * *",
                "agent_id": "alpha",
                "prompt": "Run the nightly report.",
            },
        )


def test_response_lease_lookup_requires_exactly_one_identifier() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        CronResponseLeaseRequest()
    with pytest.raises(ValueError, match="exactly one"):
        CronResponseLeaseRequest(
            schedule_id="nightly",
            execution_id="33333333-3333-4333-8333-333333333333",
        )


def test_scoped_cron_token_is_limited_to_five_mcp_tools(tmp_path: Path) -> None:
    assert CRON_MCP_ALLOWED_TOOLS == {
        "list_agents",
        "get_agent",
        "submit_prompt",
        "get_prompt_status",
        "cancel_prompt",
    }
    app = create_app(_settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    base_headers = {
        "Authorization": "Bearer cron-mcp-secret",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Host": "router:8080",
    }

    with TestClient(app) as client:
        initialized = client.post(
            "/mcp",
            headers=base_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "cron-test", "version": "1"},
                },
            },
        )
        assert initialized.status_code == 200
        headers = {**base_headers, "MCP-Protocol-Version": "2025-06-18"}

        allowed = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_agents", "arguments": {}},
            },
        )
        assert allowed.json()["result"]["isError"] is False

        semantic_rejection = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 21,
                "method": "tools/call",
                "params": {
                    "name": "submit_prompt",
                    "arguments": {
                        "agent_id": "missing",
                        "prompt": "scheduled prompt",
                        "idempotency_key": "generation-and-time",
                    },
                },
            },
        )
        rejection_text = semantic_rejection.json()["result"]["content"][0]["text"]
        assert semantic_rejection.json()["result"]["isError"] is True
        assert (
            'REMOTEAGENT_TOOL_ERROR:{"code":"agent_unavailable","retryable":false,'
            in rejection_text
        )

        companion_binding = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 22,
                "method": "tools/call",
                "params": {
                    "name": "submit_prompt",
                    "arguments": {
                        "agent_id": "missing",
                        "prompt": "scheduled prompt",
                        "companions": [
                            {
                                "stage_id": "cs_11111111111111111111111111111111",
                                "name": "reference",
                            }
                        ],
                    },
                },
            },
        )
        assert companion_binding.json()["result"]["isError"] is True
        assert (
            "cron MCP role cannot submit companion bindings"
            in companion_binding.json()["result"]["content"][0]["text"]
        )

        denied = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "list_cron_schedules", "arguments": {}},
            },
        )
        assert denied.json()["result"]["isError"] is True
        assert "cron MCP role" in denied.json()["result"]["content"][0]["text"]

        administrative = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {
                    "name": "register_agent",
                    "arguments": {
                        "definition": {
                            "id": "beta",
                            "name": "Beta",
                            "compose_file": "beta/compose.yaml",
                        }
                    },
                },
            },
        )
        assert administrative.json()["result"]["isError"] is True
        assert "cron MCP role" in administrative.json()["result"]["content"][0]["text"]

        for request_id, method in enumerate(
            ("resources/list", "resources/templates/list"), start=40
        ):
            discovery = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": request_id, "method": method},
            )
            assert discovery.status_code == 200
            assert "cannot read resources" in discovery.json()["error"]["message"]

        resource = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": "agent://alpha/configuration"},
            },
        )
        assert "cannot read resources" in resource.json()["error"]["message"]

        cron_headers = {"Authorization": "Bearer cron-mcp-secret"}
        assert client.get("/api/v1/agents", headers=cron_headers).status_code == 403
        assert client.get("/metrics", headers=cron_headers).status_code == 403
        assert client.get("/dashboard/", headers=cron_headers).status_code == 403

        router_headers = {
            **headers,
            "Authorization": "Bearer router-secret",
        }
        full_discovery = client.post(
            "/mcp",
            headers=router_headers,
            json={
                "jsonrpc": "2.0",
                "id": 50,
                "method": "resources/templates/list",
            },
        )
        assert full_discovery.status_code == 200
        assert {item["name"] for item in full_discovery.json()["result"]["resourceTemplates"]} == {
            "agent_configuration",
            "agent_artifact",
        }


def test_router_readiness_reports_cron_as_degraded_without_failing(tmp_path: Path) -> None:
    class NotReadyCron:
        async def readiness(self) -> dict[str, Any]:
            return {
                "status": "not_ready",
                "database": True,
                "schema": True,
                "router_mcp": False,
                "scheduler": True,
                "detail": "router MCP is unavailable",
            }

    settings = _settings(tmp_path, cron_api_bearer_token=None)
    app = create_app(settings, runtime=FakeRuntime(), validate_compose=False)

    with TestClient(app) as client:
        response = client.get("/readyz", headers={"Authorization": "Bearer router-secret"})
        app.state.container.cron_service = NotReadyCron()
        diagnostic = client.get("/readyz", headers={"Authorization": "Bearer router-secret"})

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["cron"] == {
        "status": "degraded",
        "detail": "cron service is unavailable",
    }
    assert diagnostic.status_code == 200
    assert diagnostic.json()["cron"] == {
        "status": "degraded",
        "database": True,
        "schema": True,
        "router_mcp": False,
        "scheduler": True,
        "detail": "router MCP is unavailable",
    }


def test_cron_token_files_are_resolved_and_must_be_independent(tmp_path: Path) -> None:
    (tmp_path / "mcp-token").write_text("mcp-secret\n", encoding="utf-8")
    (tmp_path / "api-token").write_text("api-secret\n", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        cron_mcp_bearer_token_file="mcp-token",
        cron_api_bearer_token_file="api-token",
    ).resolved()

    assert settings.cron_mcp_authorization_token() == "mcp-secret"
    assert settings.cron_api_authorization_token() == "api-secret"

    with pytest.raises(ValueError, match="must be independent"):
        create_app(
            _settings(
                tmp_path,
                bearer_token="same-secret",
                cron_mcp_bearer_token="same-secret",
            ),
            runtime=FakeRuntime(),
            validate_compose=False,
        )
