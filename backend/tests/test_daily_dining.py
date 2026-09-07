"""Synthetic behavior checks; these are not restaurant quality measurements."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from app.trip_understanding.daily_dining import build_daily_meals, meal_context, project_daily_meals
from app.trip_understanding.dining_jobs import DailyDiningWorker, read_daily_dining
from app.trip_understanding.dining import select_dining_rows
from app.trip_understanding.errors import RevisionConflictError
from app.trip_understanding.models import ActivityTimeSetCommand, DiningInsertCommand, UndoCommand
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_dining_recommendations import anchor, restaurant, row
from tests.test_experience_v3_journey import create, finish, refresh, repository_for


def card(name, index=0, *, category="景点", start=None, status="READY"):
    return NS(name=name, activity_token=f"synthetic-{index:024}", category=category, status=status, start_time=start)


def day_context(cards, day_index=1):
    day = NS(label=f"Day {day_index}", activities=cards)
    stops = [anchor().model_copy(update={"activity_token":c.activity_token,
        "day_index":day_index, "day_label":day.label, "sequence_index":i, "name":c.name,
        "canonical_place_id":f"amap:synthetic-{day_index}-{i}", "category":c.category}) for i, c in enumerate(cards) if c.status == "READY"]
    return day, stops


@pytest.mark.parametrize("start,expected", [(None,"EXISTING"),("12:00","EXISTING"),("08:00","NEEDS_CONFIRMATION"),("18:00","NEEDS_CONFIRMATION")])
def test_lunch_is_preserved_but_explicit_breakfast_and_dinner_do_not_fill_it(start, expected):
    day, stops = day_context([card("早餐餐厅", 0, category="餐饮", start=start),card("合成公园",1),card("合成博物馆",2)])
    view, before, after = meal_context(day, stops)
    assert view["status"] == expected
    if expected == "EXISTING":
        assert before is after is None
    else:
        assert before.name == "合成公园" and after.name == "合成博物馆"


def test_explicit_lunch_gap_precedes_last_stop_and_dishes_do_not_become_a_restaurant():
    cards = [card(f"合成景点{i}",i) for i in range(4)]
    cards.insert(3, card("午餐：牛肉面",9,category="餐饮",status="NEEDS_CONFIRMATION"))
    day, stops = day_context(cards)
    view, before, after = meal_context(day, stops)
    assert before.name == "合成景点2" and after.name == "合成景点3"
    assert view.get("existing_activity_token") is None


def test_service_optional_array_fields_are_not_treated_as_valid_business_names():
    assert select_dining_rows([{**row(), "business":[]}],anchor=anchor(),excluded_ids=set())[0].business_area is None
    result = select_dining_rows([{**row(),"business":{"business_area":"王府井"}}],anchor=anchor(),excluded_ids=set())
    assert result[0].business_area == "王府井"


def test_explicit_meal_role_and_slot_keep_lunch_before_first_visit():
    dinner = card("原文晚餐餐厅",0,category="餐饮")
    dinner.meal_role = "DINNER"
    day, stops = day_context([card("合成公园",1),dinner])
    day.meal_slots = [NS(meal_role="LUNCH",after_activity_token=None,before_activity_token=stops[0].activity_token)]
    view, before, after = meal_context(day,stops)
    assert view["insert_before"] and view["meal_role"] == "LUNCH"
    assert before.name == "合成公园" and after is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory","postgres"])
async def test_lunch_before_first_visit_has_distinct_signed_purpose_and_persists_role(kind):
    from app.trip_understanding.candidates import issue_candidate
    from app.trip_understanding.dining import dining_binding
    from app.trip_understanding.errors import CommandTargetChangedError
    async with repository_for(kind) as repo:
        now=datetime.now(timezone.utc)
        resource=await finish(repo,await create(repo,"before-first-meal",now),now)
        original=await repo.get_result(resource)
        token=original.result.days[0].activities[0].activity_token
        issued=issue_candidate(restaurant(),public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(token,before=True),expected_etag=original.opaque_etag,now=now)
        service=TripUnderstandingApplicationService(repo)
        command=DiningInsertCommand(command_type="DINING_INSERT",after_activity_token=token,insert_before=True,meal_role="LUNCH",candidate_token=issued.candidate_token)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource,command.model_copy(update={"insert_before":False}),
                expected_etag=original.opaque_etag,idempotency_key="wrong-side",now=now)
        await service.apply_command(resource,command,expected_etag=original.opaque_etag,idempotency_key="before-first",now=now)
        _, result=await refresh(repo,resource,now)
        assert result.result.days[0].activities[0].name == restaurant().name
        assert result.result.days[0].activities[0].meal_role == "LUNCH"
        assert result.result.days[0].activities[1].name == original.result.days[0].activities[0].name


@pytest.mark.asyncio
async def test_real_detour_changes_rank_and_missing_route_cannot_claim_fastest():
    day, stops = day_context([card("合成公园",0), card("合成博物馆",1)])
    candidates = [restaurant().model_copy(update={"canonical_place_id":f"amap:meal-{i}","name":f"合成餐厅{i}","business_area":"合成商圈"}) for i in range(3)]
    async def search(**_):
        return candidates
    class Routes:
        async def route(self, a, b, mode, **_):
            pair = (a.canonical_place_id,b.canonical_place_id)
            if "amap:meal-2" in pair:
                return NS(status="UNAVAILABLE",duration_minutes=None)
            duration = 25 if "amap:meal-0" in pair else 8 if "amap:meal-1" in pair else 10
            return NS(status="AVAILABLE",duration_minutes=duration)
    rows = await build_daily_meals(NS(days=[day]),NS(stops=stops),search=search,routes=Routes())
    assert [item["extra_minutes"] for item in rows[0]["candidates"]] == [6,40,None]
    assert [item["place"]["name"] for item in rows[0]["candidates"]] == ["合成餐厅1","合成餐厅0","合成餐厅2"]
    assert "最顺路" not in rows[0]["candidates"][2]["reason"]
    projected = project_daily_meals(rows, public_resource_id="synthetic-resource",etag="synthetic-etag")
    assert projected[0].area == "合成商圈" and projected[0].candidates[0].recommended
    assert "canonical_place_id" not in projected[0].model_dump_json()


@pytest.mark.asyncio
async def test_failed_day_does_not_remove_other_days_and_route_deadline_keeps_candidates():
    first, a = day_context([card("首日",0)])
    second, b = day_context([card("次日",1),card("次日下一站",2)],2)
    async def search(*, anchor, **_):
        if anchor.day_index == 1:
            raise TimeoutError()
        return [restaurant()]
    class SlowRoutes:
        async def route(self, *_args, **_kwargs):
            await asyncio.sleep(2)
    rows = await build_daily_meals(NS(days=[first,second]),NS(stops=a+b),search=search,routes=SlowRoutes(),deadline_seconds=.03)
    assert [day["status"] for day in rows] == ["UNAVAILABLE","AVAILABLE"]
    assert rows[1]["candidates"][0]["extra_minutes"] is None


@pytest.mark.asyncio
async def test_daily_jobs_bind_revision_and_adoption_undo_never_requery_or_route():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo,await create(repo,"daily-job",now),now)
        initial = await repo.get_result(resource)
        plan, etag = await repo.get_current_place_plan(resource)
        calls = []
        async def builder(result, plan, **_):
            calls.append(plan.plan_ref.revision)
            async def search(**_kwargs):
                return [restaurant()]
            return await build_daily_meals(result,plan,search=search)
        worker = DailyDiningWorker(repo,builder=builder)
        pending, _ = await read_daily_dining(repo,resource)
        assert pending.status == "PREPARING" and not calls
        assert await worker.run_once("dining-test")
        ready, _ = await read_daily_dining(repo,resource)
        assert ready.status == "AVAILABLE"
        available = next(day for day in ready.days if day.candidates)
        command = DiningInsertCommand(command_type="DINING_INSERT",after_activity_token=available.after_activity_token,candidate_token=available.candidates[0].candidate_token)
        service = TripUnderstandingApplicationService(repo)
        map_jobs = await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        await service.apply_command(resource,command,expected_etag=etag,idempotency_key="adopt-daily",now=now)
        resource, adopted = await refresh(repo,resource,now)
        assert any(c.name == restaurant().name for c in adopted.result.days[available.day_index-1].activities)
        stale, _ = await read_daily_dining(repo,resource)
        assert stale.status == "NEEDS_UPDATE"
        assert not await worker.run_once("no-auto-edit")
        with pytest.raises(RevisionConflictError):
            await read_daily_dining(repo,resource,request_key="stale-refresh",expected_etag=etag)
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_daily_dining_jobs") == 1
        await service.apply_command(resource,UndoCommand(command_type="UNDO"),expected_etag=adopted.opaque_etag,idempotency_key="undo-daily",now=now)
        resource, restored = await refresh(repo,resource,now)
        def content(result):
            return [day.model_dump(exclude={"activities":{"__all__":{"activity_token"}}}) for day in result.days]
        # An undo creates a new version and new opaque activity handles.
        assert content(restored.result) == content(initial.result)
        assert not await worker.run_once("no-auto-undo")
        for _ in range(2):
            await read_daily_dining(repo,resource,request_key="refresh-one",expected_etag=restored.opaque_etag)
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_daily_dining_jobs") == 2
        assert await worker.run_once("one-manual-refresh")
        assert not await worker.run_once("no-repeat")
        assert len(calls) == 2
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs") == map_jobs


@pytest.mark.asyncio
async def test_late_result_cannot_be_published_for_edited_revision_and_delete_cleans_jobs():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo,await create(repo,"daily-late",now),now)
        started, released = asyncio.Event(), asyncio.Event()
        async def builder(*_args, **_kwargs):
            started.set()
            await released.wait()
            return []
        job = asyncio.create_task(DailyDiningWorker(repo,builder=builder).run_once("late-dining"))
        await started.wait()
        initial = await repo.get_result(resource)
        await TripUnderstandingApplicationService(repo).apply_command(resource,ActivityTimeSetCommand(
            command_type="ACTIVITY_TIME_SET",activity_token=initial.result.days[0].activities[0].activity_token,start_time="10:00"),
            expected_etag=initial.opaque_etag,idempotency_key="edit-before-ready",now=now)
        released.set()
        assert await job
        resource, _ = await refresh(repo,resource,now)
        assert (await read_daily_dining(repo,resource))[0].status == "NEEDS_UPDATE"
        await TripUnderstandingApplicationService(repo).delete_trip(resource,capability_hash="a"*64,user_id=None,idempotency_key="delete-daily",now=now)
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_daily_dining_jobs") == 0


@pytest.mark.asyncio
async def test_fixed_demo_never_dispatches_live_dining_builder():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo,"daily-demo",now), now)
        await repo._pool.execute("UPDATE trip_understanding_sources SET source_type='FIXED_DEMO' WHERE understanding_id=$1",resource.understanding_id)
        async def forbidden(*_args, **_kwargs):
            pytest.fail("A fixed demo must not dispatch billable dining effects")
        assert await DailyDiningWorker(repo,builder=forbidden).run_once("fixed-demo")
        assert (await read_daily_dining(repo,resource))[0].status == "UNAVAILABLE"
