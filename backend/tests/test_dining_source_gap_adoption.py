"""Unnamed source lunch requirements are fulfilled once, with real persisted revisions."""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.trip_understanding.daily_dining import build_daily_meals, project_daily_meals, source_lunch_gaps
from app.trip_understanding.candidates import issue_candidate
from app.trip_understanding.demo import FixedBeijingPlaceResolver
from app.trip_understanding.dining import dining_binding
from app.trip_understanding.dining_jobs import DailyDiningWorker, read_daily_dining
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import DiningInsertCommand, InferenceProposal, ProposedMention, UndoCommand
from app.trip_understanding.overnight_context import overnight_segments
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_dining_recommendations import restaurant
from tests.test_experience_v3_journey import repository_for, refresh
from tests.test_recommendation_trip_view import wait_for_aggregate_waiters


LANDMARKS = [("故宫博物院", "景山公园"), ("天坛公园", "前门大街"), ("颐和园", "圆明园")]


async def create_gap_trip(repo, now, *, quote="每天中午找一家普通对外营业的餐厅", start_time=None, extra_gap=False):
    source = "北京三日游。" + "".join(f"Day {i}：{a}、{b}。" for i, (a, b) in enumerate(LANDMARKS, 1)) + quote + "。"
    mentions = []
    for day, names in enumerate(LANDMARKS, 1):
        for order, name in enumerate([*names, quote]):
            meal = order == 2
            start = source.index(name)
            mentions.append(ProposedMention(mention_id=f"gap-{day}-{order}", raw_text=name,
                span_start=start, span_end=start+len(name), role="PLANNED", day_index=day, sequence_index=order,
                atomic_place_name=None if meal else name, category_hint="餐饮" if meal else "景点",
                start_time=start_time if meal else None))
    class Provider:
        async def propose(self, text):
            return InferenceProposal(source_hash=hashlib.sha256(text.encode()).hexdigest(), destination_name="北京",
                mentions=mentions, binding={"provider": "controlled source gap test"}, day_count=3)
    output = await TripUnderstandingPipeline(Provider(), FixedBeijingPlaceResolver()).run(source)
    if extra_gap:
        output.public_result.days[0].activities.append(output.public_result.days[0].activities[-1].model_copy(update={
            "activity_token": "synthetic-second-lunch-gap-000001", "meal_role": "LUNCH"}))
    created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="source-gap",
        request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
    job = await repo.claim_next(worker_id="gap-fixture", now=now, lease_seconds=60)
    await repo.complete_job(job, output, now=now)
    return await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)


async def search(**_):
    return [restaurant()]


