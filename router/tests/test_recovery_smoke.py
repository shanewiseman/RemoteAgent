from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY = Path(__file__).parents[2]
REMOTECTL = REPOSITORY / "scripts" / "remotectl"
SCRIPT = REPOSITORY / "scripts" / "smoke_recovery.py"
SPEC = importlib.util.spec_from_file_location("remoteagent_smoke_recovery", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
smoke_recovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke_recovery
SPEC.loader.exec_module(smoke_recovery)


def _fake_command(path: Path, body: str = "exit 0\n") -> None:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o700)


def test_recovery_smoke_dispatch_is_local_only_and_forwards_bounds(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _fake_command(fake_bin / "docker")
    _fake_command(
        fake_bin / "python3",
        'printf "%s|%s|%s\\n" "$REMOTEAGENT_RECOVERY_SMOKE_TIMEOUT" '
        '"$REMOTEAGENT_RECOVERY_SMOKE_JSON" "$1"\n',
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "REMOTEAGENT_ENV_FILE": str(tmp_path / "must-not-be-read.env"),
        }
    )

    result = subprocess.run(
        ["/bin/bash", str(REMOTECTL), "--json", "smoke", "recovery", "--timeout", "1800"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    timeout, json_mode, script = result.stdout.strip().split("|", 2)
    assert timeout == "1800"
    assert json_mode == "1"
    assert Path(script) == REPOSITORY / "scripts" / "smoke_recovery.py"


def test_recovery_smoke_rejects_invalid_timeout_before_tool_access(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": str(tmp_path),
            "REMOTEAGENT_ENV_FILE": str(tmp_path / "must-not-be-read.env"),
        }
    )

    result = subprocess.run(
        ["/bin/bash", str(REMOTECTL), "smoke", "recovery", "--timeout", "59"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0
    assert "smoke recovery --timeout must be between 60 and 7200 seconds" in result.stderr


def test_recovery_smoke_refuses_remote_docker_host_before_docker_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://production.example")
    monkeypatch.setattr(
        smoke_recovery.subprocess,
        "run",
        lambda *_arguments, **_kwargs: pytest.fail("Docker must not be called"),
    )

    with pytest.raises(smoke_recovery.RecoverySmokeError, match="non-local DOCKER_HOST"):
        smoke_recovery._require_local_docker_daemon()


def test_recovery_smoke_refuses_remote_active_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(
        smoke_recovery.subprocess,
        "run",
        lambda arguments, **_kwargs: subprocess.CompletedProcess(
            arguments,
            0,
            stdout="ssh://production.example\n",
            stderr="",
        ),
    )

    with pytest.raises(smoke_recovery.RecoverySmokeError, match="non-local Docker context"):
        smoke_recovery._require_local_docker_daemon()


def test_recovery_environment_cannot_override_disposable_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    live_state = tmp_path / "live-state"
    overrides = {
        "COMPOSE_PROJECT_NAME": "live-project",
        "COMPOSE_PROFILES": "tools",
        "POSTGRES_DB": "live-database",
        "POSTGRES_USER": "live-user",
        "REDIS_URL": "redis://live-redis:6379/0",
        "REMOTEAGENT_STATE_ROOT": str(live_state),
        "REMOTEAGENT_PORT": "8080",
        "REMOTEAGENT_AUTH_VOLUME": "live-auth",
        "REMOTEAGENT_SKILLS_VOLUME": "live-skills",
        "REMOTEAGENT_DOCKER_SOCKET": "/tmp/live-docker.sock",
    }
    for key, value in overrides.items():
        monkeypatch.setenv(key, value)

    drill = smoke_recovery.Drill(60, "unix:///var/run/docker.sock")
    try:
        assert drill.environment["COMPOSE_PROJECT_NAME"] == drill.project
        assert drill.environment["REMOTEAGENT_STATE_ROOT"] == str(drill.state)
        assert drill.environment["REMOTEAGENT_PORT"] == str(drill.port)
        assert drill.environment["POSTGRES_DB"] == "remoteagent"
        assert drill.environment["POSTGRES_USER"] == "remoteagent"
        assert drill.environment["REMOTEAGENT_AUTH_VOLUME"].startswith(drill.project)
        assert drill.environment["REMOTEAGENT_SKILLS_VOLUME"].startswith(drill.project)
        assert drill.environment["REMOTEAGENT_DOCKER_SOCKET"] == "/var/run/docker.sock"
        assert "COMPOSE_PROFILES" not in drill.environment
        assert "REDIS_URL" not in drill.environment
        assert not live_state.exists()
        assert (drill.state / "secrets" / "router_bearer_token").is_file()
    finally:
        shutil.rmtree(drill.temporary, ignore_errors=True)


def test_recovery_cleanup_uses_independent_budget_and_verifies_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    timeouts: list[float] = []

    def fake_run(arguments, **kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        timeouts.append(kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(smoke_recovery.subprocess, "run", fake_run)
    drill = smoke_recovery.Drill(60)
    temporary = drill.temporary
    drill.deadline = 0
    moments = iter([100.0, 105.0, 110.0, 115.0, 120.0])
    monkeypatch.setattr(smoke_recovery.time, "monotonic", lambda: next(moments))

    drill.cleanup()

    assert not temporary.exists()
    assert any("down" in command for command in calls)
    assert [command[1:3] for command in calls[-3:]] == [
        ["container", "ls"],
        ["volume", "ls"],
        ["network", "ls"],
    ]
    assert timeouts == [85.0, 80.0, 75.0, 70.0]


def test_recovery_cleanup_failure_preserves_recovery_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        if "down" in command:
            return subprocess.CompletedProcess(command, 1, stdout="", stderr="daemon unavailable")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(smoke_recovery.subprocess, "run", fake_run)
    drill = smoke_recovery.Drill(60)
    temporary = drill.temporary
    try:
        with pytest.raises(smoke_recovery.RecoverySmokeError, match="state preserved"):
            drill.cleanup()
        assert temporary.exists()
        assert drill.env_file.exists()
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def test_recovery_startup_order_requires_both_markers() -> None:
    smoke_recovery._assert_markers_in_order(
        "starting router\nstarting cron",
        "starting router",
        "starting cron",
        "wrong order",
    )
    with pytest.raises(smoke_recovery.RecoverySmokeError, match="wrong order"):
        smoke_recovery._assert_markers_in_order(
            "starting cron",
            "starting router",
            "starting cron",
            "wrong order",
        )
    with pytest.raises(smoke_recovery.RecoverySmokeError, match="wrong order"):
        smoke_recovery._assert_markers_in_order(
            "starting cron\nstarting router",
            "starting router",
            "starting cron",
            "wrong order",
        )


def test_recovery_preserves_primary_and_cleanup_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class FailingDrill:
        def __init__(self, _timeout: int, _docker_endpoint: str) -> None:
            self.temporary = tmp_path
            self.token = "0123456789abcdef"

        @property
        def remotectl(self) -> list[str]:
            return []

        def run(self, *_arguments, **_kwargs):  # type: ignore[no-untyped-def]
            raise smoke_recovery.RecoverySmokeError("primary failure")

        def cleanup(self) -> None:
            raise smoke_recovery.RecoverySmokeError("cleanup failure")

    monkeypatch.setattr(smoke_recovery, "_require_local_docker_daemon", lambda: None)
    monkeypatch.setattr(smoke_recovery, "Drill", FailingDrill)

    with pytest.raises(smoke_recovery.RecoverySmokeError) as raised:
        smoke_recovery.run_drill(60)

    assert "primary failure" in str(raised.value)
    assert "cleanup failure" in str(raised.value)
    assert raised.value.__cause__ is not None
    assert "primary failure" in str(raised.value.__cause__)
