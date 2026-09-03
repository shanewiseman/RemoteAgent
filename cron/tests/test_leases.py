from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from remoteagent_cron.models import ResponseRecord
from remoteagent_cron.schemas import LeaseResponsesRequest


async def _add_response(service, router, clock, schedule_id, schedule_body, result):  # type: ignore[no-untyped-def]
    if not await service.list(include_disabled=True):
        await service.configure(schedule_id, schedule_body)
    clock.advance(minutes=1)
    execution_id = (await service.dispatch_due())[0]
    await service.process_execution_once(execution_id)
    job_id = next(reversed(router.jobs))
    router.jobs[job_id] = router.jobs[job_id].model_copy(
        update={"status": "succeeded", "result": result, "completed_at": clock.now()}
    )
    await service.process_execution_once(execution_id)
    return execution_id


async def test_schedule_fifo_lease_ack_and_idempotent_ack(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    first = await _add_response(service, router, clock, "daily-report", schedule_body, "first")
    second = await _add_response(service, router, clock, "daily-report", schedule_body, "second")
    lease = await service.lease_responses(
        LeaseResponsesRequest(schedule_id="daily-report", limit=1)
    )
    assert lease.lease_id and [item.execution_id for item in lease.responses] == [first]
    assert lease.more_available is True
    acknowledged = await service.acknowledge(lease.lease_id)
    assert acknowledged.status == "acknowledged" and acknowledged.deleted_count == 1
    repeated = await service.acknowledge(lease.lease_id)
    assert repeated.status == "already_acknowledged" and repeated.deleted_count == 1

    exact = await service.lease_responses(LeaseResponsesRequest(execution_id=second))
    assert len(exact.responses) == 1 and exact.responses[0].result == "second"


async def test_expired_lease_can_be_released_and_stale_ack_cannot_delete(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await _add_response(service, router, clock, "daily-report", schedule_body, "value")
    first = await service.lease_responses(LeaseResponsesRequest(schedule_id="daily-report"))
    assert first.lease_id
    clock.advance(seconds=service.settings.lease_seconds + 1)
    second = await service.lease_responses(LeaseResponsesRequest(schedule_id="daily-report"))
    assert second.lease_id and second.lease_id != first.lease_id
    stale = await service.acknowledge(first.lease_id)
    assert stale.status == "stale"
    async with service.session_factory() as session:
        assert await session.scalar(select(func.count(ResponseRecord.id))) == 1
    acknowledged = await service.acknowledge(second.lease_id)
    assert acknowledged.deleted_count == 1


async def test_empty_lease_and_oversized_single_response_are_not_stuck(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await service.configure("daily-report", schedule_body)
    empty = await service.lease_responses(LeaseResponsesRequest(schedule_id="daily-report"))
    assert empty.lease_id is None and empty.responses == []
    service.settings.response_batch_bytes = 1024
    await _add_response(service, router, clock, "daily-report", schedule_body, "x" * 5000)
    lease = await service.lease_responses(LeaseResponsesRequest(schedule_id="daily-report"))
    assert len(lease.responses) == 1 and len(lease.responses[0].result) == 5000


async def test_response_ttl_honors_active_lease(service, router, clock, schedule_body) -> None:  # type: ignore[no-untyped-def]
    await _add_response(service, router, clock, "daily-report", schedule_body, "value")
    lease = await service.lease_responses(LeaseResponsesRequest(schedule_id="daily-report"))
    assert lease.lease_id
    service.settings.response_retention_seconds = 60
    clock.advance(seconds=61)
    await service.cleanup()
    async with service.session_factory() as session:
        assert await session.scalar(select(func.count(ResponseRecord.id))) == 1
    clock.advance(seconds=service.settings.lease_seconds)
    await service.cleanup()
    async with service.session_factory() as session:
        assert await session.scalar(select(func.count(ResponseRecord.id))) == 0


async def test_byte_cap_splits_multiple_responses(service, router, clock, schedule_body) -> None:  # type: ignore[no-untyped-def]
    await _add_response(service, router, clock, "daily-report", schedule_body, "a" * 700)
    await _add_response(service, router, clock, "daily-report", schedule_body, "b" * 700)
    service.settings.response_batch_bytes = 1200
    lease = await service.lease_responses(
        LeaseResponsesRequest(schedule_id="daily-report", limit=10)
    )
    assert len(lease.responses) == 1
    assert lease.more_available is True


async def test_concurrent_leases_never_return_the_same_response(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await _add_response(service, router, clock, "daily-report", schedule_body, "one")
    await _add_response(service, router, clock, "daily-report", schedule_body, "two")
    request = LeaseResponsesRequest(schedule_id="daily-report", limit=1)
    first, second = await asyncio.gather(
        service.lease_responses(request), service.lease_responses(request)
    )
    first_ids = {item.response_id for item in first.responses}
    second_ids = {item.response_id for item in second.responses}
    assert len(first_ids) == len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)


async def test_concurrent_acknowledgements_are_idempotent_with_stable_count(
    service, router, clock, schedule_body
) -> None:  # type: ignore[no-untyped-def]
    await _add_response(service, router, clock, "daily-report", schedule_body, "value")
    lease = await service.lease_responses(LeaseResponsesRequest(schedule_id="daily-report"))
    assert lease.lease_id is not None
    first, second = await asyncio.gather(
        service.acknowledge(lease.lease_id),
        service.acknowledge(lease.lease_id),
    )
    assert {first.status, second.status} == {
        "acknowledged",
        "already_acknowledged",
    }
    assert first.deleted_count == second.deleted_count == 1
    async with service.session_factory() as session:
        assert await session.scalar(select(func.count(ResponseRecord.id))) == 0
