from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from remoteagent.agents import AgentService
from remoteagent.artifacts import ArtifactService
from remoteagent.cache import MemoryCache
from remoteagent.config import Settings
from remoteagent.db import create_engine, create_session_factory, initialize_schema
from remoteagent.jobs import JobService
from remoteagent.lease import LeaseCleanupError, LeaseHandle, LeaseLostError, LeaseManager
from remoteagent.models import JobRecord, LeaseRecord
from remoteagent.runtime import DockerComposeRuntime, RuntimeRequest, RuntimeResult
from remoteagent.scheduler import Scheduler
from remoteagent.schemas import AgentDefinition, JobStatus, PromptRequest
from remoteagent.telemetry import DashboardMetrics, TokenTelemetryCollector
from remoteagent.workspace import WorkspaceManager


class BlockingRuntime:
    def __init__(self, *, block_in: str) -> None:
        self.block_in = block_in
        self.started = asyncio.Event()
        self.released: list[str] = []
        self.release_cancelled = asyncio.Event()

    async def provision(self, request: RuntimeRequest) -> None:
        if self.block_in == "provision":
            self.started.set()
            await asyncio.Event().wait()

    async def run(self, request: RuntimeRequest, *, on_event, cancelled) -> RuntimeResult:
        if self.block_in == "run":
            self.started.set()
            await asyncio.Event().wait()
        record = TokenTelemetryCollector().finalize(
            prompt=request.prompt,
            response="complete",
            system=request.definition.base_context,
        )
        return RuntimeResult(
            response="complete",
            thread_id="11111111-1111-4111-8111-111111111111",
            usage=None,
            token_record=record,
        )

    async def release(self, request: RuntimeRequest) -> None:
        self.released.append(request.job_id)
        if self.block_in == "release":
            try:
                await asyncio.Event().wait()
            finally:
                self.release_cancelled.set()

    async def close(self) -> None:
        return None

    async def recover(self, job_ids: list[str]) -> None:
        return None


class OperationTimeoutRuntime(BlockingRuntime):
    async def run(self, request: RuntimeRequest, *, on_event, cancelled) -> RuntimeResult:
        raise TimeoutError("dependency operation timed out")


class LosingLeaseManager:
    @asynccontextmanager
    async def hold(self, name: str, owner_suffix: str, *, cancelled=None):
        handle = LeaseHandle(name, owner_suffix, 1, asyncio.Event())

        async def lose() -> None:
            await asyncio.sleep(0.02)
            handle.lost.set()

        task = asyncio.create_task(lose())
        try:
            yield handle
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class WaitingLeaseManager:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    @asynccontextmanager
    async def hold(self, name: str, owner_suffix: str, *, cancelled=None):
        self.started.set()
        await asyncio.Event().wait()
        yield LeaseHandle(name, owner_suffix, 1, asyncio.Event())


class BlockingArtifacts:
    def __init__(self, delegate: ArtifactService) -> None:
        self.delegate = delegate
        self.started = asyncio.Event()

    def snapshot(self, root: Path):
        return self.delegate.snapshot(root)

    async def ingest_changed(self, **kwargs):
        self.started.set()
        await asyncio.Event().wait()


@dataclass
class Harness:
    settings: Settings
    engine: Any
    sessions: Any
    jobs: JobService
    scheduler: Scheduler


