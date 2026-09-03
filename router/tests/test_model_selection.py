from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import func, select, update

from remoteagent.agents import AgentNotFoundError, AgentService
from remoteagent.app import create_app
from remoteagent.cache import MemoryCache
from remoteagent.config import Settings
from remoteagent.contracts import contract_documents
from remoteagent.dashboard.data import _conversation_view, _job_view as _dashboard_job_view
from remoteagent.db import create_engine, create_session_factory, initialize_schema
from remoteagent.jobs import ConversationConflictError, JobService
from remoteagent.mcp_server import build_mcp
from remoteagent.models import AgentRecord, ConversationRecord, JobRecord
from remoteagent.runtime import DockerComposeRuntime, FakeRuntime, RuntimeRequest
from remoteagent.schemas import (
    AgentDefinition,
    JobStatus,
    PromptAccepted,
    PromptRequest,
    ReasoningEffort,
    RevisionUpdate,
)
from remoteagent.workspace import WorkspaceManager


REASONING_EFFORTS = {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}


def _make_agent(
    root: Path,
    *,
    agent_id: str = "alpha",
    config_toml: str = "",
) -> AgentDefinition:
    directory = root / agent_id
    directory.mkdir(parents=True, exist_ok=True)
    compose = directory / "compose.yaml"
    compose.write_text("services:\n  agent:\n    image: example.invalid/agent\n")
    return AgentDefinition(
        id=agent_id,
        name=agent_id.title(),
        compose_file=compose,
        runner_service="agent",
        config_toml=config_toml,
        base_context="Be exact.",
    )


@dataclass(slots=True)
class _Services:
    agents: AgentService
    jobs: JobService
    sessions: object


