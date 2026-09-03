from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import LeaseRecord


class LeaseLostError(RuntimeError):
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
    ) -> None:
        self.session_factory = session_factory
        self.ttl = timedelta(seconds=ttl_seconds)
        self.retry_seconds = retry_seconds
        self.instance_id = instance_id or uuid.uuid4().hex

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
            interval = max(1.0, min(self.ttl.total_seconds() / 3, 60.0))
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
        try:
            yield handle
            handle.check()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await self.release(handle)
