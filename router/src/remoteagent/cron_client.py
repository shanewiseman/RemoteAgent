from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from pydantic import TypeAdapter, ValidationError

from .schemas import (
    CronResponseAcknowledgement,
    CronResponseLease,
    CronResponseLeaseRequest,
    CronScheduleConfiguration,
    CronScheduleDeleteResult,
    CronScheduleEnabledUpdate,
    CronScheduleView,
    CronServiceReadiness,
)


class CronServiceError(RuntimeError):
    """Base error returned to MCP callers when the cron service rejects a request."""


class CronServiceUnavailableError(CronServiceError):
    """The private cron service could not be reached or returned an invalid response."""


class CronServiceClient:
    """Typed asynchronous client for the cron service's private HTTP API."""

    def __init__(
        self,
        base_url: str,
        bearer_token: str | None,
        *,
        timeout_seconds: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        suffix = "/internal/v1"
        if normalized.endswith(suffix):
            self._root_url = normalized[: -len(suffix)]
            self._api_url = normalized
        else:
            self._root_url = normalized
            self._api_url = f"{normalized}{suffix}"
        self._bearer_token = bearer_token
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def readiness(self) -> dict[str, Any]:
        value = await self._request(
            "GET", f"{self._root_url}/readyz", accepted_error_statuses=frozenset({503})
        )
        readiness = self._validate(CronServiceReadiness, value)
        return readiness.model_dump(mode="json", by_alias=True)

    async def configure_schedule(
        self, schedule_id: str, configuration: CronScheduleConfiguration
    ) -> CronScheduleView:
        value = await self._request(
            "PUT",
            self._path(f"/schedules/{quote(schedule_id, safe='')}"),
            json=configuration.model_dump(mode="json"),
        )
        return self._validate(CronScheduleView, value)

    async def list_schedules(self, *, include_disabled: bool = False) -> list[CronScheduleView]:
        value = await self._request(
            "GET",
            self._path("/schedules"),
            params={"include_disabled": str(include_disabled).lower()},
        )
        try:
            return TypeAdapter(list[CronScheduleView]).validate_python(value)
        except ValidationError as exc:
            raise CronServiceUnavailableError(
                "cron service returned an invalid schedule list"
            ) from exc

    async def get_schedule(self, schedule_id: str) -> CronScheduleView:
        value = await self._request("GET", self._path(f"/schedules/{quote(schedule_id, safe='')}"))
        return self._validate(CronScheduleView, value)

    async def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> CronScheduleView:
        value = await self._request(
            "PATCH",
            self._path(f"/schedules/{quote(schedule_id, safe='')}/enabled"),
            json=CronScheduleEnabledUpdate(enabled=enabled).model_dump(mode="json"),
        )
        return self._validate(CronScheduleView, value)

    async def delete_schedule(self, schedule_id: str) -> CronScheduleDeleteResult:
        value = await self._request(
            "DELETE", self._path(f"/schedules/{quote(schedule_id, safe='')}")
        )
        return self._validate(CronScheduleDeleteResult, value)

    async def lease_responses(self, request: CronResponseLeaseRequest) -> CronResponseLease:
        value = await self._request(
            "POST",
            self._path("/responses/lease"),
            json=request.model_dump(mode="json"),
        )
        return self._validate(CronResponseLease, value)

    async def acknowledge_responses(self, lease_id: str) -> CronResponseAcknowledgement:
        value = await self._request(
            "POST",
            self._path("/responses/acknowledge"),
            json={"lease_id": lease_id},
        )
        return self._validate(CronResponseAcknowledgement, value)

    def _path(self, path: str) -> str:
        return f"{self._api_url}{path}"

    async def _request(
        self,
        method: str,
        url: str,
        *,
        accepted_error_statuses: frozenset[int] = frozenset(),
        **kwargs: Any,
    ) -> Any:
        if not self._bearer_token:
            raise CronServiceUnavailableError("cron API bearer token is not configured")
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._bearer_token}"
        headers.setdefault("Accept", "application/json")
        try:
            response = await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise CronServiceUnavailableError("cron service is unavailable") from exc
        try:
            value = response.json()
        except ValueError as exc:
            raise CronServiceUnavailableError("cron service returned invalid JSON") from exc
        if response.is_error and response.status_code not in accepted_error_statuses:
            detail = value.get("detail") if isinstance(value, dict) else None
            message = str(detail) if detail else f"HTTP {response.status_code}"
            raise CronServiceError(f"cron service request failed: {message}")
        return value

    @staticmethod
    def _validate(model: type[Any], value: Any) -> Any:
        try:
            return model.model_validate(value)
        except ValidationError as exc:
            raise CronServiceUnavailableError(
                f"cron service returned an invalid {model.__name__}"
            ) from exc
