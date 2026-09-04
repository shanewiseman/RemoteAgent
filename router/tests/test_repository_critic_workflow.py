from __future__ import annotations

import json
import time
import tomllib
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from remoteagent.app import create_app
from remoteagent.config import Settings
from remoteagent.phonebook import load_phonebook
from remoteagent.runtime import FakeRuntime, RuntimeResult
from remoteagent.schemas import UsageTotals
from remoteagent.telemetry import TokenTelemetryCollector


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CRITIC_ROOT = REPOSITORY_ROOT / "repository-critic"
SUPPORTING_CONTEXT = {
    "references/evidence-and-precedence.md",
    "references/architecture-and-quality.md",
    "references/testing-and-coverage.md",
    "references/ecosystem-coverage-tools.md",
    "references/security-api-operations.md",
}


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


def test_checked_in_repository_critic_contract() -> None:
    definitions = {
        definition.id: definition
        for definition in load_phonebook(REPOSITORY_ROOT / "phonebook.toml", REPOSITORY_ROOT)
    }

    assert set(definitions) == {"joke-agent", "repository-critic"}
    critic = definitions["repository-critic"]
    assert critic.enabled is True
    assert critic.name == "Repository Critic"
    assert critic.compose_file == CRITIC_ROOT / "compose.yaml"
    assert critic.project_name == "remoteagent-repository-critic"
    assert critic.runner_service == "agent"
    assert critic.dependency_services == ()
    assert critic.environment["REMOTEAGENT_REVIEW_MODE"] == "repository_snapshot"
    assert critic.labels["io.remoteagent.quality"] == "true"
    assert critic.metadata["category"] == "quality"
    assert critic.metadata["review_mode"] == "repository_snapshot"
    assert critic.metadata["supports_artifacts"] is True

    config = tomllib.loads(critic.config_toml)
    assert config == {
        "cli_auth_credentials_store": "file",
        "approval_policy": "never",
        "sandbox_mode": "workspace-write",
        "sandbox_workspace_write": {"network_access": True},
    }

    supporting_links = 0
    for relative in SUPPORTING_CONTEXT:
        path = CRITIC_ROOT / relative
        assert path.is_file(), f"missing repository-critic context: {relative}"
        text = path.read_text(encoding="utf-8")
        assert "http://" not in text, f"supporting context must use HTTPS links: {relative}"
        supporting_links += text.count("https://")
    assert supporting_links >= 10

    context = critic.base_context
    assert "repository snapshot" in context.lower()
    assert "pull-request" in context.lower()
    assert "--workspace /workspace" in context
    assert "repository-review-" in context
    assert "at most one `--log` per" in context
    assert "at most six log paths total" in context
    assert "count helper" in context
    assert "whose phase is `coverage`" in context
    for relative in SUPPORTING_CONTEXT:
        assert relative in context

    toolchains = json.loads((CRITIC_ROOT / "toolchain-manifest.json").read_text(encoding="utf-8"))
    assert toolchains["schema_version"] == 1
    assert critic.metadata["toolchain_revision"] == toolchains["revision"]
    assert set(toolchains["runtimes"]) == {"python", "node", "npm", "go"}
    assert toolchains["support_runtimes"] == {"corepack_node": "22.22.2"}
    assert set(toolchains["coverage_tools"]) == {
        "coverage.py",
        "pytest-cov",
        "c8",
        "go-cover",
    }
    assert toolchains["runtime_policy"] == {
        "arbitrary_coverage_downloads": False,
        "corepack_cache_integrity": (
            "Corepack-verified registry metadata at image build; cache archive integrity is "
            "not independently checked in"
        ),
        "corepack_runtime": (
            "Corepack 0.36.0 and its dispatched managers use isolated Node 22.22.2; "
            "repository commands use Node 22.19.0"
        ),
        "default_build_hooks": False,
        "default_restore": "locked",
        "node_tool_integrity": "checked-in npm lockfileVersion 3 registry integrities",
        "python_tool_integrity": "checked-in transitive SHA-256 requirement locks",
        "unlocked_restore_label": "resolved_unlocked",
    }


def test_seeded_repository_critic_routes_its_revision_and_network_policy(tmp_path: Path) -> None:
    prompt = "Assess the complete repository snapshot, not a diff."
    response_text = "Repository snapshot assessment complete."
    runtime = FakeRuntime()
    runtime.enqueue(
        RuntimeResult(
            response=response_text,
            thread_id="22222222-2222-4222-8222-222222222222",
            usage=UsageTotals(input_tokens=20, output_tokens=5),
            token_record=_token_record(prompt, response_text),
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
        accepted = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={"agent_id": "repository-critic", "prompt": prompt},
        )
        assert accepted.status_code == 202
        job = _wait_for_terminal_job(client, headers, accepted.json()["job_id"])
        assert job["status"] == "succeeded"
        assert job["result"] == response_text

    assert len(runtime.requests) == 1
    request = runtime.requests[0]
    assert request.definition.id == "repository-critic"
    assert request.definition.metadata["review_mode"] == "repository_snapshot"
    assert request.thread_id is None
    assert request.paths.control.joinpath("AGENTS.md").read_text(encoding="utf-8") == (
        request.definition.base_context
    )
    effective_config = tomllib.loads(
        request.paths.control.joinpath("config.toml").read_text(encoding="utf-8")
    )
    assert effective_config["sandbox_mode"] == "workspace-write"
    assert effective_config["sandbox_workspace_write"] == {"network_access": True}
