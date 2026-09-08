"""Recommendation reads must describe the version they actually locked."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

from app.trip_understanding.candidates import issue_candidate
from app.trip_understanding.daily_dining import build_daily_meals, project_daily_meals
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.dining import dining_binding
from app.trip_understanding.dining_jobs import DailyDiningWorker, read_daily_dining
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import ActivityTimeSetCommand, DiningInsertCommand
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import create, finish, refresh, repository_for
from tests.test_dining_recommendations import restaurant


async def source_meal_trip(repo, now, *, status, role=None, start=None):
    created = await create(repo, f"source-meal-{status}", now)
    job = await repo.claim_next(worker_id="source-meal-fixture", now=now, lease_seconds=60)
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    meal = output.public_result.days[0].activities[0].model_copy(update={
        "activity_token": "synthetic-original-lunch-000001", "name": "合成原文用餐店",
        "category": "餐饮", "status": status, "meal_role": role, "start_time": start})
    output.public_result.days[0].activities.append(meal)
    await repo.complete_job(job, output, now=now)
    resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
    return resource, meal


async def wait_for_aggregate_waiters(conn, count):
    for _ in range(200):
        await conn.execute("SELECT pg_stat_clear_snapshot()")
        waiting = await conn.fetchval("""SELECT count(*) FROM pg_stat_activity
            WHERE datname=current_database() AND pid<>pg_backend_pid()
              AND wait_event_type='Lock' AND query ILIKE '%trip_understandings%'""")
        if waiting >= count:
            return
        await asyncio.sleep(.01)
    raise AssertionError("Expected concurrent operations did not wait for the aggregate lock")


@pytest.mark.asyncio
async def test_daily_read_waiting_behind_edit_returns_the_new_authoritative_etag():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "dining-read-edit-race", now), now)
        initial = await repo.get_result(resource)
        service = TripUnderstandingApplicationService(repo)
        edit = reader = None
        try:
            async with repo._pool.acquire() as blocker, blocker.transaction():
                await blocker.fetchrow("SELECT understanding_id FROM trip_understandings WHERE understanding_id=$1 FOR UPDATE",
                                       resource.understanding_id)
                edit = asyncio.create_task(service.apply_command(resource, ActivityTimeSetCommand(
                    command_type="ACTIVITY_TIME_SET", activity_token=initial.result.days[0].activities[0].activity_token,
                    start_time="10:00"), expected_etag=initial.opaque_etag, idempotency_key="edit-before-read", now=now))
                await wait_for_aggregate_waiters(blocker, 1)
                reader = asyncio.create_task(read_daily_dining(repo, resource))
                await wait_for_aggregate_waiters(blocker, 2)
            await edit
            view, etag = await reader
            _, current = await refresh(repo, resource, now)
            assert current.opaque_etag != initial.opaque_etag
            assert view.status == "NEEDS_UPDATE"
            assert etag == current.opaque_etag
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_daily_dining_jobs") == 1
        finally:
            for task in (edit, reader):
                if task and not task.done():
                    task.cancel()
            await asyncio.gather(*(task for task in (edit, reader) if task), return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [("NEEDS_CONFIRMATION", "NEEDS_CONFIRMATION"), ("READY", "EXISTING")])
async def test_source_meal_overrides_a_legacy_same_revision_dining_cache_without_rebuilding(status, expected):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource, meal = await source_meal_trip(repo, now, status=status)
        calls = 0

        async def search(**_kwargs):
            return [restaurant()]

        async def builder(result, plan, **_kwargs):
            nonlocal calls
            calls += 1
            legacy_result = result.model_copy(deep=True)
            legacy_result.days[0].activities = [card for card in legacy_result.days[0].activities
                                                if card.activity_token != meal.activity_token]
            return await build_daily_meals(legacy_result, plan, search=search)

        # Reproduce an old builder that missed the source meal, retaining the real source version.
        assert await DailyDiningWorker(repo, builder=builder).run_once("legacy-dining")
        original = await repo.get_result(resource)
        before = await repo._pool.fetchrow("SELECT payload_json,status,attempts FROM trip_daily_dining_jobs")
        cached = json.loads(before["payload_json"]) if isinstance(before["payload_json"], str) else before["payload_json"]
        assert cached[0]["candidates"]  # Reproduce the previously visible duplicate recommendation.

        view, etag = await read_daily_dining(repo, resource)
        assert etag == original.opaque_etag
        assert view.days[0].status == expected
        assert view.days[0].existing_activity_token == meal.activity_token
        assert view.days[0].candidates == []
        assert calls == 1
        assert dict(await repo._pool.fetchrow("SELECT payload_json,status,attempts FROM trip_daily_dining_jobs")) == dict(before)
        # An old open page can still possess an unexpired candidate. The write must also reject it.
        old_view = project_daily_meals(cached, public_resource_id=resource.public_resource_id,
                                      etag=etag, now=now)[0]
        with pytest.raises(CommandTargetChangedError):
            await TripUnderstandingApplicationService(repo).apply_command(resource, DiningInsertCommand(
                command_type="DINING_INSERT", after_activity_token=old_view.after_activity_token,
                candidate_token=old_view.candidates[0].candidate_token, meal_role="LUNCH"),
                expected_etag=etag, idempotency_key="old-page-duplicate-lunch", now=now)
        _, unchanged = await refresh(repo, resource, now)
        assert unchanged.opaque_etag == etag
        assert len(unchanged.result.days[0].activities) == len(original.result.days[0].activities)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,source_role,start,insert_role", [
    ("READY", "BREAKFAST", "08:00", "LUNCH"),
    ("NEEDS_CONFIRMATION", "DINNER", "18:00", "LUNCH"),
    ("READY", None, "08:00", "LUNCH"),
    ("NEEDS_CONFIRMATION", None, "18:00", "LUNCH"),
    ("NEEDS_CONFIRMATION", "LUNCH", None, "DINNER"),
    ("READY", "LUNCH", None, "BREAKFAST"),
])
async def test_a_different_meal_does_not_block_verified_insertion(status, source_role, start, insert_role):
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource, _ = await source_meal_trip(repo, now, status=status, role=source_role, start=start)
        original = await repo.get_result(resource)
        anchor = original.result.days[0].activities[0]
        candidate = issue_candidate(restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(anchor.activity_token), expected_etag=original.opaque_etag, now=now)
        await TripUnderstandingApplicationService(repo).apply_command(resource, DiningInsertCommand(
            command_type="DINING_INSERT", after_activity_token=anchor.activity_token,
            candidate_token=candidate.candidate_token, meal_role=insert_role),
            expected_etag=original.opaque_etag, idempotency_key="different-meal", now=now)
        _, updated = await refresh(repo, resource, now)
        assert len(updated.result.days[0].activities) == len(original.result.days[0].activities) + 1
        assert updated.result.days[0].activities[1].meal_role == insert_role


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_recommendation_read_keeps_result_and_map_in_the_requested_immutable_version(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "read-pinned-recommendation", now), now)
        original_plan, _ = await repo.get_current_place_plan(resource)
        old = await repo.load_recommendation_trip_view(resource.understanding_id, original_plan.plan_ref.revision)
        await TripUnderstandingApplicationService(repo).apply_command(resource, ActivityTimeSetCommand(
            command_type="ACTIVITY_TIME_SET", activity_token=old.result.days[0].activities[0].activity_token,
            start_time="10:00"), expected_etag=old.etag, idempotency_key="edit-pinned-trip", now=now)
        current_plan, current_etag = await repo.get_current_place_plan(resource)
        current = await repo.load_recommendation_trip_view(resource.understanding_id, current_plan.plan_ref.revision)
        old_again = await repo.load_recommendation_trip_view(resource.understanding_id, old.revision)
        assert old_again == old
        assert old_again.result.days[0].activities[0].start_time != "10:00"
        assert current.result.days[0].activities[0].start_time == "10:00"
        assert current.etag == current_etag != old.etag
        assert current.plan.plan_ref.revision == current.revision
        assert old_again.plan.plan_ref.revision == old.revision
