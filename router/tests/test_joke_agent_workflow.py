from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from remoteagent.app import create_app
from remoteagent.config import Settings
from remoteagent.runtime import FakeRuntime, RuntimeResult
from remoteagent.schemas import UsageTotals
from remoteagent.telemetry import TokenTelemetryCollector

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads(
    (REPOSITORY_ROOT / "joke-agent" / "smoke-fixture.json").read_text(encoding="utf-8")
)
THREAD_ID = "11111111-1111-4111-8111-111111111111"


def _token_record(prompt: str, response: str) -> Any:
    return TokenTelemetryCollector().finalize(prompt=prompt, response=response)


def _wait_for_terminal_job(
    client: TestClient, headers: dict[str, str], job_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled", "interrupted", "expired"}:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def _assert_joke(
    response: str,
    *,
    required: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
) -> None:
    prefix = FIXTURE["response_prefix"]
    assert response.startswith(prefix)
    assert response.count(prefix) == 1
    assert "\n" not in response and "\r" not in response
    assert FIXTURE["minimum_characters"] <= len(response) <= FIXTURE["maximum_characters"]
    assert all(value in response for value in required)
    assert all(value not in response for value in forbidden)


def test_seeded_joke_agent_authenticated_async_continuation(tmp_path: Path) -> None:
    turn_1_marker = "RA_T1_DETERMINISTIC"
    turn_2_marker = "RA_T2_DETERMINISTIC"
    memory_marker = "RA_MEMORY_DETERMINISTIC"
    prompt_1 = FIXTURE["turn_1"].format(
        turn_1_marker=turn_1_marker,
        memory_marker=memory_marker,
    )
    prompt_2 = FIXTURE["turn_2"].format(turn_2_marker=turn_2_marker)
    response_1 = (
        "JOKE: The telescope spotted a star comedian, but its focus was "
        f"{turn_1_marker} while the punchline stayed {memory_marker}."
    )
    response_2 = (
        "JOKE: The bicycle remembered the telescope's best pun, so it geared up with "
        f"{turn_2_marker} and carried {memory_marker} along for the ride."
    )

    runtime = FakeRuntime()
    runtime.enqueue(
        RuntimeResult(
            response=response_1,
            thread_id=THREAD_ID,
            usage=UsageTotals(input_tokens=20, output_tokens=10),
            token_record=_token_record(prompt_1, response_1),
        )
    )
    runtime.enqueue(
        RuntimeResult(
            response=response_2,
            thread_id=THREAD_ID,
            usage=UsageTotals(input_tokens=25, output_tokens=12),
            token_record=_token_record(prompt_2, response_2),
        )
    )
    settings = Settings(
        environment="test",
        repository_root=REPOSITORY_ROOT,
        data_dir=tmp_path / "state",
        agents_root=REPOSITORY_ROOT,
        phonebook_path=REPOSITORY_ROOT / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        bearer_token="test-secret",
        dashboard_enabled=False,
        scheduler_poll_seconds=0.01,
        subscription_lease_ttl_seconds=30,
        subscription_lease_retry_seconds=0.01,
        mcp_allowed_hosts=["testserver"],
    ).resolved()
    app = create_app(settings, runtime=runtime, validate_compose=False)
    headers = {"Authorization": "Bearer test-secret"}

    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/agents")
        assert unauthorized.status_code == 401
        discovery = client.get("/api/v1/agents", headers=headers)
        assert discovery.status_code == 200
        assert [agent["id"] for agent in discovery.json()] == [
            FIXTURE["agent_id"],
            "repository-critic",
        ]

        first_accepted = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={"agent_id": FIXTURE["agent_id"], "prompt": prompt_1},
        )
        assert first_accepted.status_code == 202
        first = first_accepted.json()
        first_job = _wait_for_terminal_job(client, headers, first["job_id"])
        assert first_job["status"] == "succeeded"
        assert first_job["result"] == response_1
        _assert_joke(
            first_job["result"],
            required=("telescope", turn_1_marker, memory_marker),
        )

        second_accepted = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "agent_id": FIXTURE["agent_id"],
                "prompt": prompt_2,
                "conversation_key": first["conversation_key"],
            },
        )
        assert second_accepted.status_code == 202
        second = second_accepted.json()
        assert second["conversation_key"] == first["conversation_key"]
        assert second["job_id"] != first["job_id"]
        second_job = _wait_for_terminal_job(client, headers, second["job_id"])
        assert second_job["status"] == "succeeded"
        assert second_job["result"] == response_2
        _assert_joke(
            second_job["result"],
            required=("bicycle", turn_2_marker, memory_marker),
            forbidden=(turn_1_marker,),
        )

    assert [request.prompt for request in runtime.requests] == [prompt_1, prompt_2]
    assert runtime.requests[0].thread_id is None
    assert runtime.requests[1].thread_id == THREAD_ID
    assert runtime.provisioned == [first["job_id"], second["job_id"]]
    assert runtime.released == [first["job_id"], second["job_id"]]
