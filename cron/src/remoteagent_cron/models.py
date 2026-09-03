from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class ScheduleRecord(TimestampMixin, Base):
    __tablename__ = "cron_schedules"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    generation_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="enabled", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    current_revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    next_fire_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    active_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    persistent_conversation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skipped_occurrences: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_failure: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    revisions: Mapped[list[ScheduleRevisionRecord]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", passive_deletes=True
    )
    executions: Mapped[list[ExecutionRecord]] = relationship(
        back_populates="schedule", cascade="all, delete-orphan", passive_deletes=True
    )


class ScheduleRevisionRecord(Base):
    __tablename__ = "cron_schedule_revisions"
    __table_args__ = (
        UniqueConstraint("schedule_id", "revision", name="uq_cron_schedule_revision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("cron_schedules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(255), nullable=False)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(63), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    conversation_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    schedule: Mapped[ScheduleRecord] = relationship(back_populates="revisions")
    executions: Mapped[list[ExecutionRecord]] = relationship(back_populates="revision")


class ExecutionRecord(TimestampMixin, Base):
    __tablename__ = "cron_executions"
    __table_args__ = (
        UniqueConstraint("generation_id", "scheduled_for", name="uq_cron_generation_occurrence"),
        Index("ix_cron_execution_state", "state", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("cron_schedules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("cron_schedule_revisions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    continuation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    router_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    conversation_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    schedule: Mapped[ScheduleRecord] = relationship(back_populates="executions")
    revision: Mapped[ScheduleRevisionRecord] = relationship(back_populates="executions")
    response: Mapped[ResponseRecord | None] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        uselist=False,
        passive_deletes=True,
    )


class ResponseLeaseRecord(Base):
    __tablename__ = "cron_response_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="active", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ResponseRecord(Base):
    __tablename__ = "cron_responses"
    __table_args__ = (
        Index("ix_cron_response_schedule_fifo", "schedule_id", "created_at"),
        Index("ix_cron_response_lease", "lease_id", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        ForeignKey("cron_executions.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    schedule_id: Mapped[str] = mapped_column(
        ForeignKey("cron_schedules.id", ondelete="CASCADE"), nullable=False
    )
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("cron_schedule_revisions.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(63), nullable=False)
    router_job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    conversation_key: Mapped[str] = mapped_column(String(64), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    usage: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    lease_id: Mapped[str | None] = mapped_column(
        ForeignKey("cron_response_leases.id", ondelete="SET NULL"), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    execution: Mapped[ExecutionRecord] = relationship(back_populates="response")