async def make_harness(
    tmp_path: Path,
    runtime: BlockingRuntime,
    *,
    timeout: int = 5,
    cleanup_timeout: float = 0.1,
    concurrency: int = 1,
    lease_manager: Any | None = None,
) -> Harness:
    settings = Settings(
        _env_file=None,
        environment="test",
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        dashboard_enabled=False,
        scheduler_enabled=False,
        scheduler_concurrency=concurrency,
        scheduler_poll_seconds=0.01,
        subscription_lease_ttl_seconds=30,
        subscription_lease_retry_seconds=0.01,
        job_timeout_seconds=timeout,
        job_cleanup_timeout_seconds=cleanup_timeout,
    ).resolved()
    settings.ensure_directories()
    engine = create_engine(settings.database_url)
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    agent_dir = tmp_path / "alpha"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services:\n  agent:\n    image: example.invalid/agent\n")
    agents = AgentService(sessions, tmp_path)
    await agents.register(
        AgentDefinition(
            id="alpha",
            name="Alpha",
            compose_file=compose_file,
            runner_service="agent",
            base_context="Be exact.",
        )
    )
    workspaces = WorkspaceManager(settings.data_dir)
    jobs = JobService(sessions, workspaces, MemoryCache())
    scheduler = Scheduler(
        settings,
        agent_service=agents,
        job_service=jobs,
        artifact_service=ArtifactService(
            sessions,
            settings.data_dir / "artifact-store",
            max_file_bytes=1_000_000,
            max_files_per_job=10,
        ),
        workspaces=workspaces,
        runtime=runtime,
        lease_manager=lease_manager
        or LeaseManager(
            sessions,
            ttl_seconds=30,
            retry_seconds=0.01,
            instance_id="test",
        ),
        telemetry=DashboardMetrics(),
    )
    return Harness(settings, engine, sessions, jobs, scheduler)


@pytest.mark.asyncio
async def test_queued_job_expires_from_durable_created_at(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime, timeout=1)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="old"))
    async with harness.sessions() as session, session.begin():
        record = await session.get(JobRecord, accepted.job_id)
        assert record is not None
        record.created_at = datetime.now(UTC) - timedelta(seconds=2)

    assert not await harness.scheduler.run_once()
    expired = await harness.jobs.get(accepted.job_id)
    assert expired.status is JobStatus.EXPIRED
    assert expired.error == "job exceeded the configured end-to-end deadline"
    assert not runtime.released
    await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_in", ["provision", "run"])
