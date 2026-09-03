from __future__ import annotations

import json

from remoteagent.dashboard.diagnostics import diagnostic_bundle, redact


def test_recursive_redaction_covers_keys_and_embedded_tokens() -> None:
    source = {
        "authorization": "Bearer very-secret-value",
        "nested": {
            "password": "hunter2",
            "message": "failed with Bearer abcdefghijklmnop",
        },
    }

    result = redact(source)

    assert result["authorization"] == "[redacted]"
    assert result["nested"]["password"] == "[redacted]"
    assert "very-secret-value" not in json.dumps(result)
    assert "abcdefghijklmnop" not in json.dumps(result)


def test_bounded_bundle_remains_valid_json() -> None:
    payload = diagnostic_bundle(
        {"components": {"large": "x" * 50_000}, "queue": {"running": 3}},
        max_bytes=256,
    )

    parsed = json.loads(payload)
    assert len(payload) <= 256
    assert parsed["truncated"] is True
