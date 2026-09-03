from __future__ import annotations

from typing import Any


def run_online_migrations(connection: Any, migration_context: Any, target_metadata: Any) -> None:
    """Run migrations under one committed transaction and PostgreSQL xact lock."""

    postgres = connection.dialect.name == "postgresql"
    migration_context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    # Take the lock inside Alembic's transaction. Executing a session-lock
    # statement first triggers SQLAlchemy autobegin, causing Alembic to reuse an
    # implicit transaction that connection close can roll back with all DDL and
    # the version row. PostgreSQL releases this xact lock on commit or rollback.
    with migration_context.begin_transaction():
        if postgres:
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('remoteagent-schema-migrations'))"
            )
        migration_context.run_migrations()
