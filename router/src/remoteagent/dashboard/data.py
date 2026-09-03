"""Read-only dashboard projection over core services and durable records."""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text

from remoteagent.models import (
    AgentRecord,
    AgentRevisionRecord,
    ArtifactRecord,
    CompanionStageRecord,
    ConversationCompanionRecord,
    ConversationRecord,
    JobEventRecord,
    JobRecord,
)

from .diagnostics import redact
from .util import container_from_app, invoke, json_safe, page_payload

ACTIVE_JOB_STATUSES = (
    "queued",
    "provisioning",
    "waiting_for_lease",
    "running",
    "collecting",
)


def _encode_cursor(created_at: datetime, identifier: str) -> str:
    raw = json.dumps([created_at.isoformat(), identifier], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        padded = value + "=" * (-len(value) % 4)
        timestamp, identifier = json.loads(base64.urlsafe_b64decode(padded).decode())
        return datetime.fromisoformat(timestamp), str(identifier)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _preview(value: str | None, limit: int = 2048) -> str | None:
    if value is None:
        return None
    cleaned = "".join(
        character for character in value if character in "\n\t" or ord(character) >= 32
    )
    return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"


def _count(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _usage_view(record: JobRecord) -> dict[str, Any]:
    """Expose count provenance without guessing whether legacy totals were exact."""

    metadata = record.runtime_metadata if isinstance(record.runtime_metadata, Mapping) else {}
    detailed = metadata.get("token_usage")
    if isinstance(detailed, Mapping) and str(detailed.get("quality")) in {
        "exact",
        "estimated",
        "unavailable",
    }:
        result = {
            key: json_safe(detailed.get(key))
            for key in (
                "schema_version",
                "quality",
                "source",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
                "total_tokens",
                "estimation_method",
                "observed_at",
            )
        }
        contributors = detailed.get("contributors")
        result["contributors"] = (
            [
                {
                    "component": _preview(str(item.get("component", "component")), 128),
                    "tokens": _count(item.get("tokens")),
                    "quality": (
                        str(item.get("quality"))
                        if str(item.get("quality")) in {"exact", "estimated", "unavailable"}
                        else "unavailable"
                    ),
                    "provenance": redact(str(item.get("provenance", ""))),
                }
                for item in contributors
                if isinstance(item, Mapping)
            ]
            if isinstance(contributors, list)
            else []
        )
        input_tokens = _count(result.get("input_tokens"))
        output_tokens = _count(result.get("output_tokens"))
        if (
            result.get("total_tokens") is None
            and input_tokens is not None
            and output_tokens is not None
        ):
            # reasoning_output_tokens is already a component of output_tokens.
            result["total_tokens"] = input_tokens + output_tokens
        return result

    legacy = record.usage if isinstance(record.usage, Mapping) else {}
    input_tokens = _count(legacy.get("input_tokens"))
    cached_input_tokens = _count(legacy.get("cached_input_tokens"))
    output_tokens = _count(legacy.get("output_tokens"))
    reasoning_output_tokens = _count(legacy.get("reasoning_output_tokens"))
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    components = (
        ("input", input_tokens),
        ("cached_input", cached_input_tokens),
        ("output", output_tokens),
        ("reasoning_output", reasoning_output_tokens),
    )
    return {
        "schema_version": 1,
        "quality": "unavailable",
        "source": "legacy.usage_without_provenance" if legacy else "unavailable",
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total,
        "estimation_method": None,
        "contributors": [
            {
                "component": component,
                "tokens": tokens,
                "quality": "unavailable",
                "provenance": "count quality was not persisted",
            }
            for component, tokens in components
        ]
        + [
            {
                "component": component,
                "tokens": None,
                "quality": "unavailable",
                "provenance": "per-component attribution unavailable",
            }
            for component in ("user", "system", "context", "tools", "auth.json")
        ],
    }


def _agent_view(record: AgentRecord, *, active_jobs: int = 0) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "description": _preview(record.description, 4096),
        "enabled": record.enabled,
        "active_jobs": active_jobs,
        "active": active_jobs > 0,
        "revision": record.current_revision,
        "runner_service": record.runner_service,
        "dependency_services": list(record.dependency_services or []),
        "labels": dict(record.labels or {}),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _companion_view(record: ConversationCompanionRecord) -> dict[str, Any]:
    """Bounded metadata only; companion bytes are never a dashboard surface."""

    return {
        "id": record.id,
        "stage_id": record.stage_id,
        "conversation_key": record.conversation_key,
        "introduced_job_id": record.introduced_job_id,
        "introducing_sequence": record.introducing_sequence,
        "name": _preview(record.name, 128),
        "version": record.version,
        "kind": record.kind,
        "status": record.status,
        "path": f"/workspace/companions/{record.name}",
        "size_bytes": record.size_bytes,
        "file_count": record.file_count,
        "sha256": record.sha256,
        "resolved_git_commit": record.resolved_git_commit,
        "activated_at": record.activated_at.isoformat() if record.activated_at else None,
        "superseded_at": record.superseded_at.isoformat() if record.superseded_at else None,
        "last_activation_error": _preview(redact(record.last_activation_error), 2_048),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _job_view(
    record: JobRecord,
    *,
    detail: bool = False,
    companion_additions: list[ConversationCompanionRecord] | None = None,
) -> dict[str, Any]:
    result = {
        "id": record.id,
        "agent_id": record.agent_id,
        "conversation_key": record.conversation_key,
        "sequence": record.sequence,
        "status": record.status,
        "agent_revision": record.agent_revision,
        "model": record.model,
        "reasoning_effort": record.reasoning_effort,
        "prompt": _preview(record.prompt, 64_000 if detail else 512),
        "result": _preview(record.result, 128_000 if detail else 512),
        "error": _preview(redact(record.error), 16_000 if detail else 512),
        "usage": _usage_view(record),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
        "started_at": record.started_at.isoformat() if record.started_at else None,
        "completed_at": record.completed_at.isoformat() if record.completed_at else None,
        "cancel_requested": record.cancel_requested,
        "companion_additions": [
            _companion_view(item) for item in (companion_additions or [])[:20]
        ],
    }
    if detail:
        metadata = dict(record.runtime_metadata or {})
        for key in list(metadata):
            lowered = str(key).lower()
            if any(
                sensitive in lowered
                for sensitive in (
                    "container_id",
                    "volume",
                    "workspace",
                    "codex_home",
                    "auth",
                    "token",
                    "secret",
                    "stderr",
                    "stdout",
                    "command",
                    "argv",
                )
            ):
                metadata.pop(key, None)
        result["runtime_metadata"] = json_safe(redact(metadata))
    return result


def _history_job_view(record: JobRecord) -> dict[str, Any]:
    """A useful but bounded turn projection for multi-turn history pages."""

    result = _job_view(record)
    result["prompt"] = _preview(record.prompt, 4_096)
    result["result"] = _preview(record.result, 8_192)
    result["error"] = _preview(redact(record.error), 2_048)
    return result


def _conversation_view(record: ConversationRecord) -> dict[str, Any]:
    return {
        "key": record.key,
        "agent_id": record.agent_id,
        "agent_revision": record.agent_revision,
        "status": record.status,
        "model": record.model,
        "reasoning_effort": record.reasoning_effort,
        "has_codex_thread": bool(record.codex_thread_id),
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _artifact_view(record: ArtifactRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "job_id": record.job_id,
        "conversation_key": record.conversation_key,
        "name": record.relative_path.rsplit("/", 1)[-1],
        "relative_path": record.relative_path,
        "media_type": record.media_type,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "created_at": record.created_at.isoformat(),
    }


def _sanitized_event(record: JobEventRecord) -> dict[str, Any]:
    payload = dict(record.payload or {})
    event_lower = record.event_type.lower()
    if "reasoning" in event_lower:
        payload = {"redacted": "model reasoning is not exposed"}
    payload = redact(payload)
    return {
        "id": record.id,
        "job_id": record.job_id,
        "sequence": record.sequence,
        "type": record.event_type,
        "payload": json_safe(payload),
        "created_at": record.created_at.isoformat(),
    }


class DashboardData:
    """Adapter that prefers core service projections and has ORM fallbacks."""

    def __init__(self, app: Any) -> None:
        self.app = app

    @property
    def container(self) -> Any:
        return container_from_app(self.app)

    @property
    def session_factory(self) -> Any:
        return getattr(self.container, "session_factory", None)

    @property
    def dashboard_service(self) -> Any:
        return getattr(self.container, "dashboard_service", None)

    def history_days(self) -> int:
        settings = getattr(self.container, "settings", None)
        try:
            value = int(getattr(settings, "dashboard_history_days", 30))
        except (TypeError, ValueError):
            value = 30
        return min(3650, max(1, value))

    def history_default_turns(self) -> int:
        settings = getattr(self.container, "settings", None)
        try:
            value = int(getattr(settings, "dashboard_history_default_turns", 100))
        except (TypeError, ValueError):
            value = 100
        return min(500, max(1, value))

    def history_cutoff(self) -> datetime:
        return datetime.now(UTC) - timedelta(days=self.history_days())

    async def summary(self) -> dict[str, Any]:
        projected = await invoke(self.dashboard_service, ("summary", "dashboard_summary"))
        if projected is not None:
            return dict(json_safe(projected))
        async with self.session_factory() as session:
            agent_rows = (
                await session.execute(
                    select(AgentRecord.enabled, func.count()).group_by(AgentRecord.enabled)
                )
            ).all()
            job_rows = (
                await session.execute(
                    select(JobRecord.status, func.count()).group_by(JobRecord.status)
                )
            ).all()
            active_agents = await session.scalar(
                select(func.count(func.distinct(JobRecord.agent_id))).where(
                    JobRecord.status.in_(ACTIVE_JOB_STATUSES)
                )
            )
            conversation_count = await session.scalar(
                select(func.count()).select_from(ConversationRecord)
            )
            artifact_count = await session.scalar(select(func.count()).select_from(ArtifactRecord))
            companion_rows = (
                await session.execute(
                    select(ConversationCompanionRecord.status, func.count()).group_by(
                        ConversationCompanionRecord.status
                    )
                )
            ).all()
            companion_stage_rows = (
                await session.execute(
                    select(CompanionStageRecord.status, func.count()).group_by(
                        CompanionStageRecord.status
                    )
                )
            ).all()
        agents = {"ready" if enabled else "disabled": count for enabled, count in agent_rows}
        return {
            "agents": agents,
            "active_agents": int(active_agents or 0),
            "jobs": {status: count for status, count in job_rows},
            "conversations": int(conversation_count or 0),
            "artifacts": int(artifact_count or 0),
            "companions": {status: count for status, count in companion_rows},
            "companion_stages": {status: count for status, count in companion_stage_rows},
        }

    async def list_agents(
        self, *, cursor: str | None, limit: int, status: str | None = None
    ) -> dict[str, Any]:
        service = getattr(self.container, "agent_service", None)
        projected = await invoke(
            self.dashboard_service or service,
            ("list_agents", "agents"),
            kwargs={"cursor": cursor, "limit": limit, "status": status},
        )
        if projected is not None:
            return page_payload(projected)
        statement = select(AgentRecord).order_by(AgentRecord.id).limit(limit + 1)
        if cursor:
            statement = statement.where(AgentRecord.id > cursor)
        if status == "enabled" or status == "ready":
            statement = statement.where(AgentRecord.enabled.is_(True))
        elif status == "disabled":
            statement = statement.where(AgentRecord.enabled.is_(False))
        async with self.session_factory() as session:
            records = list((await session.scalars(statement)).all())
            active_rows = (
                await session.execute(
                    select(JobRecord.agent_id, func.count())
                    .where(
                        JobRecord.agent_id.in_([record.id for record in records[:limit]]),
                        JobRecord.status.in_(ACTIVE_JOB_STATUSES),
                    )
                    .group_by(JobRecord.agent_id)
                )
            ).all()
        active_by_agent = {agent_id: int(count) for agent_id, count in active_rows}
        return {
            "items": [
                _agent_view(item, active_jobs=active_by_agent.get(item.id, 0))
                for item in records[:limit]
            ],
            "next_cursor": records[limit - 1].id if len(records) > limit else None,
        }

    async def get_agent(self, agent_id: str) -> dict[str, Any] | None:
        service = getattr(self.container, "agent_service", None)
        projected = await invoke(
            self.dashboard_service or service,
            ("get_agent", "agent"),
            positional=(agent_id,),
        )
        if projected is not None:
            return dict(json_safe(projected))
        async with self.session_factory() as session:
            record = await session.get(AgentRecord, agent_id)
            if record is None:
                return None
            revision = await session.scalar(
                select(AgentRevisionRecord).where(
                    AgentRevisionRecord.agent_id == agent_id,
                    AgentRevisionRecord.revision == record.current_revision,
                )
            )
            active_jobs = await session.scalar(
                select(func.count())
                .select_from(JobRecord)
                .where(
                    JobRecord.agent_id == agent_id,
                    JobRecord.status.in_(ACTIVE_JOB_STATUSES),
                )
            )
        result = _agent_view(record, active_jobs=int(active_jobs or 0))
        if revision:
            result["config_toml"] = _preview(revision.config_toml, 128_000)
            result["base_context"] = _preview(revision.base_context, 128_000)
            result["checksum"] = revision.checksum
        return result

    async def agent_revisions(
        self, agent_id: str, *, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        async with self.session_factory() as session:
            statement = (
                select(AgentRevisionRecord)
                .where(AgentRevisionRecord.agent_id == agent_id)
                .order_by(AgentRevisionRecord.revision.desc())
                .limit(limit + 1)
            )
            if cursor and cursor.isdigit():
                statement = statement.where(AgentRevisionRecord.revision < int(cursor))
            records = list((await session.scalars(statement)).all())
        return {
            "items": [
                {
                    "revision": item.revision,
                    "checksum": item.checksum,
                    "created_at": item.created_at.isoformat(),
                }
                for item in records[:limit]
            ],
            "next_cursor": str(records[limit - 1].revision) if len(records) > limit else None,
        }

    async def list_jobs(
        self,
        *,
        cursor: str | None,
        limit: int,
        status: str | None = None,
        agent_id: str | None = None,
        conversation_key: str | None = None,
    ) -> dict[str, Any]:
        service = getattr(self.container, "job_service", None)
        projected = await invoke(
            self.dashboard_service or service,
            ("list_jobs", "jobs"),
            kwargs={
                "cursor": cursor,
                "limit": limit,
                "status": status,
                "agent_id": agent_id,
                "conversation_key": conversation_key,
            },
        )
        if projected is not None:
            return page_payload(projected)
        statement = (
            select(JobRecord)
            .order_by(JobRecord.created_at.desc(), JobRecord.id.desc())
            .limit(limit + 1)
        )
        decoded = _decode_cursor(cursor)
        if decoded:
            timestamp, identifier = decoded
            statement = statement.where(
                or_(
                    JobRecord.created_at < timestamp,
                    and_(JobRecord.created_at == timestamp, JobRecord.id < identifier),
                )
            )
        if status:
            statement = statement.where(JobRecord.status == status)
        if agent_id:
            statement = statement.where(JobRecord.agent_id == agent_id)
        if conversation_key:
            statement = statement.where(JobRecord.conversation_key == conversation_key)
        async with self.session_factory() as session:
            records = list((await session.scalars(statement)).all())
        next_cursor = (
            _encode_cursor(records[limit - 1].created_at, records[limit - 1].id)
            if len(records) > limit
            else None
        )
        return {"items": [_job_view(item) for item in records[:limit]], "next_cursor": next_cursor}

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        service = getattr(self.container, "job_service", None)
        projected = await invoke(
            self.dashboard_service or service,
            ("get_job", "job"),
            positional=(job_id,),
        )
        if projected is not None:
            return dict(json_safe(projected))
        async with self.session_factory() as session:
            record = await session.get(JobRecord, job_id)
            companions = (
                list(
                    (
                        await session.scalars(
                            select(ConversationCompanionRecord)
                            .where(ConversationCompanionRecord.introduced_job_id == job_id)
                            .order_by(ConversationCompanionRecord.name)
                            .limit(20)
                        )
                    ).all()
                )
                if record
                else []
            )
        return (
            _job_view(record, detail=True, companion_additions=companions) if record else None
        )

    async def job_events(self, job_id: str, *, after: int = 0, limit: int = 200) -> dict[str, Any]:
        async with self.session_factory() as session:
            records = list(
                (
                    await session.scalars(
                        select(JobEventRecord)
                        .where(JobEventRecord.job_id == job_id, JobEventRecord.sequence > after)
                        .order_by(JobEventRecord.sequence)
                        .limit(limit)
                    )
                ).all()
            )
        return {"items": [_sanitized_event(item) for item in records], "next_cursor": None}

    async def list_conversations(
        self,
        *,
        cursor: str | None,
        limit: int,
        agent_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        service = getattr(self.container, "job_service", None)
        projected = await invoke(
            self.dashboard_service or service,
            ("list_conversations", "conversations"),
            kwargs={"cursor": cursor, "limit": limit, "agent_id": agent_id, "status": status},
        )
        if projected is not None:
            return page_payload(projected)
        statement = (
            select(ConversationRecord)
            .where(ConversationRecord.updated_at >= self.history_cutoff())
            .order_by(ConversationRecord.updated_at.desc(), ConversationRecord.key.desc())
            .limit(limit + 1)
        )
        decoded = _decode_cursor(cursor)
        if decoded:
            timestamp, identifier = decoded
            statement = statement.where(
                or_(
                    ConversationRecord.updated_at < timestamp,
                    and_(
                        ConversationRecord.updated_at == timestamp,
                        ConversationRecord.key < identifier,
                    ),
                )
            )
        if agent_id:
            statement = statement.where(ConversationRecord.agent_id == agent_id)
        if status:
            statement = statement.where(ConversationRecord.status == status)
        async with self.session_factory() as session:
            records = list((await session.scalars(statement)).all())
        next_cursor = (
            _encode_cursor(records[limit - 1].updated_at, records[limit - 1].key)
            if len(records) > limit
            else None
        )
        return {
            "items": [_conversation_view(item) for item in records[:limit]],
            "next_cursor": next_cursor,
        }

    async def get_conversation(self, key: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(ConversationRecord, key)
        return _conversation_view(record) if record else None

    async def conversation_companions(
        self, key: str, *, include_history: bool = False, limit: int = 200
    ) -> dict[str, Any]:
        """Return read-only, metadata-only companion state for dashboard detail."""

        selected_limit = min(500, max(1, limit))
        statement = (
            select(ConversationCompanionRecord)
            .where(ConversationCompanionRecord.conversation_key == key)
            .order_by(
                ConversationCompanionRecord.name,
                ConversationCompanionRecord.version.desc(),
            )
            .limit(selected_limit + 1)
        )
        if not include_history:
            statement = statement.where(
                ConversationCompanionRecord.status.in_(("active", "pending"))
            )
        async with self.session_factory() as session:
            records = list((await session.scalars(statement)).all())
        return {
            "items": [_companion_view(item) for item in records[:selected_limit]],
            "truncated": len(records) > selected_limit,
            "include_history": include_history,
        }

    async def conversation_turns(self, key: str, *, limit: int | None = None) -> dict[str, Any]:
        selected_limit = self.history_default_turns() if limit is None else min(500, max(1, limit))
        async with self.session_factory() as session:
            records = list(
                (
                    await session.scalars(
                        select(JobRecord)
                        .where(
                            JobRecord.conversation_key == key,
                            JobRecord.created_at >= self.history_cutoff(),
                        )
                        .order_by(JobRecord.sequence.desc())
                        .limit(selected_limit)
                    )
                ).all()
            )
        records.reverse()
        return {
            "items": [_history_job_view(item) for item in records],
            "next_cursor": None,
            "history_days": self.history_days(),
        }

    async def list_artifacts(
        self,
        *,
        cursor: str | None,
        limit: int,
        job_id: str | None = None,
        conversation_key: str | None = None,
        media_type: str | None = None,
    ) -> dict[str, Any]:
        service = getattr(self.container, "artifact_service", None)
        projected = await invoke(
            self.dashboard_service or service,
            ("list_artifacts", "artifacts"),
            kwargs={
                "cursor": cursor,
                "limit": limit,
                "job_id": job_id,
                "conversation_key": conversation_key,
                "media_type": media_type,
            },
        )
        if projected is not None:
            return page_payload(projected)
        statement = (
            select(ArtifactRecord)
            .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
            .limit(limit + 1)
        )
        decoded = _decode_cursor(cursor)
        if decoded:
            timestamp, identifier = decoded
            statement = statement.where(
                or_(
                    ArtifactRecord.created_at < timestamp,
                    and_(ArtifactRecord.created_at == timestamp, ArtifactRecord.id < identifier),
                )
            )
        if job_id:
            statement = statement.where(ArtifactRecord.job_id == job_id)
        if conversation_key:
            statement = statement.where(ArtifactRecord.conversation_key == conversation_key)
        if media_type:
            statement = statement.where(ArtifactRecord.media_type == media_type)
        async with self.session_factory() as session:
            records = list((await session.scalars(statement)).all())
        next_cursor = (
            _encode_cursor(records[limit - 1].created_at, records[limit - 1].id)
            if len(records) > limit
            else None
        )
        return {
            "items": [_artifact_view(item) for item in records[:limit]],
            "next_cursor": next_cursor,
        }

    async def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            record = await session.get(ArtifactRecord, artifact_id)
        return _artifact_view(record) if record else None

    async def artifact_content(
        self, artifact_id: str, *, preview: bool = False, max_bytes: int | None = None
    ) -> Any:
        service = getattr(self.container, "artifact_service", None)
        names = (
            ("read_preview", "read_artifact_preview")
            if preview
            else ("read_artifact", "get_artifact_content")
        )
        projected = await invoke(
            service,
            names,
            kwargs={"max_bytes": max_bytes},
            positional=(artifact_id,),
        )
        if projected is not None or preview:
            # Raster images are previewed only when a service explicitly
            # returns a sanitized derivative. Never bless an original upload.
            return projected

        located = await invoke(service, ("path",), positional=(artifact_id,))
        path = located[0] if isinstance(located, tuple) and located else None
        if not isinstance(path, Path):
            return None
        if max_bytes is not None:

            def read_limited() -> bytes:
                with path.open("rb") as handle:
                    return handle.read(max_bytes + 1)

            return await asyncio.to_thread(read_limited)

        async def stream() -> Any:
            handle = await asyncio.to_thread(path.open, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(handle.read, 64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(handle.close)

        return stream()

    async def activity_events(self, *, after_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        projected = await invoke(
            self.dashboard_service,
            ("list_activity_events", "activity_events"),
            kwargs={"after_id": after_id, "limit": limit},
        )
        if projected is not None:
            payload = page_payload(projected)
            return list(payload["items"])
        async with self.session_factory() as session:
            records = list(
                (
                    await session.scalars(
                        select(JobEventRecord)
                        .where(JobEventRecord.id > after_id)
                        .order_by(JobEventRecord.id)
                        .limit(limit)
                    )
                ).all()
            )
        return [_sanitized_event(item) for item in records]

    async def system(self) -> dict[str, Any]:
        projected = await invoke(self.dashboard_service, ("system", "system_status"))
        if projected is not None:
            return dict(json_safe(projected))
        result: dict[str, Any] = {"postgres": "unknown", "redis": "not_configured"}
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
            result["postgres"] = "ok"
        except Exception:  # noqa: BLE001 - health reporting must survive driver-specific failures
            result["postgres"] = "unavailable"
        cache = getattr(self.container, "cache", None)
        settings = getattr(self.container, "settings", None)
        redis_client = getattr(self.container, "redis", None)
        if redis_client is None:
            redis_client = getattr(self.container, "redis_client", None)
        cache_client = getattr(cache, "client", None) if cache is not None else None
        accelerator = cache if cache_client is not None else redis_client
        if accelerator is not None:
            try:
                await accelerator.ping()
                result["redis"] = "ok"
            except Exception:  # noqa: BLE001 - cache implementations expose different errors
                result["redis"] = "unavailable"
            result["cache_backend"] = "redis"
        elif cache is not None:
            result["redis"] = (
                "unavailable" if getattr(settings, "redis_url", None) else "not_configured"
            )
            result["cache_backend"] = "memory"
        scheduler = getattr(self.container, "scheduler", None)
        if scheduler is not None:
            result["scheduler"] = {
                "running": bool(getattr(scheduler, "running", False)),
                "worker_id": _preview(str(getattr(scheduler, "worker_id", "")), 128),
            }
        result["version"] = str(
            getattr(self.container, "version", getattr(settings, "version", "unknown"))
        )
        return result

    async def debug(self) -> dict[str, Any]:
        projected = await invoke(self.dashboard_service, ("debug", "debug_status"))
        if projected is not None:
            return dict(json_safe(redact(projected)))
        async with self.session_factory() as session:
            statuses = (
                await session.execute(
                    select(JobRecord.status, func.count()).group_by(JobRecord.status)
                )
            ).all()
            oldest = await session.scalar(
                select(func.min(JobRecord.created_at)).where(
                    JobRecord.status.in_(("queued", "provisioning", "running"))
                )
            )
        return {
            "queue": {status: count for status, count in statuses},
            "oldest_nonterminal_at": oldest.isoformat() if oldest else None,
            "components": await self.system(),
        }


__all__ = ["DashboardData"]
