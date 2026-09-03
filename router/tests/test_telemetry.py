from __future__ import annotations

from remoteagent.telemetry import (
    DashboardMetrics,
    TokenCountQuality,
    TokenTelemetryCollector,
    estimate_tokens,
    exact_usage_from_event,
)


def test_exact_total_does_not_double_count_reasoning() -> None:
    usage = exact_usage_from_event(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 12,
                "cached_input_tokens": 4,
                "output_tokens": 8,
                "reasoning_output_tokens": 6,
            },
        }
    )

    assert usage is not None
    assert usage.quality is TokenCountQuality.EXACT
    assert usage.total_tokens == 20
    assert usage.reasoning_output_tokens == 6


def test_exact_upstream_total_is_preserved() -> None:
    usage = exact_usage_from_event(
        {
            "type": "turn_completed",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 99},
        }
    )

    assert usage is not None
    assert usage.total_tokens == 99


def test_estimation_sums_visible_input_components_and_labels_provenance() -> None:
    collector = TokenTelemetryCollector()
    usage = collector.finalize(
        prompt="a" * 8,
        response="b" * 8,
        system="c" * 4,
        context="d" * 4,
        tools="e" * 4,
    )

    assert usage.quality is TokenCountQuality.ESTIMATED
    assert usage.input_tokens == 5
    assert usage.output_tokens == 2
    assert usage.total_tokens == 7
    contributors = {item.component: item for item in usage.contributors}
    assert contributors["user"].tokens == 2
    assert contributors["system"].tokens == 1
    assert contributors["context"].tokens == 1
    assert contributors["tools"].tokens == 1
    assert contributors["auth.json"].quality is TokenCountQuality.UNAVAILABLE
    assert contributors["hidden_codex_context"].tokens is None


def test_exact_aggregate_keeps_component_estimates_visibly_distinct() -> None:
    collector = TokenTelemetryCollector()
    collector.observe_event(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 30, "output_tokens": 10, "reasoning_output_tokens": 3},
        }
    )
    usage = collector.finalize(prompt="hello", response="answer", system="policy")

    assert usage.quality is TokenCountQuality.EXACT
    assert usage.total_tokens == 40
    contributors = {item.component: item for item in usage.contributors}
    assert contributors["input"].quality is TokenCountQuality.EXACT
    assert contributors["user"].quality is TokenCountQuality.ESTIMATED
    assert contributors["context"].quality is TokenCountQuality.UNAVAILABLE
    assert contributors["auth.json"].quality is TokenCountQuality.UNAVAILABLE


def test_no_observations_are_unavailable() -> None:
    usage = TokenTelemetryCollector().finalize()

    assert usage.quality is TokenCountQuality.UNAVAILABLE
    assert usage.total_tokens is None
    assert {item.component for item in usage.contributors} >= {
        "user",
        "system",
        "context",
        "tools",
        "auth.json",
    }


def test_utf8_estimate_is_documented_bytes_over_four() -> None:
    assert estimate_tokens("12345") == 2
    assert estimate_tokens("é") == 1
    assert estimate_tokens("") == 0


def test_prometheus_output_uses_only_bounded_labels() -> None:
    metrics = DashboardMetrics()
    usage = TokenTelemetryCollector().finalize(prompt="hello", response="world")
    metrics.observe_token_usage(usage)
    metrics.observe_http("/dashboard/jobs/{job_id}", "get", 200, 0.01)

    payload, content_type = metrics.render()

    assert b"remoteagent_tokens_total" in payload
    assert b"/dashboard/jobs/{job_id}" in payload
    assert "text/plain" in content_type
