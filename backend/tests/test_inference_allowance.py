"""Dispatch reservations survive process/job changes; refusals send no model call."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace

import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import InferenceProviderUnavailableError, JobLeaseLostError
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded, inference_allowance
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from tests.test_experience_v3_journey import create, repository_for


def controlled_two_day_input():
    source = ("北京两日游。\nDay1：故宫博物院，馆内看太和殿。然后去景山公园，再去王府井。"
              "\nDay2：颐和园，然后去圆明园。")
    visits = [("故宫博物院", 1), ("景山公园", 1), ("王府井", 1), ("颐和园", 2), ("圆明园", 2)]
    first = dict(destination="北京", day_labels=[None, None], activities=[
        dict(source_quote=name, place_name=name, role="PLANNED", category="景点", day_index=day,
             city="北京", city_evidence="北京两日游") for name, day in visits])
    second = dict(city_fields=[], source_visits=[dict(parent_index=0, kind="VISIT",
        source_quote="太和殿", optional=False, evidence="故宫博物院，馆内看太和殿")])
    return dict(source=source, responses=[first, second])


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", range(int(os.getenv("TRIPCHECK_RELIABILITY_ROUNDS", "1"))))
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_concurrent_reservations_cannot_overspend_and_takeover_keeps_allowance(kind, round_number):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "budget", now)
        job = await repo.claim_next(worker_id="same-name", now=now, lease_seconds=1)
        results = await asyncio.gather(*(repo.reserve_inference_call(job, now=now) for _ in range(32)), return_exceptions=True)
        remaining = [result for result in results if isinstance(result, float)]
        assert len(remaining) == 31
        assert all(0 < seconds <= 600 for seconds in remaining)
        assert sum(isinstance(result, InferenceAllowanceExceeded) for result in results) == 1
        # A restarted repository and a new attempt must see the same depletion.
        if kind == "postgres":
            repo = PostgresTripUnderstandingRepository(repo._pool, repo._get_source_cipher())
        replacement = await repo.claim_next(worker_id="same-name", now=now + timedelta(seconds=2), lease_seconds=60)
        with pytest.raises(JobLeaseLostError):
            await repo.reserve_inference_call(job, now=now + timedelta(seconds=2))
        with pytest.raises(InferenceAllowanceExceeded, match="BUDGET_EXHAUSTED"):
            await repo.reserve_inference_call(replacement, now=now + timedelta(seconds=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", range(int(os.getenv("TRIPCHECK_RELIABILITY_ROUNDS", "1"))))
async def test_shared_deadline_rejects_request_and_rolls_back_late_publication(round_number):
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "deadline", now)
        job = await repo.claim_next(worker_id="deadline", now=now, lease_seconds=60)
        await repo.reserve_inference_call(job, now=now)
        async with repo._pool.acquire() as conn:
            await conn.execute("UPDATE trip_understanding_sources SET inference_deadline_at=clock_timestamp()-interval '1 millisecond'")
        with pytest.raises(InferenceAllowanceExceeded, match="DEADLINE_EXCEEDED"):
            await repo.reserve_inference_call(job, now=now)
        with pytest.raises(InferenceAllowanceExceeded, match="DEADLINE_EXCEEDED"):
            await repo.complete_job(job, output, now=now)
        async with repo._pool.acquire() as conn:
            assert await conn.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources") == 30
            assert await conn.fetchval("SELECT count(*) FROM trip_understanding_results") == 0
            assert await conn.fetchval("SELECT current_result_id FROM trip_understandings") is None
            assert await conn.fetchval("SELECT count(*) FROM trip_understanding_revisions") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("round_number", range(int(os.getenv("TRIPCHECK_RELIABILITY_ROUNDS", "1"))))
async def test_reservation_checks_lease_after_waiting_for_source_lock(round_number):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "reservation-lock", now)
        job = await repo.claim_next(worker_id="lease", now=now, lease_seconds=0.5)
        pending = None
        try:
            async with repo._pool.acquire() as blocker, blocker.transaction():
                await blocker.fetchrow("SELECT source_id FROM trip_understanding_sources FOR UPDATE")
                pending = asyncio.create_task(repo.reserve_inference_call(job, now=now))
                async with asyncio.timeout(5):
                    while not await blocker.fetchval("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='transactionid' AND NOT granted)"):
                        await asyncio.sleep(0.01)
                # The job row is held by the reserver, so expire its lease by
                # actual passage of time instead of another conflicting write.
                await asyncio.sleep(0.6)
            with pytest.raises(JobLeaseLostError):
                await pending
            async with repo._pool.acquire() as conn:
                assert await conn.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources") == 31
                assert await conn.fetchval("SELECT inference_deadline_at FROM trip_understanding_sources") is None
        finally:
            if pending and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_rejected_dispatch_has_zero_actual_model_calls():
    calls = []
    async def denied():
        raise InferenceAllowanceExceeded("MODEL_CALL_BUDGET_EXHAUSTED")
    async def response(**kwargs):
        calls.append(kwargs)
        raise AssertionError("request must not be sent")
    provider = ExperienceQwenProvider(api_key="test", base_url="https://example.invalid/v1", model="kimi-test",
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=response))))
    with inference_allowance(denied), pytest.raises(InferenceProviderUnavailableError) as exc:
        await provider.propose("Day1：去故宫。")
    assert calls == []
    assert exc.value.external_call_count == 0
    assert exc.value.category == "MODEL_CALL_BUDGET_EXHAUSTED"


@pytest.mark.asyncio
async def test_supplement_denial_keeps_first_reply_and_does_not_count_unsent_request():
    baseline = controlled_two_day_input()
    reservations = 0
    calls = []
    async def reserve():
        nonlocal reservations
        reservations += 1
        if reservations > 1:
            raise InferenceAllowanceExceeded("MODEL_CALL_BUDGET_EXHAUSTED")
    async def response(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(baseline["responses"][0], ensure_ascii=False)), finish_reason="stop")], usage=None)
    provider = ExperienceQwenProvider(api_key="test", base_url="https://example.invalid/v1", model="kimi-test",
        enable_source_visits=True, client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=response))))
    with inference_allowance(reserve):
        proposal = await provider.propose(baseline["source"])
    assert len(calls) == proposal.binding["external_calls"] == 1
    assert len(proposal.mentions) >= 5
    assert proposal.unprocessed_count > 0


@pytest.mark.asyncio
async def test_official_worker_reserves_both_controlled_replies_on_the_durable_source():
    from app.trip_understanding.pipeline import TripUnderstandingPipeline
    from app.trip_understanding.worker import TripUnderstandingWorker
    from tests.semantic_page_replays import FixedReplayPlaces
    from tests.test_semantic_supplement_budget import Client, provider

    baseline = controlled_two_day_input()
    client = Client(*baseline["responses"])
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a" * 64, source_text=baseline["source"],
            idempotency_key="worker-budget", request_hash="b" * 64, now=now, ttl_hours=24)
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(provider(client), FixedReplayPlaces()))
        assert await worker.run_once("worker-budget")
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        stored = await repo.get_result(resource)
        assert [len(day.activities) for day in stored.result.days] == [3, 2]
        assert len(client.calls) == 2
        async with repo._pool.acquire() as conn:
            row = await conn.fetchrow("SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources")
            assert row["inference_calls_remaining"] == 29
            assert now < row["inference_deadline_at"] <= datetime.now(timezone.utc) + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_worker_with_exhausted_budget_finishes_with_actionable_failure_without_dispatch():
    from app.trip_understanding.pipeline import TripUnderstandingPipeline
    from app.trip_understanding.worker import TripUnderstandingWorker
    from tests.semantic_page_replays import FixedReplayPlaces
    from tests.test_semantic_supplement_budget import Client, provider

    client = Client()
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a" * 64, source_text="北京。Day1：去故宫博物院。",
            idempotency_key="empty-budget", request_hash="b" * 64, now=now, ttl_hours=24)
        await repo._pool.execute("UPDATE trip_understanding_sources SET inference_calls_remaining=0")
        worker = TripUnderstandingWorker(repo, full_pipeline=TripUnderstandingPipeline(provider(client), FixedReplayPlaces()))
        assert await worker.run_once("worker-empty-budget")
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        assert resource.state == "FAILED" and resource.failure_category == "MODEL_CALL_BUDGET_EXHAUSTED"
        assert client.calls == []
        events = await repo.list_events(resource, after_event_id=0)
        assert events[-1].payload.message == "本次整理的尝试次数已用完，已停止继续请求"
