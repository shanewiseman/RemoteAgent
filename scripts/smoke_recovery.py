#!/usr/bin/env python3
"""Exercise backup/restore against a disposable RemoteAgent deployment.

The drill uses unique Compose, state, port, container, network, and volume names.
It never reads or writes the configured live deployment state or secrets.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class RecoverySmokeError(RuntimeError):
    pass


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "scripts" / "remotectl"
COMPOSE_FILE = ROOT / "compose.yaml"
CLEANUP_COMMAND_TIMEOUT_SECONDS = 90


def _require_local_docker_daemon() -> str:
    configured_host = os.environ.get("DOCKER_HOST", "").strip()
    if configured_host and not configured_host.startswith(("unix://", "npipe://")):
        raise RecoverySmokeError("smoke recovery refuses a non-local DOCKER_HOST")
    try:
        context = subprocess.run(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                '{{(index .Endpoints "docker").Host}}',
            ],
            cwd=ROOT,
            env=os.environ,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoverySmokeError(f"could not inspect the Docker context: {exc}") from exc
    if context.returncode != 0:
        detail = (context.stderr or context.stdout).strip() or "no diagnostic"
        raise RecoverySmokeError(f"could not inspect the Docker context: {detail[:1000]}")
    endpoint = context.stdout.strip()
    if not endpoint.startswith(("unix://", "npipe://")):
        raise RecoverySmokeError("smoke recovery refuses a non-local Docker context")
    return endpoint


class Drill:
    def __init__(
        self,
        timeout: int,
        docker_endpoint: str = "unix:///var/run/docker.sock",
    ) -> None:
        self.deadline = time.monotonic() + timeout
        self.token = secrets.token_hex(8)
        self.project = f"remoteagent-recovery-{self.token}"
        self.temporary = Path(tempfile.mkdtemp(prefix="remoteagent-recovery-"))
        try:
            self.state = self.temporary / "state"
            self.env_file = self.temporary / "recovery.env"
            self.port = self._available_port()
            self.router_token = f"router-{secrets.token_hex(24)}"
            self.commands: list[str] = []
            self.docker_endpoint = docker_endpoint
            self._prepare_state()
        except BaseException:
            shutil.rmtree(self.temporary, ignore_errors=True)
            raise

    @staticmethod
    def _available_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _prepare_state(self) -> None:
        for path in (
            self.state / "secrets",
            self.state / "locks",
            self.state / "conversations",
            self.state / "artifact-store",
            self.state / "backups",
            self.state / "empty" / "workspace",
            self.state / "empty" / "sessions",
            self.state / "empty" / "artifacts",
        ):
            path.mkdir(parents=True, mode=0o700)
        values = {
            "router_bearer_token": self.router_token,
            "postgres_password": f"postgres-{secrets.token_hex(24)}",
            "cron_mcp_token": f"cron-mcp-{secrets.token_hex(24)}",
            "cron_api_token": f"cron-api-{secrets.token_hex(24)}",
        }
        for name, value in values.items():
            path = self.state / "secrets" / name
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)
        if self.docker_endpoint.startswith("unix://"):
            docker_socket = Path(self.docker_endpoint.removeprefix("unix://"))
        else:
            docker_socket = Path(self.docker_endpoint.removeprefix("npipe://"))
        docker_gid = docker_socket.stat().st_gid if docker_socket.exists() else 998
        version = os.environ.get("REMOTEAGENT_VERSION", "local")
        lines = {
            "COMPOSE_PROJECT_NAME": self.project,
            "POSTGRES_DB": "remoteagent",
            "POSTGRES_USER": "remoteagent",
            "REMOTEAGENT_AUTH_VOLUME": f"{self.project}-codex-auth",
            "REMOTEAGENT_BIND_ADDRESS": "127.0.0.1",
            "REMOTEAGENT_DASHBOARD_ALLOW_HTTP": "true",
            "REMOTEAGENT_DOCKER_GID": str(docker_gid),
            "REMOTEAGENT_DOCKER_SOCKET": str(docker_socket),
            "REMOTEAGENT_GID": str(os.getgid()),
            "REMOTEAGENT_INSTANCE_ID": self.project,
            "REMOTEAGENT_PORT": str(self.port),
            "REMOTEAGENT_REPO_ROOT": str(ROOT),
            "REMOTEAGENT_SCHEDULER_ENABLED": "false",
            "REMOTEAGENT_SKILLS_VOLUME": f"{self.project}-common-skills",
            "REMOTEAGENT_STATE_ROOT": str(self.state),
            "REMOTEAGENT_UID": str(os.getuid()),
            "REMOTEAGENT_VERSION": version,
        }
        self.env_file.write_text(
            "".join(f"{key}={value}\n" for key, value in sorted(lines.items())),
            encoding="utf-8",
        )
        self.env_file.chmod(0o600)
        # Compose gives exported shell variables precedence over --env-file.
        # Remove every deployment namespace, then explicitly reassert this
        # drill's generated values so a shell configured for the live project
        # cannot redirect state, ports, databases, or named volumes.
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("REMOTEAGENT_", "COMPOSE_", "POSTGRES_"))
            and key not in {"DATABASE_URL", "REDIS_URL"}
        }
        self.environment.update(lines)

    @property
    def compose(self) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "--project-name",
            self.project,
            "-f",
            str(COMPOSE_FILE),
        ]

    @property
    def remotectl(self) -> list[str]:
        return [str(REMOTE), "--env-file", str(self.env_file), "--project", self.project]

    def run(
        self,
        arguments: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RecoverySmokeError("recovery smoke exceeded its total timeout")
        environment = {**self.environment, **(extra_env or {})}
        self.commands.append(" ".join(arguments[:4]))
        try:
            result = subprocess.run(
                arguments,
                cwd=ROOT,
                env=environment,
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RecoverySmokeError(f"command timed out: {' '.join(arguments[:4])}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout)[-4_000:]
            raise RecoverySmokeError(
                f"command failed ({result.returncode}): {' '.join(arguments[:4])}\n{detail}"
            )
        return result

    def start(self) -> None:
        # The explicit split is part of the recovery contract: the router must
        # become healthy before cron starts and calls its MCP surface.
        self.run(
            self.compose
            + ["up", "-d", "--wait", "--wait-timeout", "180", "postgres", "redis", "router"]
        )
        self.run(self.compose + ["up", "-d", "--wait", "--wait-timeout", "180", "cron"])
        self.wait_router()

    def wait_router(self) -> None:
        url = f"http://127.0.0.1:{self.port}/healthz"
        last_error = ""
        while time.monotonic() < self.deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                    if response.status == 200:
                        return
            except (OSError, urllib.error.URLError) as exc:
                last_error = str(exc)
            time.sleep(0.25)
        raise RecoverySmokeError(f"router did not become healthy: {last_error}")

    def api(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> Any:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.router_token}",
                "Content-Type": content_type,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
                payload = response.read()
        except urllib.error.HTTPError as exc:
            raise RecoverySmokeError(
                f"API {method} {path} failed: {exc.code} {exc.read().decode(errors='replace')}"
            ) from exc
        return json.loads(payload) if payload else None

    def sql(self, statement: str, *, tuples: bool = True) -> str:
        arguments = self.compose + [
            "exec",
            "-T",
            "--user",
            "postgres",
            "postgres",
            "psql",
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--username",
            "remoteagent",
            "--dbname",
            "remoteagent",
        ]
        if tuples:
            arguments.extend(["--tuples-only", "--no-align"])
        arguments.extend(["--command", statement])
        return self.run(arguments).stdout.strip()

    def backup(self, path: Path) -> None:
        self.run(self.remotectl + ["backup", "create", "--output", str(path)])
        self.run(self.remotectl + ["backup", "verify", str(path)])

    def restore(
        self, path: Path, *, failpoints: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        environment = None
        if failpoints:
            environment = {
                "REMOTEAGENT_ALLOW_RESTORE_FAILPOINTS": "true",
                "REMOTEAGENT_RESTORE_FAILPOINT": failpoints,
            }
        return self.run(
            self.remotectl + ["restore", str(path), "--yes"],
            check=failpoints is None,
            extra_env=environment,
        )

    def companion_source(self, conversation_key: str) -> Path:
        relative = self.sql(
            "SELECT source_storage_path FROM conversation_companions "
            f"WHERE conversation_key = {_quoted(conversation_key)} ORDER BY version LIMIT 1"
        )
        if not relative:
            raise RecoverySmokeError("restored companion has no accepted-source path")
        root = (self.state / "conversations" / conversation_key).resolve()
        candidate = root / relative
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise RecoverySmokeError("restored companion source escaped or is missing") from exc
        if candidate.is_symlink() or not resolved.is_file():
            raise RecoverySmokeError("restored companion accepted source is not a regular file")
        return resolved

    def cleanup(self) -> None:
        # Cleanup has its own budget: the drill deadline is most likely to be
        # exhausted on the failure path where cleanup matters most. Preserve the
        # env/state directory whenever disposal cannot be proven so the operator
        # retains the exact project identity and credentials needed to recover.
        failures: list[str] = []
        cleanup_deadline = time.monotonic() + CLEANUP_COMMAND_TIMEOUT_SECONDS

        def cleanup_run(arguments: list[str]) -> subprocess.CompletedProcess[str]:
            remaining = cleanup_deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, CLEANUP_COMMAND_TIMEOUT_SECONDS)
            return subprocess.run(
                arguments,
                cwd=ROOT,
                env=self.environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
            )

        down_arguments = self.compose + [
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "30",
        ]
        self.commands.append(" ".join(down_arguments[:4]))
        try:
            down = cleanup_run(down_arguments)
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"Compose cleanup could not complete: {exc}")
        else:
            if down.returncode != 0:
                detail = (down.stderr or down.stdout).strip() or "no diagnostic"
                failures.append(f"Compose cleanup failed: {detail[-1000:]}")

        inventories = {
            "containers": [
                "docker",
                "container",
                "ls",
                "--all",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            "volumes": [
                "docker",
                "volume",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
            "networks": [
                "docker",
                "network",
                "ls",
                "--quiet",
                "--filter",
                f"label=com.docker.compose.project={self.project}",
            ],
        }
        for kind, arguments in inventories.items():
            try:
                inventory = cleanup_run(arguments)
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"could not verify disposable {kind}: {exc}")
                continue
            if inventory.returncode != 0:
                detail = (inventory.stderr or inventory.stdout).strip() or "no diagnostic"
                failures.append(f"could not verify disposable {kind}: {detail[-1000:]}")
                continue
            remaining = inventory.stdout.split()
            if remaining:
                failures.append(f"disposable {kind} remain: {', '.join(remaining[:20])}")

        if failures:
            raise RecoverySmokeError(
                "; ".join(failures)
                + f"; recovery state preserved at {self.temporary} for project {self.project}"
            )
        shutil.rmtree(self.temporary)


def _quoted(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RecoverySmokeError(message)


def _assert_markers_in_order(output: str, first: str, second: str, message: str) -> None:
    first_index = output.find(first)
    second_index = output.find(second)
    _assert(
        first_index >= 0 and second_index >= 0 and first_index < second_index,
        message,
    )


def run_drill(timeout: int) -> dict[str, Any]:
    docker_endpoint = _require_local_docker_daemon()
    drill = Drill(timeout, docker_endpoint)
    current_archive = drill.temporary / "current.tar.gz"
    pre_cron_archive = drill.temporary / "pre-cron.tar.gz"
    conversation = f"recovery_{drill.token}"
    companion_data = b"recovery companion\n"
    artifact_data = b"recovery artifact\n"
    artifact_digest = hashlib.sha256(artifact_data).hexdigest()
    artifact_id = f"a_{secrets.token_hex(16)}"
    primary_error: BaseException | None = None
    try:
        drill.run(drill.remotectl + ["agent", "validate", "--all"])
        drill.start()

        companion_digest = hashlib.sha256(companion_data).hexdigest()
        query = urllib.parse.urlencode(
            {"filename": "evidence.txt", "kind": "file", "sha256": companion_digest}
        )
        stage = drill.api(
            "POST",
            f"/api/v1/companion-stages/uploads?{query}",
            body=companion_data,
            content_type="application/octet-stream",
        )
        accepted = drill.api(
            "POST",
            "/api/v1/jobs",
            body=json.dumps(
                {
                    "agent_id": "joke-agent",
                    "prompt": "Recovery drill fixture.",
                    "conversation_key": conversation,
                    "idempotency_key": f"recovery-{drill.token}",
                    "companions": [{"stage_id": stage["id"], "name": "evidence"}],
                }
            ).encode(),
        )
        job_id = accepted["job_id"]
        drill.api("POST", f"/api/v1/jobs/{job_id}/cancel", body=b"{}")
        artifact_path = drill.state / "artifact-store" / job_id / "evidence.txt"
        artifact_path.parent.mkdir(parents=True, mode=0o700)
        artifact_path.write_bytes(artifact_data)
        artifact_path.chmod(0o600)
        drill.sql(
            "UPDATE conversations SET codex_thread_id = 'thread-before-backup' "
            f"WHERE key = {_quoted(conversation)};"
            "INSERT INTO artifacts "
            "(id, job_id, conversation_key, relative_path, storage_path, media_type, "
            "size_bytes, sha256, created_at) VALUES ("
            f"{_quoted(artifact_id)}, {_quoted(job_id)}, {_quoted(conversation)}, "
            f"'evidence.txt', {_quoted(str(artifact_path))}, 'text/plain', "
            f"{len(artifact_data)}, {_quoted(artifact_digest)}, CURRENT_TIMESTAMP);",
            tuples=False,
        )
        drill.backup(current_archive)

        # Produce a genuine older-schema archive with router state but no cron
        # tables/version record, then let normal startup migrate it forward.
        drill.run(drill.compose + ["stop", "cron", "router"])
        drill.sql(
            "DROP TABLE IF EXISTS cron_responses, cron_response_leases, cron_executions, "
            "cron_schedule_revisions, cron_schedules CASCADE; "
            "DROP TABLE IF EXISTS cron_alembic_version;",
            tuples=False,
        )
        drill.backup(pre_cron_archive)
        drill.start()

        marker = drill.state / "conversations" / "newer-object"
        marker.write_text("must disappear", encoding="utf-8")
        artifact_path.write_bytes(b"newer artifact")
        drill.sql(
            "UPDATE conversations SET codex_thread_id = 'thread-after-backup' "
            f"WHERE key = {_quoted(conversation)}; CREATE TABLE newer_restore_object(id integer);",
            tuples=False,
        )
        restored = drill.restore(current_archive)
        _assert_markers_in_order(
            restored.stderr,
            "starting router after restored database",
            "starting cron after router is healthy",
            "restore did not start router before cron",
        )
        _assert(
            drill.sql(
                f"SELECT codex_thread_id FROM conversations WHERE key = {_quoted(conversation)}"
            )
            == "thread-before-backup",
            "current restore lost continuation identity",
        )
        _assert(drill.sql("SELECT to_regclass('public.newer_restore_object')") == "", "newer DB object survived restore")
        _assert(not marker.exists(), "newer filesystem object survived restore")
        _assert(_sha256(artifact_path) == artifact_digest, "artifact bytes changed across restore")
        artifact = drill.api("GET", f"/api/v1/artifacts/{artifact_id}")
        _assert(artifact["sha256"] == artifact_digest, "artifact metadata hash changed")
        companions = drill.api("GET", f"/api/v1/conversations/{conversation}/companions")
        _assert(
            len(companions) == 1 and companions[0]["sha256"] == companion_digest,
            "companion hash changed across restore",
        )
        companion_source = drill.companion_source(conversation)
        _assert(
            companion_source.read_bytes() == companion_data
            and _sha256(companion_source) == companion_digest,
            "companion accepted-source bytes changed across restore",
        )

        drill.sql("CREATE TABLE newer_restore_object(id integer);", tuples=False)
        drill.restore(pre_cron_archive)
        _assert(drill.sql("SELECT to_regclass('public.newer_restore_object')") == "", "pre-cron restore retained newer object")
        _assert(
            drill.sql("SELECT to_regclass('public.cron_schedules')") == "cron_schedules",
            "cron did not migrate the pre-cron archive",
        )
        _assert(drill.sql("SELECT count(*) FROM cron_schedules") == "0", "pre-cron restore retained cron rows")
        _assert(
            drill.sql(
                f"SELECT codex_thread_id FROM conversations WHERE key = {_quoted(conversation)}"
            )
            == "thread-before-backup",
            "pre-cron restore lost continuation identity",
        )
        pre_cron_companion_source = drill.companion_source(conversation)
        _assert(
            pre_cron_companion_source.read_bytes() == companion_data
            and _sha256(pre_cron_companion_source) == companion_digest,
            "pre-cron restore lost companion accepted-source bytes",
        )

        # A tree-swap failure occurs before PostgreSQL mutation and restores both
        # predecessors. A database failure rolls both trees back. The final
        # combined failpoint proves rollback failure leaves all services stopped.
        artifact_path.write_bytes(b"before swap failure")
        drill.sql(
            "UPDATE conversations SET codex_thread_id = 'swap-failure-marker' "
            f"WHERE key = {_quoted(conversation)}",
            tuples=False,
        )
        failure = drill.restore(current_archive, failpoints="swap-artifact-store")
        _assert(failure.returncode != 0 and "database was untouched" in failure.stderr, "tree-swap failpoint did not fail safely")
        drill.run(drill.compose + ["up", "-d", "--wait", "postgres"])
        _assert(
            drill.sql(
                f"SELECT codex_thread_id FROM conversations WHERE key = {_quoted(conversation)}"
            )
            == "swap-failure-marker",
            "tree-swap failure mutated PostgreSQL",
        )
        _assert(artifact_path.read_bytes() == b"before swap failure", "tree-swap rollback lost prior artifact tree")
        drill.start()

        artifact_path.write_bytes(b"before database failure")
        drill.sql(
            "UPDATE conversations SET codex_thread_id = 'database-failure-marker' "
            f"WHERE key = {_quoted(conversation)}",
            tuples=False,
        )
        failure = drill.restore(current_archive, failpoints="database-restore")
        _assert(failure.returncode != 0 and "transaction rolled back" in failure.stderr, "database failpoint did not roll back")
        drill.run(drill.compose + ["up", "-d", "--wait", "postgres"])
        _assert(
            drill.sql(
                f"SELECT codex_thread_id FROM conversations WHERE key = {_quoted(conversation)}"
            )
            == "database-failure-marker",
            "database failure changed the prior database",
        )
        _assert(artifact_path.read_bytes() == b"before database failure", "database failure did not restore both trees")
        drill.start()

        failure = drill.restore(
            current_archive,
            failpoints="database-restore,rollback-conversations",
        )
        _assert(failure.returncode != 0 and "rollback failed" in failure.stderr, "rollback failpoint was not observed")
        running = drill.run(
            drill.compose
            + ["ps", "--status", "running", "--quiet", "postgres", "redis", "router", "cron"]
        ).stdout.strip()
        _assert(not running, "services were running after a failed rollback")

        return {
            "status": "passed",
            "project": drill.project,
            "current_archive": "verified",
            "pre_cron_archive": "verified_and_migrated",
            "continuation_identity": "verified",
            "companion_hash": companion_digest,
            "artifact_hash": artifact_digest,
            "newer_objects_removed": True,
            "rollback_failpoints": [
                "swap-artifact-store",
                "database-restore",
                "database-restore,rollback-conversations",
            ],
            "startup_order": ["router", "cron"],
        }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            drill.cleanup()
        except BaseException as cleanup_error:
            if primary_error is not None:
                raise RecoverySmokeError(
                    f"recovery drill failed: {primary_error}; cleanup also failed: {cleanup_error}"
                ) from primary_error
            raise


def main() -> int:
    timeout = int(os.environ.get("REMOTEAGENT_RECOVERY_SMOKE_TIMEOUT", "1800"))
    if not 60 <= timeout <= 7200:
        print("recovery smoke timeout must be between 60 and 7200 seconds", file=sys.stderr)
        return 2
    try:
        result = run_drill(timeout)
    except (RecoverySmokeError, OSError, ValueError) as exc:
        print(f"recovery smoke failed: {exc}", file=sys.stderr)
        return 1
    if os.environ.get("REMOTEAGENT_RECOVERY_SMOKE_JSON") == "1":
        print(json.dumps(result, sort_keys=True))
    else:
        print("recovery smoke passed")
        print("  current and pre-cron archives: verified")
        print("  continuation, companion, and artifact identity: verified")
        print("  rollback failpoints and router-before-cron startup: verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
