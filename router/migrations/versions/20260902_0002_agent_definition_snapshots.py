"""Persist complete immutable definitions with every agent revision.

Revision ID: 20260902_0002
Revises: 20260901_0001
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "20260902_0002"
down_revision = "20260901_0001"
branch_labels = None
depends_on = None


def _checksum(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def upgrade() -> None:
    op.add_column(
        "agent_revisions",
        sa.Column("definition_snapshot", sa.JSON(), nullable=True),
    )

    agents = sa.table(
        "agents",
        sa.column("id", sa.String()),
        sa.column("name", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("compose_file", sa.Text()),
        sa.column("project_name", sa.String()),
        sa.column("runner_service", sa.String()),
        sa.column("dependency_services", sa.JSON()),
        sa.column("environment", sa.JSON()),
        sa.column("labels", sa.JSON()),
        sa.column("definition_metadata", sa.JSON()),
        sa.column("enabled", sa.Boolean()),
    )
    revisions = sa.table(
        "agent_revisions",
        sa.column("id", sa.Integer()),
        sa.column("agent_id", sa.String()),
        sa.column("config_toml", sa.Text()),
        sa.column("base_context", sa.Text()),
        sa.column("definition_snapshot", sa.JSON()),
        sa.column("checksum", sa.String()),
    )
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.select(
                revisions.c.id,
                revisions.c.config_toml,
                revisions.c.base_context,
                agents.c.id.label("agent_id"),
                agents.c.name,
                agents.c.description,
                agents.c.compose_file,
                agents.c.project_name,
                agents.c.runner_service,
                agents.c.dependency_services,
                agents.c.environment,
                agents.c.labels,
                agents.c.definition_metadata,
                agents.c.enabled,
            ).select_from(revisions.join(agents, revisions.c.agent_id == agents.c.id))
        )
        .mappings()
        .all()
    )
    for row in rows:
        # Historical structural values did not exist before this migration.
        # Backfill the best recoverable definition: each revision's own config
        # and context plus the agent's structural projection at migration time.
        snapshot = {
            "id": row["agent_id"],
            "name": row["name"],
            "description": row["description"],
            "compose_file": row["compose_file"],
            "project_name": row["project_name"],
            "runner_service": row["runner_service"],
            "dependency_services": list(row["dependency_services"] or []),
            "enabled": bool(row["enabled"]),
            "config_toml": row["config_toml"],
            "base_context": row["base_context"],
            "environment": dict(row["environment"] or {}),
            "labels": dict(row["labels"] or {}),
            "metadata": dict(row["definition_metadata"] or {}),
        }
        connection.execute(
            revisions.update()
            .where(revisions.c.id == row["id"])
            .values(definition_snapshot=snapshot, checksum=_checksum(snapshot))
        )

    with op.batch_alter_table("agent_revisions") as batch_op:
        batch_op.alter_column(
            "definition_snapshot",
            existing_type=sa.JSON(),
            nullable=False,
        )


def downgrade() -> None:
    revisions = sa.table(
        "agent_revisions",
        sa.column("id", sa.Integer()),
        sa.column("config_toml", sa.Text()),
        sa.column("base_context", sa.Text()),
        sa.column("checksum", sa.String()),
    )
    connection = op.get_bind()
    rows = (
        connection.execute(
            sa.select(revisions.c.id, revisions.c.config_toml, revisions.c.base_context)
        )
        .mappings()
        .all()
    )
    for row in rows:
        legacy_payload = f"{row['config_toml']}\0{row['base_context']}".encode()
        connection.execute(
            revisions.update()
            .where(revisions.c.id == row["id"])
            .values(checksum=hashlib.sha256(legacy_payload).hexdigest())
        )
    with op.batch_alter_table("agent_revisions") as batch_op:
        batch_op.drop_column("definition_snapshot")
