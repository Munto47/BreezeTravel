"""Synthetic behavior checks; these are not restaurant quality measurements."""
import asyncio
from datetime import datetime, timedelta, timezone
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


@pytest.mark.parametrize("code", ["050000","050400","050500","050600","050700","050800","050900"])
def test_daily_lunch_excludes_tea_drinks_cake_and_generic_food_service(code):
    # Types observed in the first live run; FOOD alone does not prove a lunch venue.
    source={**row(),"typecode":code,"type":"餐饮服务"}
    assert select_dining_rows([source],anchor=anchor(),excluded_ids=set(),meal_only=True) == []


@pytest.mark.parametrize("code",["050100","050108","050200","050300"])
def test_daily_lunch_accepts_specific_meal_serving_categories(code):
    source={**row(),"typecode":code,"type":"餐饮服务"}
    assert select_dining_rows([source],anchor=anchor(),excluded_ids=set(),meal_only=True)


@pytest.mark.parametrize("name", ["广州富力丽思卡尔顿酒店宴会厅", "合成婚宴中心", "合成婚宴会馆", "合成团膳餐厅", "合成中央厨房"])
def test_daily_lunch_excludes_group_event_catering_even_with_restaurant_type(name):
    source = {**row(), "name": name, "typecode": "050100", "type": "餐饮服务;中餐厅"}
    assert select_dining_rows([source], anchor=anchor(), excluded_ids=set(), meal_only=True) == []


@pytest.mark.parametrize("name", ["合成酒店中餐厅", "合成食堂", "合成家宴餐厅"])
def test_meal_qualification_keeps_public_hotel_restaurants_and_does_not_overmatch_characters(name):
    source = {**row(), "name": name, "typecode": "050100", "type": "餐饮服务;中餐厅"}
    assert select_dining_rows([source], anchor=anchor(), excluded_ids=set(), meal_only=True)


@pytest.mark.asyncio
async def test_reused_worker_id_cannot_publish_previous_attempt_after_manual_retry():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "dining-attempt-fence", now), now)
        _, etag = await repo.get_current_place_plan(resource)
        entered = [asyncio.Event(), asyncio.Event()]
        released = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def builder(*_args, **_kwargs):
            nonlocal calls
            index = calls
            calls += 1
            entered[index].set()
            await released[index].wait()
            return []

        worker = DailyDiningWorker(repo, builder=builder)
        first = asyncio.create_task(worker.run_once("reused-dining-worker"))
        second = None
        try:
            await entered[0].wait()
            await repo._pool.execute("UPDATE trip_daily_dining_jobs SET lease_until=NOW()-INTERVAL '1 second'")
            assert not await worker.run_once("lease-sweep")
            await repo._pool.execute("UPDATE trip_daily_dining_jobs SET finished_at=NOW()-INTERVAL '1 minute'")
            await read_daily_dining(repo, resource, request_key="manual-retry", expected_etag=etag)
            second = asyncio.create_task(worker.run_once("reused-dining-worker"))
            await entered[1].wait()
            released[0].set()
            assert await first
            current = await repo._pool.fetchrow("SELECT status,attempts,lease_owner FROM trip_daily_dining_jobs")
            assert dict(current) == {"status": "BUILDING", "attempts": 2, "lease_owner": "reused-dining-worker"}
            released[1].set()
            assert await second
            assert await repo._pool.fetchval("SELECT status FROM trip_daily_dining_jobs") == "READY"
        finally:
            for event in released:
                event.set()
            await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


def test_unknown_intermediate_stop_is_not_skipped_for_detour_comparison():
    day,stops=day_context([card("已确认甲",0),card("未确认乙",1,status="NEEDS_CONFIRMATION"),card("已确认丙",2)])
    _,before,after=meal_context(day,stops)
    assert before.name == "已确认甲" and after is None


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
            from app.trip_understanding.route_connection import connection_evidence
            # A comparison fixture must explicitly connect its known endpoints.
            return NS(status="AVAILABLE",duration_minutes=duration,provider_binding={"route_connection": connection_evidence(a,b,
                [NS(longitude=s.longitude,latitude=s.latitude) for s in (a,b)])})
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


@pytest.mark.asyncio
async def test_near_expiry_daily_read_cannot_extend_snapshot_adoption_lifetime(monkeypatch):
    from app.trip_understanding import dining_jobs
    from app.trip_understanding.candidates import issue_candidate, verify_candidate
    from app.trip_understanding.errors import CommandTargetChangedError

    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "daily-expiry-boundary", now), now)
        _, etag = await repo.get_current_place_plan(resource)

        async def builder(result, plan, **_):
            async def search(**_kwargs):
                return [restaurant()]
            return await build_daily_meals(result, plan, search=search)

        assert await DailyDiningWorker(repo, builder=builder).run_once("expiry-worker")
        finished = now - timedelta(minutes=14, seconds=50)
        await repo._pool.execute("UPDATE trip_daily_dining_jobs SET finished_at=$1", finished)

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.current

        Clock.current = now
        monkeypatch.setattr(dining_jobs, "datetime", Clock)
        ready, _ = await read_daily_dining(repo, resource)
        available = next(day for day in ready.days if day.candidates)
        command = DiningInsertCommand(command_type="DINING_INSERT",
            after_activity_token=available.after_activity_token, insert_before=available.insert_before,
            meal_role="LUNCH", candidate_token=available.candidates[0].candidate_token)
        Clock.current = now + timedelta(seconds=10)
        assert (await read_daily_dining(repo, resource))[0].status == "NEEDS_UPDATE"
        with pytest.raises(CommandTargetChangedError):
            await TripUnderstandingApplicationService(repo).apply_command(resource, command,
                expected_etag=etag, idempotency_key="expired-daily-adoption", now=Clock.current)
        assert (await repo.get_current_place_plan(resource))[1] == etag

        # Manual place candidates have no recommendation snapshot and retain
        # their existing ten-minute validity. A refresh cannot lengthen either.
        plain = issue_candidate(restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=available.after_activity_token, expected_etag=etag, now=now)
        binding = dict(public_resource_id=resource.public_resource_id,
            activity_token=available.after_activity_token, expected_etag=etag)
        assert verify_candidate(plain.candidate_token, **binding, now=now + timedelta(minutes=9)) == restaurant()
        with pytest.raises(CommandTargetChangedError):
            verify_candidate(plain.candidate_token, **binding, now=now + timedelta(minutes=10))


