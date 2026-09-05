#!/usr/bin/env python3
"""Run destructive PostgreSQL acceptance checks in a disposable local container.

The harness never joins or addresses the deployment Compose project. Every
Docker object, database identity, host port, and filesystem state root is
generated for this invocation and removed on exit.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
for source_root in (ROOT_DIR / "router" / "src", ROOT_DIR / "cron" / "src"):
    source = str(source_root)
    if source not in sys.path:
        sys.path.insert(0, source)

POSTGRES_IMAGE = "postgres:17.6-alpine"
RESOURCE_PREFIX = "remoteagent-smoke-postgres-"
OWNER_LABEL_KEY = "com.remoteagent.postgres-smoke.owner"
TOKEN_RE = re.compile(r"^[0-9a-f]{12}$")
START_TIMEOUT_SECONDS = 60
MIGRATION_TIMEOUT_SECONDS = 90
MIGRATION_CONTENTION_TIMEOUT_SECONDS = 30
SMOKE_TIMEOUT_SECONDS = 180
MIGRATION_LOCK_SQL = (
    "SELECT pg_advisory_xact_lock(hashtext('remoteagent-schema-migrations'))",
    "SELECT pg_advisory_xact_lock(hashtext('remoteagent-cron-schema-migrations'))",
)
MIGRATION_WAITER_SQL = """
SELECT count(*)
FROM pg_stat_activity
WHERE datname = current_database()
  AND pid <> pg_backend_pid()
  AND wait_event_type = 'Lock'
  AND wait_event = 'advisory'
