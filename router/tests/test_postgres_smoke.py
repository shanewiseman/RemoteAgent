from __future__ import annotations

import asyncio
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).parents[2]
SCRIPT_PATH = REPOSITORY_ROOT / "scripts" / "smoke_postgres.py"
SPEC = importlib.util.spec_from_file_location("remoteagent_smoke_postgres", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
smoke_postgres = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke_postgres
SPEC.loader.exec_module(smoke_postgres)


def _completed(
    arguments: list[str],
    returncode: int = 0,
    output: str = "",
    error: str = "",
):
    return subprocess.CompletedProcess(arguments, returncode, stdout=output, stderr=error)


def test_disposable_postgres_names_are_unique_and_strictly_smoke_scoped() -> None:
    first = smoke_postgres.DisposablePostgres()
    second = smoke_postgres.DisposablePostgres()

    assert first.container != second.container
    assert first.volume != second.volume
    assert first.container.startswith(smoke_postgres.RESOURCE_PREFIX)
    assert first.volume == f"{first.container}-data"
    assert first.database == f"ra_smoke_{first.token}"
    assert first.user == f"ra_smoke_{first.token}"
    smoke_postgres.DisposablePostgres._validate_target(first.container)
    smoke_postgres.DisposablePostgres._validate_target(first.volume, volume=True)
    with pytest.raises(ValueError):
        smoke_postgres.DisposablePostgres("remoteagent")
    with pytest.raises(smoke_postgres.SmokeFailure, match="refusing"):
        smoke_postgres.DisposablePostgres._validate_target("remoteagent-postgres-1")


def test_disposable_postgres_uses_ephemeral_loopback_port_and_always_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run(arguments, **kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append((command, kwargs.get("env")))
        if command[1:3] == ["context", "inspect"]:
            return _completed(command, output="unix:///var/run/docker.sock\n")
        if command[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            if "--format" in command:
                return _completed(command, output=postgres.owner_id + "\n")
            return _completed(command, returncode=1, error="No such container or volume")
        if command[1:3] == ["volume", "create"]:
            return _completed(command, output=command[-1] + "\n")
        if command[1] == "run":
            return _completed(command, output="container-id\n")
        if command[1] == "exec":
            return _completed(command, output="accepting connections\n")
        if command[1] == "inspect":
            return _completed(command, output="49152\n")
        if command[1:3] in (["rm", "--force"], ["volume", "rm"]):
            return _completed(command)
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("012345abcdef")
    with pytest.raises(RuntimeError, match="body failed"):
        with postgres.running() as endpoint:
            assert endpoint.host == "127.0.0.1"
            assert endpoint.port == 49152
            raise RuntimeError("body failed")

    run_call, run_environment = next(item for item in calls if item[0][1] == "run")
    assert "127.0.0.1::5432" in run_call
    assert "--pull=missing" in run_call
    assert "POSTGRES_PASSWORD" in run_call
    assert postgres.password not in run_call
    assert f"{smoke_postgres.OWNER_LABEL_KEY}={postgres.owner_id}" in run_call
    assert run_environment is not None
    assert run_environment["POSTGRES_PASSWORD"] == postgres.password
    removal_calls = [
        call[0]
        for call in calls
        if call[0][1:3] in (["rm", "--force"], ["volume", "rm"])
    ]
    assert [call[1:3] for call in removal_calls] == [
        ["rm", "--force"],
        ["volume", "rm"],
    ]
    assert removal_calls[0][-1] == postgres.container
    assert removal_calls[1][-1] == postgres.volume


def test_disposable_postgres_removes_volume_when_container_start_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["context", "inspect"]:
            return _completed(command, output="unix:///var/run/docker.sock\n")
        if command[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            if "--format" in command:
                if command[1] == "container":
                    return _completed(command, returncode=1, error="No such container")
                return _completed(command, output=postgres.owner_id)
            return _completed(command, returncode=1, error="No such container or volume")
        if command[1:3] == ["volume", "create"]:
            return _completed(command, output=command[-1])
        if command[1] == "run":
            return _completed(command, returncode=1)
        if command[1:3] == ["volume", "rm"]:
            return _completed(command)
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("fedcba987654")
    with pytest.raises(smoke_postgres.SmokeFailure, match="Docker command failed"):
        with postgres.running():
            raise AssertionError("unreachable")

    assert ["docker", "volume", "rm", "--force", postgres.volume] in calls
    assert not any(command[1:3] == ["rm", "--force"] for command in calls)


def test_disposable_postgres_refuses_preexisting_generated_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["context", "inspect"]:
            return _completed(command, output="unix:///var/run/docker.sock\n")
        if command[1:3] == ["container", "inspect"]:
            return _completed(command, returncode=1)
        if command[1:3] == ["volume", "inspect"]:
            return _completed(command)
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("123456abcdef")
    with pytest.raises(smoke_postgres.SmokeFailure, match="volume name is already in use"):
        with postgres.running():
            raise AssertionError("unreachable")

    assert not any(command[1:3] == ["volume", "create"] for command in calls)
    assert not any(command[1:3] == ["volume", "rm"] for command in calls)


def test_disposable_postgres_refuses_remote_docker_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOCKER_HOST", "ssh://production.example")
    monkeypatch.setattr(
        smoke_postgres.subprocess,
        "run",
        lambda *_arguments, **_kwargs: pytest.fail("Docker must not be called"),
    )

    postgres = smoke_postgres.DisposablePostgres("456789abcdef")
    with pytest.raises(smoke_postgres.SmokeFailure, match="non-local DOCKER_HOST"):
        with postgres.running():
            raise AssertionError("unreachable")


def test_cleanup_attempts_volume_after_container_cleanup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        if command[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            return _completed(command, output=postgres.owner_id)
        if command[1:3] == ["rm", "--force"]:
            raise OSError("Docker unavailable")
        if command[1:3] == ["volume", "rm"]:
            return _completed(command)
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("abcdef012345")
    postgres._container_created = True
    postgres._volume_created = True

    failures = postgres.cleanup()

    assert len(failures) == 1
    assert "container cleanup failed" in failures[0]
    assert ["docker", "volume", "rm", "--force", postgres.volume] in calls


def test_volume_create_timeout_cleans_exact_owned_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["context", "inspect"]:
            return _completed(command, output="unix:///var/run/docker.sock\n")
        if command[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            if "--format" in command:
                return _completed(command, output=postgres.owner_id)
            return _completed(command, returncode=1, error="No such container or volume")
        if command[1:3] == ["volume", "create"]:
            raise subprocess.TimeoutExpired(command, timeout=60)
        if command[1:3] == ["volume", "rm"]:
            return _completed(command)
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("101010abcdef")

    with pytest.raises(smoke_postgres.SmokeFailure, match="could not complete"):
        with postgres.running():
            raise AssertionError("unreachable")

    create_call = next(command for command in calls if command[1:3] == ["volume", "create"])
    assert f"{smoke_postgres.OWNER_LABEL_KEY}={postgres.owner_id}" in create_call
    assert ["docker", "volume", "rm", "--force", postgres.volume] in calls
    assert not any(command[1:3] == ["rm", "--force"] for command in calls)


def test_container_run_timeout_cleans_exact_owned_container_and_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        if command[1:3] == ["context", "inspect"]:
            return _completed(command, output="unix:///var/run/docker.sock\n")
        if command[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            if "--format" in command:
                return _completed(command, output=postgres.owner_id)
            return _completed(command, returncode=1, error="No such container or volume")
        if command[1:3] == ["volume", "create"]:
            return _completed(command, output=postgres.volume)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, timeout=60)
        if command[1:3] in (["rm", "--force"], ["volume", "rm"]):
            return _completed(command)
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("202020abcdef")

    with pytest.raises(smoke_postgres.SmokeFailure, match="could not complete"):
        with postgres.running():
            raise AssertionError("unreachable")

    assert ["docker", "rm", "--force", postgres.container] in calls
    assert ["docker", "volume", "rm", "--force", postgres.volume] in calls


def test_cleanup_never_removes_candidates_with_foreign_owner_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(arguments, **_kwargs):  # type: ignore[no-untyped-def]
        command = list(arguments)
        calls.append(command)
        if command[1:3] in (["container", "inspect"], ["volume", "inspect"]):
            return _completed(command, output="another-invocation")
        if command[1:3] in (["rm", "--force"], ["volume", "rm"]):
            pytest.fail("foreign-owned resources must never be removed")
        raise AssertionError(command)

    monkeypatch.setattr(smoke_postgres.subprocess, "run", fake_run)
    postgres = smoke_postgres.DisposablePostgres("303030abcdef")
    postgres._container_created = True
    postgres._volume_created = True

    failures = postgres.cleanup()

    assert len(failures) == 2
    assert all("ownership label does not match" in failure for failure in failures)
    assert not any(command[1:3] in (["rm", "--force"], ["volume", "rm"]) for command in calls)


def test_migration_smoke_waits_for_all_processes_to_contend_on_advisory_locks() -> None:
    async def scenario() -> tuple[int, list[tuple[str, int]], list[str], bool, int]:
        released = asyncio.Event()
        started: list[tuple[str, int]] = []

        class FakeResult:
            def __init__(self, value: int) -> None:
                self.value = value

            def scalar_one(self) -> int:
                return self.value

        class FakeTransaction:
            is_active = True

            async def rollback(self) -> None:
                self.is_active = False
                released.set()

        class FakeBlocker:
            def __init__(self) -> None:
                self.statements: list[str] = []
                self.transaction = FakeTransaction()

            async def begin(self):  # type: ignore[no-untyped-def]
                return self.transaction

            async def exec_driver_sql(self, statement: str) -> FakeResult:
                self.statements.append(statement)
                return FakeResult(0)

        class FakeObserver:
            def __init__(self) -> None:
                self.rollbacks = 0

            async def exec_driver_sql(self, statement: str) -> FakeResult:
                assert statement == smoke_postgres.MIGRATION_WAITER_SQL
                await asyncio.sleep(0)
                return FakeResult(len(started))

            async def rollback(self) -> None:
                self.rollbacks += 1

        async def runner(_endpoint, *, component: str, ordinal: int) -> None:  # type: ignore[no-untyped-def]
            started.append((component, ordinal))
            await released.wait()

        endpoint = smoke_postgres.PostgresEndpoint(
            container="unused",
            volume="unused",
            database="unused",
            user="unused",
            host="127.0.0.1",
            port=5432,
            url="postgresql+asyncpg://unused",
        )
        blocker = FakeBlocker()
        observer = FakeObserver()
        waiters = await smoke_postgres._run_contended_migration_processes(
            endpoint,
            blocker,
            observer,
            process_runner=runner,
            contention_timeout_seconds=1,
        )
        return (
            waiters,
            started,
            blocker.statements,
            blocker.transaction.is_active,
            observer.rollbacks,
        )

    waiters, started, statements, transaction_active, observer_rollbacks = asyncio.run(scenario())

    assert waiters == 4
    assert set(started) == {("router", 1), ("router", 2), ("cron", 1), ("cron", 2)}
    assert statements == list(smoke_postgres.MIGRATION_LOCK_SQL)
    assert transaction_active is False
    assert observer_rollbacks >= 1


def test_remotectl_dispatches_postgres_smoke_without_loading_deployment_env(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    invocation = tmp_path / "python-invocation"
    fake_python = fake_bin / "python-smoke"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'%s\\n\' "$REMOTEAGENT_POSTGRES_SMOKE_JSON|$*" '
        ' > "$REMOTEAGENT_TEST_POSTGRES_INVOCATION"\n',
        encoding="utf-8",
    )
    fake_python.chmod(0o700)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o700)

    result = subprocess.run(
        [str(REPOSITORY_ROOT / "scripts" / "remotectl"), "--json", "smoke", "postgres"],
        cwd=REPOSITORY_ROOT,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REMOTEAGENT_ENV_FILE": str(tmp_path / "does-not-exist.env"),
            "REMOTEAGENT_POSTGRES_SMOKE_PYTHON": str(fake_python),
            "REMOTEAGENT_TEST_POSTGRES_INVOCATION": str(invocation),
        },
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    mode, argument = invocation.read_text(encoding="utf-8").strip().split("|", 1)
    assert mode == "1"
    assert Path(argument) == SCRIPT_PATH


def test_remotectl_help_and_postgres_smoke_option_validation() -> None:
    remotectl = REPOSITORY_ROOT / "scripts" / "remotectl"
    help_result = subprocess.run(
        [str(remotectl), "--help"],
        cwd=REPOSITORY_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert help_result.returncode == 0
    assert "smoke postgres" in help_result.stdout

    invalid = subprocess.run(
        [str(remotectl), "smoke", "postgres", "--keep"],
        cwd=REPOSITORY_ROOT,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert invalid.returncode != 0
    assert "smoke postgres does not accept options" in invalid.stderr