@asynccontextmanager
async def _services(
    root: Path,
    *,
    config_toml: str = "",
) -> AsyncIterator[_Services]:
    engine = create_engine(f"sqlite+aiosqlite:///{root / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    agents = AgentService(sessions, root)
    await agents.register(_make_agent(root, config_toml=config_toml))
    jobs = JobService(sessions, WorkspaceManager(root / "state"), MemoryCache())
    try:
        yield _Services(agents=agents, jobs=jobs, sessions=sessions)
    finally:
        await engine.dispose()


def _all_enums(schema: object) -> set[str]:
    if isinstance(schema, dict):
        values = set(schema.get("enum", []))
        for value in schema.values():
            values.update(_all_enums(value))
        return values
    if isinstance(schema, list):
        values: set[str] = set()
        for value in schema:
            values.update(_all_enums(value))
        return values
    return set()


def test_prompt_request_validates_model_slug_and_reasoning_effort() -> None:
    request = PromptRequest(
        agent_id="alpha",
        prompt="hello",
        model="gpt-5.6-terra",
        reasoning_effort="ultra",
    )
    assert request.model == "gpt-5.6-terra"
    assert request.reasoning_effort == "ultra"

    for model in ("", "GPT-5", " gpt-5", "gpt/5", "gpt-5 ", "a" * 129):
        with pytest.raises(ValueError, match="model"):
            PromptRequest(agent_id="alpha", prompt="hello", model=model)
    with pytest.raises(ValueError, match="reasoning"):
        PromptRequest(agent_id="alpha", prompt="hello", reasoning_effort="none")


@pytest.mark.parametrize(
    ("config_toml", "message"),
    [
        ('model = "GPT-5"\n', "model"),
        ("model = 5\n", "model"),
        ('model_reasoning_effort = "none"\n', "model_reasoning_effort"),
        ("model_reasoning_effort = 5\n", "model_reasoning_effort"),
    ],
)
def test_agent_configuration_validates_execution_profile_values(
    tmp_path: Path,
    config_toml: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _make_agent(tmp_path, config_toml=config_toml)
    with pytest.raises(ValueError, match=message):
        RevisionUpdate(config_toml=config_toml)


@pytest.mark.asyncio
async def test_openapi_and_mcp_contracts_expose_optional_model_selection() -> None:
    documents = await contract_documents()
    openapi = documents["openapi.json"]
    prompt_schema = openapi["components"]["schemas"]["PromptRequest"]
    accepted_schema = openapi["components"]["schemas"]["PromptAccepted"]
    job_schema = openapi["components"]["schemas"]["JobView"]

    for schema in (prompt_schema, accepted_schema, job_schema):
        assert {"model", "reasoning_effort"} <= set(schema["properties"])
    assert not {"model", "reasoning_effort"} & set(prompt_schema["required"])
    assert {"model", "reasoning_effort"} <= set(accepted_schema["required"])
    assert {"model", "reasoning_effort"} <= set(job_schema["required"])
    assert set(openapi["components"]["schemas"]["ReasoningEffort"]["enum"]) == (REASONING_EFFORTS)

    tools = documents["mcp-tools-list.json"]["tools"]
    submit = next(tool for tool in tools if tool["name"] == "submit_prompt")
    properties = submit["inputSchema"]["properties"]
    assert {"model", "reasoning_effort"} <= set(properties)
    assert not {"model", "reasoning_effort"} & set(submit["inputSchema"]["required"])
    assert REASONING_EFFORTS <= _all_enums(submit["inputSchema"])
    assert {"model", "reasoning_effort"} <= set(submit["outputSchema"]["properties"])


@pytest.mark.asyncio
async def test_mcp_submit_prompt_forwards_and_returns_model_selection() -> None:
    class CapturingJobs:
        request: PromptRequest | None = None

        async def submit(self, request: PromptRequest) -> PromptAccepted:
            self.request = request
            return PromptAccepted(
                job_id="j_one",
                conversation_key="c_one",
                status="queued",
                model=request.model,
                reasoning_effort=request.reasoning_effort,
            )

    jobs = CapturingJobs()
    settings = SimpleNamespace(
        mcp_mount_path="/mcp",
        mcp_dns_rebinding_protection=True,
        mcp_allowed_hosts=["testserver"],
        mcp_allowed_origins=[],
    )
    server = build_mcp(
        SimpleNamespace(
            settings=settings,
            agent_service=None,
            job_service=jobs,
            artifact_service=None,
        )
    )

    _content, structured = await server.call_tool(
        "submit_prompt",
        {
            "agent_id": "alpha",
            "prompt": "hello",
            "model": "gpt-5.6-terra",
            "reasoning_effort": "high",
        },
    )

    assert jobs.request is not None
    assert jobs.request.model == "gpt-5.6-terra"
    assert jobs.request.reasoning_effort == "high"
    assert structured["model"] == "gpt-5.6-terra"
    assert structured["reasoning_effort"] == "high"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (ConversationConflictError("conversation is not active"), "conversation_conflict"),
        (RuntimeError("database unavailable"), None),
    ],
)
async def test_mcp_submit_prompt_marks_only_semantic_rejections(
    failure: Exception,
    expected_code: str | None,
) -> None:
    class RejectingJobs:
        async def submit(self, _request: PromptRequest) -> PromptAccepted:
            raise failure

    server = build_mcp(
        SimpleNamespace(
            settings=SimpleNamespace(
                mcp_mount_path="/mcp",
                mcp_dns_rebinding_protection=True,
                mcp_allowed_hosts=["testserver"],
                mcp_allowed_origins=[],
            ),
            agent_service=None,
            job_service=RejectingJobs(),
            artifact_service=None,
            cron_service=None,
        )
    )

    with pytest.raises(ToolError) as error:
        await server.call_tool(
            "submit_prompt",
            {"agent_id": "alpha", "prompt": "scheduled prompt"},
        )

    if expected_code is None:
        assert "REMOTEAGENT_TOOL_ERROR:" not in str(error.value)
        assert "database unavailable" in str(error.value)
    else:
        marker = str(error.value).split("REMOTEAGENT_TOOL_ERROR:", 1)[1]
        payload = json.loads(marker)
        assert payload == {
            "code": expected_code,
            "retryable": False,
            "message": "conversation is not active",
        }


