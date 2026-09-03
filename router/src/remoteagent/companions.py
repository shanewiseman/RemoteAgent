from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import ipaddress
import json
import logging
import lzma
import os
import shutil
import signal
import socket
import stat
import struct
import tarfile
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
import zlib
from collections.abc import AsyncIterable, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    CompanionStageRecord,
    ConversationCompanionRecord,
    ConversationRecord,
)
from .schemas import (
    COMPANION_NAME_RE,
    CompanionBinding,
    CompanionStageKind,
    CompanionStageStatus,
    CompanionStageView,
    ConversationCompanionStatus,
    ConversationCompanionView,
    GitImportRequest,
)

logger = logging.getLogger(__name__)

COMPANION_PREAMBLE_VERSION = 1
_COPY_CHUNK = 1024 * 1024
_ERROR_LIMIT = 2_000
_GIT_OUTPUT_LIMIT = 16_384


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _bounded_error(value: BaseException | str) -> str:
    text = str(value).replace("\x00", "").strip() or value.__class__.__name__
    return text[:_ERROR_LIMIT]


def _directory_usage(root: Path, *, stop_after: int | None = None) -> tuple[int, int]:
    """Return regular-file bytes/count without following links."""

    total = 0
    count = 0
    if not root.exists():
        return 0, 0
    if root.is_file() and not root.is_symlink():
        return root.stat().st_size, 1
    for directory, directory_names, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directory_names[:] = [
            name for name in directory_names if not (base / name).is_symlink()
        ]
        for filename in filenames:
            path = base / filename
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                total += info.st_size
                count += 1
                if stop_after is not None and total > stop_after:
                    return total, count
    return total, count


