from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from remoteagent.dashboard.data import DashboardData, _job_view, _sanitized_event
from remoteagent.models import AgentRecord, Base, ConversationRecord, JobEventRecord, JobRecord


def job_record(**overrides: object) -> JobRecord:
    now = datetime.now(UTC)
    values = {
        "id": "j_test",
        "agent_id": "agent",
        "conversation_key": "c_test",
        "sequence": 1,
        "prompt": "hello",
        "status": "succeeded",
        "agent_revision": 1,
        "result": "world",
        "usage": None,
        "runtime_metadata": {},
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }
    values.update(overrides)
    return JobRecord(**values)


def test_job_view_promotes_detailed_token_record() -> None:
    record = job_record(
        error="failed with Bearer very-secret-value",
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "reasoning_output_tokens": 3,
        },
        runtime_metadata={
            "stderr_tail": "password printed by subprocess",
            "token_usage": {
                "quality": "exact",
                "input_tokens": 10,
                "output_tokens": 5,
                "reasoning_output_tokens": 3,
                "total_tokens": None,
                "contributors": [
                    {
                        "component": "system",
                        "tokens": 2,
                        "quality": "estimated",
                        "provenance": "visible base context",
                    }
                ],
            },
        },
    )

    view = _job_view(record, detail=True)

    assert view["usage"]["quality"] == "exact"
    assert view["usage"]["total_tokens"] == 15
    assert view["usage"]["contributors"][0]["component"] == "system"
    assert "very-secret-value" not in view["error"]
    assert "stderr_tail" not in view["runtime_metadata"]


def test_legacy_usage_is_not_silently_called_exact() -> None:
    record = job_record(
        usage={
            "input_tokens": 10,
            "output_tokens": 5,
            "reasoning_output_tokens": 3,
        }
    )

    usage = _job_view(record)["usage"]

    assert usage["quality"] == "unavailable"
    assert usage["total_tokens"] == 15


def test_nested_event_secrets_and_reasoning_are_redacted() -> None:
    event = JobEventRecord(
        id=1,
        job_id="j_test",
        sequence=1,
        event_type="job.updated",
        payload={"nested": {"access_token": "secret"}},
        created_at=datetime.now(UTC),
    )
    reasoning = JobEventRecord(
        id=2,
        job_id="j_test",
        sequence=2,
        event_type="item.reasoning",
        payload={"text": "private chain"},
        created_at=datetime.now(UTC),
    )

    assert _sanitized_event(event)["payload"]["nested"]["access_token"] == "[redacted]"
    assert _sanitized_event(reasoning)["payload"] == {"redacted": "model reasoning is not exposed"}


@pytest.mark.asyncio
async def test_active_agents_and_history_cutoff_are_explicit() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = datetime.now(UTC)
    async with session_factory() as session, session.begin():
        session.add(
            AgentRecord(
                id="agent",
                name="Agent",
                description="",
                compose_file="compose.yaml",
                project_name="ra_agent",
                runner_service="agent",
                dependency_services=[],
                environment={},
                labels={},
                definition_metadata={},
                enabled=True,
                current_revision=1,
            )
        )
        session.add(
            ConversationRecord(
                key="c_test",
                agent_id="agent",
                codex_thread_id=None,
                workspace_path="workspace",
                codex_home_path="sessions",
                artifact_path="artifacts",
                agent_revision=1,
                status="active",
                created_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                job_record(
                    id="j_old",
                    sequence=1,
                    status="succeeded",
                    created_at=now - timedelta(days=31),
                    updated_at=now - timedelta(days=31),
                ),
                job_record(id="j_current", sequence=2, status="running"),
            ]
        )

    app = FastAPI()
    app.state.container = SimpleNamespace(
        session_factory=session_factory,
        settings=SimpleNamespace(
            dashboard_history_days=30,
            dashboard_history_default_turns=100,
        ),
        dashboard_service=None,
        agent_service=None,
        job_service=None,
    )
    data = DashboardData(app)

    summary = await data.summary()
    agents = await data.list_agents(cursor=None, limit=50)
    turns = await data.conversation_turns("c_test")

    assert summary["active_agents"] == 1
    assert agents["items"][0]["active"] is True
    assert agents["items"][0]["active_jobs"] == 1
    assert [item["id"] for item in turns["items"]] == ["j_current"]
    assert turns["history_days"] == 30
    await engine.dispose()
