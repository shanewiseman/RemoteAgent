from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import LeaseRecord

logger = logging.getLogger(__name__)


class LeaseLostError(RuntimeError):
    pass


class LeaseCleanupError(RuntimeError):
    pass


@dataclass(slots=True)
class LeaseHandle:
    name: str
    owner: str
    fencing_token: int
    lost: asyncio.Event

    def check(self) -> None:
        if self.lost.is_set():
            raise LeaseLostError(f"lease lost: {self.name}")


class LeaseManager:
    """Database-backed renewable lease with fencing tokens.

    The lease is authoritative even when Redis is unavailable. Each acquisition
    increments a token so a stale holder cannot release a successor's lease.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        ttl_seconds: int,
        retry_seconds: float,
        instance_id: str | None = None,
        renew_interval_seconds: float | None = None,
        cleanup_timeout_seconds: float = 60.0,
    ) -> None:
        self.session_factory = session_factory
        self.ttl = timedelta(seconds=ttl_seconds)
        self.retry_seconds = retry_seconds
        self.instance_id = instance_id or uuid.uuid4().hex
        self.renew_interval_seconds = renew_interval_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds

    async def try_acquire(self, name: str, owner_suffix: str) -> LeaseHandle | None:
        owner = f"{self.instance_id}:{owner_suffix}"[:128]
        now = datetime.now(UTC)
        expires_at = now + self.ttl
        try:
            async with self.session_factory() as session, session.begin():
                result = await session.execute(
                    update(LeaseRecord)
                    .where(
                        LeaseRecord.name == name,
                        or_(LeaseRecord.expires_at <= now, LeaseRecord.owner == owner),
                    )
                    .values(
                        owner=owner,
                        fencing_token=LeaseRecord.fencing_token + 1,
                        expires_at=expires_at,
                        updated_at=now,
                    )
                )
                if result.rowcount:
                    token = await session.scalar(
                        select(LeaseRecord.fencing_token).where(LeaseRecord.name == name)
                    )
                    return LeaseHandle(name, owner, int(token), asyncio.Event())
                existing = await session.get(LeaseRecord, name)
                if existing is not None:
                    return None
                record = LeaseRecord(
                    name=name,
                    owner=owner,
                    fencing_token=1,
                    expires_at=expires_at,
                )
                session.add(record)
                await session.flush()
                return LeaseHandle(name, owner, 1, asyncio.Event())
        except IntegrityError:
            # Another router won the insert race.
            return None

    async def renew(self, handle: LeaseHandle) -> bool:
        now = datetime.now(UTC)
        async with self.session_factory() as session, session.begin():
            result = await session.execute(
                update(LeaseRecord)
                .where(
                    LeaseRecord.name == handle.name,
                    LeaseRecord.owner == handle.owner,
                    LeaseRecord.fencing_token == handle.fencing_token,
                    LeaseRecord.expires_at > now,
                )
                .values(expires_at=now + self.ttl, updated_at=now)
            )
            return bool(result.rowcount)

    async def release(self, handle: LeaseHandle) -> None:
        async with self.session_factory() as session, session.begin():
            await session.execute(
                delete(LeaseRecord).where(
                    LeaseRecord.name == handle.name,
                    LeaseRecord.owner == handle.owner,
                    LeaseRecord.fencing_token == handle.fencing_token,
                )
            )

    @asynccontextmanager
    async def hold(
        self,
        name: str,
        owner_suffix: str,
        *,
        cancelled=None,
    ) -> AsyncIterator[LeaseHandle]:
        handle: LeaseHandle | None = None
        while handle is None:
            if cancelled is not None and await cancelled():
                raise asyncio.CancelledError
            handle = await self.try_acquire(name, owner_suffix)
            if handle is None:
                await asyncio.sleep(self.retry_seconds)

        async def keepalive() -> None:
            interval = self.renew_interval_seconds or max(
                1.0, min(self.ttl.total_seconds() / 3, 60.0)
            )
            try:
                while True:
                    await asyncio.sleep(interval)
                    if not await self.renew(handle):
                        handle.lost.set()
                        return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - any renewal failure invalidates ownership.
                handle.lost.set()

        task = asyncio.create_task(keepalive(), name=f"lease:{name}")
        body_failed = False
        try:
            yield handle
            handle.check()
        except BaseException:
            body_failed = True
            raise
        finally:
            cleanup_deadline = (
                asyncio.get_running_loop().time() + self.cleanup_timeout_seconds
            )
            task.cancel()
            keepalive_cleanup = asyncio.create_task(
                self._gather_task(task), name=f"lease-keepalive-cleanup:{name}"
            )
            release_error: Exception | None = None
            try:
                # Start the fenced release immediately. A keepalive coroutine
                # that is slow or cancellation-resistant must not consume the
                # whole cleanup budget before release is even attempted.
                await self._release_with_timeout(handle, deadline=cleanup_deadline)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                release_error = exc
            finally:
                keepalive_stopped = await self._await_cleanup_task(
                    keepalive_cleanup, deadline=cleanup_deadline
                )

            keepalive_error = (
                None
                if keepalive_stopped
                else LeaseCleanupError(
                    f"lease keepalive cleanup exceeded {self.cleanup_timeout_seconds:g} seconds"
                )
            )
            if body_failed:
                if release_error is not None:
                    logger.error(
                        "lease release failed while preserving the lifecycle error: %s",
                        handle.name,
                        exc_info=(
                            type(release_error),
                            release_error,
                            release_error.__traceback__,
                        ),
                    )
                if keepalive_error is not None:
                    logger.error(
                        "lease keepalive cleanup failed while preserving the lifecycle error: %s",
                        handle.name,
                    )
            elif release_error is not None:
                raise release_error
            elif keepalive_error is not None:
                raise keepalive_error

    async def _release_with_timeout(
        self, handle: LeaseHandle, *, deadline: float | None = None
    ) -> None:
        release_task = asyncio.create_task(
            self.release(handle), name=f"lease-release:{handle.name}"
        )
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + self.cleanup_timeout_seconds
        try:
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            if not remaining:
                raise TimeoutError
            await asyncio.wait_for(
                asyncio.shield(release_task), timeout=remaining
            )
        except TimeoutError as exc:
            release_task.cancel()
            release_task.add_done_callback(self._consume_background_task)
            raise LeaseCleanupError(
                f"lease release exceeded {self.cleanup_timeout_seconds:g} seconds"
            ) from exc
        except asyncio.CancelledError:
            # Deadline expiry, caller cancellation, and router shutdown can
            # arrive after the body has completed but while the fenced release
            # is in flight. Preserve that lifecycle signal, but still give the
            # release the remainder of its independent cleanup budget.
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            try:
                if remaining:
                    await asyncio.wait_for(asyncio.shield(release_task), timeout=remaining)
                elif not release_task.done():
                    raise TimeoutError
                else:
                    release_task.result()
            except TimeoutError:
                release_task.cancel()
                release_task.add_done_callback(self._consume_background_task)
                logger.error(
                    "lease release exceeded %.1f seconds during lifecycle cancellation: %s",
                    self.cleanup_timeout_seconds,
                    handle.name,
                )
            except Exception:  # noqa: BLE001 - preserve the lifecycle cancellation.
                logger.exception(
                    "lease release failed during lifecycle cancellation: %s", handle.name
                )
            raise

    async def _await_cleanup_task(self, task: asyncio.Task[None], *, deadline: float) -> bool:
        if task.done():
            self._consume_background_task(task)
            return True
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        if remaining:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                task.cancel()
                task.add_done_callback(self._consume_background_task)
                raise
        if task.done():
            self._consume_background_task(task)
            return True
        task.cancel()
        task.add_done_callback(self._consume_background_task)
        return False

    @staticmethod
    async def _gather_task(task: asyncio.Task[None]) -> None:
        await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _consume_background_task(task: asyncio.Task[None]) -> None:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.result()
