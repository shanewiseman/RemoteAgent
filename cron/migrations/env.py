from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from remoteagent_cron.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("REMOTEAGENT_CRON_DATABASE_URL") or os.environ.get("DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url)
target_metadata = Base.metadata


def configure(connection=None) -> None:  # type: ignore[no-untyped-def]
    kwargs = {
        "target_metadata": target_metadata,
        "compare_type": True,
        "version_table": "cron_alembic_version",
    }
    if connection is None:
        context.configure(
            url=config.get_main_option("sqlalchemy.url"),
            literal_binds=True,
            dialect_opts={"paramstyle": "named"},
            **kwargs,
        )
    else:
        context.configure(connection=connection, **kwargs)


def run_migrations_offline() -> None:
    configure()
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    configure(connection)
    with context.begin_transaction():
        if connection.dialect.name == "postgresql":
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('remoteagent-cron-schema-migrations'))"
            )
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