def test_rest_submit_and_poll_expose_profile_and_conflicts(tmp_path: Path) -> None:
    settings = Settings(
        environment="test",
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'rest.db'}",
        bearer_token="test-secret",
        dashboard_enabled=False,
        scheduler_enabled=False,
        mcp_allowed_hosts=["testserver"],
    ).resolved()
    app = create_app(settings, runtime=FakeRuntime(), validate_compose=False)
    headers = {"Authorization": "Bearer test-secret"}
    definition = _make_agent(tmp_path)

    with TestClient(app) as client:
        registered = client.post(
            "/api/v1/agents",
            headers=headers,
            json={"definition": definition.model_dump(mode="json")},
        )
        assert registered.status_code == 201

        submitted = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "agent_id": "alpha",
                "prompt": "hello",
                "model": "gpt-5.6-terra",
                "reasoning_effort": "high",
            },
        )
        assert submitted.status_code == 202
        accepted = submitted.json()
        assert accepted["model"] == "gpt-5.6-terra"
        assert accepted["reasoning_effort"] == "high"

        polled = client.get(f"/api/v1/jobs/{accepted['job_id']}", headers=headers)
        assert polled.status_code == 200
        assert polled.json()["model"] == "gpt-5.6-terra"
        assert polled.json()["reasoning_effort"] == "high"

        conflict = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "agent_id": "alpha",
                "prompt": "continue",
                "conversation_key": accepted["conversation_key"],
                "model": "gpt-5.6-sol",
            },
        )
        assert conflict.status_code == 409
        assert "model" in conflict.json()["detail"].lower()


@pytest.mark.asyncio
async def test_new_conversation_resolves_each_field_from_request_then_agent_config(
    tmp_path: Path,
) -> None:
    config = 'model = "gpt-config"\nmodel_reasoning_effort = "medium"\n'
    async with _services(tmp_path, config_toml=config) as services:
        cases = [
            ({}, "gpt-config", "medium"),
            ({"model": "gpt-request"}, "gpt-request", "medium"),
            ({"reasoning_effort": "high"}, "gpt-config", "high"),
            (
                {"model": "gpt-request-both", "reasoning_effort": "xhigh"},
                "gpt-request-both",
                "xhigh",
            ),
        ]
        for index, (overrides, expected_model, expected_effort) in enumerate(cases, start=1):
            accepted = await services.jobs.submit(
                PromptRequest(
                    agent_id="alpha",
                    prompt=f"turn {index}",
                    conversation_key=f"c_case_{index}",
                    **overrides,
                )
            )
            assert accepted.model == expected_model
            assert accepted.reasoning_effort == expected_effort
            view = await services.jobs.get(accepted.job_id)
            assert view.model == expected_model
            assert view.reasoning_effort == expected_effort
            async with services.sessions() as session:  # type: ignore[operator]
                conversation = await session.get(ConversationRecord, accepted.conversation_key)
                job = await session.get(JobRecord, accepted.job_id)
                assert conversation is not None
                assert job is not None
                assert conversation.model == expected_model
                assert conversation.reasoning_effort == expected_effort
                assert job.model == expected_model
                assert job.reasoning_effort == expected_effort


@pytest.mark.asyncio
async def test_omitted_legacy_request_keeps_codex_inherited_defaults(tmp_path: Path) -> None:
    async with _services(tmp_path) as services:
        request = PromptRequest(agent_id="alpha", prompt="legacy payload")
        accepted = await services.jobs.submit(request)
        assert accepted.model is None
        assert accepted.reasoning_effort is None
        view = await services.jobs.get(accepted.job_id)
        assert view.model is None
        assert view.reasoning_effort is None

        execution = await services.jobs.claim_next()
        assert execution is not None
        assert execution.model is None
        assert execution.reasoning_effort is None


