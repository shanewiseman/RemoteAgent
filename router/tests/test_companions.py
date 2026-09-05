from __future__ import annotations

import asyncio
import hashlib
import io
import os
import signal
import sqlite3
import struct
import subprocess
import sys
import tarfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from remoteagent.agents import AgentService
from remoteagent.app import create_app
from remoteagent.artifacts import ArtifactService
from remoteagent.cache import MemoryCache
from remoteagent.companions import (
    CompanionPolicyError,
    CompanionService,
    CompanionTooLargeError,
)
from remoteagent.config import Settings
from remoteagent.db import create_engine, create_session_factory, initialize_schema
from remoteagent.jobs import JobService
from remoteagent.lease import LeaseManager
from remoteagent.mcp_server import build_mcp
from remoteagent.models import CompanionStageRecord, ConversationCompanionRecord, JobRecord
from remoteagent.runtime import FakeRuntime, RuntimeResult
from remoteagent.scheduler import Scheduler
from remoteagent.schemas import (
    AgentDefinition,
    CompanionStageView,
    ConversationCompanionView,
    GitImportRequest,
    JobStatus,
    PromptRequest,
    UsageTotals,
)
from remoteagent.telemetry import DashboardMetrics, TokenTelemetryCollector
from remoteagent.workspace import WorkspaceManager


def _stage_id(index: int) -> str:
    return f"cs_{index:032x}"


def _agent(root: Path) -> AgentDefinition:
    directory = root / "alpha"
    directory.mkdir(parents=True, exist_ok=True)
    compose = directory / "compose.yaml"
    compose.write_text("services:\n  agent:\n    image: example.invalid/agent\n", encoding="utf-8")
    return AgentDefinition(
        id="alpha",
        name="Alpha",
        compose_file=compose,
        runner_service="agent",
        base_context="Be exact.",
    )


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "test",
        "repository_root": tmp_path,
        "data_dir": tmp_path / "state",
        "agents_root": tmp_path,
        "phonebook_path": tmp_path / "phonebook.toml",
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'router.db'}",
        "bearer_token": "router-secret",
        "dashboard_enabled": False,
        "scheduler_enabled": False,
        "companion_cleanup_interval_seconds": 10,
    }
    values.update(overrides)
    return Settings(**values).resolved()


def _service(
    sessions: Any,
    workspaces: WorkspaceManager,
    *,
    staging_root: Path,
    max_object_bytes: int = 1_000_000,
    max_files: int = 100,
    max_additions: int = 20,
    max_active_names: int = 200,
    max_conversation_bytes: int = 10_000_000,
    max_staging_bytes: int = 10_000_000,
    stage_ttl: timedelta = timedelta(hours=24),
) -> CompanionService:
    return CompanionService(
        sessions,
        staging_root,
        workspaces.root,
        max_upload_bytes=max_object_bytes,
        max_archive_bytes=max_object_bytes,
        max_git_mirror_bytes=max_object_bytes,
        max_git_checkout_bytes=max_object_bytes,
        max_files=max_files,
        max_additions_per_turn=max_additions,
        max_active_names=max_active_names,
        max_conversation_bytes=max_conversation_bytes,
        max_staging_bytes=max_staging_bytes,
        stage_ttl=stage_ttl,
        git_workers=1,
        git_timeout_seconds=5,
        cleanup_interval_seconds=60,
        telemetry=DashboardMetrics(),
    )


async def _chunks(*values: bytes):
    for value in values:
        yield value


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _corrupt_stored_zip() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("a.txt", b"HELLO")
    payload = bytearray(output.getvalue())
    index = payload.index(b"HELLO")
    payload[index] ^= 0x01
    return bytes(payload)


def _zip64_count_override_bytes() -> bytes:
    """Build a ZIP whose ZIP64 count overrides a forged-low classic EOCD count."""

    payload = _zip_bytes({"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})
    eocd_offset = payload.rfind(b"PK\x05\x06")
    eocd = struct.unpack("<4s4H2LH", payload[eocd_offset : eocd_offset + 22])
    directory_size, directory_offset = eocd[5], eocd[6]
    name_size, extra_size, comment_size = struct.unpack(
        "<3H", payload[directory_offset + 28 : directory_offset + 34]
    )
    forged_directory_size = 46 + name_size + extra_size + comment_size
    zip64 = struct.pack(
        "<4sQ2H2L4Q",
        b"PK\x06\x06",
        44,
        45,
        45,
        0,
        0,
        3,
        3,
        directory_size,
        directory_offset,
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, eocd_offset, 1)
    classic = struct.pack(
        "<4s4H2LH",
        b"PK\x05\x06",
        0,
        0,
        1,
        1,
        forged_directory_size,
        directory_offset,
        0,
    )
    return payload[:eocd_offset] + zip64 + locator + classic


def _tar_symlink_bytes() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        member = tarfile.TarInfo("escape")
        member.type = tarfile.SYMTYPE
        member.linkname = "../../outside"
        archive.addfile(member)
    return output.getvalue()


def _empty_directory_archive(kind: str) -> tuple[str, bytes]:
    output = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(output, "w") as archive:
            for index in range(3):
                archive.writestr(f"empty-{index}/", b"")
        return "empty.zip", output.getvalue()
    with tarfile.open(fileobj=output, mode="w") as archive:
        for index in range(3):
            member = tarfile.TarInfo(f"empty-{index}")
            member.type = tarfile.DIRTYPE
            archive.addfile(member)
    return "empty.tar", output.getvalue()


def _implicit_directory_archive(kind: str) -> tuple[str, bytes]:
    output = io.BytesIO()
    if kind == "zip":
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("a/b/c/d.txt", b"x")
        return "nested.zip", output.getvalue()
    with tarfile.open(fileobj=output, mode="w") as archive:
        member = tarfile.TarInfo("a/b/c/d.txt")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    return "nested.tar", output.getvalue()


def _runtime_state_archive(link_target: str) -> bytes:
    companion_id = "cc_" + "a" * 32
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in (
            "conversations",
            "conversations/conversation",
            "conversations/conversation/workspace",
            "conversations/conversation/workspace/.remoteagent",
            "conversations/conversation/workspace/.remoteagent/companions",
            f"conversations/conversation/workspace/.remoteagent/companions/{companion_id}",
            "conversations/conversation/workspace/companions",
            "artifact-store",
        ):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            archive.addfile(member)
        content = b"companion data"
        member = tarfile.TarInfo(
            f"conversations/conversation/workspace/.remoteagent/companions/{companion_id}/content"
        )
        member.size = len(content)
        member.mode = 0o600
        archive.addfile(member, io.BytesIO(content))
        member = tarfile.TarInfo("conversations/conversation/workspace/companions/reference")
        member.type = tarfile.SYMTYPE
        member.linkname = link_target
        archive.addfile(member)
    return output.getvalue()


def _runtime_state_special_archive() -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name in ("conversations", "artifact-store"):
            member = tarfile.TarInfo(name)
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            archive.addfile(member)
        member = tarfile.TarInfo("conversations/unsafe-fifo")
        member.type = tarfile.FIFOTYPE
        member.mode = 0o600
        archive.addfile(member)
    return output.getvalue()


def _write_backup_bundle(destination: Path, state_archive: bytes) -> None:
    payloads = {
        "manifest": b"version=test\ncreated=2026-09-02T00:00:00Z\n",
        "postgres.dump": b"test database dump",
        "state.tar.gz": state_archive,
    }
    checksums = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in payloads.items()
    ).encode("ascii")
    with tarfile.open(destination, mode="w:gz") as archive:
        for name, content in {**payloads, "SHA256SUMS": checksums}.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(content))


