from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from .mcp_client import RouterMCPError
from .scheduling import CronValidationError
from .schemas import (
    AcknowledgeResponsesRequest,
    AcknowledgeResponsesResult,
    ConfigureScheduleRequest,
    DeleteScheduleResult,
    LeaseResponsesRequest,
    ReadinessView,
    ResponseLease,
    ScheduleId,
    ScheduleView,
    SetScheduleEnabledRequest,
)
from .service import (
    ExecutionNotFoundError,
    LeaseNotFoundError,
    ScheduleConflictError,
    ScheduleNotFoundError,
    ScheduleValidationError,
)


def _service(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.container.service


def build_api_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health", include_in_schema=False)
    @router.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get(
        "/readyz",
        response_model=ReadinessView,
        responses={503: {"model": ReadinessView}},
        include_in_schema=False,
    )
    async def ready(request: Request) -> Any:
        result = await request.app.state.container.readiness()
        if result.status != "ready":
            return JSONResponse(
                status_code=503,
                content=result.model_dump(mode="json", by_alias=True),
            )
        return result

    @router.put(
        "/internal/v1/schedules/{schedule_id}",
        response_model=ScheduleView,
        tags=["Schedules"],
        operation_id="cronScheduleConfigure",
    )
    async def configure_schedule(
        request: Request,
        schedule_id: ScheduleId,
        body: ConfigureScheduleRequest,
    ) -> ScheduleView:
        try:
            return await _service(request).configure(schedule_id, body)
        except ScheduleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except CronValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ScheduleConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RouterMCPError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get(
        "/internal/v1/schedules",
        response_model=list[ScheduleView],
        tags=["Schedules"],
        operation_id="cronSchedulesList",
    )
    async def list_schedules(
        request: Request, include_disabled: bool = False
    ) -> list[ScheduleView]:
        return await _service(request).list(include_disabled=include_disabled)

    @router.get(
        "/internal/v1/schedules/{schedule_id}",
        response_model=ScheduleView,
        tags=["Schedules"],
        operation_id="cronScheduleGet",
    )
    async def get_schedule(request: Request, schedule_id: ScheduleId) -> ScheduleView:
        try:
            return await _service(request).get(schedule_id)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc

    @router.patch(
        "/internal/v1/schedules/{schedule_id}/enabled",
        response_model=ScheduleView,
        tags=["Schedules"],
        operation_id="cronScheduleSetEnabled",
    )
    async def set_schedule_enabled(
        request: Request,
        schedule_id: ScheduleId,
        body: SetScheduleEnabledRequest,
    ) -> ScheduleView:
        try:
            return await _service(request).set_enabled(schedule_id, body.enabled)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        except ScheduleConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.delete(
        "/internal/v1/schedules/{schedule_id}",
        response_model=DeleteScheduleResult,
        tags=["Schedules"],
        operation_id="cronScheduleDelete",
    )
    async def delete_schedule(request: Request, schedule_id: ScheduleId) -> DeleteScheduleResult:
        try:
            return await _service(request).delete(schedule_id)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc

    @router.post(
        "/internal/v1/responses/lease",
        response_model=ResponseLease,
        tags=["Responses"],
        operation_id="cronResponsesLease",
    )
    async def lease_responses(request: Request, body: LeaseResponsesRequest) -> ResponseLease:
        try:
            return await _service(request).lease_responses(body)
        except ScheduleNotFoundError as exc:
            raise HTTPException(status_code=404, detail="schedule not found") from exc
        except ExecutionNotFoundError as exc:
            raise HTTPException(status_code=404, detail="execution not found") from exc

    @router.post(
        "/internal/v1/responses/acknowledge",
        response_model=AcknowledgeResponsesResult,
        tags=["Responses"],
        operation_id="cronResponsesAcknowledge",
    )
    async def acknowledge_responses(
        request: Request, body: AcknowledgeResponsesRequest
    ) -> AcknowledgeResponsesResult:
        try:
            return await _service(request).acknowledge(body.lease_id)
        except LeaseNotFoundError as exc:
            raise HTTPException(status_code=404, detail="lease not found") from exc

    return router