@pytest.mark.asyncio
async def test_conversation_profile_is_sticky_across_revisions_and_conflicts(
    tmp_path: Path,
) -> None:
    initial_config = 'model = "gpt-config-one"\nmodel_reasoning_effort = "medium"\n'
    async with _services(tmp_path, config_toml=initial_config) as services:
        first = await services.jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="one",
                model="gpt-conversation",
                reasoning_effort="high",
            )
        )
        await services.agents.update_revision(
            "alpha",
            RevisionUpdate(
                config_toml=('model = "gpt-config-two"\nmodel_reasoning_effort = "minimal"\n'),
                base_context=None,
            ),
        )

        omitted = await services.jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="two",
                conversation_key=first.conversation_key,
            )
        )
        matching = await services.jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="three",
                conversation_key=first.conversation_key,
                model="gpt-conversation",
                reasoning_effort="high",
            )
        )
        for accepted in (omitted, matching):
            assert accepted.model == "gpt-conversation"
            assert accepted.reasoning_effort == "high"
            view = await services.jobs.get(accepted.job_id)
            assert view.model == "gpt-conversation"
            assert view.reasoning_effort == "high"

        with pytest.raises(ConversationConflictError, match="model"):
            await services.jobs.submit(
                PromptRequest(
                    agent_id="alpha",
                    prompt="wrong model",
                    conversation_key=first.conversation_key,
                    model="gpt-other",
                )
            )
        with pytest.raises(ConversationConflictError, match="reasoning"):
            await services.jobs.submit(
                PromptRequest(
                    agent_id="alpha",
                    prompt="wrong effort",
                    conversation_key=first.conversation_key,
                    reasoning_effort="low",
                )
            )

        async with services.sessions() as session:  # type: ignore[operator]
            count = await session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(JobRecord.conversation_key == first.conversation_key)
            )
            assert count == 3


@pytest.mark.asyncio
async def test_idempotency_replay_always_returns_the_original_profile(
    tmp_path: Path,
) -> None:
    async with _services(tmp_path) as services:
        original = await services.jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="same prompt",
                idempotency_key="retry-key",
                model="gpt-original",
                reasoning_effort="high",
            )
        )

        replays = [
            PromptRequest(
                agent_id="alpha",
                prompt="same prompt",
                idempotency_key="retry-key",
            ),
            PromptRequest(
                agent_id="alpha",
                prompt="same prompt",
                idempotency_key="retry-key",
                conversation_key=original.conversation_key,
                model="gpt-original",
                reasoning_effort="high",
            ),
            PromptRequest(
                agent_id="alpha",
                prompt="different prompt",
                idempotency_key="retry-key",
                conversation_key=original.conversation_key,
                model="gpt-original",
                reasoning_effort="high",
            ),
            PromptRequest(
                agent_id="alpha",
                prompt="same prompt",
                idempotency_key="retry-key",
                conversation_key="c_different",
                model="gpt-original",
                reasoning_effort="high",
            ),
            PromptRequest(
                agent_id="alpha",
                prompt="same prompt",
                idempotency_key="retry-key",
                conversation_key=original.conversation_key,
                model="gpt-other",
                reasoning_effort="high",
            ),
            PromptRequest(
                agent_id="alpha",
                prompt="same prompt",
                idempotency_key="retry-key",
                conversation_key=original.conversation_key,
                model="gpt-original",
                reasoning_effort="low",
            ),
        ]
        for replay in replays:
            repeated = await services.jobs.submit(replay)
            assert repeated == original
            assert repeated.model == "gpt-original"
            assert repeated.reasoning_effort == "high"

        async with services.sessions() as session:  # type: ignore[operator]
            count = await session.scalar(select(func.count()).select_from(JobRecord))
            assert count == 1
            stored = await session.get(JobRecord, original.job_id)
            assert stored is not None
            assert stored.model == "gpt-original"
            assert stored.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_idempotency_replay_survives_agent_becoming_disabled(
    tmp_path: Path,
) -> None:
    async with _services(tmp_path) as services:
        original = await services.jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="scheduled prompt",
                idempotency_key="cron-generation-and-time",
            )
        )
        async with services.sessions() as session, session.begin():  # type: ignore[operator]
            await session.execute(
                update(AgentRecord).where(AgentRecord.id == "alpha").values(enabled=False)
            )

        replay = await services.jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="scheduled prompt",
                idempotency_key="cron-generation-and-time",
            )
        )

        assert replay == original
        with pytest.raises(AgentNotFoundError):
            await services.jobs.submit(
                PromptRequest(
                    agent_id="alpha",
                    prompt="new scheduled prompt",
                    idempotency_key="new-cron-occurrence",
                )
            )


