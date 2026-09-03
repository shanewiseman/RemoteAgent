from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_migration_upgrade_and_downgrade_uses_independent_version_table(tmp_path) -> None:  # type: ignore[no-untyped-def]
    package_root = Path(__file__).resolve().parents[1]
    database_path = tmp_path / "migration.db"
    config = Config(str(package_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{database_path}")

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM cron_alembic_version").fetchone()
    assert revision == ("20260902_0001",)
    assert {
        "cron_schedules",
        "cron_schedule_revisions",
        "cron_executions",
        "cron_response_leases",
        "cron_responses",
    } <= tables
    assert "alembic_version" not in tables

    command.downgrade(config, "base")
    with sqlite3.connect(database_path) as connection:
        remaining = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert not any(
        name.startswith("cron_") and name != "cron_alembic_version" for name in remaining
    )
