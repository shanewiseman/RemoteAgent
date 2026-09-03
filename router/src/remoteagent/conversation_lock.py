from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ConversationRecord


def _advisory_lock_id(conversation_key: str) -> int:
    """Map a validated conversation key into PostgreSQL's signed bigint lock space."""

    digest = hashlib.sha256(conversation_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


async def lock_conversation(
    session: AsyncSession,
    conversation_key: str,
) -> ConversationRecord | None:
    """Serialize a conversation mutation for the current transaction.

    The advisory lock covers the not-yet-created row case as well as existing
    rows. The row lock remains the authoritative serialization boundary and is
    used on every supported database.
    """

    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        await session.execute(
            select(func.pg_advisory_xact_lock(_advisory_lock_id(conversation_key)))
        )
    return await session.scalar(
        select(ConversationRecord)
        .where(ConversationRecord.key == conversation_key)
        .with_for_update()
    )
