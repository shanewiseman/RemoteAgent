from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import ArtifactRecord, JobRecord
from .schemas import ArtifactView, JobStatus, validate_artifact_relative_path

logger = logging.getLogger(__name__)

_JOB_ID_RE = re.compile(r"^j_[0-9a-f]{32}$")
_DIRECTORY_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class ArtifactNotFoundError(LookupError):
    pass


class ArtifactPolicyError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactSnapshot:
    files: dict[str, tuple[int, int, int, int]]


def _view(record: ArtifactRecord) -> ArtifactView:
    return ArtifactView(
        id=record.id,
        job_id=record.job_id,
        conversation_key=record.conversation_key,
        relative_path=record.relative_path,
        media_type=record.media_type,
        size_bytes=record.size_bytes,
        sha256=record.sha256,
        resource_uri=f"artifact://{record.id}",
    )


class ArtifactService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store_root: Path,
        *,
        max_file_bytes: int,
        max_files_per_job: int,
    ) -> None:
        self.session_factory = session_factory
        self.store_root = store_root.resolve()
        self.max_file_bytes = max_file_bytes
        self.max_files_per_job = max_files_per_job
        self.store_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def snapshot(self, source_root: Path) -> ArtifactSnapshot:
        return ArtifactSnapshot(self._inventory(source_root))

    def _inventory(self, source_root: Path) -> dict[str, tuple[int, int, int, int]]:
        root = source_root.resolve()
        result: dict[str, tuple[int, int, int, int]] = {}
        if not root.exists():
            return result
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
                if not stat.S_ISREG(info.st_mode):
                    continue
                relative = path.relative_to(root).as_posix()
                validate_artifact_relative_path(relative)
                result[relative] = (
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_size,
                    info.st_ino,
                )
        return result

    async def ingest_changed(
        self,
        *,
        job_id: str,
        conversation_key: str,
        source_root: Path,
        before: ArtifactSnapshot,
    ) -> list[ArtifactView]:
        current = self._inventory(source_root)
        changed = sorted(
            relative
            for relative, signature in current.items()
            if before.files.get(relative) != signature
        )
        if len(changed) > self.max_files_per_job:
            raise ArtifactPolicyError("artifact file-count limit exceeded")
        records: list[ArtifactRecord] = []
        for relative in changed:
            expected_size = current[relative][2]
            if expected_size > self.max_file_bytes:
                raise ArtifactPolicyError(f"artifact exceeds size limit: {relative}")
            records.append(
                self._copy_one(
                    job_id=job_id,
                    conversation_key=conversation_key,
                    source_root=source_root,
                    relative=relative,
                )
            )
        if records:
            async with self.session_factory() as session, session.begin():
                session.add_all(records)
        return [_view(record) for record in records]

    def _copy_one(
        self,
        *,
        job_id: str,
        conversation_key: str,
        source_root: Path,
        relative: str,
    ) -> ArtifactRecord:
        relative = validate_artifact_relative_path(relative)
        source = source_root.resolve() / Path(PurePosixPath(relative))
        try:
            source.resolve().relative_to(source_root.resolve())
        except ValueError as exc:
            raise ArtifactPolicyError("artifact escaped source root") from exc
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise ArtifactPolicyError(f"cannot securely open artifact {relative}: {exc}") from exc
        destination = self.store_root / job_id / Path(PurePosixPath(relative))
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_name: str | None = None
        digest = hashlib.sha256()
        size = 0
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode):
                raise ArtifactPolicyError(f"artifact is not a regular file: {relative}")
            with os.fdopen(descriptor, "rb", closefd=True) as source_handle:
                descriptor = -1
                with tempfile.NamedTemporaryFile(
                    dir=destination.parent, prefix=".ingest-", delete=False
                ) as output:
                    temporary_name = output.name
                    while chunk := source_handle.read(1024 * 1024):
                        size += len(chunk)
                        if size > self.max_file_bytes:
                            raise ArtifactPolicyError(f"artifact exceeds size limit: {relative}")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary_name, destination)
            temporary_name = None
            os.chmod(destination, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
        media_type = mimetypes.guess_type(relative)[0] or "application/octet-stream"
        return ArtifactRecord(
            id=f"a_{uuid.uuid4().hex}",
            job_id=job_id,
            conversation_key=conversation_key,
            relative_path=relative,
            storage_path=str(destination),
            media_type=media_type,
            size_bytes=size,
            sha256=digest.hexdigest(),
        )

    async def list(
        self,
        *,
        job_id: str | None = None,
        conversation_key: str | None = None,
        limit: int = 1_000,
    ) -> list[ArtifactView]:
        async with self.session_factory() as session:
            statement = select(ArtifactRecord).order_by(ArtifactRecord.created_at).limit(limit)
            if job_id:
                statement = statement.where(ArtifactRecord.job_id == job_id)
            if conversation_key:
                statement = statement.where(ArtifactRecord.conversation_key == conversation_key)
            return [_view(item) for item in (await session.scalars(statement)).all()]

    async def get(self, artifact_id: str) -> ArtifactView:
        async with self.session_factory() as session:
            record = await session.get(ArtifactRecord, artifact_id)
            if record is None:
                raise ArtifactNotFoundError(artifact_id)
            return _view(record)

    async def path(self, artifact_id: str) -> tuple[Path, ArtifactView]:
        async with self.session_factory() as session:
            record = await session.get(ArtifactRecord, artifact_id)
            if record is None:
                raise ArtifactNotFoundError(artifact_id)
            path = Path(record.storage_path).resolve()
            try:
                path.relative_to(self.store_root)
            except ValueError as exc:
                raise ArtifactPolicyError("stored artifact escaped artifact store") from exc
            if not path.is_file() or path.is_symlink():
                raise ArtifactNotFoundError(artifact_id)
            return path, _view(record)

    def canonical_storage_path(
        self, *, job_id: str, relative_path: str, storage_path: str
    ) -> tuple[Path, str] | None:
        """Return a validated canonical record path without resolving filesystem links."""

        if not _JOB_ID_RE.fullmatch(job_id):
            return None
        try:
            relative = validate_artifact_relative_path(relative_path)
        except ValueError:
            return None
        expected = self.store_root / job_id / Path(PurePosixPath(relative))
        if Path(storage_path) != expected:
            return None
        return expected, relative

    async def delete_artifact_file(self, *, job_id: str, relative_path: str) -> None:
        """Unlink one artifact through no-follow directory descriptors."""

        if not _JOB_ID_RE.fullmatch(job_id):
            raise ArtifactPolicyError("invalid artifact cleanup job id")
        try:
            relative = validate_artifact_relative_path(relative_path)
        except ValueError as exc:
            raise ArtifactPolicyError("invalid artifact cleanup path") from exc
        components = (job_id, *PurePosixPath(relative).parts)
        descriptors: list[int] = []
        try:
            descriptor = os.open(self.store_root, _DIRECTORY_OPEN_FLAGS)
            descriptors.append(descriptor)
            for component in components[:-1]:
                descriptor = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=descriptor)
                descriptors.append(descriptor)
            try:
                os.unlink(components[-1], dir_fd=descriptor)
            except FileNotFoundError:
                pass
        except FileNotFoundError:
            pass
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    async def delete_storage(self, job_id: str) -> None:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ArtifactPolicyError("invalid artifact cleanup job id")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ArtifactPolicyError("safe artifact directory cleanup is unavailable")
        try:
            root_descriptor = os.open(self.store_root, _DIRECTORY_OPEN_FLAGS)
        except FileNotFoundError:
            return
        try:
            try:
                info = os.stat(job_id, dir_fd=root_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(job_id, dir_fd=root_descriptor)
            else:
                # A same-name symlink or special entry is itself confined to the
                # store. Unlink it, but never resolve or traverse its target.
                os.unlink(job_id, dir_fd=root_descriptor)
        finally:
            os.close(root_descriptor)

    async def reconcile_orphans(self, *, grace_seconds: int) -> int:
        """Remove stale, untracked artifact-store entries without following links."""

        cutoff = time.time() - grace_seconds
        terminal_statuses = {status.value for status in JobStatus if status.terminal}
        async with self.session_factory() as session:
            job_rows = (
                await session.execute(select(JobRecord.id, JobRecord.status))
            ).all()
            artifact_rows = (
                await session.execute(
                    select(
                        ArtifactRecord.job_id,
                        ArtifactRecord.relative_path,
                        ArtifactRecord.storage_path,
                    )
                )
            ).all()

        # Unexpected status values fail safe: their whole job directory remains
        # protected instead of being treated as terminal.
        protected_jobs = {
            job_id
            for job_id, status_value in job_rows
            if _JOB_ID_RE.fullmatch(job_id) and status_value not in terminal_statuses
        }
        durable_paths: dict[str, set[str]] = defaultdict(set)
        for job_id, relative_path, storage_path in artifact_rows:
            canonical = self.canonical_storage_path(
                job_id=job_id,
                relative_path=relative_path,
                storage_path=storage_path,
            )
            if canonical is not None:
                durable_paths[job_id].add(canonical[1])

        try:
            root_descriptor = os.open(self.store_root, _DIRECTORY_OPEN_FLAGS)
        except FileNotFoundError:
            return 0
        removed = 0
        try:
            with os.scandir(root_descriptor) as entries:
                for entry in entries:
                    job_id = entry.name
                    if not _JOB_ID_RE.fullmatch(job_id) or job_id in protected_jobs:
                        continue
                    try:
                        info = os.stat(job_id, dir_fd=root_descriptor, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            removed += self._reconcile_directory(
                                parent_descriptor=root_descriptor,
                                name=job_id,
                                relative_prefix=PurePosixPath(),
                                durable_paths=durable_paths.get(job_id, set()),
                                cutoff=cutoff,
                                display_path=job_id,
                            )
                        elif info.st_mtime <= cutoff:
                            os.unlink(job_id, dir_fd=root_descriptor)
                            removed += 1
                    except Exception:  # noqa: BLE001 - isolate each candidate path.
                        logger.warning(
                            "artifact orphan cleanup failed; path retained for retry: %s",
                            job_id,
                            exc_info=True,
                        )
        finally:
            os.close(root_descriptor)
        return removed

    def _reconcile_directory(
        self,
        *,
        parent_descriptor: int,
        name: str,
        relative_prefix: PurePosixPath,
        durable_paths: set[str],
        cutoff: float,
        display_path: str,
    ) -> int:
        removed = 0
        descriptor = os.open(name, _DIRECTORY_OPEN_FLAGS, dir_fd=parent_descriptor)
        opened_info = os.fstat(descriptor)
        directory_was_stale = opened_info.st_mtime <= cutoff
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    relative = relative_prefix / entry.name
                    relative_text = relative.as_posix()
                    nested_display = f"{display_path}/{entry.name}"
                    try:
                        info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            removed += self._reconcile_directory(
                                parent_descriptor=descriptor,
                                name=entry.name,
                                relative_prefix=relative,
                                durable_paths=durable_paths,
                                cutoff=cutoff,
                                display_path=nested_display,
                            )
                        elif relative_text not in durable_paths and info.st_mtime <= cutoff:
                            os.unlink(entry.name, dir_fd=descriptor)
                            removed += 1
                    except Exception:  # noqa: BLE001 - isolate each candidate path.
                        logger.warning(
                            "artifact orphan cleanup failed; path retained for retry: %s",
                            nested_display,
                            exc_info=True,
                        )
        finally:
            os.close(descriptor)

        # The directory may be removed only after every child has independently
        # been handled. ENOTEMPTY and races are retained and retried next pass.
        try:
            info = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (
                stat.S_ISDIR(info.st_mode)
                and (info.st_dev, info.st_ino) == (opened_info.st_dev, opened_info.st_ino)
                and (directory_was_stale or removed > 0)
            ):
                os.rmdir(name, dir_fd=parent_descriptor)
                removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            # A non-empty directory is expected whenever it still contains a
            # durable or young path, so avoid noisy logs for that retryable case.
            pass
        return removed
