"""Relative collaboration: retain selected visits; real transport facts only.

HTTP responses below are fixed transport fixtures, not live AMap evidence.
"""
import asyncio

import httpx
import pytest

from app.schemas.place import Place
from app.services.collaboration_relative_route import plan_relative_route, relative_days
from app.trip_understanding.collaboration_import import prepare_collaboration_import
pytest_plugins = ["tests.test_experience_runtime"]  # Real runtime HTTP fixture.


def places():
    return [Place(place_id=f"place_{i}", name=name, city="北京", address="北京",
                  category=category, coords={"lng": lng, "lat": lat}, estimated_duration=9999,
                  opening_hours="仅09:00-09:01") for i, (name, category, lng, lat) in enumerate([
                      ("故宫博物院", "attraction", 116.397, 39.918),
                      ("景山公园", "attraction", 116.397, 39.925),
                      ("全聚德前门店", "food", 116.397, 39.899),
                      ("北京饭店", "hotel", 116.409, 39.908)])]


@pytest.mark.asyncio
async def test_relative_selected_places_all_retained_without_clock_capacity_and_directed_routes():
    requests = []

    def transport(request):
        requests.append((request.url.params["origin"], request.url.params["destination"]))
        return httpx.Response(200, json={"status": "1", "route": {"paths": [{"duration": "660", "distance": "2300"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        result, total = await plan_relative_route(places(), trip_days=2, thread_id="fixed",
                                                  api_key="fixed", client=client)
    slots = [slot for day in result.days for slot in day.slots]
    assert len(result.days) == 2 and sorted(s.place_id for s in slots) == sorted(p.place_id for p in places())
    assert all(day.date is None for day in result.days)
    assert all(s.start_time is s.end_time is None and not s.tips for s in slots)
    expected_edges = [(f"{a.place['coords']['lng']},{a.place['coords']['lat']}",
                       f"{b.place['coords']['lng']},{b.place['coords']['lat']}")
                      for day in result.days for a, b in zip(day.slots, day.slots[1:])]
    assert requests == expected_edges and len(requests) == 2
    assert total == 4.6
    assert all(s.transport.duration_mins == 11 and s.transport.status == "AVAILABLE" for s in slots if s.transport)
    # Actual import does not re-run interpretation or drop hotel/food visits.
    route = result.model_dump(mode="json")
    prepared = prepare_collaboration_import(user_id="fixed", room_id="fixed", saved_itinerary_id="fixed",
        city="北京", itinerary_data=route, idempotency_key="fixed")
    assert [m.atomic_place_name for m in prepared.initial_plan.mentions] == [s.place['name'] for s in slots]
    assert all(m.start_time is m.end_time is m.time_hint is None for m in prepared.initial_plan.mentions)


@pytest.mark.parametrize("payload", [{"status": "0"}, {"status": "1", "route": {"paths": [{}]}},
    {"status": "1", "route": {"paths": [{"duration": "nan", "distance": "20"}]}}, []])
@pytest.mark.asyncio
async def test_unavailable_routes_never_become_estimates_or_remove_places(payload):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (
        calls.append(request.url.path) or httpx.Response(200, json=payload)))) as client:
        result, total = await plan_relative_route(places(), trip_days=1, thread_id="fixed", api_key="fixed", client=client)
    assert len(calls) == 3 and len(result.days[0].slots) == 4
    assert all(slot.transport is None for slot in result.days[0].slots) and total is None


@pytest.mark.asyncio
async def test_deadline_keeps_all_selected_without_retry_or_more_calls():
    calls = []

    async def slow(request):
        calls.append(request.url.path)
        await asyncio.sleep(1)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
        result, total = await plan_relative_route(places(), trip_days=1, thread_id="fixed", api_key="fixed", client=client,
                                                  deadline_seconds=0.01)
    assert len(calls) == 1 and len(result.days[0].slots) == 4 and total is None


def test_attraction_only_is_valid_and_more_days_keep_empty_day_labels():
    result = relative_days(places()[:2], 3)
    assert [[p.place_id for p in day] for day in result] == [["place_0"], ["place_1"], []]
    with pytest.raises(ValueError):
        relative_days([places()[0], places()[0]], 2)


def test_old_saved_schedule_import_adopts_relative_order_without_processing_invalid_legacy_clocks():
    saved = {"days": [{"date": "2026-09-06", "slots": [
        {"startTime": "22:00", "endTime": "09:00", "place": {"name": "景山公园", "category": "attraction"}}]}]}
    prepared = prepare_collaboration_import(user_id="fixed", room_id="fixed", saved_itinerary_id="fixed",
        city="北京", itinerary_data=saved, idempotency_key="fixed")
    assert prepared.source_text == "北京1日行程。\nDay 1\n去景山公园（景点）。"
    assert prepared.initial_plan.day_labels == {}
    assert prepared.initial_plan.mentions[0].start_time is None


@pytest.mark.asyncio
async def test_relative_api_never_enters_legacy_scheduler_or_uses_clock_preferences(monkeypatch):
    import importlib
    from types import SimpleNamespace
    from app.schemas.api import OptimizeRequest
    endpoint = importlib.import_module("app.api.optimize")
    calls = []

    async def member(*args, **kwargs):
        calls.append("membership")

    async def forbidden(*args, **kwargs):
        raise AssertionError("Relative route must not enter weather/scheduler/model tips")

    monkeypatch.setattr(endpoint, "require_room_member", member)
    monkeypatch.setattr(endpoint, "get_settings", lambda: SimpleNamespace(demo_mode=False, amap_api_key=""))
    monkeypatch.setattr(endpoint, "run_planner", forbidden)
    result = await endpoint.optimize(OptimizeRequest(thread_id="fixed", room_id="room", relative_only=True,
        places=places()[:2], trip_days=2, user_prefs={"wake_up_time": "09:00"}), current_user="owner")
    assert calls == ["membership"] and result.backup_pool == []
    assert sum(len(day.slots) for day in result.itinerary.days) == 2
    assert all(day.date is None and all(s.start_time is None for s in day.slots) for day in result.itinerary.days)


def test_actual_experience_http_projects_clockless_relative_route_and_real_direction_facts(client, monkeypatch):
    from app import experience_main as runtime
    from app.utils.auth import get_optional_user
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    http, _cache = client
    http.app.dependency_overrides[get_optional_user] = lambda: "owner"
    monkeypatch.setattr(runtime.optimize, "require_room_member", AsyncMock())
    monkeypatch.setattr(runtime.optimize, "get_settings", lambda: SimpleNamespace(demo_mode=False, amap_api_key="fixed"))
    actual_client = httpx.AsyncClient
    calls = []

    def fixed(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status":"1", "route":{"paths":[{"duration":"900", "distance":"3000"}]}})

    monkeypatch.setattr(runtime.optimize.httpx, "AsyncClient", lambda **kwargs:
        actual_client(transport=httpx.MockTransport(fixed), **kwargs))
    result = http.post("/api/optimize", json={"thread_id":"fixed", "room_id":"fixed", "relative_only":True,
        "trip_days":1, "places":[p.model_dump(mode="json") for p in places()[:2]]})
    assert result.status_code == 200, result.text
    body = result.json()
    slots = body['itinerary']['days'][0]['slots']
    assert len(calls) == 1 and len(slots) == 2
    assert slots[0]['transport'] == {"mode":"driving", "duration_mins":15, "distance_km":3.0, "status":"AVAILABLE"}
    assert slots[1]['transport'] is None
    assert all(s['start_time'] is None and s['end_time'] is None for s in slots)
    assert body['backup_pool'] == []
    assert 'task_spec' not in body and 'source_quote' not in str(body)
