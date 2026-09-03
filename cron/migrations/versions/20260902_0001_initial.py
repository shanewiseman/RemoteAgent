"""Initial durable cron service schema.

Revision ID: 20260902_0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cron_schedules",
        sa.Column("id", sa.String(length=63), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("current_revision_id", sa.String(length=36), nullable=False),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_execution_id", sa.String(length=36), nullable=True),
        sa.Column("last_execution_id", sa.String(length=36), nullable=True),
        sa.Column("persistent_conversation_key", sa.String(length=64), nullable=True),
        sa.Column("skipped_occurrences", sa.BigInteger(), nullable=False),
        sa.Column("last_failure", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id"),
    )
    op.create_index(
        "ix_cron_schedules_next_fire_at", "cron_schedules", ["next_fire_at"], unique=False
    )
    op.create_table(
        "cron_schedule_revisions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("schedule_id", sa.String(length=63), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("cron_expression", sa.String(length=255), nullable=False),
        sa.Column("timezone", sa.String(length=128), nullable=False),
        sa.Column("agent_id", sa.String(length=63), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
        sa.Column("conversation_mode", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["schedule_id"], ["cron_schedules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("schedule_id", "revision", name="uq_cron_schedule_revision"),
    )
    op.create_index(
        "ix_cron_schedule_revisions_schedule_id",
        "cron_schedule_revisions",
        ["schedule_id"],
        unique=False,
    )
    op.create_table(
        "cron_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("schedule_id", sa.String(length=63), nullable=False),
        sa.Column("generation_id", sa.String(length=36), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("continuation_key", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("router_job_id", sa.String(length=64), nullable=True),
        sa.Column("conversation_key", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["cron_schedule_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["cron_schedules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("generation_id", "scheduled_for", name="uq_cron_generation_occurrence"),
        sa.UniqueConstraint("router_job_id"),
    )
    op.create_index(
        "ix_cron_execution_state", "cron_executions", ["state", "updated_at"], unique=False
    )
    op.create_index(
        "ix_cron_executions_revision_id", "cron_executions", ["revision_id"], unique=False
    )
    op.create_index(
        "ix_cron_executions_schedule_id", "cron_executions", ["schedule_id"], unique=False
    )
    op.create_table(
        "cron_response_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "cron_responses",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("schedule_id", sa.String(length=63), nullable=False),
        sa.Column("revision_id", sa.String(length=36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("agent_id", sa.String(length=63), nullable=False),
        sa.Column("router_job_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_key", sa.String(length=64), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("lease_id", sa.String(length=36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["execution_id"], ["cron_executions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["lease_id"], ["cron_response_leases.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["revision_id"], ["cron_schedule_revisions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["schedule_id"], ["cron_schedules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id"),
    )
    op.create_index(
        "ix_cron_response_lease",
        "cron_responses",
        ["lease_id", "lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_cron_response_schedule_fifo",
        "cron_responses",
        ["schedule_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("cron_responses")
    op.drop_table("cron_response_leases")
    op.drop_table("cron_executions")
    op.drop_table("cron_schedule_revisions")
    op.drop_table("cron_schedules")