@pytest.mark.asyncio
async def test_idempotency_replay_recovers_orphaned_job_after_agent_deletion(
    tmp_path: Path,
) -> None:
    database = tmp_path / "deleted-agent-replay.db"
    database_url = f"sqlite+aiosqlite:///{database}"
    engine = create_engine(database_url)
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    agents = AgentService(sessions, tmp_path)
    await agents.register(_make_agent(tmp_path))
    jobs = JobService(sessions, WorkspaceManager(tmp_path / "state"), MemoryCache())
    original = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="scheduled prompt",
            idempotency_key="cron-generation-and-time",
        )
    )
    for status in (
        JobStatus.PROVISIONING,
        JobStatus.WAITING_FOR_LEASE,
        JobStatus.RUNNING,
        JobStatus.COLLECTING,
    ):
        await jobs.transition(original.job_id, status)
    await jobs.transition(
        original.job_id,
        JobStatus.SUCCEEDED,
        result="durable scheduled response",
    )
    await engine.dispose()

    # Simulate imported/recovered state from an older deployment where the
    # durable job outlives its current agent registry row. Production foreign
    # keys prevent this through normal router operations.
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM agents WHERE id = ?", ("alpha",))

    recovered_engine = create_engine(database_url)
    recovered_sessions = create_session_factory(recovered_engine)
    recovered_jobs = JobService(
        recovered_sessions,
        WorkspaceManager(tmp_path / "recovered-state"),
        MemoryCache(),
    )
    try:
        replay = await recovered_jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="scheduled prompt",
                idempotency_key="cron-generation-and-time",
            )
        )
        completed = await recovered_jobs.get(replay.job_id)

        assert replay.job_id == original.job_id
        assert replay.status == JobStatus.SUCCEEDED
        assert completed.result == "durable scheduled response"
        with pytest.raises(AgentNotFoundError):
            await recovered_jobs.submit(
                PromptRequest(
                    agent_id="alpha",
                    prompt="new scheduled prompt",
                    idempotency_key="new-cron-occurrence",
                )
            )
    finally:
        await recovered_engine.dispose()


@pytest.mark.parametrize("thread_id", [None, "11111111-1111-4111-8111-111111111111"])
def test_runtime_argv_forces_profile_for_new_and_resumed_turns(
    tmp_path: Path,
    thread_id: str | None,
) -> None:
    settings = Settings(
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
    ).resolved()
    definition = _make_agent(
        tmp_path,
        config_toml='model = "gpt-config"\nmodel_reasoning_effort = "minimal"\n',
    )
    paths = WorkspaceManager(settings.data_dir).ensure_conversation("c_runtime")
    request = RuntimeRequest(
        job_id="j_runtime",
        conversation_key="c_runtime",
        prompt="hello",
        thread_id=thread_id,
        model="gpt-request",
        reasoning_effort=ReasoningEffort.HIGH,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_runtime"),
    )

    argv = DockerComposeRuntime(settings).codex_argv(request)
    assert argv.count("--model") == 1
    assert argv[argv.index("--model") + 1] == "gpt-request"
    assert 'model_reasoning_effort="high"' in argv
    assert "gpt-config" not in argv
    assert 'model_reasoning_effort="minimal"' not in argv
    if thread_id is None:
        assert argv[:2] == ["codex", "exec"]
        assert "resume" not in argv
    else:
        assert argv[:3] == ["codex", "exec", "resume"]
        assert argv[-2:] == [thread_id, "-"]


def test_runtime_argv_omits_profile_when_codex_should_inherit_defaults(tmp_path: Path) -> None:
    settings = Settings(
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
    ).resolved()
    definition = _make_agent(tmp_path)
    paths = WorkspaceManager(settings.data_dir).ensure_conversation("c_default")
    request = RuntimeRequest(
        job_id="j_default",
        conversation_key="c_default",
        prompt="hello",
        thread_id=None,
        model=None,
        reasoning_effort=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_default"),
    )

    argv = DockerComposeRuntime(settings).codex_argv(request)
    assert "--model" not in argv
    assert not any(value.startswith("model_reasoning_effort=") for value in argv)


