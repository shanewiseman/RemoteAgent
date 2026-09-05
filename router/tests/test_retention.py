from __future__ import annotations

import asyncio
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import remoteagent.retention as retention_module
from remoteagent.artifacts import ArtifactService
from remoteagent.cache import MemoryCache
from remoteagent.config import Settings
from remoteagent.db import create_engine, create_session_factory, initialize_schema
from remoteagent.jobs import JobService
from remoteagent.models import AgentRecord, ArtifactRecord, ConversationRecord, JobRecord
from remoteagent.retention import RetentionWorker
from remoteagent.schemas import ConversationStatus, JobStatus
from remoteagent.workspace import WorkspaceManager


def _job_id(character: str) -> str:
    return f"j_{character * 32}"


def _agent() -> AgentRecord:
    return AgentRecord(
        id="alpha",
        name="Alpha",
        compose_file="compose.yaml",
        project_name="alpha",
        runner_service="agent",
        dependency_services=[],
        environment={},
        labels={},
        definition_metadata={},
        enabled=True,
        current_revision=1,
    )


def _conversation(key: str, root: Path) -> ConversationRecord:
    return ConversationRecord(
        key=key,
        agent_id="alpha",
        workspace_path=str(root / key / "workspace"),
        codex_home_path=str(root / key / "codex-home"),
        artifact_path=str(root / key / "artifacts"),
        agent_revision=1,
        status=ConversationStatus.ACTIVE.value,
    )


def _job(job_id: str, conversation_key: str, status: JobStatus, sequence: int = 1) -> JobRecord:
    return JobRecord(
        id=job_id,
        agent_id="alpha",
        conversation_key=conversation_key,
        sequence=sequence,
        prompt="test",
        status=status.value,
        agent_revision=1,
        runtime_metadata={},
    )


def _make_old(path: Path) -> None:
    timestamp = time.time() - 7200
    os.utime(path, (timestamp, timestamp), follow_symlinks=False)


