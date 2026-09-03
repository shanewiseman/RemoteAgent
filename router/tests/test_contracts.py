from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from remoteagent.app import create_app
from remoteagent.config import Settings
from remoteagent.contracts import contract_documents
from remoteagent.mcp_server import build_mcp
from remoteagent.schemas import AgentSummary, AgentView


@pytest.mark.asyncio
async def test_checked_in_client_contracts_match_implementation() -> None:
    expected = await contract_documents()
    contract_root = Path(__file__).parents[2] / "docs" / "api"

    for name, document in expected.items():
        checked_in = json.loads((contract_root / name).read_text(encoding="utf-8"))
        assert checked_in == document, (
            f"{name} is stale; run `make api-contracts` from the repository root"
        )


@pytest.mark.asyncio
async def test_mcp_contract_has_precise_output_schemas() -> None:
    documents = await contract_documents()
    tools = documents["mcp-tools-list.json"]["tools"]

    assert len(tools) == 20
    assert {tool["name"] for tool in tools} == {
        "list_agents",
        "get_agent",
        "register_agent",
        "update_agent_configuration",
        "submit_prompt",
        "stage_git_repository",
        "get_companion_stage",
        "list_conversation_companions",
        "get_prompt_status",
        "cancel_prompt",
        "list_artifacts",
        "archive_conversation",
        "delete_conversation",
        "configure_cron_schedule",
        "list_cron_schedules",
        "get_cron_schedule",
        "set_cron_schedule_enabled",
        "delete_cron_schedule",
        "retrieve_cron_responses",
        "acknowledge_cron_responses",
    }
    assert all("outputSchema" in tool for tool in tools)
    assert (
        next(tool for tool in tools if tool["name"] == "get_agent")["outputSchema"]["title"]
        == "AgentView"
    )
    configure = next(tool for tool in tools if tool["name"] == "configure_cron_schedule")
    assert configure["outputSchema"]["title"] == "CronScheduleView"
    assert configure["inputSchema"]["properties"]["schedule_id"]["pattern"]
    retrieve = next(tool for tool in tools if tool["name"] == "retrieve_cron_responses")
    assert retrieve["outputSchema"]["title"] == "CronResponseLease"
    submit = next(tool for tool in tools if tool["name"] == "submit_prompt")
    assert submit["inputSchema"]["properties"]["companions"]["maxItems"] == 20
    for name in ("stage_git_repository", "get_companion_stage"):
        companion_tool = next(tool for tool in tools if tool["name"] == name)
        assert companion_tool["outputSchema"]["title"] == "CompanionStageView"
    companion_list = next(
        tool for tool in tools if tool["name"] == "list_conversation_companions"
    )
    assert companion_list["outputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_openapi_contract_has_stable_operations_and_bearer_auth() -> None:
    openapi = (await contract_documents())["openapi.json"]
    operations = [
        operation
        for path in openapi["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "patch", "delete"}
    ]

    assert openapi["openapi"].startswith("3.1.")
    assert openapi["security"] == [{"bearerAuth": []}]
    assert openapi["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert len({operation["operationId"] for operation in operations}) == len(operations)
    assert all("401" in operation["responses"] for operation in operations)


@pytest.mark.asyncio
async def test_runtime_serves_the_checked_in_openapi_contract(tmp_path: Path) -> None:
    expected = (await contract_documents())["openapi.json"]
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        dashboard_enabled=False,
        scheduler_enabled=False,
    )
    app = create_app(settings, runtime=SimpleNamespace(), validate_compose=False)

    assert app.openapi() == expected


@pytest.mark.asyncio
async def test_typed_mcp_results_preserve_standard_structured_shapes() -> None:
    class FakeAgentService:
        async def list(self, *, include_disabled: bool = False) -> list[AgentSummary]:
            assert include_disabled is False
            return [
                AgentSummary(
                    id="alpha",
                    name="Alpha",
                    description="Test agent",
                    enabled=True,
                    revision=1,
                )
            ]

        async def get(self, agent_id: str) -> AgentView:
            assert agent_id == "alpha"
            return AgentView(
                id="alpha",
                name="Alpha",
                description="Test agent",
                enabled=True,
                revision=1,
                compose_file="alpha/compose.yaml",
                project_name="remoteagent-alpha",
                runner_service="agent",
                dependency_services=[],
                environment={},
                labels={},
                metadata={},
                config_toml="",
                base_context="",
            )

    settings = SimpleNamespace(
        mcp_mount_path="/mcp",
        mcp_dns_rebinding_protection=True,
        mcp_allowed_hosts=["testserver"],
        mcp_allowed_origins=[],
    )
    server = build_mcp(
        SimpleNamespace(
            settings=settings,
            agent_service=FakeAgentService(),
            job_service=None,
            artifact_service=None,
        )
    )

    _list_content, list_structured = await server.call_tool("list_agents", {})
    _get_content, get_structured = await server.call_tool("get_agent", {"agent_id": "alpha"})

    assert list_structured == {
        "result": [
            {
                "id": "alpha",
                "name": "Alpha",
                "description": "Test agent",
                "enabled": True,
                "revision": 1,
            }
        ]
    }
    assert get_structured["id"] == "alpha"
    assert get_structured["runner_service"] == "agent"
