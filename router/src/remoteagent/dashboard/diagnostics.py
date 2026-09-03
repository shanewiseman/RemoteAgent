"""Bounded, recursively redacted diagnostic bundle generation."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .util import json_safe

MAX_DIAGNOSTIC_BYTES = 1024 * 1024
MAX_DIAGNOSTIC_EVENTS = 200
_SENSITIVE_KEY = re.compile(
    r"(?:auth|bearer|cookie|credential|password|secret|token|private[_-]?key)", re.IGNORECASE
)
_TOKEN_VALUE = re.compile(
    r"(?i)(?:bearer\s+[^\s\"'<>]{4,}|sk-[A-Za-z0-9_-]{12,}|eyJ[A-Za-z0-9_.-]{20,})"
)


def redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return "[depth limit]"
    value = json_safe(value)
    if isinstance(value, str):
        return _TOKEN_VALUE.sub("[redacted]", value[:64_000])
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            result[key_text] = (
                "[redacted]" if _SENSITIVE_KEY.search(key_text) else redact(item, depth=depth + 1)
            )
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item, depth=depth + 1) for item in list(value)[:MAX_DIAGNOSTIC_EVENTS]]
    return value


def diagnostic_bundle(data: Mapping[str, Any], *, max_bytes: int = MAX_DIAGNOSTIC_BYTES) -> bytes:
    sanitized = redact(data)
    encoded = json.dumps(sanitized, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    summary = {
        "truncated": True,
        "original_bytes": len(encoded),
        "message": "Diagnostic bundle exceeded the safe export limit.",
        "components": redact(data.get("components", {})),
        "queue": redact(data.get("queue", {})),
    }
    encoded = json.dumps(summary, indent=2, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    minimal = {
        "truncated": True,
        "original_bytes": len(encoded),
        "message": "Diagnostic bundle exceeded the safe export limit.",
    }
    encoded = json.dumps(minimal, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    # Extremely small caller-supplied limits cannot fit the normal envelope;
    # still return valid JSON instead of slicing through a UTF-8 sequence.
    return b"{}" if max_bytes >= 2 else b""


__all__ = ["MAX_DIAGNOSTIC_BYTES", "MAX_DIAGNOSTIC_EVENTS", "diagnostic_bundle", "redact"]
