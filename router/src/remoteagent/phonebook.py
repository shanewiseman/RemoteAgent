from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .schemas import AgentDefinition


def _safe_child(base: Path, value: str | Path, *, kind: str, must_exist: bool = True) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        raise ValueError(f"{kind} must be a relative path")
    resolved_base = base.resolve()
    resolved = (resolved_base / candidate).resolve()
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"{kind} escapes agent directory") from exc
    if must_exist and not resolved.is_file():
        raise ValueError(f"{kind} is not a file: {resolved}")
    return resolved


def _read_text(path: Path, *, kind: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read {kind} {path}: {exc}") from exc


def _definition_from_raw(
    raw: dict[str, Any],
    *,
    agents_root: Path,
    source_directory: Path | None = None,
) -> AgentDefinition:
    values = dict(raw)
    values.pop("schema_version", None)
    agent_id = values.get("id")
    if not isinstance(agent_id, str):
        raise TypeError("agent entry requires a string id")
    agent_directory = (agents_root.resolve() / agent_id).resolve()
    try:
        agent_directory.relative_to(agents_root.resolve())
    except ValueError as exc:
        raise ValueError("agent directory escapes agents root") from exc
    if source_directory is not None and source_directory.resolve() != agent_directory:
        raise ValueError(f"manifest for {agent_id!r} must be directly inside {agent_directory}")

    compose_value = values.get("compose_file")
    if not isinstance(compose_value, str):
        raise TypeError(f"agent {agent_id!r} requires compose_file")
    # Manifest-relative paths are local to the declared agent directory. For
    # legacy inline records, tolerate an `agent-id/...` prefix but still enforce
    # the same boundary.
    if source_directory is None:
        prefixed = Path(compose_value)
        if prefixed.parts and prefixed.parts[0] == agent_id:
            prefixed = Path(*prefixed.parts[1:])
        compose_value = str(prefixed)
    values["compose_file"] = _safe_child(agent_directory, compose_value, kind="compose_file")

    config_file = values.pop("config_file", None)
    if config_file is not None:
        if "config_toml" in values:
            raise ValueError("specify config_file or config_toml, not both")
        config_path = _safe_child(agent_directory, config_file, kind="config_file")
        values["config_toml"] = _read_text(config_path, kind="Codex config")
    base_context_file = values.pop("base_context_file", None)
    if base_context_file is not None:
        if "base_context" in values:
            raise ValueError("specify base_context_file or base_context, not both")
        context_path = _safe_child(agent_directory, base_context_file, kind="base_context_file")
        values["base_context"] = _read_text(context_path, kind="base context")
    return AgentDefinition.model_validate(values)


def _definition_from_entry(raw_entry: dict[str, Any], agents_root: Path) -> AgentDefinition:
    entry = dict(raw_entry)
    manifest = entry.pop("manifest", None)
    if manifest is None:
        return _definition_from_raw(entry, agents_root=agents_root)
    if not isinstance(manifest, str):
        raise TypeError("manifest must be a relative path")
    reference_id = entry.get("id")
    if not isinstance(reference_id, str):
        raise TypeError("manifest reference requires id")
    agent_directory = (agents_root.resolve() / reference_id).resolve()
    manifest_path = _safe_child(agent_directory, Path(manifest).name, kind="manifest")
    requested = Path(manifest)
    expected = Path(reference_id) / requested.name
    if requested != expected and requested != Path(requested.name):
        raise ValueError(f"manifest for {reference_id!r} must be {expected.as_posix()}")
    try:
        with manifest_path.open("rb") as handle:
            manifest_values = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot load manifest {manifest_path}: {exc}") from exc
    if "agent" in manifest_values:
        manifest_values = manifest_values["agent"]
    if not isinstance(manifest_values, dict):
        raise TypeError(f"manifest {manifest_path} is not an agent table")
    if manifest_values.get("id") != reference_id:
        raise ValueError("phonebook id does not match manifest id")
    unexpected = set(entry) - {"id", "enabled"}
    if unexpected:
        raise ValueError(f"unsupported manifest reference fields: {sorted(unexpected)}")
    combined = dict(manifest_values)
    if "enabled" in entry:
        combined["enabled"] = entry["enabled"]
    return _definition_from_raw(
        combined, agents_root=agents_root, source_directory=manifest_path.parent
    )


def _phonebook_entries(path: Path) -> list[Any]:
    if not path.exists():
        return []
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"cannot load phonebook {path}: {exc}") from exc
    entries = document.get("agents", [])
    if not isinstance(entries, list):
        raise TypeError("phonebook 'agents' must be an array of tables")
    return entries


def load_phonebook(path: Path, agents_root: Path) -> list[AgentDefinition]:
    """Load manifest references (preferred) and legacy inline definitions.

    Every referenced file must remain under ``agents_root/<agent-id>`` after
    symlink resolution. This prevents a registry mutation from turning the
    router into an arbitrary host-file reader or Compose launcher.
    """

    definitions: list[AgentDefinition] = []
    seen: set[str] = set()
    for raw_entry in _phonebook_entries(path):
        if not isinstance(raw_entry, dict):
            raise TypeError("each phonebook agent must be a table")
        definition = _definition_from_entry(raw_entry, agents_root)
        if definition.id in seen:
            raise ValueError(f"duplicate agent id: {definition.id}")
        seen.add(definition.id)
        definitions.append(definition)
    return definitions


def load_phonebook_partial(
    path: Path, agents_root: Path
) -> tuple[list[AgentDefinition], dict[str, str]]:
    """Load valid entries while isolating malformed agent manifests."""

    try:
        entries = _phonebook_entries(path)
    except (TypeError, ValueError) as exc:
        return [], {"phonebook": str(exc)}
    definitions: list[AgentDefinition] = []
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries):
        label = (
            str(raw_entry.get("id"))
            if isinstance(raw_entry, dict) and raw_entry.get("id")
            else f"entry-{index}"
        )
        try:
            if not isinstance(raw_entry, dict):
                raise TypeError("each phonebook agent must be a table")
            definition = _definition_from_entry(raw_entry, agents_root)
            if definition.id in seen:
                raise ValueError(f"duplicate agent id: {definition.id}")
            seen.add(definition.id)
            definitions.append(definition)
        except Exception as exc:  # noqa: BLE001 - isolate validation per manifest.
            errors[f"{label}@{index}"] = str(exc)
    return definitions, errors


def validate_runtime_definition(definition: AgentDefinition, agents_root: Path) -> AgentDefinition:
    """Apply the same project-boundary policy to MCP/REST registrations."""

    agent_directory = (agents_root.resolve() / definition.id).resolve()
    supplied = definition.compose_file
    if supplied.is_absolute():
        compose = supplied.resolve()
    else:
        parts = supplied.parts
        if parts and parts[0] == definition.id:
            supplied = Path(*parts[1:])
        compose = (agent_directory / supplied).resolve()
    try:
        compose.relative_to(agent_directory)
    except ValueError as exc:
        raise ValueError("compose_file must remain inside the agent's top-level directory") from exc
    if not compose.is_file():
        raise ValueError(f"compose_file is not a file: {compose}")
    return definition.model_copy(update={"compose_file": compose})
