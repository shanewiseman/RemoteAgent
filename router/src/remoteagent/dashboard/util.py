"""Shared helpers that keep dashboard integration loosely coupled to core services."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def setting(settings: Any, name: str, default: Any = None) -> Any:
    if settings is None:
        return default
    if isinstance(settings, Mapping):
        return settings.get(name, default)
    return getattr(settings, name, default)


def reveal_secret(value: Any) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter())
    return str(value)


def container_from_app(app: Any) -> Any:
    container = getattr(app.state, "container", None)
    if container is None:
        raise RuntimeError("router lifespan has not initialized app.state.container")
    return container


def metrics_from_app(app: Any) -> Any:
    """Resolve the one metrics registry shared by core and dashboard code."""

    container = getattr(app.state, "container", None)
    telemetry = getattr(container, "telemetry", None) if container is not None else None
    if telemetry is not None and all(
        callable(getattr(telemetry, name, None)) for name in ("observe_http", "render")
    ):
        return telemetry
    return getattr(app.state, "dashboard_metrics", None)


def json_safe(value: Any, *, max_depth: int = 12) -> Any:
    """Convert common ORM/Pydantic/domain values without exposing private state."""

    if max_depth < 0:
        return "[depth limit]"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value, max_depth=max_depth - 1)
    if isinstance(value, Path):
        return value.name
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return json_safe(model_dump(mode="json"), max_depth=max_depth - 1)
    if dataclasses.is_dataclass(value):
        return json_safe(dataclasses.asdict(value), max_depth=max_depth - 1)
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item, max_depth=max_depth - 1)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_safe(item, max_depth=max_depth - 1) for item in value]
    # SQLAlchemy entities expose their mapped public values in __dict__. Avoid
    # traversing relationships implicitly, which can trigger async lazy loads.
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {
            str(key): json_safe(item, max_depth=max_depth - 1)
            for key, item in attrs.items()
            if not str(key).startswith("_")
        }
    return str(value)


async def maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def invoke(
    target: Any,
    names: Sequence[str],
    *,
    kwargs: Mapping[str, Any] | None = None,
    positional: Sequence[Any] = (),
    default: Any = None,
) -> Any:
    """Invoke the first supported service method using signature-safe kwargs."""

    if target is None:
        return default
    supplied = dict(kwargs or {})
    for name in names:
        function = getattr(target, name, None)
        if not callable(function):
            continue
        try:
            signature = inspect.signature(function)
            positional_names = [
                parameter.name
                for parameter in signature.parameters.values()
                if parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ][: len(positional)]
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if accepts_kwargs:
                filtered = {
                    key: value for key, value in supplied.items() if key not in positional_names
                }
            else:
                filtered = {
                    key: value
                    for key, value in supplied.items()
                    if key in signature.parameters and key not in positional_names
                }
        except (TypeError, ValueError):
            filtered = supplied
        return await maybe_await(function(*positional, **filtered))
    return default


def page_payload(value: Any) -> dict[str, Any]:
    safe = json_safe(value)
    if safe is None:
        return {"items": [], "next_cursor": None}
    if isinstance(safe, dict):
        if "items" in safe:
            safe.setdefault("next_cursor", None)
            return safe
        for key in ("agents", "jobs", "conversations", "artifacts", "events", "revisions"):
            if key in safe and isinstance(safe[key], list):
                return {"items": safe[key], "next_cursor": safe.get("next_cursor")}
    if isinstance(safe, list):
        return {"items": safe, "next_cursor": None}
    return {"items": [safe], "next_cursor": None}
