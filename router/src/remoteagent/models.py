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


class AgentRecord(TimestampMixin, Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(63), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    compose_file: Mapped[str] = mapped_column(Text, nullable=False)
    project_name: Mapped[str] = mapped_column(String(63), nullable=False, unique=True)
    runner_service: Mapped[str] = mapped_column(String(128), nullable=False)
    dependency_services: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    environment: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    labels: Mapped[dict[str, str]] = mapped_column(JSON, default=dict, nullable=False)
    definition_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    current_revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    revisions: Mapped[list[AgentRevisionRecord]] = relationship(
        back_populates="agent", cascade="all, delete-orphan"
    )


class AgentRevisionRecord(Base):
    __tablename__ = "agent_revisions"
    __table_args__ = (UniqueConstraint("agent_id", "revision", name="uq_agent_revision"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    config_toml: Mapped[str] = mapped_column(Text, default="", nullable=False)
    base_context: Mapped[str] = mapped_column(Text, default="", nullable=False)
    definition_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    agent: Mapped[AgentRecord] = relationship(back_populates="revisions")


class CompanionStageRecord(TimestampMixin, Base):
    __tablename__ = "companion_stages"
    __table_args__ = (
        Index("ix_companion_stages_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(35), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="queued", nullable=False)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Paths are relative to the router's configured runtime root. Never persist
    # host-absolute staging paths, which would make restore and relocation unsafe.
    storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    file_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    companion: Mapped[ConversationCompanionRecord | None] = relationship(
        back_populates="stage", uselist=False
    )


class ConversationRecord(TimestampMixin, Base):
    __tablename__ = "conversations"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    codex_thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    codex_home_path: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_path: Mapped[str] = mapped_column(Text, nullable=False)
    agent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    companions: Mapped[list[ConversationCompanionRecord]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", passive_deletes=True
    )


class JobRecord(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("conversation_key", "sequence", name="uq_conversation_sequence"),
        UniqueConstraint("agent_id", "idempotency_key", name="uq_agent_idempotency"),
        Index("ix_jobs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        ForeignKey("agents.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    conversation_key: Mapped[str] = mapped_column(
        ForeignKey("conversations.key", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(256), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    agent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    thread_id_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict[str, int] | None] = mapped_column(JSON, nullable=True)
    runtime_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    events: Mapped[list[JobEventRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    artifacts: Mapped[list[ArtifactRecord]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )
    companion_additions: Mapped[list[ConversationCompanionRecord]] = relationship(
        back_populates="introduced_job", passive_deletes=True
    )


class ConversationCompanionRecord(TimestampMixin, Base):
    __tablename__ = "conversation_companions"
    __table_args__ = (
        UniqueConstraint("stage_id", name="uq_conversation_companion_stage"),
        UniqueConstraint(
            "conversation_key",
            "name",
            "version",
            name="uq_conversation_companion_version",
        ),
        Index(
            "ix_conversation_companions_conversation_status",
            "conversation_key",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    stage_id: Mapped[str] = mapped_column(
        ForeignKey("companion_stages.id", ondelete="RESTRICT"), nullable=False
    )
    conversation_key: Mapped[str] = mapped_column(
        ForeignKey("conversations.key", ondelete="CASCADE"), nullable=False
    )
    introduced_job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    introducing_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # Both locations are relative to the conversation storage root. The source
    # is immutable; the optional working path points at the persistent editable
    # copy created during activation.
    source_storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    working_storage_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    resolved_git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    stage: Mapped[CompanionStageRecord] = relationship(back_populates="companion")
    conversation: Mapped[ConversationRecord] = relationship(back_populates="companions")
    introduced_job: Mapped[JobRecord | None] = relationship(
        back_populates="companion_additions"
    )


class JobEventRecord(Base):
    __tablename__ = "job_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_job_event_sequence"),)

    # INTEGER is required for autoincrementing primary keys on SQLite and is
    # ample for an operational event stream.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    job: Mapped[JobRecord] = relationship(back_populates="events")


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("job_id", "relative_path", name="uq_job_artifact_path"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    conversation_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    job: Mapped[JobRecord] = relationship(back_populates="artifacts")


class LeaseRecord(Base):
    __tablename__ = "leases"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner: Mapped[str] = mapped_column(String(128), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