@pytest.mark.asyncio
async def test_artifact_reconciliation_is_confined_durable_and_symlink_safe(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    store = tmp_path / "artifact-store"
    service = ArtifactService(sessions, store, max_file_bytes=1000, max_files_per_job=10)
    active_id = _job_id("a")
    terminal_id = _job_id("b")
    unknown_id = _job_id("c")
    symlink_id = _job_id("d")

    active_file = store / active_id / "untracked.txt"
    active_file.parent.mkdir()
    active_file.write_text("active", encoding="utf-8")
    durable_file = store / terminal_id / "durable.txt"
    durable_file.parent.mkdir()
    durable_file.write_text("durable", encoding="utf-8")
    terminal_orphan = store / terminal_id / "orphan.txt"
    terminal_orphan.write_text("orphan", encoding="utf-8")
    young_orphan = store / terminal_id / "young.txt"
    young_orphan.write_text("young", encoding="utf-8")
    unknown_orphan = store / unknown_id / "orphan.txt"
    unknown_orphan.parent.mkdir()
    unknown_orphan.write_text("unknown", encoding="utf-8")
    invalid_orphan = store / "unexpected-name" / "orphan.txt"
    invalid_orphan.parent.mkdir()
    invalid_orphan.write_text("invalid", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")
    (store / symlink_id).symlink_to(outside, target_is_directory=True)

    for path in (
        active_file,
        active_file.parent,
        durable_file,
        terminal_orphan,
        durable_file.parent,
        unknown_orphan,
        unknown_orphan.parent,
        invalid_orphan,
        invalid_orphan.parent,
        store / symlink_id,
    ):
        _make_old(path)

    async with sessions() as session, session.begin():
        session.add(_agent())
        session.add_all(
            [
                _conversation("active", tmp_path),
                _conversation("terminal", tmp_path),
            ]
        )
        session.add_all(
            [
                _job(active_id, "active", JobStatus.RUNNING),
                _job(terminal_id, "terminal", JobStatus.SUCCEEDED),
            ]
        )
        session.add(
            ArtifactRecord(
                id="a_durable",
                job_id=terminal_id,
                conversation_key="terminal",
                relative_path="durable.txt",
                storage_path=str(durable_file),
                media_type="text/plain",
                size_bytes=7,
                sha256="0" * 64,
            )
        )

    assert await service.reconcile_orphans(grace_seconds=3600) >= 3
    assert active_file.read_text(encoding="utf-8") == "active"
    assert durable_file.read_text(encoding="utf-8") == "durable"
    assert not terminal_orphan.exists()
    assert young_orphan.read_text(encoding="utf-8") == "young"
    assert not (store / unknown_id).exists()
    assert invalid_orphan.read_text(encoding="utf-8") == "invalid"
    assert not (store / symlink_id).exists()
    assert sentinel.read_text(encoding="utf-8") == "keep"
    await engine.dispose()


@pytest.mark.asyncio
async def test_artifact_reconciliation_isolates_failure_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    service = ArtifactService(
        sessions, tmp_path / "artifact-store", max_file_bytes=1000, max_files_per_job=10
    )
    job_root = service.store_root / _job_id("e")
    job_root.mkdir()
    failed = job_root / "fail.txt"
    successful = job_root / "succeed.txt"
    failed.write_text("fail", encoding="utf-8")
    successful.write_text("succeed", encoding="utf-8")
    for path in (failed, successful, job_root):
        _make_old(path)

    original_unlink = os.unlink
    injected = False

    def fail_one_unlink(path: str, *, dir_fd: int | None = None) -> None:
        nonlocal injected
        if path == "fail.txt" and not injected:
            injected = True
            raise OSError("injected unlink failure")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", fail_one_unlink)
    await service.reconcile_orphans(grace_seconds=3600)
    assert failed.exists()
    assert not successful.exists()

    await service.reconcile_orphans(grace_seconds=3600)
    assert not job_root.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_reconciles_at_startup_and_on_each_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    settings = Settings(
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        artifact_orphan_grace_seconds=123,
    ).resolved()
    service = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1000,
        max_files_per_job=10,
    )
    reconcile = AsyncMock(return_value=0)
    monkeypatch.setattr(service, "reconcile_orphans", reconcile)
    worker = RetentionWorker(settings, sessions, service)

    async def idle_loop() -> None:
        await asyncio.Future()

    monkeypatch.setattr(worker, "_loop", idle_loop)
    await worker.start()
    reconcile.assert_awaited_once_with(grace_seconds=123)
    await worker.stop()

    reconcile.reset_mock()
    await worker.run_once()
    reconcile.assert_awaited_once_with(grace_seconds=123)
    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_tombstone_survives_artifact_cleanup_failure(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    conversation_key = "conversation"
    first_job = _job_id("f")
    second_job = _job_id("1")
    async with sessions() as session, session.begin():
        session.add(_agent())
        session.add(_conversation(conversation_key, tmp_path))
        session.add_all(
            [
                _job(first_job, conversation_key, JobStatus.SUCCEEDED, 1),
                _job(second_job, conversation_key, JobStatus.FAILED, 2),
            ]
        )
    jobs = JobService(sessions, WorkspaceManager(tmp_path / "state"), MemoryCache())
    attempted: list[str] = []

    async def failing_cleanup(job_id: str) -> None:
        attempted.append(job_id)
        if job_id == first_job:
            raise OSError("injected cleanup failure")

    with pytest.raises(OSError, match="injected cleanup failure"):
        await jobs.delete_conversation(
            conversation_key,
            artifact_cleanup=failing_cleanup,
        )
    assert set(attempted) == {first_job, second_job}
    async with sessions() as session:
        tombstone = await session.get(ConversationRecord, conversation_key)
        assert tombstone is not None
        assert tombstone.status == ConversationStatus.DELETED.value

    attempted.clear()

    async def successful_cleanup(job_id: str) -> None:
        attempted.append(job_id)

    await jobs.delete_conversation(
        conversation_key,
        artifact_cleanup=successful_cleanup,
    )
    assert set(attempted) == {first_job, second_job}
    async with sessions() as session:
        assert await session.get(ConversationRecord, conversation_key) is None
        assert list(await session.scalars(select(JobRecord))) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_keeps_tombstone_and_job_inventory_until_cleanup_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    settings = Settings(
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        artifact_retention_seconds=60,
        job_retention_seconds=60,
        conversation_retention_seconds=60,
    ).resolved()
    service = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1000,
        max_files_per_job=10,
    )
    conversation_key = "deleted-conversation"
    first_job = _job_id("2")
    second_job = _job_id("3")
    completed_at = datetime.now(UTC) - timedelta(minutes=2)
    conversation = _conversation(conversation_key, tmp_path)
    conversation.status = ConversationStatus.DELETED.value
    jobs = [
        _job(first_job, conversation_key, JobStatus.SUCCEEDED, 1),
        _job(second_job, conversation_key, JobStatus.FAILED, 2),
    ]
    for job in jobs:
        job.completed_at = completed_at
    async with sessions() as session, session.begin():
        session.add(_agent())
        session.add(conversation)
        session.add_all(jobs)

    attempted: list[str] = []

    async def failing_cleanup(job_id: str) -> None:
        attempted.append(job_id)
        if job_id == first_job:
            raise OSError("injected cleanup failure")

    monkeypatch.setattr(service, "delete_storage", failing_cleanup)
    worker = RetentionWorker(settings, sessions, service)
    result = await worker.run_once()
    assert result["jobs"] == 0
    assert result["conversations"] == 0
    assert set(attempted) == {first_job, second_job}
    async with sessions() as session:
        tombstone = await session.get(ConversationRecord, conversation_key)
        assert tombstone is not None
        assert tombstone.status == ConversationStatus.DELETED.value
        assert set(await session.scalars(select(JobRecord.id))) == {first_job, second_job}

    async def successful_cleanup(job_id: str) -> None:
        attempted.append(job_id)

    monkeypatch.setattr(service, "delete_storage", successful_cleanup)
    result = await worker.run_once()
    assert result["conversations"] == 1
    async with sessions() as session:
        assert await session.get(ConversationRecord, conversation_key) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_enforces_age_boundaries_active_jobs_and_path_confinement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen_now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return frozen_now if tz is not None else frozen_now.replace(tzinfo=None)

    monkeypatch.setattr(retention_module, "datetime", FrozenDateTime)
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        artifact_retention_seconds=60,
        job_retention_seconds=60,
        conversation_retention_seconds=60,
    ).resolved()
    service = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1000,
        max_files_per_job=10,
    )
    old_job_id = _job_id("4")
    boundary_job_id = _job_id("5")
    active_job_id = _job_id("6")
    unsafe_job_id = _job_id("7")
    old_file = service.store_root / old_job_id / "old.txt"
    boundary_file = service.store_root / boundary_job_id / "boundary.txt"
    outside_file = tmp_path / "outside.txt"
    for path, value in (
        (old_file, "old"),
        (boundary_file, "boundary"),
        (outside_file, "outside"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")

    conversations = {
        key: _conversation(key, settings.data_dir / "conversations")
        for key in ("old", "boundary", "active", "unsafe")
    }
    conversations["active"].updated_at = frozen_now - timedelta(seconds=61)
    jobs = {
        "old": _job(old_job_id, "old", JobStatus.SUCCEEDED),
        "boundary": _job(boundary_job_id, "boundary", JobStatus.SUCCEEDED),
        "active": _job(active_job_id, "active", JobStatus.RUNNING),
        "unsafe": _job(unsafe_job_id, "unsafe", JobStatus.SUCCEEDED),
    }
    jobs["old"].completed_at = frozen_now - timedelta(seconds=61)
    jobs["boundary"].completed_at = frozen_now - timedelta(seconds=60)
    jobs["unsafe"].completed_at = frozen_now - timedelta(seconds=60)
    artifacts = [
        ArtifactRecord(
            id="a_old",
            job_id=old_job_id,
            conversation_key="old",
            relative_path="old.txt",
            storage_path=str(old_file),
            media_type="text/plain",
            size_bytes=3,
            sha256="0" * 64,
            created_at=frozen_now - timedelta(seconds=61),
        ),
        ArtifactRecord(
            id="a_boundary",
            job_id=boundary_job_id,
            conversation_key="boundary",
            relative_path="boundary.txt",
            storage_path=str(boundary_file),
            media_type="text/plain",
            size_bytes=8,
            sha256="1" * 64,
            created_at=frozen_now - timedelta(seconds=60),
        ),
        ArtifactRecord(
            id="a_unsafe",
            job_id=unsafe_job_id,
            conversation_key="unsafe",
            relative_path="outside.txt",
            storage_path=str(outside_file),
            media_type="text/plain",
            size_bytes=7,
            sha256="2" * 64,
            created_at=frozen_now - timedelta(seconds=61),
        ),
    ]
    async with sessions() as session, session.begin():
        session.add(_agent())
        session.add_all(conversations.values())
        session.add_all(jobs.values())
        session.add_all(artifacts)

    result = await RetentionWorker(settings, sessions, service).run_once()

    assert result == {"artifacts": 1, "jobs": 1, "conversations": 0}
    assert not old_file.exists()
    assert boundary_file.read_text(encoding="utf-8") == "boundary"
    assert outside_file.read_text(encoding="utf-8") == "outside"
    async with sessions() as session:
        assert await session.get(JobRecord, old_job_id) is None
        assert await session.get(JobRecord, boundary_job_id) is not None
        assert await session.get(JobRecord, active_job_id) is not None
        assert await session.get(ArtifactRecord, "a_boundary") is not None
        assert await session.get(ArtifactRecord, "a_unsafe") is not None
        assert await session.get(ConversationRecord, "active") is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_retention_restart_retries_workspace_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'router.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    settings = Settings(
        _env_file=None,
        repository_root=tmp_path,
        data_dir=tmp_path / "state",
        agents_root=tmp_path,
        phonebook_path=tmp_path / "phonebook.toml",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        artifact_retention_seconds=60,
        job_retention_seconds=60,
        conversation_retention_seconds=60,
    ).resolved()
    service = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1000,
        max_files_per_job=10,
    )
    conversation_key = "workspace-retry"
    job_id = _job_id("8")
    conversation = _conversation(conversation_key, settings.data_dir / "conversations")
    conversation.status = ConversationStatus.DELETED.value
    job = _job(job_id, conversation_key, JobStatus.SUCCEEDED)
    job.completed_at = datetime.now(UTC) - timedelta(minutes=2)
    workspace = settings.data_dir / "conversations" / conversation_key
    workspace.mkdir(parents=True)
    (workspace / "state.txt").write_text("retry", encoding="utf-8")
    async with sessions() as session, session.begin():
        session.add(_agent())
        session.add(conversation)
        session.add(job)

    original_rmtree = retention_module.shutil.rmtree

    def fail_workspace(path: Path, *args: Any, **kwargs: Any) -> None:
        if Path(path) == workspace:
            raise OSError("injected workspace cleanup failure")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(retention_module.shutil, "rmtree", fail_workspace)
    first = await RetentionWorker(settings, sessions, service).run_once()
    assert first["conversations"] == 0
    async with sessions() as session:
        assert await session.get(ConversationRecord, conversation_key) is not None
        assert await session.get(JobRecord, job_id) is not None

    monkeypatch.setattr(retention_module.shutil, "rmtree", original_rmtree)
    second = await RetentionWorker(settings, sessions, service).run_once()
    assert second["conversations"] == 1
    assert not workspace.exists()
    async with sessions() as session:
        assert await session.get(ConversationRecord, conversation_key) is None
        assert await session.get(JobRecord, job_id) is None
    await engine.dispose()


def test_artifact_orphan_grace_defaults_to_one_hour() -> None:
    assert Settings().artifact_orphan_grace_seconds == 3600