def _token_record(prompt: str, response: str = "ok"):
    return TokenTelemetryCollector().finalize(prompt=prompt, response=response, system="Be exact.")


def test_prompt_companion_bindings_require_safe_unique_names_and_stage_ids() -> None:
    valid = PromptRequest(
        agent_id="alpha",
        prompt="use it",
        companions=[{"stage_id": _stage_id(1), "name": "reference-repo.v1"}],
    )
    assert valid.companions[0].name == "reference-repo.v1"

    for name in ("../escape", "/absolute", "two parts", "_leading"):
        with pytest.raises(ValidationError):
            PromptRequest(
                agent_id="alpha",
                prompt="bad",
                companions=[{"stage_id": _stage_id(1), "name": name}],
            )
    with pytest.raises(ValidationError, match="duplicate stage ids"):
        PromptRequest(
            agent_id="alpha",
            prompt="bad",
            companions=[
                {"stage_id": _stage_id(1), "name": "one"},
                {"stage_id": _stage_id(1), "name": "two"},
            ],
        )
    with pytest.raises(ValidationError, match="duplicate names"):
        PromptRequest(
            agent_id="alpha",
            prompt="bad",
            companions=[
                {"stage_id": _stage_id(1), "name": "same"},
                {"stage_id": _stage_id(2), "name": "same"},
            ],
        )
    with pytest.raises(ValidationError):
        PromptRequest(
            agent_id="alpha",
            prompt="too many",
            companions=[
                {"stage_id": _stage_id(index + 1), "name": f"item-{index}"} for index in range(21)
            ],
        )


def test_rest_companion_binding_limit_returns_413(tmp_path: Path) -> None:
    app = create_app(_settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/jobs",
            headers={"Authorization": "Bearer router-secret"},
            json={
                "agent_id": "alpha",
                "prompt": "too many",
                "companions": [
                    {"stage_id": _stage_id(index + 1), "name": f"item-{index}"}
                    for index in range(21)
                ],
            },
        )
    assert response.status_code == 413
    assert response.json() == {"detail": "too many companion additions for one turn"}
    with pytest.raises(ValidationError):
        _settings(tmp_path, companion_max_per_turn=21)


