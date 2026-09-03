from __future__ import annotations

import asyncio
import pytest
from sqlalchemy import func, select

from remoteagent_cron.models import (
    ExecutionRecord,
    ResponseRecord,
    ScheduleRecord,
    ScheduleRevisionRecord,
)
from remoteagent_cron.service import ScheduleNotFoundError, ScheduleValidationError


async def _dispatch(service, clock):  # type: ignore[no-untyped-def]
    clock.advance(minutes=1)
    values = await service.dispatch_due()
    assert len(values) == 1
    return values[0]


async def _succeed(service, router, clock, execution_id: str, result: str = "done") -> None:  # type: ignore[no-untyped-def]
    assert await service.process_execution_once(execution_id) is not None
    job_id = next(reversed(router.jobs))
    router.jobs[job_id] = router.jobs[job_id].model_copy(
        update={
            "status": "succeeded",
            "result": result,
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 4,
                "reasoning_output_tokens": 1,
            },
            "completed_at": clock.now(),
        }
    )
    assert await service.process_execution_once(execution_id) is None


async def test_configure_is_idempotent_and_updates_immutable_revisions(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    first = await service.configure("daily-report", schedule_body)
    repeated = await service.configure("daily-report", schedule_body)
    assert repeated.revision == 1
    assert repeated.revision_id == first.revision_id

    changed = await service.configure(
        "daily-report", schedule_body.model_copy(update={"prompt": "New report"})
    )
    assert changed.revision == 2
    async with service.session_factory() as session:
        revisions = await session.scalar(select(func.count(ScheduleRevisionRecord.id)))
    assert revisions == 2


async def test_concurrent_updates_append_distinct_revisions(service, schedule_body) -> None:  # type: ignore[no-untyped-def]
    await service.configure("daily-report", schedule_body)
    first, second = await asyncio.gather(
        service.configure(
            "daily-report", schedule_body.model_copy(update={"prompt": "first update"})
        ),
        service.configure(
            "daily-report", schedule_body.model_copy(update={"prompt": "second update"})
        ),
    )
    assert {first.revision, second.revision} == {2, 3}
    current = await service.get("daily-report")
    assert current.revision == 3


async def test_disabled_agent_is_rejected(service, router, schedule_body) -> None:  # type: ignore[no-untyped-def]
    router.enabled_agents.clear()
    with pytest.raises(ScheduleValidationError):
        await service.configure("daily-report", schedule_body)


async def test_pause_resume_and_startup_recovery_skip_misfires(
    service, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await service.configure("daily-report", schedule_body)
    paused = await service.set_enabled("daily-report", False)
    assert paused.status == "disabled" and paused.next_fire_at is None
    clock.advance(days=3)
    resumed = await service.set_enabled("daily-report", True)
    assert resumed.next_fire_at > clock.now()
    clock.advance(days=1)
    await service.recover()
    recovered = await service.get("daily-report")
    assert recovered.next_fire_at > clock.now()
    assert recovered.last_execution_id is None


async def test_overlap_is_skipped_instead_of_queued(service, clock, schedule_body) -> None:  # type: ignore[no-untyped-def]
    await service.configure("daily-report", schedule_body)
    execution_id = await _dispatch(service, clock)
    clock.advance(minutes=1)
    assert await service.dispatch_due() == []
    view = await service.get("daily-report")
    assert view.active_execution_id == execution_id
    assert view.skipped_occurrences == 1
    async with service.session_factory() as session:
        assert await session.scalar(select(func.count(ExecutionRecord.id))) == 1


async def test_persistent_conversation_survives_prompt_timing_changes_but_not_profile_changes(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    persistent = schedule_body.model_copy(update={"conversation_mode": "persistent"})
    await service.configure("daily-report", persistent)
    first_execution = await _dispatch(service, clock)
    await _succeed(service, router, clock, first_execution)

    changed_prompt = persistent.model_copy(
        update={"prompt": "Changed", "cron_expression": "*/2 * * * *"}
    )
    await service.configure("daily-report", changed_prompt)
    clock.advance(minutes=2)
    second_execution = (await service.dispatch_due())[0]
    await service.process_execution_once(second_execution)
    assert router.submit_calls[-1]["conversation_key"] == "conversation-1"
    router.jobs["job-2"] = router.jobs["job-2"].model_copy(
        update={"status": "failed", "error": "expected", "completed_at": clock.now()}
    )
    await service.process_execution_once(second_execution)

    changed_profile = changed_prompt.model_copy(update={"model": "gpt-5.6-sol"})
    await service.configure("daily-report", changed_profile)
    clock.advance(minutes=2)
    third_execution = (await service.dispatch_due())[0]
    await service.process_execution_once(third_execution)
    assert router.submit_calls[-1]["conversation_key"] is None


async def test_stale_identity_map_cannot_restore_cleared_persistent_conversation(
    service, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    original = schedule_body.model_copy(update={"conversation_mode": "persistent"})
    await service.configure("daily-report", original)
    async with service.session_factory() as stale_session:
        stale_schedule = await stale_session.get(ScheduleRecord, "daily-report")
        assert stale_schedule is not None
        old_revision = await stale_session.get(
            ScheduleRevisionRecord, stale_schedule.current_revision_id
        )
        assert old_revision is not None
        await stale_session.commit()

        await service.configure(
            "daily-report", original.model_copy(update={"model": "gpt-5.6-sol"})
        )
        await service._capture_persistent_conversation(
            stale_session, stale_schedule, old_revision, "stale-conversation"
        )
        await stale_session.commit()

    async with service.session_factory() as session:
        current = await session.get(ScheduleRecord, "daily-report")
        assert current is not None
        assert current.persistent_conversation_key is None


async def test_success_creates_response_and_failure_only_records_metadata(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await service.configure("daily-report", schedule_body)
    execution_id = await _dispatch(service, clock)
    await _succeed(service, router, clock, execution_id, "complete text")
    view = await service.get("daily-report")
    assert view.pending_response_count == 1
    async with service.session_factory() as session:
        response = (await session.execute(select(ResponseRecord))).scalar_one()
        assert response.result == "complete text"

    clock.advance(minutes=1)
    failed_execution = (await service.dispatch_due())[0]
    await service.process_execution_once(failed_execution)
    router.jobs["job-2"] = router.jobs["job-2"].model_copy(
        update={"status": "failed", "error": "boom", "completed_at": clock.now()}
    )
    await service.process_execution_once(failed_execution)
    view = await service.get("daily-report")
    assert view.pending_response_count == 1
    assert view.last_failure and view.last_failure.error == "boom"


async def test_delete_waits_for_active_execution_then_cascades(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await service.configure("daily-report", schedule_body)
    execution_id = await _dispatch(service, clock)
    result = await service.delete("daily-report")
    assert result.status == "deleting"
    await _succeed(service, router, clock, execution_id)
    with pytest.raises(ScheduleNotFoundError):
        await service.get("daily-report")

    recreated = await service.configure("daily-report", schedule_body)
    assert recreated.generation_id


async def test_schedule_id_reuse_gets_a_new_generation(service, schedule_body) -> None:  # type: ignore[no-untyped-def]
    original = await service.configure("reusable", schedule_body)
    deleted = await service.delete("reusable")
    assert deleted.status == "deleted"
    recreated = await service.configure("reusable", schedule_body)
    assert recreated.generation_id != original.generation_id
