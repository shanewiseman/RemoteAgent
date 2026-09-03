"""Initial durable router schema.

Revision ID: 20260901_0001
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260901_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("compose_file", sa.Text(), nullable=False),
        sa.Column("project_name", sa.String(length=63), nullable=False),
        sa.Column("runner_service", sa.String(length=128), nullable=False),
        sa.Column("dependency_services", sa.JSON(), nullable=False),
        sa.Column("environment", sa.JSON(), nullable=False),
        sa.Column("labels", sa.JSON(), nullable=False),
        sa.Column("definition_metadata", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_name"),
    )
    op.create_table(
        "agent_revisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("agent_id", sa.String(length=63), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("config_toml", sa.Text(), nullable=False),
        sa.Column("base_context", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "revision", name="uq_agent_revision"),
    )
    op.create_index("ix_agent_revisions_agent_id", "agent_revisions", ["agent_id"], unique=False)
    op.create_table(
        "conversations",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=63), nullable=False),
        sa.Column("codex_thread_id", sa.String(length=128), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("codex_home_path", sa.Text(), nullable=False),
        sa.Column("artifact_path", sa.Text(), nullable=False),
        sa.Column("agent_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index("ix_conversations_agent_id", "conversations", ["agent_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("agent_id", sa.String(length=63), nullable=False),
        sa.Column("conversation_key", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("agent_revision", sa.Integer(), nullable=False),
        sa.Column("thread_id_snapshot", sa.String(length=128), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("usage", sa.JSON(), nullable=True),
        sa.Column("runtime_metadata", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["conversation_key"], ["conversations.key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("agent_id", "idempotency_key", name="uq_agent_idempotency"),
        sa.UniqueConstraint("conversation_key", "sequence", name="uq_conversation_sequence"),
    )
    op.create_index("ix_jobs_agent_id", "jobs", ["agent_id"], unique=False)
    op.create_index("ix_jobs_conversation_key", "jobs", ["conversation_key"], unique=False)
    op.create_index("ix_jobs_status_created", "jobs", ["status", "created_at"], unique=False)
    op.create_table(
        "leases",
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=128), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"], unique=False)
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_key", sa.String(length=64), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "relative_path", name="uq_job_artifact_path"),
    )
    op.create_index(
        "ix_artifacts_conversation_key", "artifacts", ["conversation_key"], unique=False
    )
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"], unique=False)


def downgrade() -> None:
    op.drop_table("artifacts")
    op.drop_table("job_events")
    op.drop_table("leases")
    op.drop_table("jobs")
    op.drop_table("conversations")
    op.drop_table("agent_revisions")
    op.drop_table("agents")