def test_companion_migration_upgrades_and_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REMOTEAGENT_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    database = tmp_path / "migration.db"
    router_root = Path(__file__).parents[1]
    config = Config(str(router_root / "alembic.ini"))
    config.set_main_option("script_location", str(router_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database}")

    command.upgrade(config, "20260902_0003")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "companion_stages" not in tables
    assert "conversation_companions" not in tables

    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        foreign_keys = connection.execute(
            "PRAGMA foreign_key_list(conversation_companions)"
        ).fetchall()
    assert {"companion_stages", "conversation_companions"} <= tables
    assert any(row[2] == "jobs" and row[6] == "SET NULL" for row in foreign_keys)
    assert any(row[2] == "conversations" and row[6] == "CASCADE" for row in foreign_keys)

    command.downgrade(config, "20260902_0003")
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "companion_stages" not in tables
    assert "conversation_companions" not in tables


@pytest.mark.parametrize(
    "unsafe_target",
    [
        "/etc/passwd",
        "../other-in-conversation",
        "../../../../artifact-store/escaped",
        "../../../../../outside",
    ],
)
def test_remotectl_backup_validator_allows_contained_companion_link_and_rejects_escape(
    tmp_path: Path, unsafe_target: str
) -> None:
    repository_root = Path(__file__).parents[2]
    env_file = tmp_path / "remoteagent.env"
    env_file.write_text("", encoding="utf-8")

    valid = tmp_path / "valid-backup.tar.gz"
    _write_backup_bundle(
        valid,
        _runtime_state_archive(f"../.remoteagent/companions/cc_{'a' * 32}/content"),
    )
    valid_result = subprocess.run(
        [
            str(repository_root / "scripts" / "remotectl"),
            "--env-file",
            str(env_file),
            "backup",
            "verify",
            str(valid),
        ],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert valid_result.returncode == 0, valid_result.stderr

    unsafe = tmp_path / "unsafe-backup.tar.gz"
    _write_backup_bundle(unsafe, _runtime_state_archive(unsafe_target))
    unsafe_result = subprocess.run(
        [
            str(repository_root / "scripts" / "remotectl"),
            "--env-file",
            str(env_file),
            "backup",
            "verify",
            str(unsafe),
        ],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert unsafe_result.returncode != 0
    assert "unsafe runtime-state archive link" in unsafe_result.stderr


def test_remotectl_backup_validator_rejects_special_runtime_member(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[2]
    env_file = tmp_path / "remoteagent.env"
    env_file.write_text("", encoding="utf-8")
    archive = tmp_path / "special-member-backup.tar.gz"
    _write_backup_bundle(archive, _runtime_state_special_archive())

    result = subprocess.run(
        [
            str(repository_root / "scripts" / "remotectl"),
            "--env-file",
            str(env_file),
            "backup",
            "verify",
            str(archive),
        ],
        cwd=repository_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode != 0
    assert "unsafe runtime-state archive special member" in result.stderr


def test_remotectl_restore_expires_ephemeral_unclaimed_companion_stages(
    tmp_path: Path,
) -> None:
    repository_root = Path(__file__).parents[2]
    state_root = tmp_path / "state"
    (state_root / "conversations").mkdir(parents=True)
    (state_root / "artifact-store").mkdir()
    env_file = tmp_path / "remoteagent.env"
    env_file.write_text(
        f"REMOTEAGENT_STATE_ROOT={state_root}\nPOSTGRES_USER=test\nPOSTGRES_DB=test\n",
        encoding="utf-8",
    )
    archive = tmp_path / "backup.tar.gz"
    _write_backup_bundle(
        archive,
        _runtime_state_archive(f"../.remoteagent/companions/cc_{'a' * 32}/content"),
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    psql_log = tmp_path / "psql.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
case " $* " in
  *" pg_restore "*)
    cat >/dev/null
    printf 'SELECT 1;\\n'
    ;;
  *" psql "*)
    {
      printf '%s\\n' '--- psql invocation ---'
      printf 'args: %s\\n' "$*"
      cat
    } >> "$REMOTEAGENT_TEST_PSQL_LOG"
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    result = subprocess.run(
        [
            str(repository_root / "scripts" / "remotectl"),
            "--env-file",
            str(env_file),
            "restore",
            str(archive),
            "--yes",
        ],
        cwd=repository_root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REMOTEAGENT_TEST_PSQL_LOG": str(psql_log),
        },
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    statements = psql_log.read_text(encoding="utf-8")
    assert statements.count("--- psql invocation ---") == 1
    assert "to_regclass('public.companion_stages') IS NOT NULL" in statements
    assert "storage_path = NULL" in statements
    assert "claimed_at IS NULL" in statements
    assert "status IN ('queued', 'importing', 'ready')" in statements


@pytest.mark.parametrize(
    ("failpoints", "command_failure", "message"),
    [
        ("swap-artifact-store", "", "database was untouched"),
        ("database-restore", "", "PostgreSQL transaction rolled back"),
        (
            "database-restore,rollback-conversations",
            "",
            "database restore and filesystem rollback failed",
        ),
        ("", "stop", "failed to stop all core services"),
        ("", "quiescence", "core services remained running after stop"),
        ("", "postgres-start", "prior filesystem trees restored"),
    ],
)
def test_remotectl_restore_failpoints_preserve_predecessors_or_stop_services(
    tmp_path: Path, failpoints: str, command_failure: str, message: str
) -> None:
    repository_root = Path(__file__).parents[2]
    state_root = tmp_path / "state"
    conversations = state_root / "conversations"
    artifacts = state_root / "artifact-store"
    conversations.mkdir(parents=True)
    artifacts.mkdir()
    (conversations / "prior.txt").write_text("prior conversation", encoding="utf-8")
    (artifacts / "prior.txt").write_text("prior artifact", encoding="utf-8")
    env_file = tmp_path / "remoteagent.env"
    env_file.write_text(
        f"REMOTEAGENT_STATE_ROOT={state_root}\nPOSTGRES_USER=test\nPOSTGRES_DB=test\n",
        encoding="utf-8",
    )
    archive = tmp_path / "backup.tar.gz"
    _write_backup_bundle(
        archive,
        _runtime_state_archive(f"../.remoteagent/companions/cc_{'a' * 32}/content"),
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$*" >> "$REMOTEAGENT_TEST_DOCKER_LOG"
if [[ "$REMOTEAGENT_TEST_RESTORE_COMMAND_FAILURE" = stop && " $* " == *" stop cron router redis postgres "* ]]; then
  exit 7
fi
if [[ "$REMOTEAGENT_TEST_RESTORE_COMMAND_FAILURE" = quiescence && " $* " == *" ps --status running --quiet cron router redis postgres "* ]]; then
  printf 'still-running\n'
  exit 0
fi
if [[ "$REMOTEAGENT_TEST_RESTORE_COMMAND_FAILURE" = postgres-start && " $* " == *" up -d --wait postgres "* ]]; then
  exit 8
fi
case " $* " in
  *" pg_restore "*)
    cat >/dev/null
    printf 'SELECT 1;\n'
    ;;
  *" psql "*)
    cat >/dev/null
    if [[ "$*" == *"REMOTEAGENT restore failpoint after restored SQL"* ]]; then
      exit 9
    fi
    ;;
esac
""",
        encoding="utf-8",
    )
    docker.chmod(0o700)
    result = subprocess.run(
        [
            str(repository_root / "scripts" / "remotectl"),
            "--env-file",
            str(env_file),
            "restore",
            str(archive),
            "--yes",
        ],
        cwd=repository_root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REMOTEAGENT_ALLOW_RESTORE_FAILPOINTS": "true",
            "REMOTEAGENT_RESTORE_FAILPOINT": failpoints,
            "REMOTEAGENT_TEST_DOCKER_LOG": str(docker_log),
            "REMOTEAGENT_TEST_RESTORE_COMMAND_FAILURE": command_failure,
        },
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert result.returncode != 0
    assert message in result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    pg_restore_calls = [line for line in calls.splitlines() if " pg_restore " in f" {line} "]
    assert len(pg_restore_calls) == 2
    assert all(line.startswith("run --rm --interactive --pull=never") for line in pg_restore_calls)
    assert all("--network none" in line and "--read-only" in line for line in pg_restore_calls)
    assert all("--entrypoint pg_restore postgres:17.6-alpine" in line for line in pg_restore_calls)
    if failpoints == "swap-artifact-store":
        assert " psql " not in f" {calls} "
        assert (conversations / "prior.txt").read_text(encoding="utf-8") == "prior conversation"
        assert (artifacts / "prior.txt").read_text(encoding="utf-8") == "prior artifact"
    elif failpoints == "database-restore":
        assert (conversations / "prior.txt").read_text(encoding="utf-8") == "prior conversation"
        assert (artifacts / "prior.txt").read_text(encoding="utf-8") == "prior artifact"
        assert "REMOTEAGENT restore failpoint after restored SQL" in calls
        assert " psql " in f" {calls} "
        assert "stop postgres" in calls
    elif command_failure:
        assert (conversations / "prior.txt").read_text(encoding="utf-8") == "prior conversation"
        assert (artifacts / "prior.txt").read_text(encoding="utf-8") == "prior artifact"
        if command_failure in {"stop", "quiescence", "postgres-start"}:
            assert " psql " not in f" {calls} "
    else:
        assert "REMOTEAGENT restore failpoint after restored SQL" in calls
        assert " psql " in f" {calls} "
        assert "stop cron router redis postgres" in calls


@pytest.mark.asyncio
async def test_streamed_upload_hash_archive_safety_and_limits(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'companions.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    workspaces = WorkspaceManager(tmp_path / "state")
    service = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
        max_object_bytes=32,
        max_files=2,
    )

    expected = hashlib.sha256(b"hello world").hexdigest()
    file_stage = await service.stage_upload(
        _chunks(b"hello ", b"world"),
        filename="notes.txt",
        kind="file",
        expected_sha256=expected,
    )
    assert file_stage.status == "ready"
    assert file_stage.size_bytes == 11
    assert file_stage.file_count == 1
    assert file_stage.sha256 == expected

    with pytest.raises(CompanionPolicyError, match="digest|SHA|hash|sha256"):
        await service.stage_upload(
            _chunks(b"different"),
            filename="notes.txt",
            kind="file",
            expected_sha256="0" * 64,
        )
    with pytest.raises(CompanionPolicyError, match="size|limit"):
        await service.stage_upload(_chunks(b"x" * 33), filename="large.bin", kind="file")
    safe_archive = _zip_bytes({"docs/a.txt": b"a", "docs/b.txt": b"b"})
    # Compressed input and expanded tree are bounded independently. This test's
    # tiny upload cap is raised only for the valid extraction check.
    extraction = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "other-staging",
        max_object_bytes=max(1_000, len(safe_archive)),
        max_files=3,
    )
    with pytest.raises(CompanionPolicyError, match="archive|path|travers"):
        await extraction.stage_upload(
            _chunks(_zip_bytes({"../escape.txt": b"no"})),
            filename="unsafe.zip",
            kind="archive",
        )
    with pytest.raises(CompanionPolicyError, match="link|archive"):
        await extraction.stage_upload(
            _chunks(_tar_symlink_bytes()),
            filename="unsafe.tar.gz",
            kind="archive",
        )
    with pytest.raises(CompanionPolicyError) as corrupt:
        await extraction.stage_upload(
            _chunks(_corrupt_stored_zip()),
            filename="corrupt.zip",
            kind="archive",
        )
    assert corrupt.value.status_code == 422
    archive_stage = await extraction.stage_upload(
        _chunks(safe_archive), filename="reference.zip", kind="archive"
    )
    assert archive_stage.status == "ready"
    assert archive_stage.file_count == 2

    with pytest.raises(CompanionPolicyError, match="file-count|files|limit"):
        await extraction.stage_upload(
            _chunks(_zip_bytes({"a": b"1", "b": b"2", "c": b"3", "d": b"4"})),
            filename="many.zip",
            kind="archive",
        )
    await engine.dispose()


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
@pytest.mark.asyncio
async def test_archive_file_limit_counts_empty_directories(
    tmp_path: Path, archive_kind: str
) -> None:
    engine = create_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'empty-directories-{archive_kind}.db'}"
    )
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    workspaces = WorkspaceManager(tmp_path / "state")
    service = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
        max_files=2,
    )
    filename, payload = _empty_directory_archive(archive_kind)
    with pytest.raises(CompanionPolicyError) as error:
        await service.stage_upload(_chunks(payload), filename=filename, kind="archive")
    assert error.value.status_code == 413
    await engine.dispose()


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
@pytest.mark.asyncio
async def test_archive_file_limit_counts_implicit_parent_directories(
    tmp_path: Path, archive_kind: str
) -> None:
    engine = create_engine(
        f"sqlite+aiosqlite:///{tmp_path / f'implicit-directories-{archive_kind}.db'}"
    )
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    workspaces = WorkspaceManager(tmp_path / "state")
    service = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
        max_files=2,
    )
    filename, payload = _implicit_directory_archive(archive_kind)
    with pytest.raises(CompanionTooLargeError):
        await service.stage_upload(_chunks(payload), filename=filename, kind="archive")
    await engine.dispose()


@pytest.mark.asyncio
async def test_zip_entry_limit_is_checked_before_constructing_zip_info_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _zip_bytes({"a.txt": b"a", "b.txt": b"b", "c.txt": b"c"})
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'zip-entry-preflight.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    service = _service(
        sessions,
        WorkspaceManager(tmp_path / "state"),
        staging_root=tmp_path / "state" / "companion-staging",
        max_files=2,
    )

    def unexpected_zipfile_construction(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("ZipFile constructed before central-directory entry-count admission")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_zipfile_construction)
    try:
        with pytest.raises(CompanionTooLargeError):
            await service.stage_upload(_chunks(payload), filename="too-many.zip", kind="archive")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_zip64_entry_count_is_authoritative_before_zip_info_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _zip64_count_override_bytes()
    assert len(zipfile.ZipFile(io.BytesIO(payload)).infolist()) == 3
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'zip64-entry-preflight.db'}")
    await initialize_schema(engine)
    service = _service(
        create_session_factory(engine),
        WorkspaceManager(tmp_path / "state"),
        staging_root=tmp_path / "state" / "companion-staging",
        max_files=2,
    )

    def unexpected_zipfile_construction(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("ZipFile constructed before authoritative ZIP64 count admission")

    monkeypatch.setattr(zipfile, "ZipFile", unexpected_zipfile_construction)
    try:
        with pytest.raises(CompanionTooLargeError):
            await service.stage_upload(_chunks(payload), filename="too-many.zip", kind="archive")
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_safe_archive_activates_from_verified_extracted_tree(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive-activation.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    stage = await companions.stage_upload(
        _chunks(_zip_bytes({"docs/reference.txt": b"archive content"})),
        filename="reference.zip",
        kind="archive",
    )
    accepted = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="use archive",
            companions=[{"stage_id": stage.id, "name": "reference"}],
        )
    )

    active = await companions.prepare_for_turn(accepted.conversation_key, 1)
    assert active[0].sha256 == stage.sha256
    alias = workspaces.paths(accepted.conversation_key).companion_aliases / "reference"
    assert (alias / "docs" / "reference.txt").read_bytes() == b"archive content"
    await engine.dispose()


def test_rest_upload_binding_single_use_and_idempotent_replay(tmp_path: Path) -> None:
    definition = _agent(tmp_path)
    app = create_app(_settings(tmp_path), runtime=FakeRuntime(), validate_compose=False)
    headers = {"Authorization": "Bearer router-secret"}

    with TestClient(app) as client:
        registration = client.post(
            "/api/v1/agents",
            headers=headers,
            json={"definition": definition.model_dump(mode="json")},
        )
        assert registration.status_code == 201, registration.text

        payload = b"persistent reference"
        digest = hashlib.sha256(payload).hexdigest()
        upload = client.post(
            "/api/v1/companion-stages/uploads",
            params={"filename": "notes.txt", "kind": "file", "sha256": digest},
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=payload,
        )
        assert upload.status_code == 201, upload.text
        first_stage = upload.json()
        assert first_stage["status"] == "ready"
        assert first_stage["sha256"] == digest

        accepted = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "agent_id": "alpha",
                "prompt": "Use the notes.",
                "idempotency_key": "request-one",
                "companions": [{"stage_id": first_stage["id"], "name": "notes"}],
            },
        )
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        assert [
            (item["name"], item["version"], item["status"]) for item in body["companion_additions"]
        ] == [("notes", 1, "pending")]

        second = client.post(
            "/api/v1/companion-stages/uploads",
            params={"filename": "other.txt", "kind": "file"},
            headers={**headers, "Content-Type": "application/octet-stream"},
            content=b"unused on replay",
        ).json()
        replay = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "agent_id": "alpha",
                "prompt": "A changed replay body must be ignored.",
                "idempotency_key": "request-one",
                "companions": [{"stage_id": second["id"], "name": "other"}],
            },
        )
        assert replay.status_code == 202, replay.text
        assert replay.json()["job_id"] == body["job_id"]
        replay_addition = replay.json()["companion_additions"][0]
        original_addition = body["companion_additions"][0]
        assert {
            key: replay_addition[key] for key in ("id", "stage_id", "name", "version", "sha256")
        } == {
            key: original_addition[key] for key in ("id", "stage_id", "name", "version", "sha256")
        }
        assert (
            client.get(f"/api/v1/companion-stages/{second['id']}", headers=headers).json()["status"]
            == "ready"
        )

        reuse = client.post(
            "/api/v1/jobs",
            headers=headers,
            json={
                "agent_id": "alpha",
                "prompt": "Cannot claim twice.",
                "conversation_key": body["conversation_key"],
                "idempotency_key": "request-two",
                "companions": [{"stage_id": first_stage["id"], "name": "again"}],
            },
        )
        assert reuse.status_code == 409

        listed = client.get(
            f"/api/v1/conversations/{body['conversation_key']}/companions", headers=headers
        )
        assert listed.status_code == 200
        assert [(item["name"], item["status"]) for item in listed.json()] == [("notes", "pending")]
        assert (
            client.get(f"/api/v1/companion-stages/{_stage_id(999)}", headers=headers).status_code
            == 404
        )


@pytest.mark.asyncio
async def test_concurrent_idempotent_submissions_claim_only_original_companions(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'idempotency-race.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    first_stage, second_stage = await asyncio.gather(
        companions.stage_upload(_chunks(b"first"), filename="first.txt", kind="file"),
        companions.stage_upload(_chunks(b"second"), filename="second.txt", kind="file"),
    )

    first, replay = await asyncio.gather(
        jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="original",
                idempotency_key="same-request",
                companions=[{"stage_id": first_stage.id, "name": "first"}],
            )
        ),
        jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="retry payload is ignored",
                idempotency_key="same-request",
                companions=[{"stage_id": second_stage.id, "name": "second"}],
            )
        ),
    )

    assert replay.job_id == first.job_id
    assert replay.conversation_key == first.conversation_key
    assert [
        (item.id, item.stage_id, item.name, item.version)
        for item in replay.companion_additions
    ] == [
        (item.id, item.stage_id, item.name, item.version)
        for item in first.companion_additions
    ]
    statuses = {
        first_stage.id: (await companions.get_stage(first_stage.id)).status,
        second_stage.id: (await companions.get_stage(second_stage.id)).status,
    }
    assert sorted(statuses.values()) == ["claimed", "ready"]
    assert len(jobs._submission_locks) == 0
    await engine.dispose()


def test_rest_rejects_hash_mismatch_malicious_archive_and_capacity(tmp_path: Path) -> None:
    unsafe_root = tmp_path / "unsafe"
    app = create_app(
        _settings(unsafe_root, companion_max_object_bytes=1_000),
        runtime=FakeRuntime(),
        validate_compose=False,
    )
    headers = {
        "Authorization": "Bearer router-secret",
        "Content-Type": "application/octet-stream",
    }
    with TestClient(app) as client:
        mismatch = client.post(
            "/api/v1/companion-stages/uploads",
            params={"filename": "bad.txt", "kind": "file", "sha256": "0" * 64},
            headers=headers,
            content=b"not zero",
        )
        assert mismatch.status_code == 422

        traversal = client.post(
            "/api/v1/companion-stages/uploads",
            params={"filename": "bad.zip", "kind": "archive"},
            headers=headers,
            content=_zip_bytes({"../escape": b"x"}),
        )
        assert traversal.status_code == 422

    capacity_root = tmp_path / "capacity"
    capacity_app = create_app(
        _settings(
            capacity_root,
            companion_max_object_bytes=256,
            companion_staging_max_bytes=10,
        ),
        runtime=FakeRuntime(),
        validate_compose=False,
    )
    with TestClient(capacity_app) as client:
        first = client.post(
            "/api/v1/companion-stages/uploads",
            params={"filename": "one.bin", "kind": "file"},
            headers=headers,
            content=b"123456",
        )
        assert first.status_code == 201
        second = client.post(
            "/api/v1/companion-stages/uploads",
            params={"filename": "two.bin", "kind": "file"},
            headers=headers,
            content=b"abcdef",
        )
        assert second.status_code == 507


@pytest.mark.asyncio
async def test_sequence_activation_edit_inheritance_and_atomic_replacement(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'turns.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)

    first = await jobs.submit(PromptRequest(agent_id="alpha", prompt="first has no future data"))
    staged_v1 = await companions.stage_upload(
        _chunks(b"version one"), filename="reference.txt", kind="file"
    )
    second = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="second introduces it",
            conversation_key=first.conversation_key,
            companions=[{"stage_id": staged_v1.id, "name": "reference"}],
        )
    )

    assert await companions.prepare_for_turn(first.conversation_key, 1) == []
    alias = workspaces.paths(first.conversation_key).companion_aliases / "reference"
    assert not alias.exists()
    active_v1 = await companions.prepare_for_turn(first.conversation_key, 2)
    assert [item.name for item in active_v1] == ["reference"]
    assert alias.read_text(encoding="utf-8") == "version one"

    alias.write_text("agent edit", encoding="utf-8")
    third = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="inherit edit",
            conversation_key=first.conversation_key,
        )
    )
    inherited = await companions.prepare_for_turn(first.conversation_key, 3)
    assert [item.version for item in inherited] == [1]
    assert alias.read_text(encoding="utf-8") == "agent edit"

    staged_v2 = await companions.stage_upload(
        _chunks(b"version two"), filename="reference.txt", kind="file"
    )
    replacement = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="replace it",
            conversation_key=first.conversation_key,
            companions=[{"stage_id": staged_v2.id, "name": "reference"}],
        )
    )
    # The old editable version stays visible until the replacement's sequence.
    assert alias.read_text(encoding="utf-8") == "agent edit"
    active_v2 = await companions.prepare_for_turn(first.conversation_key, 4)
    assert [(item.name, item.version) for item in active_v2] == [("reference", 2)]
    assert alias.read_text(encoding="utf-8") == "version two"

    current = await companions.list_conversation(first.conversation_key)
    assert [(item.name, item.version, item.status) for item in current] == [
        ("reference", 2, "active")
    ]
    history = await companions.list_conversation(first.conversation_key, include_history=True)
    assert {(item.version, str(item.status)) for item in history} == {
        (1, "superseded"),
        (2, "active"),
    }
    assert second.companion_additions[0].introducing_sequence == 2
    assert third.companion_additions == []
    assert replacement.companion_additions[0].version == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_introducing_turn_activates_on_next_runnable_turn(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancelled.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    staged = await companions.stage_upload(
        _chunks(b"survives cancellation"), filename="input.txt", kind="file"
    )
    introducing = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="cancel me",
            companions=[{"stage_id": staged.id, "name": "input"}],
        )
    )
    await jobs.cancel(introducing.job_id)
    successor = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="use accepted input",
            conversation_key=introducing.conversation_key,
        )
    )

    assert successor.job_id != introducing.job_id
    active = await companions.prepare_for_turn(introducing.conversation_key, 2)
    assert [(item.name, item.introducing_sequence, item.status) for item in active] == [
        ("input", 1, "active")
    ]
    alias = workspaces.paths(introducing.conversation_key).companion_aliases / "input"
    assert alias.read_text(encoding="utf-8") == "survives cancellation"
    await engine.dispose()


@pytest.mark.asyncio
async def test_prepare_repairs_stable_link_and_recreates_missing_working_copy(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'repair.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    staged = await companions.stage_upload(
        _chunks(b"immutable original"), filename="input.txt", kind="file"
    )
    accepted = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="first",
            companions=[{"stage_id": staged.id, "name": "input"}],
        )
    )
    await companions.prepare_for_turn(accepted.conversation_key, 1)
    alias = workspaces.paths(accepted.conversation_key).companion_aliases / "input"
    working = alias.resolve()
    alias.write_text("agent edit", encoding="utf-8")

    # An altered alias is repaired without replacing the intact editable copy.
    alias.unlink()
    alias.symlink_to("missing-target")
    await companions.prepare_for_turn(accepted.conversation_key, 2)
    assert alias.resolve() == working
    assert alias.read_text(encoding="utf-8") == "agent edit"

    # If the editable copy itself disappears, the immutable accepted source is
    # the recovery point and the stable name is switched only after recreation.
    working.unlink()
    await companions.prepare_for_turn(accepted.conversation_key, 3)
    assert alias.read_text(encoding="utf-8") == "immutable original"
    assert alias.resolve().is_file()
    await engine.dispose()


@pytest.mark.asyncio
async def test_superseded_cleanup_never_follows_working_symlink_into_sibling(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'symlink-cleanup.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    victim_stage = await companions.stage_upload(
        _chunks(b"victim data"), filename="victim.txt", kind="file"
    )
    old_stage = await companions.stage_upload(_chunks(b"old data"), filename="old.txt", kind="file")
    first = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="activate both",
            companions=[
                {"stage_id": victim_stage.id, "name": "victim"},
                {"stage_id": old_stage.id, "name": "replace-me"},
            ],
        )
    )
    await companions.prepare_for_turn(first.conversation_key, 1)
    aliases = workspaces.paths(first.conversation_key).companion_aliases
    victim_alias = aliases / "victim"
    old_alias = aliases / "replace-me"
    victim_working = victim_alias.resolve()
    old_working = old_alias.resolve()
    assert victim_working.read_bytes() == b"victim data"

    # Simulate an agent replacing the managed old version's leaf with a link to
    # another live companion. Supersession must unlink only inside cc_old and
    # must never resolve the link and recursively delete cc_victim.
    old_working.unlink()
    old_working.symlink_to(victim_working)
    replacement_stage = await companions.stage_upload(
        _chunks(b"new data"), filename="new.txt", kind="file"
    )
    await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="replace one",
            conversation_key=first.conversation_key,
            companions=[{"stage_id": replacement_stage.id, "name": "replace-me"}],
        )
    )
    await companions.prepare_for_turn(first.conversation_key, 2)

    assert victim_working.is_file()
    assert victim_alias.read_bytes() == b"victim data"
    assert old_alias.read_bytes() == b"new data"
    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_immutable_source_fails_before_runtime_and_retries_next_turn(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.ensure_directories()
    engine = create_engine(settings.database_url)
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    agents = AgentService(sessions, tmp_path)
    await agents.register(_agent(tmp_path))
    workspaces = WorkspaceManager(settings.data_dir)
    companions = _service(
        sessions,
        workspaces,
        staging_root=settings.data_dir / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    artifacts = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1_000_000,
        max_files_per_job=10,
    )
    runtime = FakeRuntime()
    scheduler = Scheduler(
        settings,
        agent_service=agents,
        job_service=jobs,
        artifact_service=artifacts,
        workspaces=workspaces,
        runtime=runtime,
        lease_manager=LeaseManager(
            sessions, ttl_seconds=30, retry_seconds=0.01, instance_id="retry-test"
        ),
        telemetry=DashboardMetrics(),
        companion_service=companions,
    )
    content = b"recoverable"
    staged = await companions.stage_upload(_chunks(content), filename="source.txt", kind="file")
    accepted = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="will fail before Codex",
            companions=[{"stage_id": staged.id, "name": "source"}],
        )
    )
    companion_id = accepted.companion_additions[0].id
    async with sessions() as session:
        record = await session.get(ConversationCompanionRecord, companion_id)
        assert record is not None
        immutable_source = (
            workspaces.paths(accepted.conversation_key).root / record.source_storage_path
        )
    immutable_source.unlink()

    assert await scheduler.run_once()
    failed = await jobs.get(accepted.job_id)
    assert failed.status is JobStatus.FAILED
    assert "immutable source" in (failed.error or "")
    assert runtime.requests == []
    pending = await companions.list_conversation(accepted.conversation_key)
    assert pending[0].status == "pending"
    assert pending[0].last_activation_error

    immutable_source.parent.mkdir(parents=True, exist_ok=True)
    immutable_source.write_bytes(content)
    os.chmod(immutable_source, 0o600)
    retry = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="retry activation",
            conversation_key=accepted.conversation_key,
        )
    )
    effective = companions.build_prompt_preamble(
        "retry activation", await companions.list_conversation(accepted.conversation_key)
    )
    runtime.enqueue(
        RuntimeResult(
            response="ok",
            thread_id="11111111-1111-4111-8111-111111111111",
            usage=UsageTotals(input_tokens=5, output_tokens=1),
            token_record=_token_record(effective),
        )
    )
    assert await scheduler.run_once()
    assert (await jobs.get(retry.job_id)).status is JobStatus.SUCCEEDED
    assert runtime.requests[0].prompt.endswith("retry activation")
    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_delete_removes_companion_and_claimed_stage_rows(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'delete.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    staged = await companions.stage_upload(_chunks(b"delete me"), filename="input.txt", kind="file")
    accepted = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="bind",
            companions=[{"stage_id": staged.id, "name": "input"}],
        )
    )
    companion_id = accepted.companion_additions[0].id
    await jobs.cancel(accepted.job_id)
    await jobs.delete_conversation(accepted.conversation_key)

    async with sessions() as session:
        assert await session.get(ConversationCompanionRecord, companion_id) is None
        assert await session.get(CompanionStageRecord, staged.id) is None
    assert not workspaces.paths(accepted.conversation_key).root.exists()
    await engine.dispose()


@pytest.mark.asyncio
async def test_job_retention_preserves_sequence_and_companion_visibility(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'job-retention.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    companions = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)

    first_stage = await companions.stage_upload(
        _chunks(b"first"), filename="first.txt", kind="file"
    )
    first = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="first",
            companions=[{"stage_id": first_stage.id, "name": "first"}],
        )
    )
    await jobs.cancel(first.job_id)
    second_stage = await companions.stage_upload(
        _chunks(b"second"), filename="second.txt", kind="file"
    )
    second = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="second",
            conversation_key=first.conversation_key,
            companions=[{"stage_id": second_stage.id, "name": "second"}],
        )
    )
    await jobs.cancel(second.job_id)

    # Model terminal job retention: introducing_job_id becomes NULL while the
    # conversation-owned companion metadata deliberately remains.
    async with sessions() as session, session.begin():
        for job_id in (first.job_id, second.job_id):
            record = await session.get(JobRecord, job_id)
            assert record is not None
            await session.delete(record)

    continuation = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="continue after retention",
            conversation_key=first.conversation_key,
        )
    )
    async with sessions() as session:
        continuation_record = await session.get(JobRecord, continuation.job_id)
        assert continuation_record is not None
        assert continuation_record.sequence == 3
    visible = await companions.prepare_for_turn(first.conversation_key, 3)
    assert {(item.name, item.introducing_sequence) for item in visible} == {
        ("first", 1),
        ("second", 2),
    }
    await engine.dispose()


@pytest.mark.asyncio
async def test_stage_expiry_and_recovery_mark_missing_staged_bytes(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'recovery.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    workspaces = WorkspaceManager(tmp_path / "state")
    expiring = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
        stage_ttl=timedelta(microseconds=-1),
    )
    expired = await expiring.stage_upload(_chunks(b"old"), filename="old.txt", kind="file")
    assert (await expiring.get_stage(expired.id)).status == "expired"

    service = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    missing = await service.stage_upload(
        _chunks(b"missing after restore"), filename="missing.txt", kind="file"
    )
    async with sessions() as session:
        record = await session.get(CompanionStageRecord, missing.id)
        assert record is not None and record.storage_path is not None
        stored = service.storage_root / record.storage_path
    stored.unlink()
    await service.recover()
    recovered = await service.get_stage(missing.id)
    assert recovered.status in {"failed", "expired"}
    assert recovered.error
    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_byte_and_active_name_limits_leave_stages_unclaimed(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'quota.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")
    byte_limited = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
        max_conversation_bytes=5,
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=byte_limited)
    oversized = await byte_limited.stage_upload(_chunks(b"123456"), filename="six.txt", kind="file")
    with pytest.raises(CompanionPolicyError) as error:
        await jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="too large",
                companions=[{"stage_id": oversized.id, "name": "six"}],
            )
        )
    assert error.value.status_code == 413
    assert (await byte_limited.get_stage(oversized.id)).status == "ready"

    name_limited = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
        max_active_names=1,
    )
    named_jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=name_limited)
    first_stage = await name_limited.stage_upload(_chunks(b"one"), filename="one.txt", kind="file")
    first = await named_jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="one",
            companions=[{"stage_id": first_stage.id, "name": "one"}],
        )
    )
    second_stage = await name_limited.stage_upload(_chunks(b"two"), filename="two.txt", kind="file")
    with pytest.raises(CompanionPolicyError) as error:
        await named_jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="two",
                conversation_key=first.conversation_key,
                companions=[{"stage_id": second_stage.id, "name": "two"}],
            )
        )
    assert error.value.status_code == 413
    assert (await name_limited.get_stage(second_stage.id)).status == "ready"
    await engine.dispose()


@pytest.mark.asyncio
async def test_scheduler_uses_effective_preamble_but_persists_raw_prompt_and_metadata(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    settings.ensure_directories()
    engine = create_engine(settings.database_url)
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    agents = AgentService(sessions, tmp_path)
    await agents.register(_agent(tmp_path))
    workspaces = WorkspaceManager(settings.data_dir)
    companions = _service(
        sessions,
        workspaces,
        staging_root=settings.data_dir / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    artifacts = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1_000_000,
        max_files_per_job=10,
    )
    runtime = FakeRuntime()
    scheduler = Scheduler(
        settings,
        agent_service=agents,
        job_service=jobs,
        artifact_service=artifacts,
        workspaces=workspaces,
        runtime=runtime,
        lease_manager=LeaseManager(
            sessions, ttl_seconds=30, retry_seconds=0.01, instance_id="companions-test"
        ),
        telemetry=DashboardMetrics(),
        companion_service=companions,
    )

    staged = await companions.stage_upload(_chunks(b"facts"), filename="facts.txt", kind="file")
    raw_prompt = "Answer from the supplied facts."
    accepted = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt=raw_prompt,
            companions=[{"stage_id": staged.id, "name": "facts"}],
        )
    )
    active = await companions.prepare_for_turn(accepted.conversation_key, 1)
    expected_effective = companions.build_prompt_preamble(raw_prompt, active)
    runtime.enqueue(
        RuntimeResult(
            response="ok",
            thread_id="11111111-1111-4111-8111-111111111111",
            usage=UsageTotals(input_tokens=10, output_tokens=1),
            token_record=_token_record(expected_effective),
        )
    )
    assert await scheduler.run_once()
    assert runtime.requests[0].prompt == expected_effective
    assert "/workspace/companions/facts" in expected_effective
    assert "facts" in expected_effective
    assert staged.sha256 in expected_effective
    async with sessions() as session:
        record = await session.get(JobRecord, accepted.job_id)
        assert record is not None
        assert record.prompt == raw_prompt
        assert record.runtime_metadata["companion_preamble_version"] == companions.preamble_version
        assert record.runtime_metadata["visible_companion_ids"] == [
            accepted.companion_additions[0].id
        ]
        assert (
            record.runtime_metadata["token_usage"]["input_tokens"]
            == _token_record(expected_effective).input_tokens
        )

    legacy = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="plain continuation",
            conversation_key=accepted.conversation_key,
        )
    )
    # Existing companions are inherited, so a continuation is also prefixed.
    inherited_prompt = companions.build_prompt_preamble(
        "plain continuation",
        await companions.list_conversation(accepted.conversation_key),
    )
    runtime.enqueue(
        RuntimeResult(
            response="ok",
            thread_id="11111111-1111-4111-8111-111111111111",
            usage=UsageTotals(input_tokens=10, output_tokens=1),
            token_record=_token_record(inherited_prompt),
        )
    )
    assert await scheduler.run_once()
    assert runtime.requests[1].prompt == inherited_prompt
    assert (await jobs.get(legacy.job_id)).status is JobStatus.SUCCEEDED
    await engine.dispose()


@pytest.mark.asyncio
async def test_interrupted_recovery_retains_visible_companion_snapshot(tmp_path: Path) -> None:
    class SimulatedRouterDeath(BaseException):
        pass

    settings = _settings(tmp_path)
    settings.ensure_directories()
    engine = create_engine(settings.database_url)
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    agents = AgentService(sessions, tmp_path)
    await agents.register(_agent(tmp_path))
    workspaces = WorkspaceManager(settings.data_dir)
    companions = _service(
        sessions,
        workspaces,
        staging_root=settings.data_dir / "companion-staging",
    )
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=companions)
    artifacts = ArtifactService(
        sessions,
        settings.data_dir / "artifact-store",
        max_file_bytes=1_000_000,
        max_files_per_job=10,
    )
    runtime = FakeRuntime()
    scheduler = Scheduler(
        settings,
        agent_service=agents,
        job_service=jobs,
        artifact_service=artifacts,
        workspaces=workspaces,
        runtime=runtime,
        lease_manager=LeaseManager(
            sessions, ttl_seconds=30, retry_seconds=0.01, instance_id="crash-test"
        ),
        telemetry=DashboardMetrics(),
        companion_service=companions,
    )
    staged = await companions.stage_upload(_chunks(b"facts"), filename="facts.txt", kind="file")
    accepted = await jobs.submit(
        PromptRequest(
            agent_id="alpha",
            prompt="crash after activation",
            companions=[{"stage_id": staged.id, "name": "facts"}],
        )
    )
    runtime.enqueue(SimulatedRouterDeath())

    with pytest.raises(SimulatedRouterDeath):
        await scheduler.run_once()
    recovered = await jobs.recover_interrupted()
    assert recovered == [accepted.job_id]
    view = await jobs.get(accepted.job_id)
    assert view.status is JobStatus.INTERRUPTED
    async with sessions() as session:
        record = await session.get(JobRecord, accepted.job_id)
        assert record is not None
        assert record.runtime_metadata["companion_preamble_version"] == companions.preamble_version
        assert record.runtime_metadata["visible_companion_ids"] == [
            accepted.companion_additions[0].id
        ]
    await engine.dispose()


@pytest.mark.asyncio
async def test_git_import_rejects_unsafe_urls_before_network(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'git-policy.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    service = _service(
        sessions,
        WorkspaceManager(tmp_path / "state"),
        staging_root=tmp_path / "state" / "companion-staging",
    )

    for url in (
        "http://github.com/openai/example.git",
        "https://user:secret@example.com/repository.git",
        "https://example.com/repository.git?token=secret",
        "https://example.com/repo sitory.git",
        "https://example.com/repository\\name.git",
        "file:///tmp/repository",
    ):
        with pytest.raises(CompanionPolicyError):
            await service.queue_git_import(GitImportRequest(url=url))

    for ref in ("-option", "/main", ".hidden", "refs/heads/.hidden", "main.lock"):
        with pytest.raises(CompanionPolicyError, match="ref"):
            await service.queue_git_import(
                GitImportRequest(url="https://example.test/repository.git", ref=ref)
            )

    # Numeric loopback is deterministic and must be rejected without relying
    # on DNS or an external network connection.
    with pytest.raises(CompanionPolicyError, match="public|address|routable"):
        await service.queue_git_import(GitImportRequest(url="https://127.0.0.1/repository.git"))

    for unsafe_address in ("224.0.0.1", "ff02::1", "fec0::1"):
        async def unsafe_resolver(
            _host: str, _port: int, value: str = unsafe_address
        ) -> list[str]:
            return [value]

        service.resolver = unsafe_resolver
        with pytest.raises(CompanionPolicyError, match="routable"):
            await service.queue_git_import(
                GitImportRequest(url="https://example.test/repository.git")
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_subprocess_timeout_kills_stubborn_child_with_redirected_stdio(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'subprocess-timeout.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    service = _service(
        sessions,
        WorkspaceManager(tmp_path / "state"),
        staging_root=tmp_path / "state" / "companion-staging",
    )
    child_pid_file = tmp_path / "child.pid"
    child_program = """
import os
import pathlib
import signal
import sys
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="ascii")
time.sleep(60)
"""
    parent_program = """
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", sys.argv[2], sys.argv[1]],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
while not __import__("pathlib").Path(sys.argv[1]).exists():
    time.sleep(0.01)
child.wait()
"""

    def process_is_running(pid: int) -> bool:
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        except (FileNotFoundError, ProcessLookupError):
            return False
        return stat_line.split(") ", 1)[1].split()[0] != "Z"

    child_pid: int | None = None
    try:
        with pytest.raises(TimeoutError):
            await service._run_subprocess(
                [sys.executable, "-c", parent_program, str(child_pid_file), child_program],
                cwd=None,
                env=dict(os.environ),
                timeout_seconds=0.5,
            )
        assert child_pid_file.exists()
        child_pid = int(child_pid_file.read_text(encoding="ascii"))
        for _ in range(50):
            if not process_is_running(child_pid):
                break
            await asyncio.sleep(0.02)
        assert not process_is_running(child_pid)
    finally:
        if child_pid is None and child_pid_file.exists():
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
        if child_pid is not None and process_is_running(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await engine.dispose()


@pytest.mark.asyncio
async def test_git_import_pins_ref_preserves_history_and_sanitizes_process(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "source-repository"
    repository.mkdir()

    def git(*arguments: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout.strip()

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repository)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    git("config", "user.name", "Companion Test")
    git("config", "user.email", "companion@example.invalid")
    (repository / "version.txt").write_text("one", encoding="utf-8")
    git("add", "version.txt")
    git("commit", "-m", "first")
    first_commit = git("rev-parse", "HEAD")
    git("tag", "selected")
    (repository / "version.txt").write_text("two", encoding="utf-8")
    git("commit", "-am", "second")
    assert git("rev-list", "--all", "--count") == "2"

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'git.db'}")
    await initialize_schema(engine)
    sessions = create_session_factory(engine)
    await AgentService(sessions, tmp_path).register(_agent(tmp_path))
    workspaces = WorkspaceManager(tmp_path / "state")

    async def resolver(host: str, port: int) -> list[str]:
        assert (host, port) == ("example.test", 443)
        return ["93.184.216.34"]

    service = _service(
        sessions,
        workspaces,
        staging_root=tmp_path / "state" / "companion-staging",
    )
    service.resolver = resolver
    invocations: list[tuple[list[str], dict[str, str]]] = []

    async def local_fixture_runner(argv, **kwargs):  # type: ignore[no-untyped-def]
        original = list(argv)
        environment = dict(kwargs["env"])
        invocations.append((original, environment))
        rewritten = [
            "protocol.file.allow=always" if item == "protocol.file.allow=never" else item
            for item in original
        ]
        rewritten = [
            str(repository) if item == "https://example.test/repository.git" else item
            for item in rewritten
        ]
        return await service._run_subprocess(
            rewritten,
            cwd=kwargs.get("cwd"),
            env=environment,
            timeout_seconds=kwargs["timeout_seconds"],
            size_root=kwargs.get("size_root"),
            size_limit=kwargs.get("size_limit"),
        )

    service.subprocess_runner = local_fixture_runner
    jobs = JobService(sessions, workspaces, MemoryCache(), companion_service=service)
    await service.start()
    try:
        stage = await service.queue_git_import(
            GitImportRequest(
                url="https://example.test/repository.git",
                ref="selected",
            )
        )
        for _ in range(200):
            stage = await service.get_stage(stage.id)
            if stage.status in {"ready", "failed"}:
                break
            await asyncio.sleep(0.01)
        assert stage.status == "ready", stage.error
        assert stage.resolved_git_commit == first_commit
        assert stage.file_count == 1

        accepted = await jobs.submit(
            PromptRequest(
                agent_id="alpha",
                prompt="use repository",
                companions=[{"stage_id": stage.id, "name": "repository"}],
            )
        )
        active = await service.prepare_for_turn(accepted.conversation_key, 1)
        assert active[0].resolved_git_commit == first_commit
        checkout = workspaces.paths(accepted.conversation_key).companion_aliases / "repository"
        assert (checkout / "version.txt").read_text(encoding="utf-8") == "one"
        history = subprocess.run(
            ["git", "-C", str(checkout), "rev-list", "--all", "--count"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ).stdout.strip()
        assert history == "2"

        remote_argv, remote_env = next(
            (argv, env) for argv, env in invocations if "clone" in argv and "--mirror" in argv
        )
        assert "http.followRedirects=false" in remote_argv
        assert "credential.helper=" in remote_argv
        assert "protocol.allow=never" in remote_argv
        assert "protocol.https.allow=always" in remote_argv
        assert "protocol.file.allow=never" in remote_argv
        assert "http.curloptResolve=example.test:443:93.184.216.34" in remote_argv
        assert remote_env["GIT_TERMINAL_PROMPT"] == "0"
        assert remote_env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert remote_env["GIT_LFS_SKIP_SMUDGE"] == "1"
        assert remote_env["HOME"] == str(service.git_home)
        assert remote_env["XDG_CONFIG_HOME"] == str(service.git_home / ".config")
        assert service.git_home.stat().st_mode & 0o777 == 0o700
        assert not (service.git_home / ".netrc").exists()
        assert not (service.git_home / ".gitconfig").exists()
        assert (
            not {
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "GIT_ASKPASS_REQUIRE",
            }
            & remote_env.keys()
        )
    finally:
        await service.stop()
        await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_companion_tools_preserve_typed_results() -> None:
    now = datetime.now(UTC)
    stage = CompanionStageView(
        id=_stage_id(7),
        kind="git",
        status="queued",
        source_metadata={"host": "example.com", "ref": "main"},
        expires_at=now + timedelta(hours=24),
        created_at=now,
        updated_at=now,
    )
    companion = ConversationCompanionView(
        id="cc_" + "8" * 32,
        stage_id=stage.id,
        conversation_key="conversation",
        introduced_job_id="j_" + "9" * 32,
        introducing_sequence=1,
        name="repository",
        version=1,
        kind="git",
        status="pending",
        path="/workspace/companions/repository",
        size_bytes=10,
        file_count=1,
        sha256="a" * 64,
        resolved_git_commit="b" * 40,
        created_at=now,
        updated_at=now,
    )

    class Companions:
        requested_url: str | None = None

        async def queue_git_import(self, request):  # type: ignore[no-untyped-def]
            self.requested_url = request.url
            return stage

        async def get_stage(self, stage_id: str) -> CompanionStageView:
            assert stage_id == stage.id
            return stage

        async def list_conversation(
            self, conversation_key: str, *, include_history: bool = False
        ) -> list[ConversationCompanionView]:
            assert conversation_key == "conversation"
            assert include_history is True
            return [companion]

    companions = Companions()
    server = build_mcp(
        SimpleNamespace(
            settings=SimpleNamespace(
                mcp_mount_path="/mcp",
                mcp_dns_rebinding_protection=True,
                mcp_allowed_hosts=["testserver"],
                mcp_allowed_origins=[],
            ),
            companion_service=companions,
            agent_service=None,
            job_service=None,
            artifact_service=None,
            cron_service=None,
        )
    )
    _content, staged = await server.call_tool(
        "stage_git_repository", {"url": "https://example.com/repository.git", "ref": "main"}
    )
    assert companions.requested_url == "https://example.com/repository.git"
    assert staged["id"] == stage.id
    _content, fetched = await server.call_tool("get_companion_stage", {"stage_id": stage.id})
    assert fetched["status"] == "queued"
    _content, listed = await server.call_tool(
        "list_conversation_companions",
        {"conversation_key": "conversation", "include_history": True},
    )
    assert listed["result"][0]["path"] == "/workspace/companions/repository"

    with pytest.raises(ToolError) as invalid_stage:
        await server.call_tool("get_companion_stage", {"stage_id": "not-a-stage"})
    assert "REMOTEAGENT_TOOL_ERROR" in str(invalid_stage.value)
    assert "companion_invalid" in str(invalid_stage.value)

    for malformed in (None, {}, "not-a-list"):
        with pytest.raises(ToolError) as invalid_bindings:
            await server.call_tool(
                "submit_prompt",
                {
                    "agent_id": "alpha",
                    "prompt": "use companions",
                    "companions": malformed,
                },
            )
        assert "REMOTEAGENT_TOOL_ERROR" in str(invalid_bindings.value)
        assert "companion_invalid" in str(invalid_bindings.value)

    with pytest.raises(ToolError) as too_many:
        await server.call_tool(
            "submit_prompt",
            {
                "agent_id": "alpha",
                "prompt": "use companions",
                "companions": [
                    {"stage_id": _stage_id(index), "name": f"item-{index}"}
                    for index in range(21)
                ],
            },
        )
    assert "REMOTEAGENT_TOOL_ERROR" in str(too_many.value)
    assert "companion_limit_exceeded" in str(too_many.value)