async def test_absolute_deadline_covers_provision_and_execution(
    tmp_path: Path, block_in: str
) -> None:
    runtime = BlockingRuntime(block_in=block_in)
    harness = await make_harness(tmp_path, runtime, timeout=1)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="slow"))
    async with harness.sessions() as session, session.begin():
        record = await session.get(JobRecord, accepted.job_id)
        assert record is not None
        record.created_at = datetime.now(UTC) - timedelta(seconds=0.8)

    assert await harness.scheduler.run_once()
    failed = await harness.jobs.get(accepted.job_id)
    assert failed.status is JobStatus.FAILED
    assert failed.error == "job exceeded the configured end-to-end deadline"
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_absolute_deadline_covers_artifact_collection(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime, timeout=1)
    artifacts = BlockingArtifacts(harness.scheduler.artifact_service)
    harness.scheduler.artifact_service = artifacts
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="collect"))
    async with harness.sessions() as session, session.begin():
        record = await session.get(JobRecord, accepted.job_id)
        assert record is not None
        record.created_at = datetime.now(UTC) - timedelta(seconds=0.8)

    assert await harness.scheduler.run_once()
    failed = await harness.jobs.get(accepted.job_id)
    assert artifacts.started.is_set()
    assert failed.status is JobStatus.FAILED
    assert failed.error == "job exceeded the configured end-to-end deadline"
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_absolute_deadline_covers_success_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = BlockingRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime, timeout=60)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="persist"))

    # Exercise a real timeout, but expire it only after persistence starts so
    # database/filesystem setup does not have to finish within a 200 ms window.
    deadline = asyncio.timeout(None)
    timeout_delays: list[float] = []

    def controlled_timeout(delay: float) -> asyncio.Timeout:
        timeout_delays.append(delay)
        return deadline

    monkeypatch.setattr(asyncio, "timeout", controlled_timeout)

    original_transition = harness.jobs.transition
    persistence_started = asyncio.Event()

    async def transition(job_id: str, status: JobStatus, **kwargs: Any):
        if status is JobStatus.SUCCEEDED:
            persistence_started.set()
            deadline.reschedule(asyncio.get_running_loop().time())
            await asyncio.Event().wait()
        return await original_transition(job_id, status, **kwargs)

    monkeypatch.setattr(harness.jobs, "transition", transition)
    assert await harness.scheduler.run_once()
    failed = await harness.jobs.get(accepted.job_id)
    assert persistence_started.is_set()
    assert len(timeout_delays) == 1
    assert 0 < timeout_delays[0] <= harness.settings.job_timeout_seconds
    assert deadline.expired()
    assert failed.status is JobStatus.FAILED
    assert failed.error == "job exceeded the configured end-to-end deadline"
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("block_in", ["provision", "run"])
async def test_cancellation_interrupts_runtime_phase_and_is_terminal_cancelled(
    tmp_path: Path, block_in: str
) -> None:
    runtime = BlockingRuntime(block_in=block_in)
    harness = await make_harness(tmp_path, runtime)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="cancel"))

    execution = asyncio.create_task(harness.scheduler.run_once())
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    await harness.jobs.cancel(accepted.job_id)
    assert await asyncio.wait_for(execution, timeout=2)

    cancelled = await harness.jobs.get(accepted.job_id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.error == "cancellation requested"
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_deadline_interrupts_lease_wait(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="none")
    lease_manager = WaitingLeaseManager()
    harness = await make_harness(
        tmp_path,
        runtime,
        timeout=1,
        lease_manager=lease_manager,
    )
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="wait"))
    async with harness.sessions() as session, session.begin():
        record = await session.get(JobRecord, accepted.job_id)
        assert record is not None
        record.created_at = datetime.now(UTC) - timedelta(seconds=0.5)

    assert await harness.scheduler.run_once()
    failed = await harness.jobs.get(accepted.job_id)
    assert failed.status is JobStatus.FAILED
    assert failed.error == "job exceeded the configured end-to-end deadline"
    assert lease_manager.started.is_set()
    assert not runtime.released
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_interrupts_lease_wait(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="none")
    lease_manager = WaitingLeaseManager()
    harness = await make_harness(tmp_path, runtime, lease_manager=lease_manager)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="wait"))

    execution = asyncio.create_task(harness.scheduler.run_once())
    await asyncio.wait_for(lease_manager.started.wait(), timeout=1)
    await harness.jobs.cancel(accepted.job_id)
    assert await asyncio.wait_for(execution, timeout=2)
    assert (await harness.jobs.get(accepted.job_id)).status is JobStatus.CANCELLED
    assert not runtime.released
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_interrupts_collection(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime)
    artifacts = BlockingArtifacts(harness.scheduler.artifact_service)
    harness.scheduler.artifact_service = artifacts
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="collect"))

    execution = asyncio.create_task(harness.scheduler.run_once())
    await asyncio.wait_for(artifacts.started.wait(), timeout=1)
    assert (await harness.jobs.get(accepted.job_id)).status is JobStatus.COLLECTING
    await harness.jobs.cancel(accepted.job_id)
    assert await asyncio.wait_for(execution, timeout=2)
    assert (await harness.jobs.get(accepted.job_id)).status is JobStatus.CANCELLED
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_interrupts_success_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = BlockingRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="persist"))
    original_transition = harness.jobs.transition
    persistence_started = asyncio.Event()

    async def transition(job_id: str, status: JobStatus, **kwargs: Any):
        if status is JobStatus.SUCCEEDED:
            persistence_started.set()
            await asyncio.Event().wait()
        return await original_transition(job_id, status, **kwargs)

    monkeypatch.setattr(harness.jobs, "transition", transition)
    execution = asyncio.create_task(harness.scheduler.run_once())
    await asyncio.wait_for(persistence_started.wait(), timeout=1)
    await harness.jobs.cancel(accepted.job_id)
    assert await asyncio.wait_for(execution, timeout=2)

    cancelled = await harness.jobs.get(accepted.job_id)
    assert cancelled.status is JobStatus.CANCELLED
    assert cancelled.error == "cancellation requested"
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_lease_loss_interrupts_job_without_killing_scheduler_worker(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="run")
    harness = await make_harness(
        tmp_path,
        runtime,
        lease_manager=LosingLeaseManager(),
    )
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="lease"))

    await harness.scheduler.start()
    try:
        for _ in range(100):
            if (await harness.jobs.get(accepted.job_id)).status.terminal:
                break
            await asyncio.sleep(0.01)
        interrupted = await harness.jobs.get(accepted.job_id)
        assert interrupted.status is JobStatus.INTERRUPTED
        assert interrupted.error == "subscription lease was lost"
        assert harness.scheduler.running
    finally:
        await harness.scheduler.stop()
        await harness.engine.dispose()


