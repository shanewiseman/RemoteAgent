from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .compose import ComposeProjectValidator
from .models import AgentRecord, AgentRevisionRecord
from .phonebook import validate_runtime_definition
from .schemas import AgentDefinition, AgentSummary, AgentView, RevisionUpdate


class AgentNotFoundError(LookupError):
    pass


class AgentConflictError(RuntimeError):
    pass


def _definition_snapshot(definition: AgentDefinition) -> dict[str, Any]:
    """Return the JSON-safe, complete executable definition for a revision."""

    return definition.model_dump(mode="json")


def _revision_checksum(snapshot: dict[str, Any]) -> str:
    payload = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AgentService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        agents_root,
        compose_validator: ComposeProjectValidator | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.agents_root = agents_root
        self.compose_validator = compose_validator
        self._agent_locks: dict[str, asyncio.Lock] = {}

    async def synchronize(self, definitions: Iterable[AgentDefinition]) -> dict[str, str]:
        """Seed missing agents without overwriting the durable registry.

        Runtime registration and configuration revisions are authoritative once
        an agent exists.  Checked-in phonebook changes are applied explicitly
        with ``register(..., replace=True)`` instead of being replayed on every
        router restart.
        """

        errors: dict[str, str] = {}
        for definition in definitions:
            try:
                await self._register(definition, replace=False, preserve_enabled=True)
            except AgentConflictError:
                # A changed seed is expected after an MCP/API revision or an
                # operator override. Preserve that durable state until an
                # explicit replace operation reconciles it.
                continue
            except ValueError as exc:
                errors[definition.id] = str(exc)
        return errors

    async def register(self, definition: AgentDefinition, *, replace: bool = False) -> AgentView:
        return await self._register(definition, replace=replace, preserve_enabled=False)

    async def _register(
        self,
        definition: AgentDefinition,
        *,
        replace: bool,
        preserve_enabled: bool,
    ) -> AgentView:
        definition = validate_runtime_definition(definition, self.agents_root)
        if self.compose_validator is not None:
            await self.compose_validator.validate(definition)
        lock = self._agent_locks.setdefault(definition.id, asyncio.Lock())
        async with lock:
            return await self._register_locked(
                definition,
                replace=replace,
                preserve_enabled=preserve_enabled,
            )

    async def _register_locked(
        self,
        definition: AgentDefinition,
        *,
        replace: bool,
        preserve_enabled: bool,
    ) -> AgentView:
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(AgentRecord).where(AgentRecord.id == definition.id).with_for_update()
            )
            current = (
                await self._get_revision(session, record.id, record.current_revision)
                if record is not None
                else None
            )
            if record is not None and preserve_enabled:
                definition = definition.model_copy(update={"enabled": record.enabled})
            snapshot = _definition_snapshot(definition)
            if record is not None and not replace:
                unchanged = current is not None and current.definition_snapshot == snapshot
                if unchanged:
                    return await self._view(session, record)
                raise AgentConflictError(
                    f"agent already exists with a different definition: {definition.id}"
                )
            if record is None:
                record = AgentRecord(
                    id=definition.id,
                    name=definition.name,
                    description=definition.description,
                    compose_file=str(definition.compose_file),
                    project_name=definition.project_name or f"remoteagent-{definition.id}",
                    runner_service=definition.runner_service,
                    dependency_services=list(definition.dependency_services),
                    environment=definition.environment,
                    labels=definition.labels,
                    definition_metadata=definition.metadata,
                    enabled=definition.enabled,
                    current_revision=1,
                )
                session.add(record)
                session.add(
                    AgentRevisionRecord(
                        agent_id=definition.id,
                        revision=1,
                        config_toml=definition.config_toml,
                        base_context=definition.base_context,
                        definition_snapshot=snapshot,
                        checksum=_revision_checksum(snapshot),
                    )
                )
            else:
                assert current is not None
                checksum = _revision_checksum(snapshot)
                if snapshot != current.definition_snapshot:
                    record.current_revision += 1
                    session.add(
                        AgentRevisionRecord(
                            agent_id=record.id,
                            revision=record.current_revision,
                            config_toml=definition.config_toml,
                            base_context=definition.base_context,
                            definition_snapshot=snapshot,
                            checksum=checksum,
                        )
                    )
                record.name = definition.name
                record.description = definition.description
                record.compose_file = str(definition.compose_file)
                record.project_name = definition.project_name or f"remoteagent-{definition.id}"
                record.runner_service = definition.runner_service
                record.dependency_services = list(definition.dependency_services)
                record.environment = definition.environment
                record.labels = definition.labels
                record.definition_metadata = definition.metadata
                if not preserve_enabled:
                    record.enabled = definition.enabled
            await session.flush()
            return await self._view(session, record)

    async def list(self, *, include_disabled: bool = False) -> list[AgentSummary]:
        async with self.session_factory() as session:
            statement = select(AgentRecord).order_by(AgentRecord.id)
            if not include_disabled:
                statement = statement.where(AgentRecord.enabled.is_(True))
            records = (await session.scalars(statement)).all()
            return [
                AgentSummary(
                    id=item.id,
                    name=item.name,
                    description=item.description,
                    enabled=item.enabled,
                    revision=item.current_revision,
                )
                for item in records
            ]

    async def get(self, agent_id: str) -> AgentView:
        async with self.session_factory() as session:
            record = await session.get(AgentRecord, agent_id)
            if record is None:
                raise AgentNotFoundError(agent_id)
            return await self._view(session, record)

    async def definition(self, agent_id: str, revision: int | None = None) -> AgentDefinition:
        async with self.session_factory() as session:
            record = await session.get(AgentRecord, agent_id)
            if record is None:
                raise AgentNotFoundError(agent_id)
            selected = record.current_revision if revision is None else revision
            revision_record = await self._get_revision(session, agent_id, selected)
            return AgentDefinition.model_validate(revision_record.definition_snapshot)

    async def update_revision(self, agent_id: str, update: RevisionUpdate) -> AgentView:
        lock = self._agent_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            return await self._update_revision_locked(agent_id, update)

    async def _update_revision_locked(self, agent_id: str, update: RevisionUpdate) -> AgentView:
        async with self.session_factory() as session, session.begin():
            record = await session.scalar(
                select(AgentRecord).where(AgentRecord.id == agent_id).with_for_update()
            )
            if record is None:
                raise AgentNotFoundError(agent_id)
            current = await self._get_revision(session, agent_id, record.current_revision)
            config = current.config_toml if update.config_toml is None else update.config_toml
            context = current.base_context if update.base_context is None else update.base_context
            definition = AgentDefinition.model_validate(current.definition_snapshot).model_copy(
                update={"config_toml": config, "base_context": context}
            )
            snapshot = _definition_snapshot(definition)
            checksum = _revision_checksum(snapshot)
            if snapshot != current.definition_snapshot:
                record.current_revision += 1
                session.add(
                    AgentRevisionRecord(
                        agent_id=agent_id,
                        revision=record.current_revision,
                        config_toml=config,
                        base_context=context,
                        definition_snapshot=snapshot,
                        checksum=checksum,
                    )
                )
                await session.flush()
            return await self._view(session, record)

    async def _view(self, session: AsyncSession, record: AgentRecord) -> AgentView:
        revision = await self._get_revision(session, record.id, record.current_revision)
        definition = AgentDefinition.model_validate(revision.definition_snapshot)
        return AgentView(
            id=definition.id,
            name=definition.name,
            description=definition.description,
            enabled=record.enabled,
            revision=record.current_revision,
            compose_file=str(definition.compose_file),
            project_name=definition.project_name,
            runner_service=definition.runner_service,
            dependency_services=list(definition.dependency_services),
            environment=definition.environment,
            labels=definition.labels,
            metadata=definition.metadata,
            config_toml=definition.config_toml,
            base_context=definition.base_context,
        )

    @staticmethod
    async def _get_revision(
        session: AsyncSession, agent_id: str, revision: int
    ) -> AgentRevisionRecord:
        item = await session.scalar(
            select(AgentRevisionRecord).where(
                AgentRevisionRecord.agent_id == agent_id,
                AgentRevisionRecord.revision == revision,
            )
        )
        if item is None:
            raise AgentNotFoundError(f"missing revision {agent_id}@{revision}")
        return item
