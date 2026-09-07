"""Initial inference cannot persist private derivatives after source expiry.

All source text and inference are synthetic. PostgreSQL cases use disposable
databases and exercise real transaction locks; no provider calls are made.
"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.errors import SourceUnavailableError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from tests.test_experience_inference import Client, provider
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_partial_recovery import activity


SOURCE = "北京两日游。星河酒店，哪晚住尚未确定。\nDay1：故宫博物院。\nDay2：天坛公园。"


async def prepared(repo, started_at):
    await repo.create_full(owner_user_id="experience-owner", source_text=SOURCE,
        idempotency_key="initial-expiry", request_hash=canonical_sha256(SOURCE), now=started_at, retention_days=30)
    job = await repo.claim_next(worker_id="initial-expiry-controlled", now=started_at, lease_seconds=600)
    rows = [activity("星河酒店", day=None, category="住宿"), activity("故宫博物院"), activity("天坛公园", 2)]
    client = Client(json.dumps({"destination": "北京", "activities": rows}), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    assert len(client.calls) == 2 and len(output.public_result.pending_lodgings) == 1
    assert output.claims and output.activities
    return job, output


async def assert_no_derivatives(repo, kind, understanding_id):
    if kind == "postgres":
        for table in ("trip_understanding_results", "trip_understanding_activities", "trip_understanding_source_claims"):
            assert await repo._pool.fetchval(f"SELECT count(*) FROM {table} WHERE understanding_id=$1", understanding_id) == 0
        row = await repo._pool.fetchrow("SELECT state,current_revision,current_result_id FROM trip_understandings WHERE understanding_id=$1",
            understanding_id)
        assert row["state"] == "PROCESSING" and row["current_revision"] == 1 and row["current_result_id"] is None
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_revisions WHERE understanding_id=$1", understanding_id) == 1
    else:
        assert not repo.results and not repo.g03_pipeline_inputs and not repo.side_effects
        assert understanding_id not in repo.source_readback_mentions
        aggregate = repo.resources[repo.resources_by_understanding[understanding_id]]
        assert aggregate["state"] == "PROCESSING" and aggregate["current_revision"] == 1 and aggregate["current_result_id"] is None


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("source_clock", ["wall_expired", "logical_expired", "current"])
@pytest.mark.asyncio
async def test_initial_completion_uses_later_of_request_and_current_source_clock(kind, source_clock):
    async with repository_for(kind) as repo:
        started_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        job, output = await prepared(repo, started_at)
        expiry = datetime.now(timezone.utc) + timedelta(minutes=1)
        completed_at = started_at
        if source_clock == "wall_expired":
            expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
            assert started_at < expiry
        elif source_clock == "logical_expired":
            completed_at = expiry + timedelta(seconds=1)
        if kind == "postgres":
            await repo._pool.execute("UPDATE trip_understanding_sources SET retention_until=$2 WHERE understanding_id=$1",
                job.understanding_id, expiry)
        else:
            repo.source_expiries[job.job_id] = expiry
        if source_clock == "current":
            assert await repo.complete_job(job, output, now=completed_at) is False
            return
        with pytest.raises(SourceUnavailableError):
            await repo.complete_job(job, output, now=completed_at)
        await assert_no_derivatives(repo, kind, job.understanding_id)


@pytest.mark.asyncio
async def test_initial_completion_rechecks_source_clock_after_postgres_lock_wait():
    async with repository_for("postgres") as repo:
        requested_at = datetime.now(timezone.utc)
        job, output = await prepared(repo, requested_at)
        completion = None
        try:
            async with repo._pool.acquire() as blocker, blocker.transaction():
                await blocker.fetchval("SELECT understanding_id FROM trip_understandings WHERE understanding_id=$1 FOR UPDATE",
                    job.understanding_id)
                completion = asyncio.create_task(repo.complete_job(job, output, now=requested_at))
                for _ in range(100):
                    await blocker.execute("SELECT pg_stat_clear_snapshot()")
                    waiting = await blocker.fetchval("""SELECT EXISTS(SELECT 1 FROM pg_stat_activity
                        WHERE datname=current_database() AND pid<>pg_backend_pid() AND wait_event_type='Lock'
                          AND query LIKE '%SELECT * FROM trip_understandings%')""")
                    if waiting:
                        break
                    await asyncio.sleep(0.02)
                assert waiting and not completion.done(), "completion must actually wait for the aggregate lock"
                expiry = await blocker.fetchval("""UPDATE trip_understanding_sources SET retention_until=clock_timestamp()
                    WHERE understanding_id=$1 RETURNING retention_until""", job.understanding_id)
                assert requested_at < expiry
            with pytest.raises(SourceUnavailableError):
                await asyncio.wait_for(completion, timeout=5)
            await assert_no_derivatives(repo, "postgres", job.understanding_id)
        finally:
            if completion is not None and not completion.done():
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