"""


class SmokeFailure(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PostgresEndpoint:
    container: str
    volume: str
    database: str
    user: str
    host: str
    port: int
    url: str


class DisposablePostgres:
    """Own one strictly named local-only PostgreSQL container and volume."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or uuid.uuid4().hex[:12]
        if not TOKEN_RE.fullmatch(self.token):
            raise ValueError("disposable PostgreSQL token must be 12 lowercase hex characters")
        self.container = f"{RESOURCE_PREFIX}{self.token}"
        self.volume = f"{self.container}-data"
        self.database = f"ra_smoke_{self.token}"
        self.user = f"ra_smoke_{self.token}"
        self.password = uuid.uuid4().hex + uuid.uuid4().hex
        self.owner_id = uuid.uuid4().hex
        # Set before create/run so cleanup also covers an ambiguous CLI outcome.
        self._volume_created = False
        self._container_created = False

    @staticmethod
    def _validate_target(name: str, *, volume: bool = False) -> None:
        suffix = "-data" if volume else ""
        pattern = rf"{re.escape(RESOURCE_PREFIX)}[0-9a-f]{{12}}{re.escape(suffix)}"
        if re.fullmatch(pattern, name) is None:
            raise SmokeFailure(f"refusing to manage non-smoke Docker object: {name}")

    @staticmethod
    def _run(
        arguments: list[str],
        *,
        check: bool,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                arguments,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SmokeFailure(f"Docker command could not complete: {exc}") from exc
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            raise SmokeFailure(f"Docker command failed: {detail[:2000]}")
        return result

    def _require_local_daemon(self) -> None:
        configured_host = os.environ.get("DOCKER_HOST", "").strip()
        if configured_host and not configured_host.startswith(("unix://", "npipe://")):
            raise SmokeFailure("smoke postgres refuses a non-local DOCKER_HOST")
        context = self._run(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{(index .Endpoints \"docker\").Host}}",
            ],
            check=True,
        )
        endpoint = context.stdout.strip()
        if not endpoint.startswith(("unix://", "npipe://")):
            raise SmokeFailure("smoke postgres refuses a non-local Docker context")

    def _resource_ownership(self, *, kind: str, name: str) -> str:
        if kind == "container":
            label_template = f'{{{{ index .Config.Labels "{OWNER_LABEL_KEY}" }}}}'
            arguments = [
                "docker",
                "container",
                "inspect",
                "--format",
                label_template,
                name,
            ]
        elif kind == "volume":
            label_template = f'{{{{ index .Labels "{OWNER_LABEL_KEY}" }}}}'
            arguments = [
                "docker",
                "volume",
                "inspect",
                "--format",
                label_template,
                name,
            ]
        else:  # pragma: no cover - internal callers pass fixed kinds
            raise AssertionError(f"unsupported Docker resource kind: {kind}")
        result = self._run(arguments, check=False)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            lowered = detail.lower()
            if "no such container" in lowered or "no such volume" in lowered:
                return "absent"
            raise SmokeFailure(
                f"could not establish {kind} ownership before cleanup: "
                f"{detail[:1000] or 'no diagnostic'}"
            )
        if result.stdout.strip() == self.owner_id:
            return "owned"
        return "unowned"

    def _cleanup_resource(
        self,
        *,
        kind: str,
        name: str,
        remove_arguments: list[str],
    ) -> tuple[bool, str | None]:
        try:
            ownership = self._resource_ownership(kind=kind, name=name)
        except SmokeFailure as exc:
            return True, f"{kind} cleanup failed: {exc}"
        if ownership == "absent":
            return False, None
        if ownership != "owned":
            return (
                False,
                f"{kind} cleanup refused: ownership label does not match this smoke run",
            )
        try:
            result = self._run(remove_arguments, check=False, timeout=30)
        except SmokeFailure as exc:
            return True, f"{kind} cleanup failed: {exc}"
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
            return True, f"{kind} cleanup failed: {detail[:1000]}"
        return False, None

    def start(self) -> PostgresEndpoint:
        self._require_local_daemon()
        self._validate_target(self.container)
        self._validate_target(self.volume, volume=True)
        if (
            self._run(
                ["docker", "container", "inspect", self.container],
                check=False,
            ).returncode
            == 0
        ):
            raise SmokeFailure("generated disposable container name is already in use")
        if (
            self._run(
                ["docker", "volume", "inspect", self.volume],
                check=False,
            ).returncode
            == 0
        ):
            raise SmokeFailure("generated disposable volume name is already in use")
        label = f"{OWNER_LABEL_KEY}={self.owner_id}"
        self._volume_created = True
        self._run(
            ["docker", "volume", "create", "--label", label, self.volume],
            check=True,
        )
        docker_env = dict(os.environ)
        docker_env["POSTGRES_PASSWORD"] = self.password
        self._container_created = True
        self._run(
            [
                "docker",
                "run",
                "--detach",
                "--pull=missing",
                "--name",
                self.container,
                "--label",
                label,
                "--env",
                "POSTGRES_PASSWORD",
                "--env",
                f"POSTGRES_USER={self.user}",
                "--env",
                f"POSTGRES_DB={self.database}",
                "--publish",
                "127.0.0.1::5432",
                "--mount",
                f"type=volume,source={self.volume},target=/var/lib/postgresql/data",
                POSTGRES_IMAGE,
            ],
            check=True,
            env=docker_env,
            timeout=START_TIMEOUT_SECONDS,
        )

        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        last_detail = "PostgreSQL did not report readiness"
        while time.monotonic() < deadline:
            ready = self._run(
                [
                    "docker",
                    "exec",
                    self.container,
                    "pg_isready",
                    "--username",
                    self.user,
                    "--dbname",
                    self.database,
                ],
                check=False,
                timeout=10,
            )
            if ready.returncode == 0:
                break
            last_detail = ready.stderr.strip() or ready.stdout.strip() or last_detail
            time.sleep(0.5)
        else:
            raise SmokeFailure(f"disposable PostgreSQL did not become ready: {last_detail}")

        inspected = self._run(
            [
                "docker",
                "inspect",
                "--format",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                self.container,
            ],
            check=True,
        )
        raw_port = inspected.stdout.strip()
        if not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
            raise SmokeFailure("Docker did not assign a valid ephemeral PostgreSQL port")
        port = int(raw_port)
        url = f"postgresql+asyncpg://{self.user}:{self.password}@127.0.0.1:{port}/{self.database}"
        return PostgresEndpoint(
            container=self.container,
            volume=self.volume,
            database=self.database,
            user=self.user,
            host="127.0.0.1",
            port=port,
            url=url,
        )

    def cleanup(self) -> list[str]:
        self._validate_target(self.container)
        self._validate_target(self.volume, volume=True)
        failures: list[str] = []
        if self._container_created:
            self._container_created, failure = self._cleanup_resource(
                kind="container",
                name=self.container,
                remove_arguments=["docker", "rm", "--force", self.container],
            )
            if failure is not None:
                failures.append(failure)
        if self._volume_created:
            self._volume_created, failure = self._cleanup_resource(
                kind="volume",
                name=self.volume,
                remove_arguments=["docker", "volume", "rm", "--force", self.volume],
            )
            if failure is not None:
                failures.append(failure)
        return failures

    @contextmanager
    def running(self) -> Iterator[PostgresEndpoint]:
        primary_error: BaseException | None = None
        try:
            yield self.start()
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            cleanup_failures = self.cleanup()
            if cleanup_failures:
                cleanup_error = SmokeFailure("; ".join(cleanup_failures))
                if primary_error is not None:
                    raise cleanup_error from primary_error
                raise cleanup_error


def _redact(value: str, endpoint: PostgresEndpoint) -> str:
    redacted = value.replace(endpoint.url, "[REDACTED_DATABASE_URL]")
    return re.sub(
        r"postgresql\+asyncpg://[^\s@]+@",
        "postgresql+asyncpg://[REDACTED]@",
        redacted,
    )


async def _migration_process(
    endpoint: PostgresEndpoint,
    *,
    component: str,
    ordinal: int,
) -> None:
    component_root = ROOT_DIR / component
    environment = dict(os.environ)
    for name in (
        "DATABASE_URL",
        "REMOTEAGENT_DATABASE_URL",
        "REMOTEAGENT_CRON_DATABASE_URL",
    ):
        environment.pop(name, None)
    database_variable = (
        "REMOTEAGENT_DATABASE_URL"
        if component == "router"
        else "REMOTEAGENT_CRON_DATABASE_URL"
    )
    environment[database_variable] = endpoint.url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(component_root / "alembic.ini"),
        "upgrade",
        "head",
        cwd=component_root,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=MIGRATION_TIMEOUT_SECONDS
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    except TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise SmokeFailure(f"{component} migration process {ordinal} timed out") from exc
    if process.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace")
        raise SmokeFailure(
            f"{component} migration process {ordinal} failed: "
            f"{_redact(detail, endpoint)[-4000:]}"
        )


MigrationRunner = Callable[..., Awaitable[None]]


async def _wait_for_external_database(
    engine: Any,
    endpoint: PostgresEndpoint,
    *,
    timeout_seconds: float = START_TIMEOUT_SECONDS,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_error = "database connection did not become ready"
    while True:
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql("SELECT 1")
            return
        except Exception as exc:  # noqa: BLE001 - readiness retries driver exceptions
            last_error = _redact(str(exc), endpoint)
        if asyncio.get_running_loop().time() >= deadline:
            raise SmokeFailure(
                f"disposable PostgreSQL host connection did not become ready: {last_error}"
            )
        await asyncio.sleep(0.1)


async def _wait_for_migration_contention(
    observer_connection: Any,
    migration_tasks: list[asyncio.Task[None]],
    *,
    expected_waiters: int,
    timeout_seconds: float = MIGRATION_CONTENTION_TIMEOUT_SECONDS,
) -> int:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    observed_waiters = 0
    while True:
        result = await observer_connection.exec_driver_sql(MIGRATION_WAITER_SQL)
        current_waiters = int(result.scalar_one())
        # PostgreSQL caches cumulative-statistics snapshots for a transaction.
        # End this observer transaction so the next poll can see new waiters.
        await observer_connection.rollback()
        observed_waiters = max(observed_waiters, current_waiters)
        if observed_waiters >= expected_waiters:
            return observed_waiters
        completed = [task for task in migration_tasks if task.done()]
        if completed:
            details: list[str] = []
            for task in completed:
                if task.cancelled():
                    details.append("cancelled")
                else:
                    error = task.exception()
                    details.append("completed" if error is None else str(error))
            raise SmokeFailure(
                "migration process exited before advisory-lock contention was proven: "
                + "; ".join(details)
            )
        if asyncio.get_running_loop().time() >= deadline:
            raise SmokeFailure(
                "timed out proving concurrent migration advisory-lock contention: "
                f"expected {expected_waiters}, observed {observed_waiters}"
            )
        await asyncio.sleep(0.05)


async def _run_contended_migration_processes(
    endpoint: PostgresEndpoint,
    blocker_connection: Any,
    observer_connection: Any,
    *,
    process_runner: MigrationRunner | None = None,
    contention_timeout_seconds: float = MIGRATION_CONTENTION_TIMEOUT_SECONDS,
) -> int:
    runner = process_runner or _migration_process
    transaction = await blocker_connection.begin()
    tasks: list[asyncio.Task[None]] = []
    try:
        for statement in MIGRATION_LOCK_SQL:
            await blocker_connection.exec_driver_sql(statement)
        tasks = [
            asyncio.create_task(
                runner(endpoint, component=component, ordinal=ordinal),
                name=f"postgres-smoke-{component}-migration-{ordinal}",
            )
            for component in ("router", "cron")
            for ordinal in (1, 2)
        ]
        waiters = await _wait_for_migration_contention(
            observer_connection,
            tasks,
            expected_waiters=len(tasks),
            timeout_seconds=contention_timeout_seconds,
        )
    except BaseException:
        for task in tasks:
            task.cancel()
        try:
            if transaction.is_active:
                await transaction.rollback()
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise
    if transaction.is_active:
        await transaction.rollback()
    await asyncio.gather(*tasks)
    return waiters


async def check_concurrent_migrations(endpoint: PostgresEndpoint) -> dict[str, Any]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    router_config = Config(str(ROOT_DIR / "router" / "alembic.ini"))
    router_config.set_main_option("script_location", str(ROOT_DIR / "router" / "migrations"))
    cron_config = Config(str(ROOT_DIR / "cron" / "alembic.ini"))
    cron_config.set_main_option("script_location", str(ROOT_DIR / "cron" / "migrations"))
    expected = {
        "router": ScriptDirectory.from_config(router_config).get_current_head(),
        "cron": ScriptDirectory.from_config(cron_config).get_current_head(),
    }
    engine = create_async_engine(endpoint.url, pool_pre_ping=True)
    try:
        await _wait_for_external_database(engine, endpoint)
        async with engine.connect() as blocker, engine.connect() as observer:
            lock_waiters = await _run_contended_migration_processes(
                endpoint,
                blocker,
                observer,
            )
        async with engine.connect() as connection:
            actual = {
                "router": await connection.scalar(text("SELECT version_num FROM alembic_version")),
                "cron": await connection.scalar(
                    text("SELECT version_num FROM cron_alembic_version")
                ),
            }
    finally:
        await engine.dispose()
    if actual != expected:
        raise SmokeFailure(f"migration heads differ: expected={expected!r}, actual={actual!r}")
    return {
        "processes": 4,
        "heads": actual,
        "advisory_lock_waiters": lock_waiters,
        "contention_verified": True,
    }


async def check_router_lease(endpoint: PostgresEndpoint) -> dict[str, Any]:
    from sqlalchemy import select, update

    from remoteagent.db import create_engine, create_session_factory
    from remoteagent.lease import LeaseManager
    from remoteagent.models import LeaseRecord

    engine_a = create_engine(endpoint.url)
    engine_b = create_engine(endpoint.url)
    factory_a = create_session_factory(engine_a)
    factory_b = create_session_factory(engine_b)
    manager_a = LeaseManager(factory_a, ttl_seconds=30, retry_seconds=0.01, instance_id="a")
    manager_b = LeaseManager(factory_b, ttl_seconds=30, retry_seconds=0.01, instance_id="b")
    try:
        acquired = await asyncio.gather(
            manager_a.try_acquire("subscription", "race"),
            manager_b.try_acquire("subscription", "race"),
        )
        if sum(handle is not None for handle in acquired) != 1:
            raise SmokeFailure("lease race did not produce exactly one owner")
        if acquired[0] is not None:
            winner, loser, original = manager_a, manager_b, acquired[0]
        else:
            winner, loser, original = manager_b, manager_a, acquired[1]
        assert original is not None
        if not await winner.renew(original):
            raise SmokeFailure("current lease owner could not renew")
        if await loser.try_acquire("subscription", "race") is not None:
            raise SmokeFailure("lease was acquired before the current owner expired")

        async with factory_b() as session, session.begin():
            await session.execute(
                update(LeaseRecord)
                .where(LeaseRecord.name == original.name)
                .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        successor = await loser.try_acquire("subscription", "race")
        if successor is None or successor.fencing_token <= original.fencing_token:
            raise SmokeFailure("expired lease was not taken over with a newer fencing token")
        if await winner.renew(original):
            raise SmokeFailure("stale lease owner renewed after takeover")
        await winner.release(original)
        async with factory_b() as session:
            retained = await session.scalar(
                select(LeaseRecord).where(LeaseRecord.name == successor.name)
            )
        if (
            retained is None
            or retained.owner != successor.owner
            or retained.fencing_token != successor.fencing_token
        ):
            raise SmokeFailure("stale release removed or changed the successor lease")
        await loser.release(successor)
        return {
            "race_winners": 1,
            "renewed": True,
            "takeover_token": successor.fencing_token,
            "stale_release_preserved_successor": True,
        }
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


async def _one_chunk(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def check_router_conversation_and_companions(
    endpoint: PostgresEndpoint, state_root: Path
) -> dict[str, Any]:
    from sqlalchemy import select

    from remoteagent.cache import MemoryCache
    from remoteagent.companions import CompanionConflictError, CompanionService
    from remoteagent.db import create_engine, create_session_factory
    from remoteagent.jobs import JobService
    from remoteagent.models import (
        AgentRecord,
        AgentRevisionRecord,
        CompanionStageRecord,
        ConversationCompanionRecord,
        JobRecord,
    )
    from remoteagent.schemas import CompanionStageKind, PromptRequest
    from remoteagent.workspace import WorkspaceManager

    engine_a = create_engine(endpoint.url)
    engine_b = create_engine(endpoint.url)
    factory_a = create_session_factory(engine_a)
    factory_b = create_session_factory(engine_b)
    agent_id = "postgres-smoke"
    conversation_key = f"c_{uuid.uuid4().hex}"
    workspaces = WorkspaceManager(state_root)
    staging_root = state_root / "companion-staging"

    def companion(factory: Any) -> CompanionService:
        return CompanionService(
            factory,
            staging_root,
            workspaces.root,
            max_upload_bytes=1024 * 1024,
            max_archive_bytes=1024 * 1024,
            max_git_mirror_bytes=1024 * 1024,
            max_git_checkout_bytes=1024 * 1024,
            max_files=100,
            max_staging_bytes=1024 * 1024,
            git_workers=1,
            git_timeout_seconds=5,
        )

    companion_a = companion(factory_a)
    companion_b = companion(factory_b)
    service_a = JobService(
        factory_a,
        workspaces,
        MemoryCache("postgres-smoke-a"),
        companion_service=companion_a,
    )
    service_b = JobService(
        factory_b,
        workspaces,
        MemoryCache("postgres-smoke-b"),
        companion_service=companion_b,
    )
    try:
        async with factory_a() as session, session.begin():
            session.add(
                AgentRecord(
                    id=agent_id,
                    name="PostgreSQL smoke",
                    description="Disposable integration fixture",
                    compose_file="postgres-smoke/compose.yaml",
                    project_name="postgres-smoke",
                    runner_service="agent",
                    dependency_services=[],
                    environment={},
                    labels={"test": "postgres"},
                    definition_metadata={},
                    enabled=True,
                    current_revision=1,
                )
            )
            session.add(
                AgentRevisionRecord(
                    agent_id=agent_id,
                    revision=1,
                    config_toml="",
                    base_context="",
                    definition_snapshot={"id": agent_id},
                    checksum="0" * 64,
                )
            )

        await service_a.submit(
            PromptRequest(
                agent_id=agent_id,
                prompt="first",
                conversation_key=conversation_key,
                idempotency_key="postgres-smoke-first",
            )
        )
        await asyncio.gather(
            service_a.submit(
                PromptRequest(
                    agent_id=agent_id,
                    prompt="second-a",
                    conversation_key=conversation_key,
                    idempotency_key="postgres-smoke-second-a",
                )
            ),
            service_b.submit(
                PromptRequest(
                    agent_id=agent_id,
                    prompt="second-b",
                    conversation_key=conversation_key,
                    idempotency_key="postgres-smoke-second-b",
                )
            ),
        )
        async with factory_a() as session:
            sequences = list(
                await session.scalars(
                    select(JobRecord.sequence)
                    .where(JobRecord.conversation_key == conversation_key)
                    .order_by(JobRecord.sequence)
                )
            )
        if sequences != [1, 2, 3]:
            raise SmokeFailure(f"concurrent conversation sequence was not contiguous: {sequences}")

        stage = await companion_a.stage_upload(
            _one_chunk(b"postgres companion smoke\n"),
            filename="fixture.txt",
            kind=CompanionStageKind.FILE,
        )
        claim_results = await asyncio.gather(
            service_a.submit(
                PromptRequest(
                    agent_id=agent_id,
                    prompt="claim-a",
                    conversation_key=conversation_key,
                    idempotency_key="postgres-smoke-claim-a",
                    companions=[{"stage_id": stage.id, "name": "fixture"}],
                )
            ),
            service_b.submit(
                PromptRequest(
                    agent_id=agent_id,
                    prompt="claim-b",
                    conversation_key=conversation_key,
                    idempotency_key="postgres-smoke-claim-b",
                    companions=[{"stage_id": stage.id, "name": "fixture"}],
                )
            ),
            return_exceptions=True,
        )
        successes = [result for result in claim_results if not isinstance(result, BaseException)]
        conflicts = [result for result in claim_results if isinstance(result, CompanionConflictError)]
        if len(successes) != 1 or len(conflicts) != 1:
            raise SmokeFailure(
                "single-use companion race did not produce one claim and one conflict: "
                f"{[type(result).__name__ for result in claim_results]}"
            )
        async with factory_a() as session:
            stage_record = await session.get(CompanionStageRecord, stage.id)
            companion_rows = list(
                await session.scalars(
                    select(ConversationCompanionRecord).where(
                        ConversationCompanionRecord.stage_id == stage.id
                    )
                )
            )
            final_sequences = list(
                await session.scalars(
                    select(JobRecord.sequence)
                    .where(JobRecord.conversation_key == conversation_key)
                    .order_by(JobRecord.sequence)
                )
            )
        if stage_record is None or stage_record.claimed_at is None or len(companion_rows) != 1:
            raise SmokeFailure("winning companion claim was not persisted exactly once")
        if final_sequences != [1, 2, 3, 4]:
            raise SmokeFailure(f"failed companion claim consumed a sequence: {final_sequences}")
        return {
            "conversation_sequences": final_sequences,
            "companion_claim_winners": 1,
            "companion_claim_conflicts": 1,
        }
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


@dataclass
class MutableClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    async def sleep(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class UnusedRouter:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"cron response lease smoke unexpectedly called router.{name}")


async def check_cron_response_leases(endpoint: PostgresEndpoint) -> dict[str, Any]:
    from sqlalchemy import select

    from remoteagent_cron.config import Settings
    from remoteagent_cron.db import create_engine, create_session_factory
    from remoteagent_cron.models import (
        ExecutionRecord,
        ResponseRecord,
        ScheduleRecord,
        ScheduleRevisionRecord,
    )
    from remoteagent_cron.schemas import LeaseResponsesRequest
    from remoteagent_cron.service import CronService

    engine_a = create_engine(endpoint.url)
    engine_b = create_engine(endpoint.url)
    factory_a = create_session_factory(engine_a)
    factory_b = create_session_factory(engine_b)
    clock = MutableClock(datetime.now(UTC))
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url=endpoint.url,
        scheduler_enabled=False,
        lease_seconds=30,
        response_batch_limit=10,
        response_batch_bytes=1024 * 1024,
    )
    service_a = CronService(settings, factory_a, UnusedRouter(), clock=clock)  # type: ignore[arg-type]
    service_b = CronService(settings, factory_b, UnusedRouter(), clock=clock)  # type: ignore[arg-type]
    schedule_id = "postgres-smoke"
    generation_id = str(uuid.uuid4())
    revision_id = str(uuid.uuid4())
    now = clock.now()
    execution_ids: list[str] = []
    try:
        async with factory_a() as session, session.begin():
            session.add(
                ScheduleRecord(
                    id=schedule_id,
                    generation_id=generation_id,
                    status="enabled",
                    enabled=True,
                    current_revision=1,
                    current_revision_id=revision_id,
                    next_fire_at=None,
                    skipped_occurrences=0,
                )
            )
            session.add(
                ScheduleRevisionRecord(
                    id=revision_id,
                    schedule_id=schedule_id,
                    revision=1,
                    checksum="1" * 64,
                    cron_expression="* * * * *",
                    timezone="UTC",
                    agent_id="postgres-smoke",
                    prompt="run",
                    model=None,
                    reasoning_effort=None,
                    conversation_mode="fresh",
                )
            )
            for index in range(4):
                execution_id = str(uuid.uuid4())
                execution_ids.append(execution_id)
                session.add(
                    ExecutionRecord(
                        id=execution_id,
                        schedule_id=schedule_id,
                        generation_id=generation_id,
                        revision_id=revision_id,
                        scheduled_for=now + timedelta(seconds=index),
                        idempotency_key=f"postgres-smoke:{index}",
                        continuation_key=None,
                        state="succeeded",
                        router_job_id=f"job-{index}",
                        conversation_key=f"conversation-{index}",
                        started_at=now,
                        deadline_at=now + timedelta(minutes=5),
                        completed_at=now,
                    )
                )
                session.add(
                    ResponseRecord(
                        id=str(uuid.uuid4()),
                        execution_id=execution_id,
                        schedule_id=schedule_id,
                        revision_id=revision_id,
                        revision=1,
                        agent_id="postgres-smoke",
                        router_job_id=f"job-{index}",
                        conversation_key=f"conversation-{index}",
                        scheduled_for=now + timedelta(seconds=index),
                        completed_at=now,
                        model=None,
                        reasoning_effort=None,
                        usage=None,
                        result=f"result-{index}",
                    )
                )

        first, second = await asyncio.gather(
            service_a.lease_responses(LeaseResponsesRequest(schedule_id=schedule_id, limit=1)),
            service_b.lease_responses(LeaseResponsesRequest(schedule_id=schedule_id, limit=1)),
        )
        if (
            first.lease_id is None
            or second.lease_id is None
            or len(first.responses) != 1
            or len(second.responses) != 1
        ):
            raise SmokeFailure("concurrent cron response leases did not each acquire one response")
        leased_ids = {first.responses[0].response_id, second.responses[0].response_id}
        if len(leased_ids) != 2:
            raise SmokeFailure("concurrent cron response leases overlapped")

        clock.current += timedelta(seconds=settings.lease_seconds + 1)
        target = first.responses[0]
        replacement = await service_b.lease_responses(
            LeaseResponsesRequest(execution_id=target.execution_id, limit=1)
        )
        if replacement.lease_id is None or replacement.responses[0].response_id != target.response_id:
            raise SmokeFailure("expired cron response lease was not reassigned")
        stale = await service_a.acknowledge(first.lease_id)
        if stale.status != "stale" or stale.deleted_count != 0:
            raise SmokeFailure("stale cron response acknowledgement was not rejected")
        async with factory_a() as session:
            retained = await session.scalar(
                select(ResponseRecord).where(ResponseRecord.id == target.response_id)
            )
        if retained is None or retained.lease_id != replacement.lease_id:
            raise SmokeFailure("stale acknowledgement removed a re-leased cron response")
        return {
            "concurrent_disjoint_leases": 2,
            "expired_lease_reassigned": True,
            "stale_ack_preserved_response": True,
        }
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


async def run_checks(endpoint: PostgresEndpoint, state_root: Path) -> dict[str, Any]:
    migrations = await check_concurrent_migrations(endpoint)
    lease, conversation, cron_leases = await asyncio.gather(
        check_router_lease(endpoint),
        check_router_conversation_and_companions(endpoint, state_root),
        check_cron_response_leases(endpoint),
    )
    return {
        "concurrent_migrations": migrations,
        "router_lease": lease,
        "conversation_and_companions": conversation,
        "cron_response_leases": cron_leases,
    }


def main() -> int:
    json_output = os.environ.get("REMOTEAGENT_POSTGRES_SMOKE_JSON", "0") == "1"
    postgres = DisposablePostgres()
    started = time.monotonic()
    try:
        with postgres.running() as endpoint, tempfile.TemporaryDirectory(
            prefix=f"{RESOURCE_PREFIX}{postgres.token}-state-"
        ) as state_directory:
            checks = asyncio.run(
                asyncio.wait_for(
                    run_checks(endpoint, Path(state_directory)),
                    timeout=SMOKE_TIMEOUT_SECONDS,
                )
            )
            public_endpoint = {
                "container": endpoint.container,
                "volume": endpoint.volume,
                "database": endpoint.database,
                "host": endpoint.host,
                "port": endpoint.port,
            }
    except Exception as exc:
        detail = str(exc).replace(postgres.password, "[REDACTED]")
        if json_output:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": detail,
                        "container": postgres.container,
                        "volume": postgres.volume,
                        "cleanup_attempted": True,
                    },
                    sort_keys=True,
                )
            )
        else:
            print(f"PostgreSQL smoke failed: {detail}", file=sys.stderr)
        return 1

    result = {
        "ok": True,
        "postgres": public_endpoint,
        "checks": checks,
        "cleanup": {"container_removed": True, "volume_removed": True},
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    if json_output:
        print(json.dumps(result, sort_keys=True))
    else:
        print(
            "PostgreSQL smoke passed: migrations, lease fencing, conversation sequencing, "
            "companion claims, and cron response leases"
        )
        print(
            f"Disposable resources removed: {public_endpoint['container']} "
            f"and {public_endpoint['volume']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
