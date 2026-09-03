"""Low-cardinality Prometheus metrics and Codex token-usage telemetry.

Prometheus is an optional import at module-import time so unit tests and
administrative tooling can still inspect token records before runtime
dependencies have been installed. The router image is expected to include
``prometheus-client``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

try:  # pragma: no cover - the no-op path is exercised only in minimal tooling
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
except ImportError:  # pragma: no cover
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    CollectorRegistry = Counter = Gauge = Histogram = None  # type: ignore[assignment]

    def generate_latest(_registry: object | None = None) -> bytes:
        return b""


class TokenCountQuality(str, Enum):
    """Whether token counts came from Codex or a local heuristic."""

    EXACT = "exact"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class TokenContributor:
    """Attribution for a visible or hidden component of a turn."""

    component: str
    tokens: int | None
    quality: TokenCountQuality
    provenance: str


@dataclass(frozen=True, slots=True)
class TokenUsageRecord:
    """Versioned, persistence-safe token accounting for one completed turn."""

    schema_version: int
    quality: TokenCountQuality
    source: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None
    estimation_method: str | None
    observed_at: str
    contributors: tuple[TokenContributor, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["quality"] = self.quality.value
        result["contributors"] = [
            {**asdict(item), "quality": item.quality.value} for item in self.contributors
        ]
        return result


def _non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def estimate_tokens(text: str | bytes | None) -> int:
    """Estimate tokens using a documented UTF-8 bytes/4 heuristic.

    This intentionally never masquerades as an exact tokenizer result. It is
    useful for trend telemetry when older or failed Codex events omit usage.
    """

    if text is None:
        return 0
    raw = text if isinstance(text, bytes) else text.encode("utf-8", errors="replace")
    return math.ceil(len(raw) / 4) if raw else 0


def exact_usage_from_event(event: Mapping[str, Any]) -> TokenUsageRecord | None:
    """Return exact usage only for a terminal Codex event with usable counts."""

    event_type = str(event.get("type", ""))
    if event_type not in {"turn.completed", "turn_completed"}:
        return None
    usage = event.get("usage")
    if not isinstance(usage, Mapping):
        return None

    input_tokens = _non_negative_int(usage.get("input_tokens"))
    output_tokens = _non_negative_int(usage.get("output_tokens"))
    if input_tokens is None and output_tokens is None:
        return None
    cached = _non_negative_int(usage.get("cached_input_tokens")) or 0
    reasoning = _non_negative_int(usage.get("reasoning_output_tokens")) or 0
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    total = _non_negative_int(usage.get("total_tokens"))
    if total is None:
        # Codex reports reasoning_output_tokens as a component of output_tokens,
        # not an additional class of billed/generated tokens.
        total = input_tokens + output_tokens

    return TokenUsageRecord(
        schema_version=1,
        quality=TokenCountQuality.EXACT,
        source="codex.turn.completed",
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
        total_tokens=total,
        estimation_method=None,
        observed_at=datetime.now(UTC).isoformat(),
        contributors=(
            TokenContributor(
                "input", input_tokens, TokenCountQuality.EXACT, "codex.turn.completed.usage"
            ),
            TokenContributor(
                "output", output_tokens, TokenCountQuality.EXACT, "codex.turn.completed.usage"
            ),
            TokenContributor(
                "hidden_codex_context",
                None,
                TokenCountQuality.UNAVAILABLE,
                "codex does not expose per-component attribution",
            ),
        ),
    )


class TokenTelemetryCollector:
    """Prefer exact terminal events, falling back to clearly marked estimates."""

    def __init__(self) -> None:
        self._exact: TokenUsageRecord | None = None

    def observe_event(self, event: Mapping[str, Any]) -> TokenUsageRecord | None:
        exact = exact_usage_from_event(event)
        if exact is not None:
            self._exact = exact
        return exact

    def finalize(
        self,
        *,
        prompt: str | bytes | None = None,
        response: str | bytes | None = None,
        system: str | bytes | None = None,
        context: str | bytes | None = None,
        tools: str | bytes | None = None,
    ) -> TokenUsageRecord:
        visible_values = (
            ("user", prompt, "caller.user_prompt.utf8_bytes"),
            ("system", system, "agent.system_context.utf8_bytes"),
            ("context", context, "conversation.visible_context.utf8_bytes"),
            ("tools", tools, "tool.visible_io.utf8_bytes"),
        )
        visible_contributors = tuple(
            TokenContributor(
                component,
                estimate_tokens(value) if value is not None else None,
                TokenCountQuality.ESTIMATED if value is not None else TokenCountQuality.UNAVAILABLE,
                provenance if value is not None else f"{provenance}.not_observed",
            )
            for component, value, provenance in visible_values
        )
        hidden = TokenContributor(
            "hidden_codex_context",
            None,
            TokenCountQuality.UNAVAILABLE,
            "Codex internal instructions/cache attribution unavailable",
        )
        credentials = TokenContributor(
            "auth.json",
            None,
            TokenCountQuality.UNAVAILABLE,
            "subscription credentials are mounted but never inspected or tokenized",
        )
        if self._exact is not None:
            exact = self._exact
            return TokenUsageRecord(
                schema_version=exact.schema_version,
                quality=exact.quality,
                source=exact.source,
                input_tokens=exact.input_tokens,
                cached_input_tokens=exact.cached_input_tokens,
                output_tokens=exact.output_tokens,
                reasoning_output_tokens=exact.reasoning_output_tokens,
                total_tokens=exact.total_tokens,
                estimation_method=exact.estimation_method,
                observed_at=exact.observed_at,
                contributors=visible_contributors + exact.contributors + (credentials,),
            )
        input_tokens = sum(
            estimate_tokens(value)
            for _component, value, _provenance in visible_values
            if value is not None
        )
        output_tokens = estimate_tokens(response)
        if (
            all(value is None for _component, value, _provenance in visible_values)
            and response is None
        ):
            return TokenUsageRecord(
                schema_version=1,
                quality=TokenCountQuality.UNAVAILABLE,
                source="unavailable",
                input_tokens=None,
                cached_input_tokens=None,
                output_tokens=None,
                reasoning_output_tokens=None,
                total_tokens=None,
                estimation_method=None,
                observed_at=datetime.now(UTC).isoformat(),
                contributors=visible_contributors + (hidden, credentials),
            )
        return TokenUsageRecord(
            schema_version=1,
            quality=TokenCountQuality.ESTIMATED,
            source="text.utf8_bytes",
            input_tokens=input_tokens,
            cached_input_tokens=0,
            output_tokens=output_tokens,
            reasoning_output_tokens=0,
            total_tokens=input_tokens + output_tokens,
            estimation_method="ceil(utf8_bytes/4)",
            observed_at=datetime.now(UTC).isoformat(),
            contributors=visible_contributors + (hidden, credentials),
        )


class DashboardMetrics:
    """Prometheus instruments shared by the router and dashboard.

    No metric accepts agent, task, conversation, artifact, filename, IP, or
    error-text labels; those values are unbounded and belong in structured
    logs or PostgreSQL.
    """

    def __init__(self, registry: Any | None = None) -> None:
        self.enabled = CollectorRegistry is not None
        self.registry = registry or (
            CollectorRegistry(auto_describe=True) if self.enabled else None
        )
        if not self.enabled:
            return
        self.http_requests = Counter(
            "remoteagent_http_requests_total",
            "Router HTTP requests.",
            ("route", "method", "status"),
            registry=self.registry,
        )
        self.http_duration = Histogram(
            "remoteagent_http_request_duration_seconds",
            "Router HTTP request latency.",
            ("route", "method"),
            registry=self.registry,
        )
        self.auth_failures = Counter(
            "remoteagent_auth_failures_total",
            "Authentication failures by surface.",
            ("surface",),
            registry=self.registry,
        )
        self.sse_connections = Gauge(
            "remoteagent_sse_connections",
            "Current dashboard SSE connections.",
            registry=self.registry,
        )
        self.sse_events = Counter(
            "remoteagent_sse_events_total",
            "Dashboard SSE events by bounded event type.",
            ("type",),
            registry=self.registry,
        )
        self.sse_dropped = Counter(
            "remoteagent_sse_dropped_connections_total",
            "Dropped dashboard SSE connections.",
            ("reason",),
            registry=self.registry,
        )
        self.cache_requests = Counter(
            "remoteagent_cache_requests_total",
            "Cache requests by bounded cache and result.",
            ("cache", "result"),
            registry=self.registry,
        )
        self.tokens = Counter(
            "remoteagent_tokens_total",
            "Exact or estimated Codex tokens.",
            ("direction", "quality"),
            registry=self.registry,
        )
        self.token_observations = Counter(
            "remoteagent_token_observations_total",
            "Token observations by quality and bounded source.",
            ("quality", "source"),
            registry=self.registry,
        )
        self.companion_stage_events = Counter(
            "remoteagent_companion_stage_events_total",
            "Companion staging lifecycle observations by bounded kind and state.",
            ("kind", "status"),
            registry=self.registry,
        )
        self.companion_stage_bytes = Counter(
            "remoteagent_companion_stage_bytes_total",
            "Companion bytes observed at staging lifecycle boundaries.",
            ("kind", "status"),
            registry=self.registry,
        )
        self.companion_stage_duration = Histogram(
            "remoteagent_companion_stage_duration_seconds",
            "Companion staging duration by bounded kind and terminal state.",
            ("kind", "status"),
            registry=self.registry,
        )

    def observe_http(self, route: str, method: str, status: int, duration: float) -> None:
        if not self.enabled:
            return
        route = route if route.startswith("/") else "unknown"
        method = method.upper()
        if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}:
            method = "OTHER"
        status = status if 100 <= status <= 599 else 500
        self.http_requests.labels(route=route, method=method, status=str(status)).inc()
        self.http_duration.labels(route=route, method=method).observe(max(0.0, duration))

    def observe_token_usage(self, usage: TokenUsageRecord) -> None:
        if not self.enabled:
            return
        quality = usage.quality.value
        source = (
            usage.source
            if usage.source in {"codex.turn.completed", "text.utf8_bytes", "unavailable"}
            else "other"
        )
        self.token_observations.labels(quality=quality, source=source).inc()
        for direction, value in (
            ("input", usage.input_tokens),
            ("cached_input", usage.cached_input_tokens),
            ("output", usage.output_tokens),
            ("reasoning_output", usage.reasoning_output_tokens),
        ):
            if value is not None:
                self.tokens.labels(direction=direction, quality=quality).inc(value)

    def observe_companion_stage(
        self,
        kind: str,
        status: str,
        *,
        size_bytes: int | None = None,
        duration_seconds: float | None = None,
    ) -> None:
        """Record staging work without admitting caller-controlled labels.

        Stage IDs, companion names, source URLs, Git refs, job IDs, and
        conversation keys are intentionally absent. Unknown values collapse to
        ``other`` so future enum additions cannot create unbounded label sets.
        """

        if not self.enabled:
            return
        safe_kind = kind if kind in {"file", "archive", "git"} else "other"
        safe_status = (
            status
            if status in {"queued", "importing", "ready", "failed", "claimed", "expired"}
            else "other"
        )
        self.companion_stage_events.labels(kind=safe_kind, status=safe_status).inc()
        if size_bytes is not None:
            try:
                observed_bytes = max(0, int(size_bytes))
            except (TypeError, ValueError, OverflowError):
                observed_bytes = 0
            self.companion_stage_bytes.labels(kind=safe_kind, status=safe_status).inc(
                observed_bytes
            )
        if duration_seconds is not None:
            try:
                observed_duration = max(0.0, float(duration_seconds))
            except (TypeError, ValueError, OverflowError):
                observed_duration = 0.0
            if not math.isfinite(observed_duration):
                observed_duration = 0.0
            self.companion_stage_duration.labels(
                kind=safe_kind, status=safe_status
            ).observe(observed_duration)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST


class HTTPMetricsMiddleware:
    """Small ASGI middleware that records route-template latency/counts."""

    def __init__(
        self,
        app: Any,
        metrics: DashboardMetrics,
        metrics_resolver: Callable[[], DashboardMetrics | None] | None = None,
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.metrics_resolver = metrics_resolver

    async def __call__(self, scope: MutableMapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: MutableMapping[str, Any]) -> None:
            nonlocal status
            if message.get("type") == "http.response.start":
                status = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route_obj = scope.get("route")
            route = getattr(route_obj, "path", None) or "unmatched"
            metrics = self.metrics_resolver() if self.metrics_resolver is not None else self.metrics
            if metrics is not None:
                metrics.observe_http(
                    route,
                    str(scope.get("method", "UNKNOWN")),
                    status,
                    time.perf_counter() - started,
                )


__all__ = [
    "DashboardMetrics",
    "HTTPMetricsMiddleware",
    "TokenContributor",
    "TokenCountQuality",
    "TokenTelemetryCollector",
    "TokenUsageRecord",
    "estimate_tokens",
    "exact_usage_from_event",
]