@pytest.mark.asyncio
async def test_daily_refresh_remembers_old_keys_after_later_jobs_and_cooldown():
    from app.trip_understanding.errors import IdempotencyConflictError
    async with repository_for("postgres") as repo:
        now=datetime.now(timezone.utc)
        resource=await finish(repo,await create(repo,"daily-replay-history",now),now)
        _,etag=await repo.get_current_place_plan(resource)
        first=await read_daily_dining(repo,resource,request_key="first-refresh",expected_etag=etag)
        # A later failed generation may be explicitly retried. An old HTTP
        # replay must not spend another provider attempt after that cooldown.
        await repo._pool.execute("""UPDATE trip_daily_dining_jobs SET status='UNAVAILABLE',
            finished_at=NOW()-INTERVAL '1 minute',attempts=1""")
        await read_daily_dining(repo,resource,request_key="second-refresh",expected_etag=etag)
        await repo._pool.execute("""UPDATE trip_daily_dining_jobs SET status='UNAVAILABLE',
            finished_at=NOW()-INTERVAL '1 minute',attempts=2""")
        replay_info={}
        replay=await read_daily_dining(repo,resource,request_key="first-refresh",expected_etag=etag,replay_info=replay_info)
        assert replay == first and replay_info["replayed"]
        assert await repo._pool.fetchval("SELECT status FROM trip_daily_dining_jobs") == "UNAVAILABLE"
        with pytest.raises(IdempotencyConflictError):
            await read_daily_dining(repo,resource,request_key="first-refresh",expected_etag="a-different-version")
        await TripUnderstandingApplicationService(repo).delete_trip(resource,capability_hash="a"*64,
            user_id=None,idempotency_key="delete-daily-replays",now=now)
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_idempotency_records WHERE scope=$1",
            f"understanding:{resource.understanding_id}:dining-refresh") == 0


@pytest.mark.asyncio
async def test_daily_api_authorization_preconditions_and_concurrent_refresh_replay():
    from fastapi import FastAPI
    from httpx import ASGITransport,AsyncClient
    from app.api import trip_understandings_v3 as api
    from app.trip_understanding.demo import DEMO_SOURCE_TEXT,build_demo_pipeline
    async with repository_for("postgres") as repo:
        app=FastAPI()
        app.include_router(api.router,prefix="/api")
        app.dependency_overrides[api.get_trip_understanding_repository]=lambda:repo
        transport=ASGITransport(app=app)
        async with AsyncClient(transport=transport,base_url="http://test") as owner, AsyncClient(transport=transport,base_url="http://test") as stranger:
            created=await owner.post("/api/v3/trip-understandings",json={"mode":"DEMO"},headers={"Idempotency-Key":"daily-api-owner"})
            assert created.status_code == 202
            now=datetime.now(timezone.utc)
            job=await repo.claim_next(worker_id="daily-api",now=now,lease_seconds=30)
            await repo.complete_job(job,await build_demo_pipeline().run(DEMO_SOURCE_TEXT),now=now)
            base="/api/v3/trip-understandings/"+created.json()["public_resource_id"]
            etag=(await owner.get(base+"/result")).headers["etag"]
            endpoint=base+"/daily-dining"
            assert (await owner.get(endpoint)).json()["status"] == "PREPARING"
            for method in ["GET","POST"]:
                assert (await stranger.request(method,endpoint)).status_code == 404
            assert (await owner.post(endpoint)).status_code == 428
            assert (await owner.post(endpoint,headers={"If-Match":etag})).status_code == 400
            assert (await owner.post(endpoint,headers={"If-Match":'"tu3_stale"',"Idempotency-Key":"stale"})).status_code == 409
            headers={"If-Match":etag,"Idempotency-Key":"daily-api-refresh"}
            replies=await asyncio.gather(*(owner.post(endpoint,headers=headers) for _ in range(2)))
            assert all(r.status_code == 200 and r.headers["etag"] == etag and r.headers["cache-control"] == "no-store" for r in replies)
            assert replies[0].json() == replies[1].json()
            assert sum(r.headers.get("idempotency-replayed") == "true" for r in replies) == 1
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_daily_dining_jobs") == 1
            conflict=await owner.post(endpoint,headers={**headers,"If-Match":'"tu3_stale"'})
            assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "IDEMPOTENCY_CONFLICT"
