"""Repeatable real-storage races with fixed external responses, no model spend."""
import asyncio
from datetime import datetime, timezone
import os

import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import IdempotencyConflictError, JobLeaseLostError, ResourceAccessDeniedError, ResourceGoneError
from app.trip_understanding.models import ActivityTextEditCommand, PipelineProgressUpdate, TripUnderstandingProgressMetrics
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import create, repository_for


ROUNDS = range(int(os.getenv("TRIPCHECK_RELIABILITY_ROUNDS", "1")))


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
async def test_concurrent_identical_creation_persists_one_job_and_replays_lost_response(round_number):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        replies = await asyncio.gather(*(create(repo, "same-create", now) for _ in range(8)))
        assert len({reply.accepted.public_resource_id for reply in replies}) == 1
        assert sum(not reply.replayed for reply in replies) == 1
        # Discard the first response and retry the request as after a disconnect.
        retry = await create(repo, "same-create", now)
        assert retry.replayed and retry.accepted == replies[0].accepted
        for table in ("trip_understandings", "trip_understanding_jobs", "trip_understanding_sources"):
            assert await repo._pool.fetchval(f"SELECT count(*) FROM {table}") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
async def test_same_creation_key_with_different_body_refuses_without_extra_job(round_number):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        first = await create(repo, "body-conflict", now)
        with pytest.raises(IdempotencyConflictError):
            await repo.create_demo(capability_hash="a" * 64, source_text="Day1：去外滩。",
                idempotency_key="body-conflict", request_hash="b" * 64, now=now, ttl_hours=24)
        assert (await create(repo, "body-conflict", now)).accepted == first.accepted
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_jobs") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("successor", [None, "new-worker", "same-worker"])
async def test_expired_attempt_cannot_write_with_or_without_takeover(round_number, successor):
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "takeover", now)
        first = await repo.claim_next(worker_id="same-worker", now=now, lease_seconds=60)
        await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
        replacement = await repo.claim_next(worker_id=successor, now=datetime.now(timezone.utc), lease_seconds=60) if successor else None
        if replacement:
            assert replacement.job_id == first.job_id and replacement.attempt == first.attempt + 1
        assert not await repo.renew_lease(first, now=now, lease_seconds=60)
        assert not await repo.record_progress(first, PipelineProgressUpdate(phase="CARDS_AVAILABLE", message="日期和卡片已整理",
            progress=TripUnderstandingProgressMetrics(card_count=5), snapshot=output.public_result), now=now)
        await repo.fail_job(first, category="STALE_FAILURE", now=now, allow_retry=False)
        with pytest.raises(JobLeaseLostError):
            await repo.complete_job(first, output, now=now)
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results") == 0
        assert await repo._pool.fetchval("SELECT status FROM trip_understanding_jobs") == "RUNNING"
        if replacement:
            assert not await repo.complete_job(replacement, output, now=datetime.now(timezone.utc))
            assert await repo.complete_job(replacement, output, now=datetime.now(timezone.utc))
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("winner", ["cancel", "complete"])
async def test_cancel_and_completion_keep_the_first_committed_result(round_number, winner):
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "cancel-race", now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        job = await repo.claim_next(worker_id="cancel-worker", now=now, lease_seconds=60)
        service = TripUnderstandingApplicationService(repo)
        tasks = []
        try:
            async with repo._pool.acquire() as blocker, blocker.transaction():
                await blocker.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                    f"trip-understanding-resource:{job.understanding_id}")
                # Queue both contenders on the real resource lock in a known
                # order, then let them compete for the same persisted state.
                for index, operation in enumerate([winner, "cancel" if winner == "complete" else "complete"]):
                    tasks.append(asyncio.create_task(repo.complete_job(job, output, now=now) if operation == "complete"
                        else service.cancel_understanding(resource, idempotency_key="stop", now=now)))
                    async with asyncio.timeout(5):
                        while await blocker.fetchval("""SELECT count(*) FROM pg_locks WHERE locktype='advisory'
                            AND NOT granted AND database=(SELECT oid FROM pg_database WHERE datname=current_database())""") < index + 1:
                            await asyncio.sleep(0.01)
            replies = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        stopped = replies[0 if winner == "cancel" else 1]
        completion = replies[1 if winner == "cancel" else 0]
        assert isinstance(completion, JobLeaseLostError) if winner == "cancel" else completion is False
        retry = await service.cancel_understanding(resource, idempotency_key="stop", now=now)
        assert retry.replayed and retry.cancelled == stopped.cancelled
        if winner == "cancel":
            assert stopped.cancelled.status == "STOPPED_EMPTY"
            with pytest.raises(JobLeaseLostError):
                await repo.complete_job(job, output, now=now)
            assert not await repo.renew_lease(job, now=now, lease_seconds=60)
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results") == 0
            assert await repo._pool.fetchval("SELECT state FROM trip_understandings") == "CANCELLED"
        else:
            assert stopped.cancelled.status == "ALREADY_FINISHED"
            resource = await repo.authorize(resource.public_resource_id, capability_hash="a" * 64, now=now)
            stored = await repo.get_result(resource)
            assert stopped.opaque_etag == stored.opaque_etag
            assert sum(len(day.activities) for day in stored.result.days) == sum(len(day.activities) for day in output.public_result.days)
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_cancel_replay_rechecks_ownership_after_anonymous_claim(round_number, kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "claim-cancel", now)
        job = await repo.claim_next(worker_id="claim-worker", now=now, lease_seconds=60)
        await repo.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        service = TripUnderstandingApplicationService(repo)
        await service.cancel_understanding(resource, idempotency_key="stop-before-claim", now=now)
        claimed = await service.claim_demo(resource.public_resource_id, capability_hash="a" * 64,
            user_id="experience-owner", idempotency_key="claim", now=now)
        assert claimed.claimed.public_resource_id != resource.public_resource_id
        with pytest.raises(ResourceAccessDeniedError):
            await service.cancel_understanding(resource, idempotency_key="stop-before-claim", now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", ROUNDS)
@pytest.mark.parametrize("replay", [False, True])
@pytest.mark.parametrize("loss", ["resource_expiry", "session_expiry", "session_revocation"])
async def test_editor_waiting_for_lock_rechecks_current_access_before_write_or_replay(round_number, replay, loss):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        created = await create(repo, "edit-expiry", now)
        job = await repo.claim_next(worker_id="editor", now=now, lease_seconds=60)
        await repo.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        stored = await repo.get_result(resource)
        command = ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT",
            activity_token=stored.result.days[0].activities[0].activity_token, note="保留用户输入")
        options = dict(expected_etag=stored.opaque_etag, idempotency_key="note", request_hash="c" * 64, now=now)
        if replay:
            await repo.apply_command(resource, command, **options)
        before = await repo._pool.fetchrow("SELECT current_revision,current_result_id FROM trip_understandings")
        counts = await repo._pool.fetchrow("""SELECT (SELECT count(*) FROM trip_understanding_results) results,
            (SELECT count(*) FROM trip_understanding_idempotency_records) operations""")
        pending = None
        try:
            async with repo._pool.acquire() as blocker, blocker.transaction():
                await blocker.execute("SELECT understanding_id FROM trip_understandings FOR UPDATE")
                pending = asyncio.create_task(repo.apply_command(resource, command, **options))
                async with asyncio.timeout(5):
                    while not await blocker.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='transactionid' AND NOT granted)"):
                        await asyncio.sleep(0.01)
                if loss == "resource_expiry":
                    await blocker.execute("UPDATE trip_understandings SET source_expires_at=clock_timestamp()-interval '1 millisecond'")
                elif loss == "session_expiry":
                    await blocker.execute("UPDATE trip_understanding_anonymous_sessions SET expires_at=clock_timestamp()-interval '1 millisecond'")
                else:
                    await blocker.execute("UPDATE trip_understanding_anonymous_sessions SET revoked_at=clock_timestamp()")
            with pytest.raises(ResourceAccessDeniedError if loss == "session_revocation" else ResourceGoneError):
                await pending
            assert await repo._pool.fetchrow("SELECT current_revision,current_result_id FROM trip_understandings") == before
            assert await repo._pool.fetchrow("""SELECT (SELECT count(*) FROM trip_understanding_results) results,
                (SELECT count(*) FROM trip_understanding_idempotency_records) operations""") == counts
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
