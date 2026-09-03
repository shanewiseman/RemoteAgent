from __future__ import annotations

import pytest
from sqlalchemy import func, select

from remoteagent_cron.mcp_client import RouterMCPRejectedError, RouterMCPToolError
from remoteagent_cron.models import ExecutionRecord, ResponseRecord


async def _new_execution(service, clock, schedule_body):  # type: ignore[no-untyped-def]
    await service.configure("recovery", schedule_body)
    clock.advance(minutes=1)
    return (await service.dispatch_due())[0]


async def test_ambiguous_submission_reuses_key_past_deadline_then_cancels(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)
    router.fail_after_accept = 1
    assert await service.process_execution_once(execution_id) is not None
    first_key = router.submit_calls[-1]["idempotency_key"]

    clock.advance(seconds=20)
    assert await service.process_execution_once(execution_id) is not None
    assert router.submit_calls[-1]["idempotency_key"] == first_key
    assert len(router.accepted_by_key) == 1

    assert await service.process_execution_once(execution_id) is None
    assert router.cancel_calls == ["job-1"]
    async with service.session_factory() as session:
        execution = await session.get(ExecutionRecord, execution_id)
        assert execution and execution.state == "cancelled"
        assert await session.scalar(select(func.count(ResponseRecord.id))) == 0


async def test_failed_cancel_is_retried_until_terminal(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)
    await service.process_execution_once(execution_id)
    clock.advance(seconds=20)
    router.cancel_failures = 1
    assert await service.process_execution_once(execution_id) is not None
    assert await service.process_execution_once(execution_id) is None
    assert router.cancel_calls == ["job-1", "job-1"]


async def test_successful_nonterminal_cancel_is_requested_once_then_polled(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)
    await service.process_execution_once(execution_id)
    clock.advance(seconds=20)
    router.cancel_completes = False
    assert await service.process_execution_once(execution_id) is not None
    assert router.cancel_calls == ["job-1"]
    assert await service.process_execution_once(execution_id) is not None
    assert router.cancel_calls == ["job-1"]
    router.jobs["job-1"] = router.jobs["job-1"].model_copy(
        update={"status": "cancelled", "completed_at": clock.now()}
    )
    assert await service.process_execution_once(execution_id) is None
    assert router.cancel_calls == ["job-1"]


async def test_explicit_submission_rejection_is_terminal(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)

    async def reject(**_arguments):  # type: ignore[no-untyped-def]
        raise RouterMCPRejectedError("agent_unavailable", "agent is disabled")

    router.submit_prompt = reject
    assert await service.process_execution_once(execution_id) is None
    execution = None
    async with service.session_factory() as session:
        execution = await session.get(ExecutionRecord, execution_id)
    assert execution and execution.state == "failed"
    assert (await service.get("recovery")).active_execution_id is None


async def test_transient_tool_level_submission_error_retries(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)
    original_submit = router.submit_prompt
    calls = 0

    async def fail_once(**arguments):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RouterMCPToolError("router database temporarily unavailable")
        return await original_submit(**arguments)

    router.submit_prompt = fail_once
    delay = await service.process_execution_once(execution_id)
    assert delay == service.settings.retry_initial_seconds
    assert (await service.get("recovery")).active_execution_id == execution_id

    assert await service.process_execution_once(execution_id) is not None
    async with service.session_factory() as session:
        execution = await session.get(ExecutionRecord, execution_id)
    assert execution and execution.state == "polling"
    assert execution.router_job_id == "job-1"


async def test_restart_resumes_stored_job_and_idempotently_commits_one_response(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)
    await service.process_execution_once(execution_id)
    router.jobs["job-1"] = router.jobs["job-1"].model_copy(
        update={"status": "succeeded", "result": "recovered", "completed_at": clock.now()}
    )
    assert await service.recover() == [execution_id]
    await service.process_execution_once(execution_id)
    # A repeated recovery/poll cannot duplicate the response.
    await service.process_execution_once(execution_id)
    async with service.session_factory() as session:
        assert await session.scalar(select(func.count(ResponseRecord.id))) == 1


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled", "interrupted", "expired"])
async def test_all_router_terminal_states_are_recorded_correctly(
    service, router, clock, schedule_body, status: str
) -> None:  # type: ignore[no-untyped-def]
    execution_id = await _new_execution(service, clock, schedule_body)
    await service.process_execution_once(execution_id)
    router.jobs["job-1"] = router.jobs["job-1"].model_copy(
        update={
            "status": status,
            "result": "success text" if status == "succeeded" else None,
            "error": None if status == "succeeded" else f"ended {status}",
            "completed_at": clock.now(),
        }
    )
    assert await service.process_execution_once(execution_id) is None
    async with service.session_factory() as session:
        count = await session.scalar(select(func.count(ResponseRecord.id)))
        execution = await session.get(ExecutionRecord, execution_id)
    assert count == (1 if status == "succeeded" else 0)
    assert execution and execution.state == status