async def current_menu(repo, resource, now, *, legacy=False):
    plan, _ = await repo.get_current_place_plan(resource)
    trip = await repo.load_recommendation_trip_view(resource.understanding_id, plan.plan_ref.revision)
    raw = await build_daily_meals(trip.result, trip.plan, search=search,
                                 source_gaps={} if legacy else trip.source_lunch_gaps)
    return trip, project_daily_meals(raw, public_resource_id=resource.public_resource_id, etag=trip.etag, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_three_daily_lunch_needs_fill_in_the_middle_once_and_undo_restores_the_remaining_gap(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await create_gap_trip(repo, now)
        service = TripUnderstandingApplicationService(repo)
        map_jobs = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        for index in range(3):
            trip, menu = await current_menu(repo, resource, now)
            assert len(trip.source_lunch_gaps) == 3-index
            row = menu[index]
            assert row.after_activity_token == trip.result.days[index].activities[0].activity_token
            command = DiningInsertCommand(command_type="DINING_INSERT", after_activity_token=row.after_activity_token,
                candidate_token=row.candidates[0].candidate_token, meal_role="LUNCH")
            await service.apply_command(resource, command, expected_etag=trip.etag, idempotency_key=f"fill-{index}", now=now)
            assert (await service.apply_command(resource, command, expected_etag=trip.etag, idempotency_key=f"fill-{index}", now=now)).replayed
            resource, stored = await refresh(repo, resource, now)
            assert [card.name for card in stored.result.days[index].activities] == [LANDMARKS[index][0], restaurant().name, LANDMARKS[index][1]]
            assert sum(card.name == "地点待确认" for day in stored.result.days for card in day.activities) == 2-index
            assert sum(len(day.activities) for day in stored.result.days) == 9
            assert stored.result.map.status == "NEEDS_UPDATE"
        plan, _ = await repo.get_current_place_plan(resource)
        assert all(not segment.missing_boundaries for segment in overnight_segments(plan))
        assert (len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")) == map_jobs
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="undo-last-meal", now=now)
        resource, restored = await refresh(repo, resource, now)
        assert [card.name for card in restored.result.days[2].activities] == [*LANDMARKS[2], "地点待确认"]
        trip, _ = await current_menu(repo, resource, now)
        assert len(trip.source_lunch_gaps) == 1


@pytest.mark.asyncio
async def test_old_end_of_day_candidate_requires_explicit_update_before_filling_source_lunch():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await create_gap_trip(repo, now)
        async def old_builder(result, plan, **_):
            return await build_daily_meals(result, plan, search=search)
        assert await DailyDiningWorker(repo, builder=old_builder).run_once("old-gap-menu")
        trip, old_menu = await current_menu(repo, resource, now, legacy=True)
        before, etag = await read_daily_dining(repo, resource)
        assert before.status == "NEEDS_UPDATE" and not before.days
        row = old_menu[0]
        with pytest.raises(CommandTargetChangedError):
            await TripUnderstandingApplicationService(repo).apply_command(resource, DiningInsertCommand(
                command_type="DINING_INSERT", after_activity_token=row.after_activity_token,
                candidate_token=row.candidates[0].candidate_token, meal_role="LUNCH"),
                expected_etag=etag, idempotency_key="stale-position", now=now)
        refreshed, _ = await read_daily_dining(repo, resource, request_key="update-gap-menu", expected_etag=etag)
        assert refreshed.status == "PREPARING"
        async def new_builder(result, plan, **kwargs):
            return await build_daily_meals(result, plan, search=search, **kwargs)
        assert await DailyDiningWorker(repo, builder=new_builder).run_once("correct-gap-menu")
        ready, _ = await read_daily_dining(repo, resource)
        assert ready.days[0].after_activity_token == trip.result.days[0].activities[0].activity_token


@pytest.mark.parametrize("quote,role", [("每天晚餐找餐厅",None),("每天早餐吃包子",None),
    ("想尝试当地美食",None),("",None),("中午找一家店","DINNER"),("不安排午餐",None)])
def test_a_category_or_unique_placeholder_does_not_prove_lunch(quote, role):
    card = SimpleNamespace(name="地点待确认",activity_token="gap",category="餐饮",status="NEEDS_CONFIRMATION",meal_role=role,start_time=None)
    result = SimpleNamespace(days=[SimpleNamespace(activities=[card],meal_slots=[])])
    assert source_lunch_gaps(result,[{"public_activity_token":"gap","atomic_place_name":None,"mention_text":quote}]) == {}


@pytest.mark.asyncio
async def test_explicit_lunch_time_keeps_its_source_position_when_filled():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await create_gap_trip(repo, now, quote="12:15午餐用餐", start_time="12:15")
        trip, menu = await current_menu(repo, resource, now)
        assert set(trip.source_lunch_gaps.values()) == {"POSITIONAL"}
        row = menu[0]
        await TripUnderstandingApplicationService(repo).apply_command(resource, DiningInsertCommand(
            command_type="DINING_INSERT", after_activity_token=row.after_activity_token,
            candidate_token=row.candidates[0].candidate_token, meal_role="LUNCH"),
            expected_etag=trip.etag, idempotency_key="fixed-position", now=now)
        _, stored = await refresh(repo, resource, now)
        assert [card.name for card in stored.result.days[0].activities] == [*LANDMARKS[0], restaurant().name]
        assert stored.result.days[0].activities[-1].start_time == "12:15"


@pytest.mark.asyncio
async def test_multiple_lunch_gaps_stay_pending_and_reject_an_ambiguous_adoption():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await create_gap_trip(repo, now, extra_gap=True)
        trip, menu = await current_menu(repo, resource, now)
        assert menu[0].status == "NEEDS_CONFIRMATION" and not menu[0].candidates
        async def legacy_builder(result, plan, **_):
            return await build_daily_meals(result, plan, search=search)
        assert await DailyDiningWorker(repo, builder=legacy_builder).run_once("ambiguous-old-menu")
        cached, _ = await read_daily_dining(repo, resource)
        assert cached.days[0].status == "NEEDS_CONFIRMATION" and not cached.days[0].candidates
        assert "多处午餐" in cached.days[0].message
        anchor = trip.result.days[0].activities[0].activity_token
        candidate = issue_candidate(restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(anchor), expected_etag=trip.etag, now=now)
        with pytest.raises(CommandTargetChangedError):
            await TripUnderstandingApplicationService(repo).apply_command(resource, DiningInsertCommand(
                command_type="DINING_INSERT", after_activity_token=anchor,
                candidate_token=candidate.candidate_token, meal_role="LUNCH"),
                expected_etag=trip.etag, idempotency_key="ambiguous-gap", now=now)
        _, stored = await refresh(repo, resource, now)
        assert stored.opaque_etag == trip.etag
        assert len(stored.result.days[0].activities) == 4


@pytest.mark.asyncio
async def test_lunch_candidate_expiring_while_waiting_for_the_write_lock_is_rejected():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await create_gap_trip(repo, now)
        trip, _ = await current_menu(repo, resource, now)
        anchor = trip.result.days[0].activities[0].activity_token
        issued_at = datetime.now(timezone.utc)
        candidate = issue_candidate(restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(anchor), expected_etag=trip.etag, now=issued_at,
            expires_at=issued_at+timedelta(seconds=.2))
        async with repo._pool.acquire() as blocker, blocker.transaction():
            await blocker.fetchrow("SELECT understanding_id FROM trip_understandings WHERE understanding_id=$1 FOR UPDATE",
                                   resource.understanding_id)
            task = asyncio.create_task(TripUnderstandingApplicationService(repo).apply_command(resource, DiningInsertCommand(
                command_type="DINING_INSERT", after_activity_token=anchor,
                candidate_token=candidate.candidate_token, meal_role="LUNCH"),
                expected_etag=trip.etag, idempotency_key="gap-expired-waiting", now=issued_at))
            await wait_for_aggregate_waiters(blocker, 1)
            await asyncio.sleep(.25)
        with pytest.raises(CommandTargetChangedError):
            await task
        _, stored = await refresh(repo, resource, now)
        assert stored.opaque_etag == trip.etag
