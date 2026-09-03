"""Persist conversation execution profiles and immutable job snapshots.

Revision ID: 20260902_0003
Revises: 20260902_0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260902_0003"
down_revision = "20260902_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy rows intentionally remain NULL: their prior behavior was to
    # inherit whatever model settings Codex resolved at execution time.
    op.add_column("conversations", sa.Column("model", sa.String(length=128), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
    )
    op.add_column("jobs", sa.Column("model", sa.String(length=128), nullable=True))
    op.add_column(
        "jobs",
        sa.Column("reasoning_effort", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("model")
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("model")
