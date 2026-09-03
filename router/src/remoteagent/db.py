from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .models import Base


def create_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
    if database_url.startswith("sqlite+"):
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(database_url, **kwargs)
    if database_url.startswith("sqlite+"):

        @event.listens_for(engine.sync_engine, "connect")
        def set_sqlite_pragmas(connection, _record) -> None:  # type: ignore[no-untyped-def]
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def initialize_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def initialize_database(database: object, *, echo: bool = False) -> None:
    """Compatibility entry point for idempotent development/test bootstrap.

    Production deployments use Alembic migrations. This helper deliberately
    performs only metadata ``create_all``. Engines created by this helper are
    disposed before it returns.
    """

    owns_engine = not isinstance(database, AsyncEngine)
    if isinstance(database, AsyncEngine):
        engine = database
    elif isinstance(database, str):
        engine = create_engine(database, echo=echo)
    else:
        database_url = getattr(database, "database_url", None)
        if not isinstance(database_url, str):
            raise TypeError("initialize_database expects Settings, a URL, or AsyncEngine")
        engine = create_engine(database_url, echo=echo)
    try:
        await initialize_schema(engine)
    finally:
        if owns_engine:
            await engine.dispose()


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
