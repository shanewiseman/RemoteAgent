"""Persist staged and conversation-owned companion data.

Revision ID: 20260902_0004
Revises: 20260902_0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_0004"
down_revision = "20260902_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "companion_stages",
        sa.Column("id", sa.String(length=35), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("file_count", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("resolved_git_commit", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_companion_stages_status_expires",
        "companion_stages",
        ["status", "expires_at"],
        unique=False,
    )

    op.create_table(
        "conversation_companions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("stage_id", sa.String(length=35), nullable=False),
        sa.Column("conversation_key", sa.String(length=64), nullable=False),
        sa.Column("introduced_job_id", sa.String(length=64), nullable=True),
        sa.Column("introducing_sequence", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_storage_path", sa.Text(), nullable=False),
        sa.Column("working_storage_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("file_count", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("resolved_git_commit", sa.String(length=64), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_activation_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_key"], ["conversations.key"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["introduced_job_id"], ["jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["stage_id"], ["companion_stages.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stage_id", name="uq_conversation_companion_stage"),
        sa.UniqueConstraint(
            "conversation_key",
            "name",
            "version",
            name="uq_conversation_companion_version",
        ),
    )
    op.create_index(
        "ix_conversation_companions_conversation_status",
        "conversation_companions",
        ["conversation_key", "status"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_companions_introduced_job_id",
        "conversation_companions",
        ["introduced_job_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("conversation_companions")
    op.drop_table("companion_stages")
