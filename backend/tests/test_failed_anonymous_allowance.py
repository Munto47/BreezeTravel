"""Failed text requests do not exhaust the anonymous result allowance."""

from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.anonymous import AnonymousDailyLimitError
from app.trip_understanding.errors import ConcurrentJobLimitError
from tests.test_experience_v3_journey import create, finish, repository_for


async def fail(repo, created, now, *, legacy=False, retry=False):
    job = await repo.claim_next(worker_id="allowance-test", now=now, lease_seconds=60)
    assert job is not None
    if legacy:
        # Simulate a terminal record written before this fix, without running
        # the new failure callback or editing any real application database.
        if hasattr(repo, "resources"):
            repo.resources[created.accepted.public_resource_id]["state"] = "FAILED"
            repo.jobs[job.job_id]["status"] = "FAILED"
        else:
            async with repo._pool.acquire() as conn:
                await conn.execute("UPDATE trip_understandings SET state='FAILED' WHERE understanding_id=$1", job.understanding_id)
                await conn.execute("UPDATE trip_understanding_jobs SET status='FAILED' WHERE job_id=$1", job.job_id)
    else:
        await repo.fail_job(job, category="INFERENCE_INVALID", now=now, allow_retry=retry)
    return await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)


async def delete(repo, resource, now):
    args = dict(capability_hash="a" * 64, user_id=None, idempotency_key="delete",
                request_hash="d" * 64, now=now)
    await repo.delete_trip(resource, **args)
    assert (await repo.delete_trip(resource, **args)).replayed
    assert await repo.tombstone_reason(resource.public_resource_id) == "DELETED"


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.asyncio
async def test_three_failed_attempts_leave_three_successful_results_available(kind, legacy):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        for number in range(3):
            created = await create(repo, f"failed-{number}", now)
            resource = await fail(repo, created, now, legacy=legacy)
            assert resource.state == "FAILED"
            assert (await create(repo, f"failed-{number}", now)).replayed
        for number in range(3):
            await finish(repo, await create(repo, f"success-{number}", now), now)
        with pytest.raises(AnonymousDailyLimitError):
            await create(repo, "fourth-success", now)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_pending_and_retry_queued_attempts_still_prevent_concurrency(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "pending", now)
        assert (await create(repo, "pending", now)).replayed
        with pytest.raises(ConcurrentJobLimitError):
            await create(repo, "overlap", now)
        await fail(repo, created, now, retry=True)
        with pytest.raises(ConcurrentJobLimitError):
            await create(repo, "queued-overlap", now)
        await fail(repo, created, now + timedelta(seconds=3))
        await create(repo, "after-terminal", now + timedelta(seconds=3))


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.asyncio
async def test_deleted_failures_neither_charge_nor_reset_successful_allowance(kind, legacy):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        for number in range(2):
            success = await finish(repo, await create(repo, f"success-{number}", now), now)
            await delete(repo, success, now)
        for number in range(3):
            failed = await fail(repo, await create(repo, f"failed-{number}", now), now, legacy=legacy)
            await delete(repo, failed, now)
        await finish(repo, await create(repo, "third-success", now), now)
        with pytest.raises(AnonymousDailyLimitError):
            await create(repo, "fourth-success", now)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("terminal", ["READY", "PROCESSING"])
@pytest.mark.asyncio
async def test_deleting_all_charged_requests_cannot_reset_daily_allowance(kind, terminal):
    async with repository_for(kind) as repo:
        now = datetime(2026, 9, 7, 4, tzinfo=timezone.utc)
        for number in range(3):
            created = await create(repo, f"charged-{number}", now)
            resource = (await finish(repo, created, now) if terminal == "READY"
                        else await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now))
            await delete(repo, resource, now)
            if kind == "postgres":
                async with repo._pool.acquire() as conn:
                    rows = await conn.fetch("SELECT request_hash,response_json,response_headers_json FROM trip_understanding_idempotency_records WHERE scope LIKE 'anonymous:%:allowance'")
                    assert len(rows) == number + 1
                    assert all(resource.public_resource_id not in str(row) for row in rows)
                    assert all(str(row["response_json"]) == "{}" for row in rows)
        with pytest.raises(AnonymousDailyLimitError):
            await create(repo, "fourth", now)
        # Calendar-day allowance remains independent from the rolling session TTL.
        await create(repo, "tomorrow", now + timedelta(hours=23))


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_claimed_then_deleted_success_still_consumes_anonymous_allowance(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        first = await finish(repo, await create(repo, "first", now), now)
        claimed = await repo.claim_demo(first.public_resource_id, capability_hash="a" * 64,
            user_id="experience-owner", idempotency_key="claim", request_hash="b" * 64,
            now=now, retention_days=30)
        owned = await repo.authorize(claimed.claimed.public_resource_id, capability_hash=None,
            user_id="experience-owner", now=now)
        await repo.delete_trip(owned, capability_hash=None, user_id="experience-owner",
            idempotency_key="delete-owned", request_hash="d" * 64, now=now)
        for number in range(2):
            await finish(repo, await create(repo, f"next-{number}", now), now)
        with pytest.raises(AnonymousDailyLimitError):
            await create(repo, "fourth", now)
