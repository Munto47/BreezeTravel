"""Explicit supplier closure labels, not a promise of current opening hours.

The Shanghai name/address/coordinates below were captured in the public candidate
response at source-meal-owner-shanghai-live-v1/result.json (candidate index 2).
The supplier raw response was NOT captured: its id/category/admin fields below
are fixed test inputs, and no photograph or other fact is inferred from them.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_understandings_v3 as api
from app.trip_understanding.candidates import issue_candidate
from app.trip_understanding.dining import (
    dining_binding,
    select_dining_rows,
    source_meal_binding,
    source_meal_context,
    verify_command_candidate,
)
from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import CreateFullRequest, DiningInsertCommand, PlaceConfirmCommand, SourceMealRef
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_dining_recommendations import anchor, restaurant, row
from tests.test_experience_v3_journey import repository_for
from tests.test_source_meal_selection import SOURCE, build_source_meal_result


CAPTURED_NAME = "大壶春(四川中路店)(装修中)"
STATUSES = [
    "装修中",
    "暂停营业",
    "已关闭",
    "已停业",
    "停业装修",
    "装修停业",
    "停业整顿",
    "永久关闭",
    "暂停开放",
    "临时关闭",
    "暂不开放",
    "停止营业",
]


def test_captured_shanghai_name_cannot_become_an_available_meal_candidate():
    shanghai = anchor().model_copy(
        update={"city": "上海", "name": "固定上海用餐锚点", "longitude": 121.489, "latitude": 31.234}
    )
    fixed_supplier_row = {
        "id": "fixed-shanghai-closure",
        "name": CAPTURED_NAME,
        "address": "四川中路136号(广东路)",
        "location": "121.489181,31.234836",
        "pname": "上海市",
        "cityname": "上海市",
        "adname": "黄浦区",
        "adcode": "310101",
        "typecode": "050100",
        "type": "餐饮服务;中餐厅",
    }
    assert (
        select_dining_rows([fixed_supplier_row], anchor=shanghai, excluded_ids=set(), meal_only=True, query="大壶春")
        == []
    )


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("meal_only", [False, True])
def test_explicit_trailing_supplier_status_is_excluded_for_daily_and_source_meals(status, meal_only):
    assert (
        select_dining_rows(
            [{**row(), "name": f"固定饭店（{status}）"}], anchor=anchor(), excluded_ids=set(), meal_only=meal_only
        )
        == []
    )


@pytest.mark.parametrize(
    "name",
    [
        "大壶春(四川中路店)",
        "暂停时光餐厅",
        "不打烊餐厅",
        "装修风格餐厅",
        "固定饭店(未停业)",
        "固定饭店(正常营业)",
        "固定饭店(重新开业)",
        "固定饭店(装修风格店)",
    ],
)
def test_normal_names_negation_and_unknown_opening_facts_are_not_closure(name):
    (candidate,) = select_dining_rows(
        [{**row(), "name": name, "business": {"opentime_week": "周一全天关闭；其他时间以门店为准"}}],
        anchor=anchor(),
        excluded_ids=set(),
        meal_only=True,
    )
    assert candidate.name == name
    now = datetime.now(UTC)
    token = issue_candidate(
        candidate,
        public_resource_id="fixed-resource",
        expected_etag="fixed-etag",
        activity_token=dining_binding("a" * 24),
        now=now,
    )
    command = DiningInsertCommand(
        command_type="DINING_INSERT", after_activity_token="a" * 24, candidate_token=token.candidate_token
    )
    assert (
        verify_command_candidate(command, public_resource_id="fixed-resource", expected_etag="fixed-etag", now=now).name
        == name
    )


@pytest.mark.parametrize("command_kind", ["source_meal", "daily_meal", "restaurant_confirmation"])
@pytest.mark.parametrize("status", ["装修中", "暂停营业", "已关闭"])
def test_previously_issued_closed_restaurant_token_is_rejected_at_actual_verification(command_kind, status):
    now, token = datetime.now(UTC), "a" * 24
    place = restaurant().model_copy(update={"name": f"固定饭店({status})"})
    slot = SourceMealRef(day_index=1, slot_index=0)
    binding = (
        source_meal_binding(token, before=False, meal_slot=slot, meal_role="DINNER")
        if command_kind == "source_meal"
        else dining_binding(token)
        if command_kind == "daily_meal"
        else token
    )
    # Deliberately issue the legacy payload directly; querying first would filter
    # it and could not prove that an already open browser cannot still adopt it.
    issued = issue_candidate(
        place, public_resource_id="fixed-resource", activity_token=binding, expected_etag="fixed-etag", now=now
    )
    if command_kind == "restaurant_confirmation":
        command = PlaceConfirmCommand(
            command_type="PLACE_CONFIRM", activity_token=token, candidate_token=issued.candidate_token
        )
    else:
        command = DiningInsertCommand(
            command_type="DINING_INSERT",
            after_activity_token=token,
            candidate_token=issued.candidate_token,
            meal_slot=slot if command_kind == "source_meal" else None,
            meal_role="DINNER" if command_kind == "source_meal" else "LUNCH",
        )
    with pytest.raises(CommandTargetChangedError):
        verify_command_candidate(command, public_resource_id="fixed-resource", expected_etag="fixed-etag", now=now)


def test_source_meal_api_filters_unavailable_before_selecting_top_three_without_changing_trip():
    repo = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repo
    calls = []

    async def fixed_search(**kwargs):
        calls.append(kwargs)
        selected_anchor = kwargs["anchor"]
        rows = [
            {**row(), "id": str(i), "name": name, "location": f"{selected_anchor.longitude},{selected_anchor.latitude}"}
            for i, name in enumerate(["固定饭店(装修中)", "固定饭店(暂停营业)", "固定饭店(已关闭)", "固定饭店正常店"])
        ]
        return select_dining_rows(rows, **kwargs)

    app.dependency_overrides[api.get_dining_candidate_search] = lambda: fixed_search
    with TestClient(app) as client:
        created = client.post(
            "/api/v3/trip-understandings",
            headers={"Idempotency-Key": "closure-api"},
            json={"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}},
        )
        job = asyncio.run(repo.claim_next(worker_id="fixed-closure", now=datetime.now(UTC), lease_seconds=60))
        asyncio.run(repo.complete_job(job, asyncio.run(build_source_meal_result()), now=datetime.now(UTC)))
        path = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
        before = client.get(path + "/result")
        response = client.post(
            path + "/source-meal-candidates",
            headers={"If-Match": before.headers["etag"]},
            json={"meal_slot": {"day_index": 1, "slot_index": 1}, "query": "固定饭店"},
        )
        assert response.status_code == 200 and response.json()["status"] == "AVAILABLE"
        assert [c["name"] for c in response.json()["candidates"]] == ["固定饭店正常店"]
        assert len(calls) == 1
        after = client.get(path + "/result")
        assert after.headers["etag"] == before.headers["etag"] and after.json() == before.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_source_dinner_rejects_old_closed_token_without_saving_or_consuming_slot(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(UTC)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(
            CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner",
            idempotency_key="closed-store-trip",
            now=now,
        )
        job = await repo.claim_next(worker_id="fixed-closure", now=now, lease_seconds=60)
        await repo.complete_job(job, await build_source_meal_result(), now=now)

        async def read():
            resource = await repo.authorize(
                created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now
            )
            return resource, await repo.get_result(resource)

        resource, before = await read()
        plan, etag = await repo.get_current_place_plan(resource)
        slot = SourceMealRef(day_index=1, slot_index=1)
        context = source_meal_context(before.result, plan, slot)
        closed = restaurant().model_copy(update={"name": "固定饭店(装修中)"})
        issued = issue_candidate(
            closed,
            public_resource_id=resource.public_resource_id,
            expected_etag=etag,
            now=now,
            activity_token=source_meal_binding(
                context.after_activity_token, before=context.insert_before, meal_slot=slot, meal_role=context.meal_role
            ),
        )
        command = DiningInsertCommand(
            command_type="DINING_INSERT",
            after_activity_token=context.after_activity_token,
            insert_before=context.insert_before,
            meal_slot=slot,
            meal_role=context.meal_role,
            candidate_token=issued.candidate_token,
        )
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(
                resource, command, expected_etag=etag, idempotency_key="closed-store-adopt", now=now
            )
        _, after = await read()
        assert after.opaque_etag == before.opaque_etag and after.result == before.result
        assert after.result.days[0].meal_slots[1].selection_status == "UNSELECTED"