def _stored_tree_digest(root: Path) -> str:
    """Hash a stored tree deterministically without following symlinks."""

    digest = hashlib.sha256()
    if root.is_file() and not root.is_symlink():
        with root.open("rb") as handle:
            while chunk := handle.read(_COPY_CHUNK):
                digest.update(chunk)
        return digest.hexdigest()
    entries: list[Path] = []
    for directory, directory_names, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        directory_names.sort()
        filenames.sort()
        for name in directory_names + filenames:
            entries.append(base / name)
    for path in sorted(entries, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8", "surrogateescape")
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            digest.update(b"D\0" + relative + b"\0")
        elif stat.S_ISLNK(info.st_mode):
            digest.update(b"L\0" + relative + b"\0")
            digest.update(os.readlink(path).encode("utf-8", "surrogateescape") + b"\0")
        elif stat.S_ISREG(info.st_mode):
            digest.update(b"F\0" + relative + b"\0")
            with path.open("rb") as handle:
                while chunk := handle.read(_COPY_CHUNK):
                    digest.update(chunk)
        else:
            raise CompanionValidationError("staged data contains a special file")
    return digest.hexdigest()


class CompanionError(RuntimeError):
    """Base class for companion acquisition and activation failures."""


class CompanionNotFoundError(CompanionError, LookupError):
    pass


class CompanionConflictError(CompanionError):
    pass


class CompanionPolicyError(CompanionError):
    """A caller-visible validation/size policy failure."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


class CompanionValidationError(CompanionPolicyError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=422)


class CompanionTooLargeError(CompanionPolicyError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=413)


class CompanionCapacityError(CompanionError):
    status_code = 507


class CompanionPreparationError(CompanionError):
    pass


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stderr: str = ""
    stdout: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class Resolver(Protocol):
    def __call__(self, host: str, port: int) -> Awaitable[Sequence[str]]: ...


class SubprocessRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout_seconds: float,
        size_root: Path | None = None,
        size_limit: int | None = None,
    ) -> Awaitable[GitCommandResult]: ...


def _stage_view(record: CompanionStageRecord) -> CompanionStageView:
    return CompanionStageView(
        id=record.id,
        kind=CompanionStageKind(record.kind),
        status=CompanionStageStatus(record.status),
        source_metadata=dict(record.source_metadata or {}),
        size_bytes=record.size_bytes,
        file_count=record.file_count,
        sha256=record.sha256,
        resolved_git_commit=record.resolved_git_commit,
        error=record.error,
        expires_at=record.expires_at,
        claimed_at=record.claimed_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _companion_view(record: ConversationCompanionRecord) -> ConversationCompanionView:
    return ConversationCompanionView(
        id=record.id,
        stage_id=record.stage_id,
        conversation_key=record.conversation_key,
        introduced_job_id=record.introduced_job_id,
        introducing_sequence=record.introducing_sequence,
        name=record.name,
        version=record.version,
        kind=CompanionStageKind(record.kind),
        status=ConversationCompanionStatus(record.status),
        path=f"/workspace/companions/{record.name}",
        size_bytes=record.size_bytes,
        file_count=record.file_count,
        sha256=record.sha256,
        resolved_git_commit=record.resolved_git_commit,
        activated_at=record.activated_at,
        superseded_at=record.superseded_at,
        last_activation_error=record.last_activation_error,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class CompanionService:
    """Stages, binds, and materializes persistent conversation companion data."""

    preamble_version = COMPANION_PREAMBLE_VERSION

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        staging_root: Path,
        conversations_root: Path,
        *,
        max_upload_bytes: int = 100 * 1024 * 1024,
        max_archive_bytes: int = 100 * 1024 * 1024,
        max_git_mirror_bytes: int = 100 * 1024 * 1024,
        max_git_checkout_bytes: int = 100 * 1024 * 1024,
        max_files: int = 20_000,
        max_additions_per_turn: int = 20,
        max_active_names: int = 200,
        max_conversation_bytes: int = 1024 * 1024 * 1024,
        max_staging_bytes: int = 5 * 1024 * 1024 * 1024,
        stage_ttl: timedelta = timedelta(hours=24),
        git_workers: int = 2,
        git_timeout_seconds: float = 300,
        cleanup_interval_seconds: float = 300,
        resolver: Resolver | None = None,
        subprocess_runner: SubprocessRunner | None = None,
        telemetry: Any | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.staging_root = staging_root.resolve()
        self.conversations_root = conversations_root.resolve()
        self.storage_root = Path(
            os.path.commonpath((self.staging_root, self.conversations_root))
        ).resolve()
        self.max_upload_bytes = max_upload_bytes
        self.max_archive_bytes = max_archive_bytes
        self.max_git_mirror_bytes = max_git_mirror_bytes
        self.max_git_checkout_bytes = max_git_checkout_bytes
        self.max_files = max_files
        self.max_additions_per_turn = max_additions_per_turn
        self.max_active_names = max_active_names
        self.max_conversation_bytes = max_conversation_bytes
        self.max_staging_bytes = max_staging_bytes
        self.stage_ttl = stage_ttl
        self.git_workers = git_workers
        self.git_timeout_seconds = git_timeout_seconds
        self.cleanup_interval_seconds = cleanup_interval_seconds
        self.resolver = resolver or self._resolve_public_addresses
        self.subprocess_runner = subprocess_runner or self._run_subprocess
        self.telemetry = telemetry
        self._git_queue: asyncio.Queue[str] = asyncio.Queue()
        self._tasks: list[asyncio.Task[None]] = []
        self._queued_stage_ids: set[str] = set()
        self._queue_lock = asyncio.Lock()
        self._admission_lock = asyncio.Lock()
        self._physical_write_lock = threading.Lock()
        self._stopping = False
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.conversations_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.git_home = self.staging_root / ".git-home"
        if self.git_home.is_symlink() or (
            self.git_home.exists() and not self.git_home.is_dir()
        ):
            raise CompanionError("isolated Git home path is unsafe")
        (self.git_home / ".config").mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.git_home / "templates").mkdir(parents=True, exist_ok=True, mode=0o700)
        with contextlib.suppress(OSError):
            os.chmod(self.git_home, 0o700)

    async def stage_upload(
        self,
        chunks: AsyncIterable[bytes],
        *,
        filename: str,
        kind: CompanionStageKind | str,
        expected_sha256: str | None = None,
    ) -> CompanionStageView:
        return await self._stage_upload(chunks, filename, kind, expected_sha256)

    async def queue_git_import(
        self, request: GitImportRequest | Mapping[str, Any]
    ) -> CompanionStageView:
        request = GitImportRequest.model_validate(request)
        return await self._queue_git_import(request)

    async def stage_git_repository(
        self, url: str, ref: str | None = None
    ) -> CompanionStageView:
        return await self.queue_git_import(GitImportRequest(url=url, ref=ref))

    async def get_stage(self, stage_id: str) -> CompanionStageView:
        await self._expire_stage_if_needed(stage_id)
        async with self.session_factory() as session:
            record = await session.get(CompanionStageRecord, stage_id)
            if record is None:
                raise CompanionNotFoundError(stage_id)
            return _stage_view(record)

    async def list_conversation(
        self, conversation_key: str, *, include_history: bool = False
    ) -> list[ConversationCompanionView]:
        async with self.session_factory() as session:
            if await session.get(ConversationRecord, conversation_key) is None:
                raise CompanionNotFoundError(conversation_key)
            statement = select(ConversationCompanionRecord).where(
                ConversationCompanionRecord.conversation_key == conversation_key
            )
            if not include_history:
                statement = statement.where(
                    ConversationCompanionRecord.status
                    != ConversationCompanionStatus.SUPERSEDED.value
                )
            statement = statement.order_by(
                ConversationCompanionRecord.name,
                ConversationCompanionRecord.version,
            )
            return [_companion_view(row) for row in (await session.scalars(statement)).all()]

    async def additions_for_job(
        self, job_id: str, *, session: AsyncSession | None = None
    ) -> list[ConversationCompanionView]:
        if session is not None:
            return await self._additions_for_job(session, job_id)
        async with self.session_factory() as owned_session:
            return await self._additions_for_job(owned_session, job_id)

    async def _additions_for_job(
        self, session: AsyncSession, job_id: str
    ) -> list[ConversationCompanionView]:
        rows = (
            await session.scalars(
                select(ConversationCompanionRecord)
                .where(ConversationCompanionRecord.introduced_job_id == job_id)
                .order_by(ConversationCompanionRecord.name)
            )
        ).all()
        return [_companion_view(row) for row in rows]

    async def bind_ready_stages(
        self,
        session: AsyncSession,
        *,
        conversation_key: str,
        job_id: str,
        sequence: int,
        bindings: Sequence[CompanionBinding],
    ) -> list[ConversationCompanionView]:
        return await self._bind_ready_stages(
            session,
            conversation_key=conversation_key,
            job_id=job_id,
            sequence=sequence,
            bindings=bindings,
        )

    async def prepare_for_turn(
        self, conversation_key: str, sequence: int
    ) -> list[ConversationCompanionView]:
        return await self._prepare_for_turn(conversation_key, sequence)

    def build_prompt_preamble(
        self, raw_prompt: str, active: Sequence[ConversationCompanionView]
    ) -> str:
        if not active:
            return raw_prompt
        entries = [
            {
                "name": item.name,
                "path": item.path,
                "kind": item.kind.value,
                "version": item.version,
                "sha256": item.sha256,
                **(
                    {"resolved_git_commit": item.resolved_git_commit}
                    if item.resolved_git_commit
                    else {}
                ),
            }
            for item in sorted(active, key=lambda value: value.name)
        ]
        block = (
            f'<remoteagent-companions version="{COMPANION_PREAMBLE_VERSION}">\n'
            "Conversation companion working copies are available below. Changes made to "
            "them persist across turns. Put files intended for return to the caller in "
            "/workspace/artifacts.\n"
            f"{json.dumps(entries, sort_keys=True, separators=(',', ':'), ensure_ascii=True)}\n"
            "</remoteagent-companions>"
        )
        return f"{block}\n\n{raw_prompt}"

    async def start(self) -> None:
        if self._tasks:
            return
        self._stopping = False
        await self.recover()
        self._tasks = [
            asyncio.create_task(self._git_worker(index), name=f"companion-git-{index}")
            for index in range(self.git_workers)
        ]
        self._tasks.append(
            asyncio.create_task(self._cleanup_loop(), name="companion-cleanup")
        )

    async def stop(self) -> None:
        self._stopping = True
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def recover(self) -> None:
        await self._recover()

    async def cleanup(self) -> None:
        await self._cleanup()

    # Implementations are kept as separate methods so acquisition and filesystem
    # behavior can be replaced by deterministic fakes in unit tests.
    async def _stage_upload(
        self,
        chunks: AsyncIterable[bytes],
        filename: str,
        kind: CompanionStageKind | str,
        expected_sha256: str | None,
    ) -> CompanionStageView:
        try:
            normalized_kind = CompanionStageKind(kind)
        except ValueError as exc:
            raise CompanionValidationError("upload kind must be file or archive") from exc
        if normalized_kind is CompanionStageKind.GIT:
            raise CompanionValidationError("Git repositories must use the Git import endpoint")
        filename = self._validate_filename(filename)
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.lower()
            if len(expected_sha256) != 64 or any(
                character not in "0123456789abcdef" for character in expected_sha256
            ):
                raise CompanionValidationError("expected_sha256 must be a SHA-256 hex digest")

        stage_id = f"cs_{uuid.uuid4().hex}"
        temporary_root: Path | None = None
        digest = hashlib.sha256()
        uploaded = 0
        final_root = self.staging_root / stage_id
        finalized = False
        persisted = False
        started = time.monotonic()
        try:
            temporary_root = Path(
                tempfile.mkdtemp(prefix=f".{stage_id}-", dir=self.staging_root)
            )
            upload_path = temporary_root / ".upload"
            descriptor = os.open(upload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", buffering=0) as handle:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise CompanionValidationError("upload stream yielded a non-bytes value")
                    if not chunk:
                        continue
                    uploaded += len(chunk)
                    if uploaded > self.max_upload_bytes:
                        raise CompanionTooLargeError("upload exceeds the configured size limit")
                    await asyncio.to_thread(self._write_admitted_staging_chunk, handle, chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            upload_digest = digest.hexdigest()
            if expected_sha256 is not None and upload_digest != expected_sha256:
                raise CompanionValidationError("uploaded content does not match expected_sha256")

            if normalized_kind is CompanionStageKind.FILE:
                source = temporary_root / "content"
                os.replace(upload_path, source)
                size_bytes, file_count = uploaded, 1
            else:
                source = temporary_root / "tree"
                source.mkdir(mode=0o700)
                size_bytes, file_count = await asyncio.to_thread(
                    self._extract_archive, upload_path, source, filename
                )
                upload_path.unlink(missing_ok=True)
            source_digest = await asyncio.to_thread(_stored_tree_digest, source)
            await asyncio.to_thread(self._fsync_tree, temporary_root)
            if final_root.exists():
                raise CompanionConflictError("generated stage identifier already exists")

            async with self._admission_lock:
                await self._ensure_staging_capacity(size_bytes)
                os.replace(temporary_root, final_root)
                finalized = True
                temporary_root = None
                self._fsync_directory(self.staging_root)
                storage_path = self._relative_storage(final_root / source.name)
                record = CompanionStageRecord(
                    id=stage_id,
                    kind=normalized_kind.value,
                    status=CompanionStageStatus.READY.value,
                    source_metadata={
                        "filename": filename,
                        "upload_sha256": upload_digest,
                        "source_sha256": source_digest,
                    },
                    storage_path=storage_path,
                    size_bytes=size_bytes,
                    file_count=file_count,
                    sha256=source_digest,
                    expires_at=_utcnow() + self.stage_ttl,
                )
                try:
                    async with self.session_factory() as session, session.begin():
                        session.add(record)
                        await session.flush()
                except BaseException:
                    self._remove_stage_path(final_root)
                    raise
                persisted = True
            self._observe(
                normalized_kind.value,
                CompanionStageStatus.READY.value,
                size_bytes=size_bytes,
                duration_seconds=time.monotonic() - started,
            )
            return _stage_view(record)
        except OSError as exc:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
            if finalized and not persisted:
                self._remove_stage_path(final_root)
            if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                raise CompanionCapacityError("companion staging storage is exhausted") from exc
            raise
        except BaseException:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
            if finalized and not persisted:
                self._remove_stage_path(final_root)
            raise

    async def _queue_git_import(self, request: GitImportRequest) -> CompanionStageView:
        url, host, port, addresses = await self._validate_git_request(request)
        await self._ensure_staging_capacity(0)
        stage_id = f"cs_{uuid.uuid4().hex}"
        record = CompanionStageRecord(
            id=stage_id,
            kind=CompanionStageKind.GIT.value,
            status=CompanionStageStatus.QUEUED.value,
            source_metadata={
                "url": url,
                **({"ref": request.ref} if request.ref else {}),
                "host": host,
                "port": port,
                "pinned_addresses": list(addresses),
            },
            expires_at=_utcnow() + self.stage_ttl,
        )
        async with self.session_factory() as session, session.begin():
            session.add(record)
            await session.flush()
        self._observe(CompanionStageKind.GIT.value, CompanionStageStatus.QUEUED.value)
        await self._enqueue_git(stage_id)
        return _stage_view(record)

    def _validate_filename(self, filename: str) -> str:
        filename = unicodedata.normalize("NFC", filename.strip())
        if (
            not filename
            or len(filename) > 255
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        ):
            raise CompanionValidationError("filename must be one safe path component")
        return filename

    def _relative_storage(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.storage_root).as_posix()
        except ValueError as exc:
            raise CompanionError("companion storage escaped the configured runtime root") from exc

    def _stored_path(self, relative: str | None, *, within: Path) -> Path:
        if not relative:
            raise CompanionPreparationError("companion has no stored source")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise CompanionPreparationError("invalid persisted companion storage path")
        candidate = self.storage_root / Path(pure)
        try:
            candidate.relative_to(within)
        except ValueError as exc:
            raise CompanionPreparationError("persisted companion path escaped storage") from exc
        self._reject_symlink_ancestors(within, candidate.parent)
        return candidate

    async def _ensure_staging_capacity(self, additional_bytes: int) -> None:
        async with self.session_factory() as session:
            current = int(
                await session.scalar(
                    select(func.coalesce(func.sum(CompanionStageRecord.size_bytes), 0)).where(
                        CompanionStageRecord.status.in_(
                            (
                                CompanionStageStatus.QUEUED.value,
                                CompanionStageStatus.IMPORTING.value,
                                CompanionStageStatus.READY.value,
                            )
                        )
                    )
                )
                or 0
            )
        physical, _ = await asyncio.to_thread(
            _directory_usage, self.staging_root, stop_after=self.max_staging_bytes
        )
        if max(current + additional_bytes, physical) > self.max_staging_bytes:
            raise CompanionCapacityError("deployment companion staging capacity is exhausted")

    def _write_admitted_staging_chunk(self, handle: Any, chunk: bytes) -> None:
        """Reserve physical staging capacity and write one unbuffered chunk atomically."""

        with self._physical_write_lock:
            physical, _ = _directory_usage(
                self.staging_root,
                stop_after=max(0, self.max_staging_bytes - len(chunk)),
            )
            if physical + len(chunk) > self.max_staging_bytes:
                raise CompanionCapacityError(
                    "deployment companion staging capacity is exhausted"
                )
            remaining = memoryview(chunk)
            while remaining:
                written = handle.write(remaining)
                if not written:
                    raise OSError(errno.EIO, "short write to companion staging storage")
                remaining = remaining[written:]

    def _validated_archive_member(self, raw_name: str) -> tuple[str, tuple[str, ...]]:
        name = unicodedata.normalize("NFC", raw_name)
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(ord(character) < 32 or ord(character) == 127 for character in name)
        ):
            raise CompanionValidationError("archive contains an unsafe path")
        pure = PurePosixPath(name.rstrip("/"))
        if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
            raise CompanionValidationError("archive contains an unsafe path")
        if pure.parts[0].endswith(":"):
            raise CompanionValidationError("archive contains an absolute drive path")
        normalized = pure.as_posix()
        if len(normalized.encode("utf-8")) > 4096 or any(
            len(part.encode("utf-8")) > 255 for part in pure.parts
        ):
            raise CompanionValidationError("archive contains a path that is too long")
        key = tuple(part.casefold() for part in pure.parts)
        return normalized, key

    def _extract_archive(self, archive: Path, destination: Path, filename: str) -> tuple[int, int]:
        lower_name = filename.lower()
        try:
            if lower_name.endswith(".zip"):
                return self._extract_zip(archive, destination)
            if lower_name.endswith((".tar", ".tar.gz", ".tgz")):
                return self._extract_tar(archive, destination)
            raise CompanionValidationError("archive must be .zip, .tar, .tar.gz, or .tgz")
        except CompanionError:
            raise
        except OSError as exc:
            if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                raise CompanionCapacityError("companion staging storage is exhausted") from exc
            raise CompanionValidationError("archive could not be safely extracted") from exc
        except (
            EOFError,
            NotImplementedError,
            RuntimeError,
            ValueError,
            lzma.LZMAError,
            struct.error,
            tarfile.TarError,
            zipfile.BadZipFile,
            zipfile.LargeZipFile,
            zlib.error,
        ) as exc:
            raise CompanionValidationError("archive could not be safely extracted") from exc

    def _reserve_archive_path(
        self,
        key: tuple[str, ...],
        *,
        is_directory: bool,
        seen: dict[tuple[str, ...], bool],
        explicit: set[tuple[str, ...]],
        counts: list[int],
    ) -> None:
        if key in explicit:
            raise CompanionValidationError("archive contains duplicate normalized paths")
        for index in range(1, len(key)):
            ancestor = key[:index]
            existing = seen.get(ancestor)
            if existing is False:
                raise CompanionValidationError("archive path descends through a file")
            if existing is None:
                seen[ancestor] = True
                counts[0] += 1
        existing = seen.get(key)
        if existing is not None and existing is not is_directory:
            raise CompanionValidationError("archive file conflicts with an existing directory")
        if existing is None:
            seen[key] = is_directory
            counts[0 if is_directory else 1] += 1
        explicit.add(key)
        if counts[0] > self.max_files or counts[1] > self.max_files:
            raise CompanionTooLargeError("archive exceeds the file-count limit")

    def _extract_zip(self, archive: Path, destination: Path) -> tuple[int, int]:
        total = 0
        file_count = 0
        entry_count = 0
        seen: dict[tuple[str, ...], bool] = {}
        explicit: set[tuple[str, ...]] = set()
        counts = [0, 0]
        self._preflight_zip_directory(archive)
        try:
            handle = zipfile.ZipFile(archive)
        except (OSError, zipfile.BadZipFile) as exc:
            raise CompanionValidationError("invalid ZIP archive") from exc
        with handle:
            for member in handle.infolist():
                entry_count += 1
                if entry_count > self.max_files:
                    raise CompanionTooLargeError("archive exceeds the file-count limit")
                relative, key = self._validated_archive_member(member.filename)
                unix_mode = (member.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(unix_mode)
                is_directory = member.is_dir() or file_type == stat.S_IFDIR
                if member.flag_bits & 0x1:
                    raise CompanionValidationError("encrypted ZIP entries are not supported")
                if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise CompanionValidationError("archive links and special files are forbidden")
                if unix_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                    raise CompanionValidationError("archive special permission bits are forbidden")
                self._reserve_archive_path(
                    key,
                    is_directory=is_directory,
                    seen=seen,
                    explicit=explicit,
                    counts=counts,
                )
                target = destination / Path(PurePosixPath(relative))
                if is_directory:
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                file_count += 1
                if total + member.file_size > self.max_archive_bytes:
                    raise CompanionTooLargeError("archive exceeds the expanded-size limit")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o600)
                try:
                    with os.fdopen(descriptor, "wb", buffering=0, closefd=True) as output:
                        descriptor = -1
                        with handle.open(member, "r") as source:
                            while chunk := source.read(_COPY_CHUNK):
                                total += len(chunk)
                                if total > self.max_archive_bytes:
                                    raise CompanionTooLargeError(
                                        "archive exceeds the expanded-size limit"
                                    )
                                self._write_admitted_staging_chunk(output, chunk)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
        return total, file_count

    def _preflight_zip_directory(self, archive: Path) -> None:
        """Count central-directory records without constructing unbounded ZipInfo objects."""

        eocd_signature = b"PK\x05\x06"
        zip64_locator_signature = b"PK\x06\x07"
        zip64_eocd_signature = b"PK\x06\x06"
        central_signature = b"PK\x01\x02"
        try:
            size = archive.stat().st_size
            if size < 22:
                raise CompanionValidationError("invalid ZIP archive")
            with archive.open("rb") as handle:
                tail_size = min(size, 22 + 65_535 + 20)
                handle.seek(size - tail_size)
                tail = handle.read(tail_size)
                search_end = len(tail)
                eocd_index = -1
                eocd: tuple[bytes, int, int, int, int, int, int, int] | None = None
                while search_end:
                    candidate = tail.rfind(eocd_signature, 0, search_end)
                    if candidate < 0:
                        break
                    if candidate + 22 <= len(tail):
                        parsed = struct.unpack("<4s4H2LH", tail[candidate : candidate + 22])
                        if candidate + 22 + parsed[-1] == len(tail):
                            eocd_index = candidate
                            eocd = parsed
                            break
                    search_end = candidate
                if eocd is None:
                    raise CompanionValidationError("invalid ZIP archive")

                _, disk, directory_disk, entries_disk, entries, cd_size, cd_offset, _ = eocd
                if disk != 0 or directory_disk != 0 or entries_disk != entries:
                    raise CompanionValidationError("multi-disk ZIP archives are not supported")
                eocd_offset = size - tail_size + eocd_index
                directory_limit = eocd_offset

                locator_offset = eocd_offset - 20
                locator: bytes | None = None
                if locator_offset >= 0:
                    handle.seek(locator_offset)
                    locator = handle.read(20)
                has_zip64 = locator is not None and locator.startswith(zip64_locator_signature)
                classic_uses_zip64 = (
                    entries == 0xFFFF or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF
                )
                if has_zip64:
                    if locator is None or len(locator) != 20:
                        raise CompanionValidationError("invalid ZIP64 archive")
                    signature, zip64_disk, zip64_offset, disk_count = struct.unpack(
                        "<4sLQL", locator
                    )
                    physical_zip64_offset = locator_offset - 56
                    if (
                        signature != zip64_locator_signature
                        or zip64_disk != 0
                        or disk_count != 1
                        or physical_zip64_offset < 0
                        or zip64_offset > physical_zip64_offset
                    ):
                        raise CompanionValidationError("invalid ZIP64 archive")

                    handle.seek(zip64_offset)
                    raw_zip64 = handle.read(56)
                    zip64_record_offset = zip64_offset
                    extensible_size = physical_zip64_offset - zip64_offset
                    if not raw_zip64.startswith(zip64_eocd_signature) and (
                        zip64_offset != physical_zip64_offset
                    ):
                        # Match zipfile's support for a prepended executable: its
                        # stored offsets omit the prefix, while the record itself
                        # sits immediately before the locator.
                        handle.seek(physical_zip64_offset)
                        raw_zip64 = handle.read(56)
                        zip64_record_offset = physical_zip64_offset
                        extensible_size = 0
                    if len(raw_zip64) != 56:
                        raise CompanionValidationError("invalid ZIP64 archive")
                    (
                        signature,
                        record_size,
                        _made_by,
                        _needed,
                        disk,
                        directory_disk,
                        entries_disk,
                        entries,
                        cd_size,
                        cd_offset,
                    ) = struct.unpack("<4sQ2H2L4Q", raw_zip64)
                    if (
                        signature != zip64_eocd_signature
                        or record_size < 44
                        or disk != 0
                        or directory_disk != 0
                        or entries_disk != entries
                        or cd_offset + cd_size != zip64_offset
                        or record_size + 12 != 56 + extensible_size
                    ):
                        raise CompanionValidationError("invalid ZIP64 archive")
                    directory_limit = zip64_record_offset
                elif classic_uses_zip64:
                    raise CompanionValidationError("invalid ZIP64 archive")

                if entries > self.max_files:
                    raise CompanionTooLargeError("archive exceeds the file-count limit")
                if cd_size > directory_limit:
                    raise CompanionValidationError("invalid ZIP central directory")

                directory_start = directory_limit - cd_size
                directory_end = directory_limit
                handle.seek(directory_start)
                actual_entries = 0
                position = directory_start
                while position < directory_end:
                    fixed = handle.read(46)
                    if len(fixed) != 46 or fixed[:4] != central_signature:
                        raise CompanionValidationError("invalid ZIP central directory")
                    filename_size, extra_size, comment_size = struct.unpack(
                        "<3H", fixed[28:34]
                    )
                    record_size = 46 + filename_size + extra_size + comment_size
                    if record_size > directory_end - position:
                        raise CompanionValidationError("invalid ZIP central directory")
                    handle.seek(record_size - 46, os.SEEK_CUR)
                    position += record_size
                    actual_entries += 1
                    if actual_entries > self.max_files:
                        raise CompanionTooLargeError("archive exceeds the file-count limit")
                if position != directory_end or actual_entries != entries:
                    raise CompanionValidationError("invalid ZIP central directory")
        except CompanionError:
            raise
        except (OSError, struct.error) as exc:
            raise CompanionValidationError("invalid ZIP archive") from exc

    def _extract_tar(self, archive: Path, destination: Path) -> tuple[int, int]:
        total = 0
        file_count = 0
        entry_count = 0
        seen: dict[tuple[str, ...], bool] = {}
        explicit: set[tuple[str, ...]] = set()
        counts = [0, 0]
        with tarfile.open(archive, mode="r:*") as handle:
            for member in handle:
                entry_count += 1
                if entry_count > self.max_files:
                    raise CompanionTooLargeError("archive exceeds the file-count limit")
                relative, key = self._validated_archive_member(member.name)
                if member.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                    raise CompanionValidationError("archive special permission bits are forbidden")
                if not (member.isdir() or member.isreg()):
                    raise CompanionValidationError("archive links and special files are forbidden")
                self._reserve_archive_path(
                    key,
                    is_directory=member.isdir(),
                    seen=seen,
                    explicit=explicit,
                    counts=counts,
                )
                target = destination / Path(PurePosixPath(relative))
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                file_count += 1
                if total + member.size > self.max_archive_bytes:
                    raise CompanionTooLargeError("archive exceeds the expanded-size limit")
                source = handle.extractfile(member)
                if source is None:
                    raise CompanionValidationError("archive member could not be read")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o600)
                try:
                    with source, os.fdopen(
                        descriptor, "wb", buffering=0, closefd=True
                    ) as output:
                        descriptor = -1
                        remaining = member.size
                        while remaining:
                            chunk = source.read(min(_COPY_CHUNK, remaining))
                            if not chunk:
                                raise CompanionValidationError("truncated tar archive member")
                            remaining -= len(chunk)
                            total += len(chunk)
                            if total > self.max_archive_bytes:
                                raise CompanionTooLargeError(
                                    "archive exceeds the expanded-size limit"
                                )
                            self._write_admitted_staging_chunk(output, chunk)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
        return total, file_count

    async def _bind_ready_stages(
        self,
        session: AsyncSession,
        *,
        conversation_key: str,
        job_id: str,
        sequence: int,
        bindings: Sequence[CompanionBinding],
    ) -> list[ConversationCompanionView]:
        normalized = [CompanionBinding.model_validate(item) for item in bindings]
        if len(normalized) > self.max_additions_per_turn:
            raise CompanionTooLargeError("too many companion additions for one turn")
        stage_ids = [item.stage_id for item in normalized]
        names = [item.name for item in normalized]
        if len(set(stage_ids)) != len(stage_ids):
            raise CompanionValidationError("companion stage IDs must be unique")
        if len(set(names)) != len(names):
            raise CompanionValidationError("companion names must be unique within a turn")
        if not normalized:
            return []
        for name in names:
            if not COMPANION_NAME_RE.fullmatch(name):
                raise CompanionValidationError("invalid companion name")

        rows = (
            await session.scalars(
                select(CompanionStageRecord)
                .where(CompanionStageRecord.id.in_(stage_ids))
                .with_for_update()
            )
        ).all()
        by_id = {row.id: row for row in rows}
        missing = next((stage_id for stage_id in stage_ids if stage_id not in by_id), None)
        if missing is not None:
            raise CompanionNotFoundError(missing)
        now = _utcnow()
        for stage_id in stage_ids:
            row = by_id[stage_id]
            if row.status != CompanionStageStatus.READY.value:
                raise CompanionConflictError(f"companion stage is not ready: {stage_id}")
            if _as_utc(row.expires_at) <= now:
                raise CompanionConflictError(f"companion stage has expired: {stage_id}")
            if (
                row.storage_path is None
                or row.size_bytes is None
                or row.file_count is None
                or row.sha256 is None
            ):
                raise CompanionConflictError(f"companion stage is incomplete: {stage_id}")

        current_rows = (
            await session.scalars(
                select(ConversationCompanionRecord).where(
                    ConversationCompanionRecord.conversation_key == conversation_key,
                    ConversationCompanionRecord.status.in_(
                        (
                            ConversationCompanionStatus.PENDING.value,
                            ConversationCompanionStatus.ACTIVE.value,
                        )
                    ),
                )
            )
        ).all()
        current_names = {row.name for row in current_rows}
        if len(current_names | set(names)) > self.max_active_names:
            raise CompanionTooLargeError("conversation companion name limit exceeded")
        current_bytes = sum(row.size_bytes for row in current_rows)
        new_bytes = sum(int(by_id[stage_id].size_bytes or 0) for stage_id in stage_ids)
        if current_bytes + new_bytes > self.max_conversation_bytes:
            raise CompanionTooLargeError("conversation companion byte limit exceeded")

        max_versions: dict[str, int] = {}
        version_rows = await session.execute(
            select(
                ConversationCompanionRecord.name,
                func.max(ConversationCompanionRecord.version),
            )
            .where(
                ConversationCompanionRecord.conversation_key == conversation_key,
                ConversationCompanionRecord.name.in_(names),
            )
            .group_by(ConversationCompanionRecord.name)
        )
        for name, version in version_rows:
            max_versions[name] = int(version)

        conversation_root = self._conversation_root(conversation_key)
        object_root = conversation_root / "inputs" / "objects"
        created_roots: list[Path] = []
        records: list[ConversationCompanionRecord] = []
        try:
            object_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for binding in normalized:
                stage = by_id[binding.stage_id]
                companion_id = f"cc_{uuid.uuid4().hex}"
                source = self._stored_path(stage.storage_path, within=self.staging_root)
                if not source.exists() or source.is_symlink():
                    raise CompanionConflictError(
                        f"companion stage data is unavailable: {binding.stage_id}"
                    )
                destination_root = object_root / companion_id
                source_name = self._source_name(CompanionStageKind(stage.kind))
                destination = destination_root / source_name
                before_digest = await asyncio.to_thread(_stored_tree_digest, source)
                if before_digest != stage.sha256:
                    raise CompanionConflictError(
                        f"companion stage data is corrupt: {binding.stage_id}"
                    )
                await asyncio.to_thread(self._copy_immutable_source, source, destination_root, source_name)
                created_roots.append(destination_root)
                after_digest = await asyncio.to_thread(_stored_tree_digest, destination)
                if before_digest != after_digest:
                    raise CompanionConflictError("staged companion changed while it was promoted")
                if CompanionStageKind(stage.kind) is CompanionStageKind.GIT:
                    if not stage.resolved_git_commit:
                        raise CompanionConflictError("Git stage has no resolved commit")
                    await self._verify_git_repository(
                        destination,
                        stage.resolved_git_commit,
                        stage.sha256,
                        context="promoted Git companion",
                    )
                version = max_versions.get(binding.name, 0) + 1
                max_versions[binding.name] = version
                records.append(
                    ConversationCompanionRecord(
                        id=companion_id,
                        stage_id=stage.id,
                        conversation_key=conversation_key,
                        introduced_job_id=job_id,
                        introducing_sequence=sequence,
                        name=binding.name,
                        version=version,
                        kind=stage.kind,
                        status=ConversationCompanionStatus.PENDING.value,
                        source_storage_path=destination.relative_to(conversation_root).as_posix(),
                        working_storage_path=None,
                        size_bytes=int(stage.size_bytes),
                        file_count=int(stage.file_count),
                        sha256=stage.sha256,
                        resolved_git_commit=stage.resolved_git_commit,
                    )
                )
            session.add_all(records)
            for stage_id in stage_ids:
                stage = by_id[stage_id]
                stage.status = CompanionStageStatus.CLAIMED.value
                stage.claimed_at = now
            await session.flush()
            return [_companion_view(record) for record in records]
        except BaseException as exc:
            for root in created_roots:
                shutil.rmtree(root, ignore_errors=True)
            if isinstance(exc, OSError) and exc.errno in (
                errno.ENOSPC,
                getattr(errno, "EDQUOT", errno.ENOSPC),
            ):
                raise CompanionCapacityError(
                    "conversation companion storage is exhausted"
                ) from exc
            raise

    def _conversation_root(self, conversation_key: str) -> Path:
        candidate = (self.conversations_root / conversation_key).resolve()
        try:
            candidate.relative_to(self.conversations_root)
        except ValueError as exc:
            raise CompanionValidationError("invalid conversation key") from exc
        return candidate

    def _source_name(self, kind: CompanionStageKind) -> str:
        return {
            CompanionStageKind.FILE: "content",
            CompanionStageKind.ARCHIVE: "tree",
            CompanionStageKind.GIT: "mirror.git",
        }[kind]

    def _working_name(self, kind: CompanionStageKind) -> str:
        return {
            CompanionStageKind.FILE: "content",
            CompanionStageKind.ARCHIVE: "tree",
            CompanionStageKind.GIT: "repo",
        }[kind]

    def _copy_immutable_source(
        self, source: Path, destination_root: Path, source_name: str
    ) -> None:
        temporary = destination_root.with_name(f".{destination_root.name}-{uuid.uuid4().hex}")
        if destination_root.exists():
            raise CompanionConflictError("companion object identifier collision")
        try:
            temporary.mkdir(parents=True, mode=0o700)
            destination = temporary / source_name
            if source.is_file():
                self._copy_regular_file(source, destination)
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True)
                self._force_private_tree(destination)
            else:
                raise CompanionConflictError("staged companion is not a regular file or tree")
            self._fsync_tree(temporary)
            os.replace(temporary, destination_root)
            self._fsync_directory(destination_root.parent)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _copy_regular_file(self, source: Path, destination: Path) -> None:
        descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        temporary: str | None = None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise CompanionConflictError("companion source is not a regular file")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with os.fdopen(descriptor, "rb", closefd=True) as input_handle:
                descriptor = -1
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=".copy-", delete=False
                ) as output:
                    temporary = output.name
                    shutil.copyfileobj(input_handle, output, length=_COPY_CHUNK)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary, destination)
            temporary = None
            os.chmod(destination, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _force_private_tree(self, root: Path) -> None:
        for directory, directory_names, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            with contextlib.suppress(OSError):
                os.chmod(base, 0o700)
            directory_names[:] = [
                name for name in directory_names if not (base / name).is_symlink()
            ]
            for filename in filenames:
                path = base / filename
                if not path.is_symlink():
                    with contextlib.suppress(OSError):
                        os.chmod(path, 0o600)

    async def _prepare_for_turn(
        self, conversation_key: str, sequence: int
    ) -> list[ConversationCompanionView]:
        cleanup_records: list[tuple[str, str | None]] = []
        failure: BaseException | None = None
        active_views: list[ConversationCompanionView] = []
        async with self.session_factory() as session, session.begin():
            conversation = await session.scalar(
                select(ConversationRecord)
                .where(ConversationRecord.key == conversation_key)
                .with_for_update()
            )
            if conversation is None:
                raise CompanionNotFoundError(conversation_key)
            eligible = (
                await session.scalars(
                    select(ConversationCompanionRecord)
                    .where(
                        ConversationCompanionRecord.conversation_key == conversation_key,
                        ConversationCompanionRecord.introducing_sequence <= sequence,
                        ConversationCompanionRecord.status.in_(
                            (
                                ConversationCompanionStatus.PENDING.value,
                                ConversationCompanionStatus.ACTIVE.value,
                            )
                        ),
                    )
                    .order_by(
                        ConversationCompanionRecord.name,
                        ConversationCompanionRecord.version,
                    )
                    .with_for_update()
                )
            ).all()
            selected_by_name: dict[str, ConversationCompanionRecord] = {}
            for record in eligible:
                selected_by_name[record.name] = record
            selected = [selected_by_name[name] for name in sorted(selected_by_name)]

            prepared: list[tuple[ConversationCompanionRecord, Path]] = []
            failed_record: ConversationCompanionRecord | None = None
            try:
                for record in selected:
                    failed_record = record
                    working = await self._ensure_working_copy(record)
                    prepared.append((record, working))
                old_links: list[tuple[Path, str | None]] = []
                try:
                    for record, working in prepared:
                        stable = self._stable_path(conversation_key, record.name)
                        old_target = os.readlink(stable) if stable.is_symlink() else None
                        old_links.append((stable, old_target))
                        self._atomic_stable_link(stable, working)
                except BaseException:
                    for stable, old_target in reversed(old_links):
                        with contextlib.suppress(OSError):
                            if old_target is None:
                                if stable.is_symlink() or stable.is_file():
                                    stable.unlink()
                            else:
                                self._replace_symlink(stable, old_target)
                    raise
            except Exception as exc:  # noqa: BLE001 - activation errors become durable state.
                failure = exc
                if failed_record is not None:
                    failed_record.status = ConversationCompanionStatus.PENDING.value
                    failed_record.last_activation_error = _bounded_error(exc)
            else:
                now = _utcnow()
                selected_ids = {record.id for record in selected}
                for record in selected:
                    record.status = ConversationCompanionStatus.ACTIVE.value
                    record.activated_at = record.activated_at or now
                    record.last_activation_error = None
                for record in eligible:
                    chosen = selected_by_name[record.name]
                    if record.id not in selected_ids and record.version < chosen.version:
                        record.status = ConversationCompanionStatus.SUPERSEDED.value
                        record.superseded_at = now
                        cleanup_records.append(
                            (record.source_storage_path, record.working_storage_path)
                        )
                await session.flush()
                active_views = [_companion_view(record) for record in selected]
        if failure is not None:
            raise CompanionPreparationError(
                f"could not prepare conversation companions: {_bounded_error(failure)}"
            ) from failure
        for source_relative, working_relative in cleanup_records:
            self._remove_superseded_storage(
                conversation_key, source_relative, working_relative
            )
        return active_views

    async def _ensure_working_copy(self, record: ConversationCompanionRecord) -> Path:
        kind = CompanionStageKind(record.kind)
        conversation_root = self._conversation_root(record.conversation_key)
        source = self._conversation_stored_path(
            record.conversation_key, record.source_storage_path
        )
        if not source.exists() or source.is_symlink():
            raise CompanionPreparationError(f"immutable source is missing for {record.name}")
        await self._verify_immutable_source(record, source)
        working_root = (
            conversation_root
            / "workspace"
            / ".remoteagent"
            / "companions"
            / record.id
        )
        self._reject_symlink_ancestors(conversation_root, working_root.parent)
        if working_root.is_symlink():
            raise CompanionPreparationError(
                f"working-copy root is unsafe for {record.name}"
            )
        working = working_root / self._working_name(kind)
        if record.status == ConversationCompanionStatus.ACTIVE.value and working.exists():
            if working.is_symlink():
                raise CompanionPreparationError(f"working copy is unsafe for {record.name}")
            record.working_storage_path = working.relative_to(conversation_root).as_posix()
            return working
        if working_root.exists():
            if working_root.is_file():
                working_root.unlink()
            else:
                shutil.rmtree(working_root)
        temporary_root = working_root.with_name(f".{record.id}-{uuid.uuid4().hex}")
        temporary_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            temporary_root.mkdir(mode=0o700)
            temporary_working = temporary_root / self._working_name(kind)
            if kind is CompanionStageKind.FILE:
                await asyncio.to_thread(self._copy_regular_file, source, temporary_working)
            elif kind is CompanionStageKind.ARCHIVE:
                await asyncio.to_thread(
                    shutil.copytree, source, temporary_working, symlinks=True
                )
                await asyncio.to_thread(self._force_private_tree, temporary_working)
            else:
                await self._materialize_git(source, temporary_working, record)
            os.replace(temporary_root, working_root)
        except BaseException:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise
        record.working_storage_path = working.relative_to(conversation_root).as_posix()
        return working

    async def _verify_immutable_source(
        self, record: ConversationCompanionRecord, source: Path
    ) -> None:
        kind = CompanionStageKind(record.kind)
        if kind is not CompanionStageKind.GIT:
            digest = await asyncio.to_thread(_stored_tree_digest, source)
            if digest != record.sha256:
                raise CompanionPreparationError(
                    f"immutable source failed integrity verification for {record.name}"
                )
            return
        if not record.resolved_git_commit:
            raise CompanionPreparationError("Git companion has no resolved commit")
        await self._verify_git_repository(
            source,
            record.resolved_git_commit,
            record.sha256,
            context=record.name,
        )

    async def _verify_git_repository(
        self,
        source: Path,
        commit: str,
        expected_sha256: str,
        *,
        context: str,
    ) -> None:
        stored_digest = await asyncio.to_thread(_stored_tree_digest, source)
        if stored_digest != expected_sha256:
            raise CompanionPreparationError(
                f"immutable source failed integrity verification for {context}"
            )
        env = self._git_environment()
        fsck = await self.subprocess_runner(
            self._local_git_command()
            + ["-C", str(source), "fsck", "--full", "--strict", "--no-dangling"],
            cwd=None,
            env=env,
            timeout_seconds=self.git_timeout_seconds,
        )
        self._require_git_success(fsck, "Git companion source is corrupt")
        listing = await self.subprocess_runner(
            self._local_git_command()
            + [
                "-C",
                str(source),
                "ls-tree",
                "-rlz",
                "--full-tree",
                commit,
            ],
            cwd=None,
            env=env,
            timeout_seconds=self.git_timeout_seconds,
        )
        self._require_git_success(listing, "Git companion commit is unavailable")
        if listing.stdout_truncated:
            raise CompanionPreparationError(
                f"immutable source failed integrity verification for {context}"
            )
        self._validate_git_tree(listing.stdout)

    def _conversation_stored_path(self, conversation_key: str, relative: str) -> Path:
        root = self._conversation_root(conversation_key)
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts:
            raise CompanionPreparationError("invalid persisted conversation companion path")
        candidate = root / Path(pure)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise CompanionPreparationError("conversation companion path escaped storage") from exc
        self._reject_symlink_ancestors(root, candidate.parent)
        return candidate

    def _stable_path(self, conversation_key: str, name: str) -> Path:
        if not COMPANION_NAME_RE.fullmatch(name):
            raise CompanionPreparationError("invalid persisted companion name")
        conversation_root = self._conversation_root(conversation_key)
        root = conversation_root / "workspace" / "companions"
        self._reject_symlink_ancestors(conversation_root, root)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root / name

    def _atomic_stable_link(self, stable: Path, working: Path) -> None:
        expected = os.path.relpath(working, start=stable.parent)
        if stable.is_symlink() and os.readlink(stable) == expected:
            return
        self._replace_symlink(stable, expected)

    def _replace_symlink(self, stable: Path, target: str) -> None:
        temporary = stable.with_name(f".{stable.name}-{uuid.uuid4().hex}")
        quarantine: Path | None = None
        try:
            os.symlink(target, temporary)
            if stable.exists() and stable.is_dir() and not stable.is_symlink():
                quarantine = stable.with_name(f".{stable.name}-replaced-{uuid.uuid4().hex}")
                os.replace(stable, quarantine)
            os.replace(temporary, stable)
        finally:
            temporary.unlink(missing_ok=True)
            if quarantine is not None:
                shutil.rmtree(quarantine, ignore_errors=True)

    def _remove_superseded_storage(
        self,
        conversation_key: str,
        source_relative: str,
        working_relative: str | None,
    ) -> None:
        root = self._conversation_root(conversation_key)
        for relative, prefix, allowed_leaf_names in (
            (source_relative, ("inputs", "objects"), {"content", "tree", "mirror.git"}),
            (
                working_relative,
                ("workspace", ".remoteagent", "companions"),
                {"content", "tree", "repo"},
            ),
        ):
            if not relative:
                continue
            try:
                parts = PurePosixPath(relative).parts
                if (
                    len(parts) != len(prefix) + 2
                    or tuple(parts[: len(prefix)]) != prefix
                    or not parts[len(prefix)].startswith("cc_")
                    or parts[-1] not in allowed_leaf_names
                ):
                    raise ValueError("unexpected companion storage shape")
                expected_parent = root.joinpath(*prefix)
                self._reject_symlink_ancestors(root, expected_parent)
                object_root = expected_parent / parts[len(prefix)]
            except (OSError, ValueError):
                logger.error("refusing unsafe superseded companion cleanup path")
                continue
            if object_root.is_symlink():
                object_root.unlink(missing_ok=True)
            elif object_root.exists():
                shutil.rmtree(object_root, ignore_errors=True)

    def _reject_symlink_ancestors(self, root: Path, target: Path) -> None:
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise CompanionPreparationError("companion path escaped conversation") from exc
        current = root
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode):
                raise CompanionPreparationError("companion path has a symbolic-link ancestor")
            if not stat.S_ISDIR(info.st_mode):
                raise CompanionPreparationError("companion path has a non-directory ancestor")

    async def _recover(self) -> None:
        queued: list[str] = []
        interrupted: set[str] = set()
        stale_paths: list[Path] = []
        now = _utcnow()
        async with self.session_factory() as session, session.begin():
            records = (
                await session.scalars(
                    select(CompanionStageRecord).where(
                        CompanionStageRecord.status.in_(
                            (
                                CompanionStageStatus.QUEUED.value,
                                CompanionStageStatus.IMPORTING.value,
                                CompanionStageStatus.READY.value,
                            )
                        )
                    )
                )
            ).all()
            for record in records:
                if _as_utc(record.expires_at) <= now:
                    record.status = CompanionStageStatus.EXPIRED.value
                    record.error = "companion stage expired before it was claimed"
                    if record.storage_path:
                        with contextlib.suppress(CompanionError):
                            stale_paths.append(
                                self._stored_path(record.storage_path, within=self.staging_root)
                            )
                    continue
                if record.status == CompanionStageStatus.IMPORTING.value:
                    interrupted.add(record.id)
                    record.status = CompanionStageStatus.QUEUED.value
                    record.error = None
                    record.storage_path = None
                    record.size_bytes = None
                    record.file_count = None
                    record.sha256 = None
                    record.resolved_git_commit = None
                if record.status == CompanionStageStatus.QUEUED.value:
                    queued.append(record.id)
                elif record.status == CompanionStageStatus.READY.value:
                    try:
                        path = self._stored_path(record.storage_path, within=self.staging_root)
                    except CompanionError:
                        path = Path("/__missing_companion_stage__")
                    if not path.exists() or path.is_symlink():
                        record.status = CompanionStageStatus.FAILED.value
                        record.error = "staged companion data was missing during recovery"
                        record.storage_path = None
            await session.flush()
        for path in stale_paths:
            self._remove_stage_path(path)
        for stage_id in queued:
            self._remove_import_temporaries(stage_id)
            if stage_id in interrupted:
                self._remove_stage_path(self.staging_root / stage_id)
            await self._enqueue_git(stage_id)
        await self.cleanup()
        await self._reconcile_stage_orphans(grace_seconds=0)
        await self._reconcile_conversation_orphans(grace_seconds=0)

    async def _cleanup(self) -> None:
        now = _utcnow()
        paths: list[Path] = []
        async with self.session_factory() as session, session.begin():
            records = (
                await session.scalars(
                    select(CompanionStageRecord).where(
                        CompanionStageRecord.status
                        != CompanionStageStatus.IMPORTING.value
                    ).with_for_update()
                )
            ).all()
            companion_stage_ids = set(
                await session.scalars(select(ConversationCompanionRecord.stage_id))
            )
            for record in records:
                if (
                    record.status
                    in (
                        CompanionStageStatus.QUEUED.value,
                        CompanionStageStatus.READY.value,
                        CompanionStageStatus.FAILED.value,
                    )
                    and _as_utc(record.expires_at) <= now
                ):
                    record.status = CompanionStageStatus.EXPIRED.value
                    record.error = record.error or "companion stage expired before it was claimed"
                if record.status in (
                    CompanionStageStatus.CLAIMED.value,
                    CompanionStageStatus.EXPIRED.value,
                    CompanionStageStatus.FAILED.value,
                ) and record.storage_path:
                    with contextlib.suppress(CompanionError):
                        paths.append(
                            self._stored_path(record.storage_path, within=self.staging_root)
                        )
                    record.storage_path = None
                terminal_age = now - self.stage_ttl
                should_prune = (
                    record.id not in companion_stage_ids
                    and (
                        (
                            record.status
                            in (
                                CompanionStageStatus.EXPIRED.value,
                                CompanionStageStatus.FAILED.value,
                            )
                            and _as_utc(record.expires_at) <= terminal_age
                        )
                        or (
                            record.status == CompanionStageStatus.CLAIMED.value
                            and record.claimed_at is not None
                            and _as_utc(record.claimed_at) <= terminal_age
                        )
                    )
                )
                if should_prune:
                    await session.delete(record)
        for path in paths:
            self._remove_stage_path(path)
        live_ids: set[str]
        async with self.session_factory() as session:
            live_ids = set(
                await session.scalars(
                    select(CompanionStageRecord.id).where(
                        CompanionStageRecord.status.in_(
                            (
                                CompanionStageStatus.QUEUED.value,
                                CompanionStageStatus.IMPORTING.value,
                                CompanionStageStatus.READY.value,
                            )
                        )
                    )
                )
            )
        cutoff = time.time() - max(self.stage_ttl.total_seconds(), 60)
        for child in self.staging_root.iterdir():
            if child == self.git_home:
                continue
            identifier = child.name if child.name.startswith("cs_") else None
            if identifier in live_ids:
                continue
            try:
                old_enough = child.stat().st_mtime <= cutoff
            except OSError:
                continue
            if old_enough:
                self._remove_stage_path(child)
        await self._reconcile_conversation_orphans(
            grace_seconds=max(300, int(self.cleanup_interval_seconds * 2))
        )

    async def _reconcile_stage_orphans(self, *, grace_seconds: int) -> None:
        async with self.session_factory() as session:
            live_ids = set(
                await session.scalars(
                    select(CompanionStageRecord.id).where(
                        CompanionStageRecord.status.in_(
                            (
                                CompanionStageStatus.QUEUED.value,
                                CompanionStageStatus.IMPORTING.value,
                                CompanionStageStatus.READY.value,
                            )
                        )
                    )
                )
            )
        cutoff = time.time() - grace_seconds
        for child in self.staging_root.iterdir():
            if child == self.git_home:
                continue
            is_final = child.name.startswith("cs_")
            is_temporary = child.name.startswith(".cs_")
            if not (is_final or is_temporary):
                continue
            if is_final and child.name in live_ids:
                continue
            try:
                if child.lstat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            self._remove_stage_path(child)

    async def _reconcile_conversation_orphans(self, *, grace_seconds: int) -> None:
        async with self.session_factory() as session:
            live_rows = (
                await session.execute(
                    select(
                        ConversationCompanionRecord.conversation_key,
                        ConversationCompanionRecord.id,
                    ).where(
                        ConversationCompanionRecord.status.in_(
                            (
                                ConversationCompanionStatus.PENDING.value,
                                ConversationCompanionStatus.ACTIVE.value,
                            )
                        )
                    )
                )
            ).all()
        live: dict[str, set[str]] = {}
        for conversation_key, companion_id in live_rows:
            live.setdefault(conversation_key, set()).add(companion_id)
        cutoff = time.time() - grace_seconds
        if not self.conversations_root.exists():
            return
        for conversation_root in self.conversations_root.iterdir():
            if not conversation_root.is_dir() or conversation_root.is_symlink():
                continue
            live_ids = live.get(conversation_root.name, set())
            for components in (
                ("inputs", "objects"),
                ("workspace", ".remoteagent", "companions"),
            ):
                try:
                    container_fd = self._open_directory_chain(conversation_root, components)
                except OSError:
                    logger.warning(
                        "skipping unsafe companion orphan container: %s/%s",
                        conversation_root,
                        "/".join(components),
                    )
                    continue
                try:
                    with os.scandir(container_fd) as entries:
                        for child in entries:
                            exact_live = child.name in live_ids
                            managed_name = child.name.startswith(
                                "cc_"
                            ) or child.name.startswith(".cc_")
                            if not managed_name or exact_live:
                                continue
                            try:
                                info = child.stat(follow_symlinks=False)
                            except OSError:
                                continue
                            if info.st_mtime > cutoff:
                                continue
                            self._remove_orphan_at(container_fd, child.name, info.st_mode)
                finally:
                    os.close(container_fd)

    def _open_directory_chain(self, root: Path, components: Sequence[str]) -> int:
        """Open a managed directory without following any replaceable ancestor."""

        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(root, flags)
        try:
            for component in components:
                child_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child_descriptor
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _remove_orphan_at(self, parent_fd: int, name: str, mode: int) -> None:
        """Best-effort fd-relative removal that cannot follow a swapped container."""

        try:
            if stat.S_ISDIR(mode):
                if not shutil.rmtree.avoids_symlink_attacks:
                    logger.warning("platform cannot safely remove companion directory orphan")
                    return
                shutil.rmtree(name, dir_fd=parent_fd)
            else:
                os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            return
        except OSError:
            # An agent may replace the entry between stat and removal. Never
            # retry through a pathname because that would reintroduce a race.
            logger.warning("companion orphan changed during reconciliation: %s", name)

    async def _expire_stage_if_needed(self, stage_id: str) -> None:
        storage: Path | None = None
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(CompanionStageRecord)
                .where(CompanionStageRecord.id == stage_id)
                .with_for_update()
            )
            if (
                record is not None
                and record.status
                not in (
                    CompanionStageStatus.CLAIMED.value,
                    CompanionStageStatus.EXPIRED.value,
                )
                and _as_utc(record.expires_at) <= _utcnow()
            ):
                record.status = CompanionStageStatus.EXPIRED.value
                record.error = record.error or "companion stage expired before it was claimed"
                if record.storage_path:
                    with contextlib.suppress(CompanionError):
                        storage = self._stored_path(
                            record.storage_path, within=self.staging_root
                        )
                    record.storage_path = None
        if storage is not None:
            self._remove_stage_path(storage)

    async def _git_worker(self, worker_index: int) -> None:
        while True:
            stage_id = await self._git_queue.get()
            try:
                await self._import_git_stage(stage_id)
            except asyncio.CancelledError:
                await self._reset_interrupted_import(stage_id)
                raise
            except Exception:
                logger.exception("Git companion worker %s failed", worker_index)
            finally:
                async with self._queue_lock:
                    self._queued_stage_ids.discard(stage_id)
                self._git_queue.task_done()

    async def _enqueue_git(self, stage_id: str) -> None:
        async with self._queue_lock:
            if stage_id in self._queued_stage_ids:
                return
            self._queued_stage_ids.add(stage_id)
            await self._git_queue.put(stage_id)

    async def _reset_interrupted_import(self, stage_id: str) -> None:
        remove_storage = False
        with contextlib.suppress(Exception):
            async with self.session_factory() as session, session.begin():
                record = await session.scalar(
                    select(CompanionStageRecord)
                    .where(CompanionStageRecord.id == stage_id)
                    .with_for_update()
                )
                if (
                    record is not None
                    and record.status == CompanionStageStatus.IMPORTING.value
                ):
                    record.status = CompanionStageStatus.QUEUED.value
                    record.error = None
                    remove_storage = True
                elif record is None or record.status == CompanionStageStatus.QUEUED.value:
                    remove_storage = True
        if remove_storage:
            self._remove_import_temporaries(stage_id)

    async def _import_git_stage(self, stage_id: str) -> None:
        started = time.monotonic()
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(CompanionStageRecord)
                .where(CompanionStageRecord.id == stage_id)
                .with_for_update()
            )
            if record is None or record.status != CompanionStageStatus.QUEUED.value:
                return
            if _as_utc(record.expires_at) <= _utcnow():
                record.status = CompanionStageStatus.EXPIRED.value
                record.error = "companion stage expired before import"
                return
            record.status = CompanionStageStatus.IMPORTING.value
            record.error = None
            metadata = dict(record.source_metadata or {})
        self._observe(CompanionStageKind.GIT.value, CompanionStageStatus.IMPORTING.value)

        temporary_root: Path | None = None
        final_root = self.staging_root / stage_id
        finalized = False
        persisted = False
        try:
            temporary_root = Path(
                tempfile.mkdtemp(prefix=f".{stage_id}-", dir=self.staging_root)
            )
            mirror = temporary_root / "mirror.git"
            url = str(metadata["url"])
            host = str(metadata["host"])
            port = int(metadata["port"])
            addresses = tuple(str(item) for item in metadata["pinned_addresses"])
            ref = metadata.get("ref")
            env = self._git_environment()
            clone = await self.subprocess_runner(
                self._remote_git_command(host, port, addresses)
                + ["clone", "--mirror", "--no-local", "--", url, str(mirror)],
                cwd=None,
                env=env,
                timeout_seconds=self._remaining_git_timeout(started),
                size_root=mirror,
                size_limit=self.max_git_mirror_bytes,
            )
            self._require_git_success(clone, "Git repository import failed", url=url)
            object_format_result = await self.subprocess_runner(
                self._local_git_command()
                + ["-C", str(mirror), "rev-parse", "--show-object-format"],
                cwd=None,
                env=env,
                timeout_seconds=self._remaining_git_timeout(started),
            )
            self._require_git_success(
                object_format_result,
                "could not determine Git repository object format",
                url=url,
            )
            object_format = object_format_result.stdout.decode("ascii", "strict").strip()
            if object_format not in {"sha1", "sha256"}:
                raise CompanionValidationError("unsupported Git repository object format")
            await asyncio.to_thread(self._sanitize_bare_mirror, mirror, object_format)
            mirror_bytes, _ = await asyncio.to_thread(
                _directory_usage, mirror, stop_after=self.max_git_mirror_bytes
            )
            if mirror_bytes > self.max_git_mirror_bytes:
                raise CompanionTooLargeError("Git mirror exceeds the configured size limit")

            revision = str(ref) if ref else "HEAD"
            resolved = await self.subprocess_runner(
                self._local_git_command()
                + [
                    "-C",
                    str(mirror),
                    "rev-parse",
                    "--verify",
                    "--end-of-options",
                    f"{revision}^{{commit}}",
                ],
                cwd=None,
                env=env,
                timeout_seconds=self._remaining_git_timeout(started),
            )
            self._require_git_success(resolved, "requested Git ref was not found", url=url)
            commit = resolved.stdout.decode("ascii", "strict").strip()
            if len(commit) not in (40, 64) or any(
                character not in "0123456789abcdefABCDEF" for character in commit
            ):
                raise CompanionValidationError("Git returned an invalid resolved commit")
            commit = commit.lower()

            listing = await self.subprocess_runner(
                self._local_git_command()
                + ["-C", str(mirror), "ls-tree", "-rlz", "--full-tree", commit],
                cwd=None,
                env=env,
                timeout_seconds=self._remaining_git_timeout(started),
            )
            self._require_git_success(listing, "could not inspect selected Git tree", url=url)
            if listing.stdout_truncated:
                raise CompanionTooLargeError("selected Git tree listing is too large")
            checkout_bytes, file_count = self._validate_git_tree(listing.stdout)
            if checkout_bytes > self.max_git_checkout_bytes:
                raise CompanionTooLargeError("selected Git checkout exceeds the size limit")
            selected_tree_digest = hashlib.sha256(listing.stdout).hexdigest()
            mirror_digest = await asyncio.to_thread(_stored_tree_digest, mirror)
            self._remaining_git_timeout(started)

            async with self._admission_lock:
                await self._ensure_staging_capacity(mirror_bytes)
                if final_root.exists() or final_root.is_symlink():
                    self._remove_stage_path(final_root)
                await asyncio.to_thread(self._fsync_tree, mirror)
                self._remaining_git_timeout(started)
                os.replace(temporary_root, final_root)
                finalized = True
                temporary_root = None
                self._fsync_directory(self.staging_root)
                storage_relative = self._relative_storage(final_root / "mirror.git")
                async with self.session_factory() as session, session.begin():
                    record = await session.scalar(
                        select(CompanionStageRecord)
                        .where(CompanionStageRecord.id == stage_id)
                        .with_for_update()
                    )
                    if (
                        record is None
                        or record.status != CompanionStageStatus.IMPORTING.value
                    ):
                        self._remove_stage_path(final_root)
                        return
                    if _as_utc(record.expires_at) <= _utcnow():
                        record.status = CompanionStageStatus.EXPIRED.value
                        record.error = "companion stage expired during import"
                        self._remove_stage_path(final_root)
                        return
                    record.status = CompanionStageStatus.READY.value
                    record.storage_path = storage_relative
                    record.size_bytes = mirror_bytes
                    record.file_count = file_count
                    record.sha256 = mirror_digest
                    record.resolved_git_commit = commit
                    record.error = None
                    record.source_metadata = {
                        **dict(record.source_metadata or {}),
                        "selected_tree_sha256": selected_tree_digest,
                    }
                persisted = True
            self._observe(
                CompanionStageKind.GIT.value,
                CompanionStageStatus.READY.value,
                size_bytes=mirror_bytes,
                duration_seconds=time.monotonic() - started,
            )
        except asyncio.CancelledError:
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
            if finalized and not persisted:
                self._remove_stage_path(final_root)
            raise
        except Exception as exc:  # noqa: BLE001 - import failures become durable state.
            if temporary_root is not None:
                shutil.rmtree(temporary_root, ignore_errors=True)
            if finalized and not persisted:
                self._remove_stage_path(final_root)
            await self._fail_git_stage(stage_id, exc)

    async def _fail_git_stage(self, stage_id: str, exc: BaseException) -> None:
        error = self._sanitize_git_error(exc)
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(CompanionStageRecord)
                .where(CompanionStageRecord.id == stage_id)
                .with_for_update()
            )
            if record is None or record.status != CompanionStageStatus.IMPORTING.value:
                return
            record.status = CompanionStageStatus.FAILED.value
            record.error = error
            record.storage_path = None
        self._observe(CompanionStageKind.GIT.value, CompanionStageStatus.FAILED.value)

    def _remove_import_temporaries(self, stage_id: str) -> None:
        for path in self.staging_root.glob(f".{stage_id}-*"):
            self._remove_stage_path(path)
        # A crash can occur after the import directory is atomically renamed
        # but before the importing row is committed as ready. A queued retry
        # must not retain that unaccounted full mirror alongside the new clone.
        self._remove_stage_path(self.staging_root / stage_id)

    def _remove_stage_path(self, path: Path) -> None:
        try:
            resolved_parent = path.parent.resolve()
            resolved_parent.relative_to(self.staging_root)
        except ValueError:
            logger.error("refusing companion stage cleanup outside staging root")
            return
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.exists():
            shutil.rmtree(path, ignore_errors=True)

    async def _validate_git_request(
        self, request: GitImportRequest
    ) -> tuple[str, str, int, tuple[str, ...]]:
        if (
            "\\" in request.url
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in request.url
            )
        ):
            raise CompanionValidationError("Git URL contains unsafe characters")
        try:
            parsed = urlsplit(request.url)
            port = parsed.port or 443
        except ValueError as exc:
            raise CompanionValidationError("invalid Git URL") from exc
        if parsed.scheme.lower() != "https":
            raise CompanionValidationError("Git URL must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise CompanionValidationError("Git URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise CompanionValidationError("Git URL must not contain a query or fragment")
        if not parsed.hostname or not parsed.path or parsed.path == "/":
            raise CompanionValidationError("Git URL must identify a repository")
        try:
            host = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise CompanionValidationError("Git URL hostname is invalid") from exc
        if not (1 <= port <= 65535):
            raise CompanionValidationError("Git URL port is invalid")
        self._validate_git_ref(request.ref)
        addresses = tuple(sorted(set(await self.resolver(host, port))))
        if not addresses:
            raise CompanionValidationError("Git hostname did not resolve")
        normalized_addresses: list[str] = []
        for value in addresses:
            try:
                address = ipaddress.ip_address(value.split("%", 1)[0])
            except ValueError as exc:
                raise CompanionValidationError("Git hostname resolution was invalid") from exc
            if (
                not address.is_global
                or address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_multicast
                or address.is_reserved
                or address.is_unspecified
                or getattr(address, "is_site_local", False)
            ):
                raise CompanionValidationError(
                    "Git hostname must resolve only to globally routable addresses"
                )
            normalized_addresses.append(address.compressed)
        display_host = f"[{host}]" if ":" in host else host
        netloc = display_host if port == 443 else f"{display_host}:{port}"
        sanitized = urlunsplit(("https", netloc, parsed.path, "", ""))
        return sanitized, host, port, tuple(sorted(set(normalized_addresses)))

    def _validate_git_ref(self, ref: str | None) -> None:
        if ref is None:
            return
        if (
            not ref
            or len(ref) > 1024
            or ref in {"@", ".", ".."}
            or ref.startswith(("-", "/", "."))
            or ref.endswith((".", "/"))
            or ".." in ref
            or "@{" in ref
            or "//" in ref
            or any(character in ref for character in " ~^:?*[\\")
            or any(ord(character) < 32 or ord(character) == 127 for character in ref)
            or any(
                component.startswith(".") or component.endswith(".lock")
                for component in ref.split("/")
            )
        ):
            raise CompanionValidationError("invalid Git ref")

    def _validate_git_tree(self, listing: bytes) -> tuple[int, int]:
        total = 0
        count = 0
        seen: dict[tuple[str, ...], bool] = {}
        explicit: set[tuple[str, ...]] = set()
        counts = [0, 0]
        for entry in listing.split(b"\0"):
            if not entry:
                continue
            try:
                header, raw_path = entry.split(b"\t", 1)
                mode, object_type, _object_id, raw_size = header.split(b" ", 3)
                path = raw_path.decode("utf-8", "strict")
            except (ValueError, UnicodeDecodeError) as exc:
                raise CompanionValidationError("Git tree contains an invalid entry") from exc
            if mode == b"160000" and object_type == b"commit":
                size = 0
            elif mode in (b"100644", b"100755") and object_type == b"blob":
                try:
                    size = int(raw_size)
                except ValueError as exc:
                    raise CompanionValidationError(
                        "Git tree contains an invalid blob size"
                    ) from exc
            else:
                raise CompanionValidationError(
                    "Git tree symbolic links and special entries are not supported"
                )
            relative, key = self._validated_archive_member(path)
            del relative
            if any(part == ".git" for part in key):
                raise CompanionValidationError("Git tree contains an unsafe path")
            try:
                self._reserve_archive_path(
                    key,
                    is_directory=mode == b"160000",
                    seen=seen,
                    explicit=explicit,
                    counts=counts,
                )
            except CompanionValidationError as exc:
                raise CompanionValidationError(
                    "Git tree contains an unsafe or duplicate path"
                ) from exc
            except CompanionTooLargeError as exc:
                raise CompanionTooLargeError(
                    "selected Git tree exceeds the file-count limit"
                ) from exc
            if size < 0:
                raise CompanionValidationError("Git tree contains an invalid blob size")
            total += size
            count += 1
            if count > self.max_files:
                raise CompanionTooLargeError("selected Git tree exceeds the file-count limit")
            if total > self.max_git_checkout_bytes:
                raise CompanionTooLargeError("selected Git checkout exceeds the size limit")
        return total, count

    def _sanitize_bare_mirror(self, mirror: Path, object_format: str) -> None:
        """Replace executable/configurable Git state with a fixed local policy."""

        if not mirror.is_dir() or mirror.is_symlink():
            raise CompanionValidationError("Git did not create a safe bare mirror")
        for relative in ("objects/info/alternates", "objects/info/http-alternates"):
            if (mirror / relative).exists() or (mirror / relative).is_symlink():
                raise CompanionValidationError("Git mirror contains an external object store")
        hooks = mirror / "hooks"
        if hooks.is_symlink() or hooks.is_file():
            hooks.unlink(missing_ok=True)
        elif hooks.exists():
            shutil.rmtree(hooks)
        hooks.mkdir(mode=0o700)
        config = mirror / "config"
        format_version = b"1" if object_format == "sha256" else b"0"
        extension = (
            b"[extensions]\n\tobjectFormat = sha256\n"
            if object_format == "sha256"
            else b""
        )
        payload = (
            b"[core]\n"
            b"\trepositoryformatversion = "
            + format_version
            + b"\n"
            b"\tfilemode = true\n"
            b"\tbare = true\n"
            b"\tlogallrefupdates = false\n"
            b"\thooksPath = /dev/null\n"
            + extension
        )
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=mirror, prefix=".config-", delete=False
            ) as output:
                temporary = output.name
                os.chmod(temporary, 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, config)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        self._validate_git_storage_tree(mirror)

    def _validate_git_storage_tree(self, mirror: Path) -> None:
        for directory, directory_names, filenames in os.walk(mirror, followlinks=False):
            base = Path(directory)
            for name in directory_names:
                path = base / name
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode):
                    raise CompanionValidationError("Git mirror contains an unsafe directory")
            for filename in filenames:
                path = base / filename
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise CompanionValidationError("Git mirror contains a special file")

    def _remaining_git_timeout(self, started: float) -> float:
        remaining = self.git_timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError()
        return remaining

    def _git_environment(self) -> dict[str, str]:
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.git_home),
            "XDG_CONFIG_HOME": str(self.git_home / ".config"),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TEMPLATE_DIR": str(self.git_home / "templates"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false",
            "GCM_INTERACTIVE": "never",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_PROTOCOL_FROM_USER": "0",
        }

    def _git_config(self, *, allow_file: bool) -> list[str]:
        return [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.interactive=never",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "http.followRedirects=false",
            "-c",
            "http.proxy=",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.https.allow=always",
            "-c",
            f"protocol.file.allow={'always' if allow_file else 'never'}",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.required=false",
        ]

    def _remote_git_command(
        self, host: str, port: int, addresses: Sequence[str]
    ) -> list[str]:
        rendered = ",".join(f"[{value}]" if ":" in value else value for value in addresses)
        return self._git_config(allow_file=False) + [
            "-c",
            f"http.curloptResolve={host}:{port}:{rendered}",
        ]

    def _local_git_command(self) -> list[str]:
        return self._git_config(allow_file=True)

    def _require_git_success(
        self, result: GitCommandResult, message: str, *, url: str | None = None
    ) -> None:
        if result.returncode == 0:
            return
        detail = result.stderr.strip()
        if url:
            detail = detail.replace(url, "<repository>")
        detail = "".join(character for character in detail if character in "\n\t" or ord(character) >= 32)
        if detail:
            raise CompanionValidationError(f"{message}: {detail[-500:]}")
        raise CompanionValidationError(message)

    def _sanitize_git_error(self, exc: BaseException) -> str:
        if isinstance(exc, asyncio.TimeoutError):
            return "Git import exceeded the configured timeout"
        text = _bounded_error(exc)
        return "".join(character for character in text if character in "\n\t" or ord(character) >= 32)[
            :_ERROR_LIMIT
        ]

    async def _materialize_git(
        self,
        source: Path,
        destination: Path,
        record: ConversationCompanionRecord,
    ) -> None:
        if not record.resolved_git_commit:
            raise CompanionPreparationError("Git companion has no resolved commit")
        env = self._git_environment()
        destination.mkdir(mode=0o700)
        git_directory = destination / ".git"
        await asyncio.to_thread(shutil.copytree, source, git_directory, symlinks=True)
        await asyncio.to_thread(self._validate_git_storage_tree, git_directory)
        copied_digest = await asyncio.to_thread(_stored_tree_digest, git_directory)
        if copied_digest != record.sha256:
            raise CompanionPreparationError("Git companion changed while it was copied")
        object_format = "sha256" if len(record.resolved_git_commit) == 64 else "sha1"
        await asyncio.to_thread(
            self._write_git_worktree_config, git_directory, object_format
        )
        result = await self.subprocess_runner(
            self._local_git_command()
            + [
                "--git-dir",
                str(git_directory),
                "--work-tree",
                str(destination),
                "checkout",
                "--force",
                "--detach",
                record.resolved_git_commit,
                "--",
            ],
            cwd=None,
            env=env,
            timeout_seconds=self.git_timeout_seconds,
            size_root=destination,
            size_limit=self.max_git_mirror_bytes + self.max_git_checkout_bytes,
        )
        self._require_git_success(result, "could not check out Git companion")
        checkout_bytes, checkout_files = await asyncio.to_thread(
            self._checkout_usage, destination
        )
        if checkout_files > self.max_files or checkout_bytes > self.max_git_checkout_bytes:
            raise CompanionTooLargeError("materialized Git checkout exceeds configured limits")

    def _write_git_worktree_config(
        self, git_directory: Path, object_format: str
    ) -> None:
        config = git_directory / "config"
        format_version = b"1" if object_format == "sha256" else b"0"
        extension = (
            b"[extensions]\n\tobjectFormat = sha256\n"
            if object_format == "sha256"
            else b""
        )
        payload = (
            b"[core]\n"
            b"\trepositoryformatversion = "
            + format_version
            + b"\n"
            b"\tfilemode = true\n"
            b"\tbare = false\n"
            b"\tworktree = ..\n"
            b"\tlogallrefupdates = false\n"
            b"\thooksPath = /dev/null\n"
            b"[filter \"lfs\"]\n"
            b"\tsmudge =\n"
            b"\trequired = false\n"
            + extension
        )
        temporary: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=git_directory, prefix=".config-", delete=False
            ) as output:
                temporary = output.name
                os.chmod(temporary, 0o600)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, config)
            temporary = None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def _checkout_usage(self, root: Path) -> tuple[int, int]:
        total = 0
        count = 0
        for directory, directory_names, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            if base == root and ".git" in directory_names:
                directory_names.remove(".git")
            for name in directory_names:
                if (base / name).is_symlink():
                    raise CompanionPreparationError("Git checkout contains a symbolic link")
            for filename in filenames:
                path = base / filename
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    raise CompanionPreparationError("Git checkout contains a special file")
                total += info.st_size
                count += 1
        return total, count

    async def _cleanup_loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self.cleanup_interval_seconds)
            try:
                await self.cleanup()
            except Exception:
                logger.exception("companion cleanup failed")

    async def _resolve_public_addresses(self, host: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        try:
            results = await loop.getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise CompanionValidationError("Git hostname could not be resolved") from exc
        return tuple(sorted({str(item[4][0]) for item in results}))

    async def _run_subprocess(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout_seconds: float,
        size_root: Path | None = None,
        size_limit: int | None = None,
    ) -> GitCommandResult:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        async def read_limited(
            stream: asyncio.StreamReader | None, limit: int
        ) -> tuple[bytes, bool]:
            if stream is None:
                return b"", False
            result = bytearray()
            truncated = False
            while chunk := await stream.read(64 * 1024):
                remaining = limit - len(result)
                if remaining > 0:
                    result.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
            return bytes(result), truncated

        stdout_task = asyncio.create_task(
            read_limited(process.stdout, max(128 * 1024 * 1024, self.max_files * 8192))
        )
        stderr_task = asyncio.create_task(read_limited(process.stderr, _GIT_OUTPUT_LIMIT))
        wait_task = asyncio.create_task(process.wait())
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        failure: BaseException | None = None
        monitor_staging = False
        if size_root is not None:
            try:
                size_root.absolute().relative_to(self.staging_root)
                monitor_staging = True
            except ValueError:
                pass

        def signal_group(selected_signal: signal.Signals) -> None:
            try:
                os.killpg(process.pid, selected_signal)
            except ProcessLookupError:
                pass

        try:
            while not wait_task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    failure = TimeoutError()
                    break
                done, _ = await asyncio.wait((wait_task,), timeout=min(0.2, remaining))
                if done:
                    break
                if size_root is not None and size_limit is not None:
                    used, _ = await asyncio.to_thread(
                        _directory_usage, size_root, stop_after=size_limit
                    )
                    if used > size_limit:
                        failure = CompanionTooLargeError(
                            "Git operation exceeded the configured storage limit"
                        )
                        break
                if monitor_staging:
                    staging_used, _ = await asyncio.to_thread(
                        _directory_usage,
                        self.staging_root,
                        stop_after=self.max_staging_bytes,
                    )
                    if staging_used > self.max_staging_bytes:
                        failure = CompanionCapacityError(
                            "deployment companion staging capacity is exhausted"
                        )
                        break
            if failure is not None:
                signal_group(signal.SIGTERM)
                # The Git leader can exit while a descendant ignores SIGTERM.
                # Always end the grace period with a group-wide SIGKILL.
                await asyncio.sleep(2)
                signal_group(signal.SIGKILL)
                if not wait_task.done():
                    await asyncio.wait_for(asyncio.shield(wait_task), timeout=2)
            else:
                await wait_task
                # A successful leader must not leave a helper holding inherited
                # resources or pipe descriptors.
                signal_group(signal.SIGKILL)
        except BaseException:
            signal_group(signal.SIGKILL)
            if not wait_task.done():
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(asyncio.shield(wait_task), timeout=2)
            raise
        finally:
            try:
                stdout, stdout_truncated = await asyncio.wait_for(stdout_task, timeout=2)
                stderr_bytes, stderr_truncated = await asyncio.wait_for(
                    stderr_task, timeout=2
                )
            except TimeoutError:
                signal_group(signal.SIGKILL)
                for task in (stdout_task, stderr_task):
                    task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                stdout, stdout_truncated = b"", True
                stderr_bytes, stderr_truncated = b"", True
        if failure is not None:
            raise failure
        return GitCommandResult(
            returncode=int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr_bytes.decode("utf-8", "replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )

    def _observe(
        self,
        kind: str,
        status: str,
        *,
        size_bytes: int | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        if self.telemetry is None:
            return
        try:
            self.telemetry.observe_companion_stage(
                kind,
                status,
                size_bytes=size_bytes,
                duration_seconds=duration_seconds,
            )
        except Exception:
            logger.warning("companion telemetry observation failed", exc_info=True)

    def _fsync_directory(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _fsync_tree(self, root: Path) -> None:
        for directory, directory_names, filenames in os.walk(root, followlinks=False):
            base = Path(directory)
            directory_names[:] = [
                name for name in directory_names if not (base / name).is_symlink()
            ]
            for filename in filenames:
                path = base / filename
                if path.is_symlink():
                    continue
                descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            self._fsync_directory(base)


__all__ = [
    "COMPANION_PREAMBLE_VERSION",
    "CompanionCapacityError",
    "CompanionConflictError",
    "CompanionError",
    "CompanionNotFoundError",
    "CompanionPolicyError",
    "CompanionPreparationError",
    "CompanionService",
    "CompanionTooLargeError",
    "CompanionValidationError",
    "GitCommandResult",
]