@pytest.mark.asyncio
async def test_router_stop_interrupts_active_job_and_stops_worker(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="run")
    harness = await make_harness(tmp_path, runtime)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="shutdown"))

    await harness.scheduler.start()
    await asyncio.wait_for(runtime.started.wait(), timeout=1)
    await harness.scheduler.stop()
    interrupted = await harness.jobs.get(accepted.job_id)
    assert interrupted.status is JobStatus.INTERRUPTED
    assert interrupted.error == "router stopped while the turn was active"
    assert not harness.scheduler.running
    assert runtime.released == [accepted.job_id]
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_inner_operation_timeout_is_failed_not_deadline_expired(tmp_path: Path) -> None:
    runtime = OperationTimeoutRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="timeout"))

    assert await harness.scheduler.run_once()
    failed = await harness.jobs.get(accepted.job_id)
    assert failed.status is JobStatus.FAILED
    assert failed.error == "dependency operation timed out"
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_runtime_cleanup_timeout_preserves_success(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="release")
    harness = await make_harness(tmp_path, runtime, cleanup_timeout=0.02)
    accepted = await harness.jobs.submit(PromptRequest(agent_id="alpha", prompt="done"))

    assert await asyncio.wait_for(harness.scheduler.run_once(), timeout=1)
    assert (await harness.jobs.get(accepted.job_id)).status is JobStatus.SUCCEEDED
    await asyncio.wait_for(runtime.release_cancelled.wait(), timeout=1)
    await harness.engine.dispose()


@pytest.mark.asyncio
async def test_running_requires_every_configured_worker_to_be_live(tmp_path: Path) -> None:
    runtime = BlockingRuntime(block_in="none")
    harness = await make_harness(tmp_path, runtime, concurrency=2)
    await harness.scheduler.start()
    assert harness.scheduler.running
    assert harness.scheduler.live_worker_count == 2

    harness.scheduler._workers[0].cancel()
    await asyncio.gather(harness.scheduler._workers[0], return_exceptions=True)
    assert harness.scheduler.live_worker_count == 1
    assert not harness.scheduler.running
    await harness.scheduler.stop()
    await harness.engine.dispose()