def test_dashboard_projections_expose_conversation_and_job_profiles() -> None:
    now = datetime.now(UTC)
    conversation = ConversationRecord(
        key="c_dashboard",
        agent_id="alpha",
        codex_thread_id=None,
        workspace_path="workspace",
        codex_home_path="sessions",
        artifact_path="artifacts",
        agent_revision=2,
        model="gpt-dashboard",
        reasoning_effort="xhigh",
        status="active",
        created_at=now,
        updated_at=now,
    )
    job = JobRecord(
        id="j_dashboard",
        agent_id="alpha",
        conversation_key="c_dashboard",
        sequence=1,
        prompt="hello",
        status="queued",
        agent_revision=2,
        model="gpt-dashboard",
        reasoning_effort="xhigh",
        runtime_metadata={},
        cancel_requested=False,
        created_at=now,
        updated_at=now,
    )

    conversation_projection = _conversation_view(conversation)
    job_projection = _dashboard_job_view(job)
    assert conversation_projection["model"] == "gpt-dashboard"
    assert conversation_projection["reasoning_effort"] == "xhigh"
    assert job_projection["model"] == "gpt-dashboard"
    assert job_projection["reasoning_effort"] == "xhigh"


def test_model_selection_migration_preserves_legacy_rows_as_inherited_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REMOTEAGENT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database = tmp_path / "migration.db"
    router_root = Path(__file__).parents[1]
    config = Config(str(router_root / "alembic.ini"))
    config.set_main_option("script_location", str(router_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")
    command.upgrade(config, "20260902_0002")

    now = "2026-09-02 12:00:00"
    snapshot = {
        "id": "alpha",
        "name": "Alpha",
        "description": "",
        "compose_file": "/agents/alpha/compose.yaml",
        "project_name": "remoteagent-alpha",
        "runner_service": "agent",
        "dependency_services": [],
        "enabled": True,
        "config_toml": 'model = "gpt-legacy"\nmodel_reasoning_effort = "high"\n',
        "base_context": "",
        "environment": {},
        "labels": {},
        "metadata": {},
    }
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO agents (
                id, name, description, compose_file, project_name, runner_service,
                dependency_services, environment, labels, definition_metadata,
                enabled, current_revision, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "alpha",
                "Alpha",
                "",
                "/agents/alpha/compose.yaml",
                "remoteagent-alpha",
                "agent",
                "[]",
                "{}",
                "{}",
                "{}",
                1,
                1,
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO agent_revisions (
                agent_id, revision, config_toml, base_context, definition_snapshot,
                checksum, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "alpha",
                1,
                snapshot["config_toml"],
                "",
                json.dumps(snapshot),
                "0" * 64,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO conversations (
                key, agent_id, codex_thread_id, workspace_path, codex_home_path,
                artifact_path, agent_revision, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "c_legacy",
                "alpha",
                None,
                "/state/workspace",
                "/state/sessions",
                "/state/artifacts",
                1,
                "active",
                now,
                now,
            ),
        )
        connection.execute(
            """INSERT INTO jobs (
                id, agent_id, conversation_key, sequence, prompt, idempotency_key,
                status, agent_revision, thread_id_snapshot, result, error, usage,
                runtime_metadata, started_at, completed_at, cancel_requested,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "j_legacy",
                "alpha",
                "c_legacy",
                1,
                "legacy",
                None,
                "queued",
                1,
                None,
                None,
                None,
                None,
                "{}",
                None,
                None,
                0,
                now,
                now,
            ),
        )

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        conversation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)")
        }
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        conversation = connection.execute(
            "SELECT model, reasoning_effort FROM conversations WHERE key = 'c_legacy'"
        ).fetchone()
        job = connection.execute(
            "SELECT model, reasoning_effort FROM jobs WHERE id = 'j_legacy'"
        ).fetchone()
    assert {"model", "reasoning_effort"} <= conversation_columns
    assert {"model", "reasoning_effort"} <= job_columns
    assert conversation == (None, None)
    assert job == (None, None)

    command.downgrade(config, "20260902_0002")
    with sqlite3.connect(database) as connection:
        conversation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)")
        }
        job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    assert not {"model", "reasoning_effort"} & conversation_columns
    assert not {"model", "reasoning_effort"} & job_columns
