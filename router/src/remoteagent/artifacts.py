from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import ArtifactRecord
from .schemas import ArtifactView, validate_artifact_relative_path


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

    async def delete_storage(self, job_id: str) -> None:
        target = (self.store_root / job_id).resolve()
        try:
            target.relative_to(self.store_root)
        except ValueError as exc:
            raise ArtifactPolicyError("invalid artifact cleanup path") from exc
        if target.exists():
            shutil.rmtree(target)