class BrokenRenewLeaseManager(LeaseManager):
    def __init__(self, *args, fail_with_exception: bool, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_with_exception = fail_with_exception

    async def renew(self, handle: LeaseHandle) -> bool:
        if self.fail_with_exception:
            raise OSError("database unavailable")
        return False


class BlockingReleaseLeaseManager(LeaseManager):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.release_cancelled = asyncio.Event()

    async def release(self, handle: LeaseHandle) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            self.release_cancelled.set()


class GatedReleaseLeaseManager(LeaseManager):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.release_started = asyncio.Event()
        self.release_allowed = asyncio.Event()
        self.release_completed = asyncio.Event()

    async def release(self, handle: LeaseHandle) -> None:
        self.release_started.set()
        await self.release_allowed.wait()
        await super().release(handle)
        self.release_completed.set()


class CancellationResistantKeepaliveLeaseManager(LeaseManager):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.renew_started = asyncio.Event()
        self.renew_allowed = asyncio.Event()
        self.renew_finished = asyncio.Event()
        self.release_started = asyncio.Event()

    async def renew(self, handle: LeaseHandle) -> bool:
        self.renew_started.set()
        while not self.renew_allowed.is_set():
            try:
                await self.renew_allowed.wait()
            except asyncio.CancelledError:
                continue
        self.renew_finished.set()
        return False

    async def release(self, handle: LeaseHandle) -> None:
        self.release_started.set()
        await super().release(handle)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_with_exception", [False, True])
async def test_lease_renewal_failure_invalidates_handle(
    tmp_path: Path, fail_with_exception: bool
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    manager = BrokenRenewLeaseManager(
        sessions,
        ttl_seconds=30,
        retry_seconds=0.01,
        renew_interval_seconds=0.01,
        instance_id="broken",
        fail_with_exception=fail_with_exception,
    )

    with pytest.raises(LeaseLostError):
        async with manager.hold("subscription", "job") as handle:
            await asyncio.wait_for(handle.lost.wait(), timeout=1)
    await engine.dispose()


@pytest.mark.asyncio
async def test_lease_release_is_bounded_and_cancelled(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease-cleanup.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    manager = BlockingReleaseLeaseManager(
        sessions,
        ttl_seconds=30,
        retry_seconds=0.01,
        cleanup_timeout_seconds=0.02,
        instance_id="cleanup",
    )

    with pytest.raises(LeaseCleanupError, match="lease release exceeded"):
        async with manager.hold("subscription", "job"):
            pass
    await asyncio.wait_for(manager.release_cancelled.wait(), timeout=1)

    manager.release_cancelled.clear()
    with pytest.raises(ValueError, match="primary lifecycle error"):
        async with manager.hold("subscription-error", "job"):
            raise ValueError("primary lifecycle error")
    await asyncio.wait_for(manager.release_cancelled.wait(), timeout=1)
    await engine.dispose()


@pytest.mark.asyncio
async def test_lifecycle_cancellation_still_completes_fenced_lease_release(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease-cancel.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    manager = GatedReleaseLeaseManager(
        sessions,
        ttl_seconds=30,
        retry_seconds=0.01,
        cleanup_timeout_seconds=1,
        instance_id="cleanup-cancel",
    )

    async def hold_once() -> None:
        async with manager.hold("subscription", "job"):
            pass

    lifecycle = asyncio.create_task(hold_once())
    await asyncio.wait_for(manager.release_started.wait(), timeout=1)
    lifecycle.cancel()
    manager.release_allowed.set()
    with pytest.raises(asyncio.CancelledError):
        await lifecycle
    assert manager.release_completed.is_set()
    async with sessions() as session:
        assert await session.get(LeaseRecord, "subscription") is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_resistant_keepalive_cannot_delay_fenced_release(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease-keepalive.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    manager = CancellationResistantKeepaliveLeaseManager(
        sessions,
        ttl_seconds=30,
        retry_seconds=0.01,
        renew_interval_seconds=0.001,
        cleanup_timeout_seconds=0.05,
        instance_id="resistant-keepalive",
    )

    with pytest.raises(LeaseCleanupError, match="lease keepalive cleanup exceeded"):
        async with manager.hold("subscription", "job"):
            await asyncio.wait_for(manager.renew_started.wait(), timeout=1)

    assert manager.release_started.is_set()
    async with sessions() as session:
        assert await session.get(LeaseRecord, "subscription") is None

    manager.renew_allowed.set()
    await asyncio.wait_for(manager.renew_finished.wait(), timeout=1)
    await engine.dispose()


@pytest.mark.asyncio
async def test_stale_lease_holder_cannot_release_successor(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'fence.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    first_manager = LeaseManager(sessions, ttl_seconds=30, retry_seconds=0.01, instance_id="first")
    second_manager = LeaseManager(
        sessions, ttl_seconds=30, retry_seconds=0.01, instance_id="second"
    )
    first = await first_manager.try_acquire("subscription", "job")
    assert first is not None
    async with sessions() as session, session.begin():
        record = await session.get(LeaseRecord, "subscription")
        assert record is not None
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    second = await second_manager.try_acquire("subscription", "job")
    assert second is not None
    assert second.fencing_token > first.fencing_token

    assert not await first_manager.renew(first)
    await first_manager.release(first)
    async with sessions() as session:
        current = await session.scalar(
            select(LeaseRecord).where(LeaseRecord.name == "subscription")
        )
        assert current is not None
        assert current.owner == second.owner
    await second_manager.release(second)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_compose_command_is_terminated_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.communicating = asyncio.Event()
            self.terminated = False
            self.reaped = False

        async def communicate(self):
            self.communicating.set()
            await asyncio.Event().wait()

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(_env_file=None, repository_root=tmp_path).resolved()
    runtime = DockerComposeRuntime(settings)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services: {}\n")
    definition = AgentDefinition(id="alpha", name="Alpha", compose_file=compose_file)
    paths = WorkspaceManager(tmp_path / "state").ensure_conversation("conversation")
    request = RuntimeRequest(
        job_id="j_cancel",
        conversation_key="conversation",
        prompt="test",
        thread_id=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_cancel"),
    )

    command = asyncio.create_task(runtime._checked_compose(["docker"], request, "test"))
    await process.communicating.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    assert process.terminated
    assert process.reaped


@pytest.mark.asyncio
async def test_cancelled_compose_command_hung_wait_is_killed_within_one_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.communicating = asyncio.Event()
            self.waiting = asyncio.Event()
            self.exit = asyncio.Event()
            self.terminated = False
            self.killed = False
            self.reaped = False

        async def communicate(self):
            self.communicating.set()
            await asyncio.Event().wait()

        async def wait(self) -> int:
            self.waiting.set()
            await self.exit.wait()
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exit.set()

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        job_cleanup_timeout_seconds=0.04,
    ).resolved()
    runtime = DockerComposeRuntime(settings)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services: {}\n")
    definition = AgentDefinition(id="alpha", name="Alpha", compose_file=compose_file)
    paths = WorkspaceManager(tmp_path / "state").ensure_conversation("conversation")
    request = RuntimeRequest(
        job_id="j_cancel",
        conversation_key="conversation",
        prompt="test",
        thread_id=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_cancel"),
    )

    command = asyncio.create_task(runtime._checked_compose(["docker"], request, "test"))
    await process.communicating.wait()
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(command, timeout=1)

    assert process.waiting.is_set()
    assert process.terminated
    assert process.killed
    assert process.reaped


@pytest.mark.asyncio
async def test_cancelled_dependency_provision_records_request_for_later_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        dependency_warm_seconds=0,
    ).resolved()
    runtime = DockerComposeRuntime(settings)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services: {}\n")
    definition = AgentDefinition(
        id="alpha",
        name="Alpha",
        compose_file=compose_file,
        dependency_services=("database",),
    )
    paths = WorkspaceManager(tmp_path / "state").ensure_conversation("conversation")
    request = RuntimeRequest(
        job_id="j_cancel",
        conversation_key="conversation",
        prompt="test",
        thread_id=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_cancel"),
    )
    provision_started = asyncio.Event()
    operations: list[str] = []

    async def checked_compose(
        _argv: list[str], _request: RuntimeRequest, operation: str
    ) -> None:
        operations.append(operation)
        if operation == "dependency provisioning":
            provision_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(runtime, "_checked_compose", checked_compose)

    provisioning = asyncio.create_task(runtime.provision(request))
    await asyncio.wait_for(provision_started.wait(), timeout=1)
    state = runtime._dependencies[definition.project_name]
    assert state.last_request is request

    provisioning.cancel()
    with pytest.raises(asyncio.CancelledError):
        await provisioning

    await runtime.release(request)
    stop_task = state.stop_task
    assert stop_task is not None
    await asyncio.wait_for(asyncio.shield(stop_task), timeout=1)
    assert operations == ["dependency provisioning", "dependency warm stop"]


@pytest.mark.asyncio
async def test_cancelled_codex_process_and_blocked_readers_share_cleanup_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Input:
        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    class Output:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            if not self.sent:
                self.sent = True
                return b'{"type":"progress"}\n'
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class ErrorOutput:
        def __init__(self) -> None:
            self.cancelled = asyncio.Event()

        async def read(self, _size: int) -> bytes:
            try:
                await asyncio.Event().wait()
            finally:
                self.cancelled.set()
            return b""

    class CodexProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = Input()
            self.stdout = Output()
            self.stderr = ErrorOutput()
            self.waiting = asyncio.Event()
            self.exit = asyncio.Event()
            self.terminated = False
            self.killed = False
            self.reaped = False

        async def wait(self) -> int:
            self.waiting.set()
            await self.exit.wait()
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exit.set()

    class RemovalProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    codex_process = CodexProcess()
    removal_process = RemovalProcess()
    invocations = 0

    async def create_process(*args, **kwargs):
        nonlocal invocations
        invocations += 1
        return codex_process if invocations == 1 else removal_process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        job_cleanup_timeout_seconds=0.04,
    ).resolved()
    runtime = DockerComposeRuntime(settings)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services: {}\n")
    definition = AgentDefinition(id="alpha", name="Alpha", compose_file=compose_file)
    paths = WorkspaceManager(tmp_path / "state").ensure_conversation("conversation")
    request = RuntimeRequest(
        job_id="j_cancel",
        conversation_key="conversation",
        prompt="test",
        thread_id=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_cancel"),
    )
    event_started = asyncio.Event()
    event_cancelled = asyncio.Event()

    async def on_event(_event: dict[str, Any]) -> None:
        event_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            event_cancelled.set()

    async def cancelled() -> bool:
        await event_started.wait()
        return True

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            runtime.run(request, on_event=on_event, cancelled=cancelled), timeout=1
        )

    assert codex_process.waiting.is_set()
    assert codex_process.terminated
    assert codex_process.killed
    assert codex_process.reaped
    assert event_cancelled.is_set()
    assert codex_process.stderr.cancelled.is_set()
    assert invocations == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_stream", ["stdout", "stderr"])
async def test_cancellation_after_codex_exit_does_not_wait_for_resistant_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    blocked_stream: str,
) -> None:
    class Input:
        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

    blocked = asyncio.Event()
    cancellation_seen = asyncio.Event()
    allowed = asyncio.Event()
    finished = asyncio.Event()

    async def resist_cancellation() -> None:
        blocked.set()
        while not allowed.is_set():
            try:
                await allowed.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
        finished.set()

    class Output:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            if blocked_stream == "stdout" and not self.sent:
                self.sent = True
                return b'{"type":"progress"}\n'
            raise StopAsyncIteration

    class ErrorOutput:
        async def read(self, _size: int) -> bytes:
            if blocked_stream == "stderr":
                await resist_cancellation()
            return b""

    class CodexProcess:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdin = Input()
            self.stdout = Output()
            self.stderr = ErrorOutput()

        async def wait(self) -> int:
            return 0

        def terminate(self) -> None:
            raise AssertionError("completed process must not be terminated")

        def kill(self) -> None:
            raise AssertionError("completed process must not be killed")

    class RemovalProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    invocations = 0

    async def create_process(*args, **kwargs):
        nonlocal invocations
        invocations += 1
        return CodexProcess() if invocations == 1 else RemovalProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        job_cleanup_timeout_seconds=0.04,
    ).resolved()
    runtime = DockerComposeRuntime(settings)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services: {}\n")
    definition = AgentDefinition(id="alpha", name="Alpha", compose_file=compose_file)
    paths = WorkspaceManager(tmp_path / "state").ensure_conversation("conversation")
    request = RuntimeRequest(
        job_id="j_cancel",
        conversation_key="conversation",
        prompt="test",
        thread_id=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_cancel"),
    )

    async def on_event(_event: dict[str, Any]) -> None:
        if blocked_stream == "stdout":
            await resist_cancellation()

    execution = asyncio.create_task(
        runtime.run(request, on_event=on_event, cancelled=lambda: asyncio.sleep(0, result=False))
    )
    await asyncio.wait_for(blocked.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=1)

    assert cancellation_seen.is_set()
    assert invocations == 2

    allowed.set()
    await asyncio.wait_for(finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_cancellation_during_stdin_drain_still_reaps_codex_and_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Input:
        def __init__(self) -> None:
            self.draining = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.allowed = asyncio.Event()
            self.finished = asyncio.Event()

        def write(self, _value: bytes) -> None:
            return None

        async def drain(self) -> None:
            self.draining.set()
            while not self.allowed.is_set():
                try:
                    await self.allowed.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
            self.finished.set()

        def close(self) -> None:
            return None

    class Output:
        def __aiter__(self):
            return self

        async def __anext__(self) -> bytes:
            await asyncio.Event().wait()
            raise StopAsyncIteration

    class ErrorOutput:
        async def read(self, _size: int) -> bytes:
            await asyncio.Event().wait()
            return b""

    class CodexProcess:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.stdin = Input()
            self.stdout = Output()
            self.stderr = ErrorOutput()
            self.exit = asyncio.Event()
            self.terminated = False
            self.killed = False
            self.reaped = False

        async def wait(self) -> int:
            await self.exit.wait()
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exit.set()

    class RemovalProcess:
        returncode = 0

        async def wait(self) -> int:
            return 0

    codex_process = CodexProcess()
    invocations = 0

    async def create_process(*args, **kwargs):
        nonlocal invocations
        invocations += 1
        return codex_process if invocations == 1 else RemovalProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        job_cleanup_timeout_seconds=0.04,
    ).resolved()
    runtime = DockerComposeRuntime(settings)
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    compose_file = agent_dir / "compose.yaml"
    compose_file.write_text("services: {}\n")
    definition = AgentDefinition(id="alpha", name="Alpha", compose_file=compose_file)
    paths = WorkspaceManager(tmp_path / "state").ensure_conversation("conversation")
    request = RuntimeRequest(
        job_id="j_cancel",
        conversation_key="conversation",
        prompt="test",
        thread_id=None,
        definition=definition,
        paths=paths,
        output_path=paths.job_output("j_cancel"),
    )

    execution = asyncio.create_task(
        runtime.run(request, on_event=lambda _event: asyncio.sleep(0), cancelled=lambda: asyncio.sleep(0, result=False))
    )
    await asyncio.wait_for(codex_process.stdin.draining.wait(), timeout=1)
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(execution, timeout=1)

    assert codex_process.stdin.cancelled.is_set()
    assert codex_process.terminated
    assert codex_process.killed
    assert codex_process.reaped
    assert invocations == 2

    codex_process.stdin.allowed.set()
    await asyncio.wait_for(codex_process.stdin.finished.wait(), timeout=1)


@pytest.mark.asyncio
async def test_worker_container_removal_wait_is_bounded_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.waiting = asyncio.Event()
            self.exit = asyncio.Event()
            self.terminated = False
            self.killed = False
            self.reaped = False

        async def wait(self) -> int:
            self.waiting.set()
            await self.exit.wait()
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exit.set()

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        job_cleanup_timeout_seconds=0.02,
    ).resolved()
    runtime = DockerComposeRuntime(settings)

    await asyncio.wait_for(runtime._remove_worker_container("remoteagent-j-test"), timeout=1)
    await asyncio.wait_for(process.exit.wait(), timeout=1)
    await asyncio.sleep(0)

    assert process.waiting.is_set()
    assert process.terminated
    assert process.killed
    assert process.reaped


@pytest.mark.asyncio
async def test_worker_container_removal_preserves_lifecycle_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Process:
        def __init__(self) -> None:
            self.returncode: int | None = None
            self.waiting = asyncio.Event()
            self.exit = asyncio.Event()
            self.terminated = False
            self.killed = False
            self.reaped = False

        async def wait(self) -> int:
            self.waiting.set()
            await self.exit.wait()
            self.reaped = True
            assert self.returncode is not None
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exit.set()

    process = Process()

    async def create_process(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    settings = Settings(_env_file=None, repository_root=tmp_path).resolved()
    runtime = DockerComposeRuntime(settings)

    cleanup = asyncio.create_task(runtime._remove_worker_container("remoteagent-j-test"))
    await asyncio.wait_for(process.waiting.wait(), timeout=1)
    cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cleanup
    await asyncio.wait_for(process.exit.wait(), timeout=1)
    await asyncio.sleep(0)

    assert process.terminated
    assert process.killed
    assert process.reaped
