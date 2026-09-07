"""Twelve controlled HTTP journeys; model/map outputs are injected, not live evidence."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_understandings_v3 as api
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.map_render import InternalRouteModeFact, MapRenderer, RouteGeometryPoint
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import ResolvedPlace
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.worker import TripUnderstandingWorker


CITIES = [
    ("北京", ["故宫博物院", "景山公园"], [(116.397, 39.918), (116.397, 39.925)]),
    ("上海", ["上海博物馆", "豫园"], [(121.475, 31.228), (121.492, 31.227)]),
    ("杭州", ["浙江省博物馆", "岳王庙"], [(120.137, 30.252), (120.122, 30.255)]),
]
SCENARIOS = ["normal", "ambiguous", "schedule_conflict", "edit_recovery"]


class FixedSemantics:
    def __init__(self, payload):
        self.payload = payload

    async def propose(self, source_text):
        return proposal_from_draft(source_text, SemanticDraft.model_validate(self.payload))


class RecordedPlaces:
    def __init__(self, city, names, positions):
        self.city, self.names, self.positions = city, names, positions

    async def resolve(self, *, city, atomic_place_name, category_hint):
        assert city == self.city
        if atomic_place_name not in self.names:
            return None
        index = self.names.index(atomic_place_name)
        x, y = self.positions[index]
        return ResolvedPlace(canonical_place_id=f"controlled:{city}:{index}", name=atomic_place_name,
            category="景点", area_or_address=f"{city}记录地点", provider_binding={"provider": "CONTROLLED_TEST",
                "coordinates": {"longitude": x, "latitude": y}, "external_calls": 0})


class TwentyMinuteRoutes:
    def __init__(self):
        self.calls = 0

    async def route(self, origin, destination, mode, *, observed_at):
        self.calls += 1
        return InternalRouteModeFact(mode=mode, status="AVAILABLE", duration_minutes=20,
            distance_meters=1200, transfer_count=0, request_hash="a" * 64, response_hash="b" * 64,
            geometry=[RouteGeometryPoint(longitude=origin.longitude, latitude=origin.latitude),
                      RouteGeometryPoint(longitude=destination.longitude, latitude=destination.latitude)],
            provider_binding={"provider": "CONTROLLED_TEST"}, external_call_count=0,
            observed_at=observed_at, expires_at=observed_at + timedelta(hours=24))


@pytest.mark.parametrize("city,names,positions", CITIES, ids=["beijing", "shanghai", "hangzhou"])
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_anonymous_http_journey(city, names, positions, scenario):
    source = f"{city} 9月12日10点去{names[0]}游览两小时，11点去{names[1]}。旧行程取消。"
    activities = [
        dict(source_quote=f"10点去{names[0]}游览两小时", place_name=names[0], role="PLANNED", day_index=1,
             start_time="10:00", visit_duration_minutes=120, time_evidence=f"10点去{names[0]}游览两小时"),
        dict(source_quote=f"11点去{names[1]}", place_name=names[1], role="PLANNED", day_index=1,
             start_time="11:00", visit_duration_minutes=None, time_evidence=f"11点去{names[1]}"),
        dict(source_quote="旧行程取消", place_name=None, role="EXCLUDED"),
    ]
    if scenario == "ambiguous":
        activities[0]["place_name"] = None
    semantics = FixedSemantics(dict(destination=city, day_labels=["9月12日"], activities=activities))
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repository
    client = TestClient(app)
    pipeline = TripUnderstandingPipeline(semantics, RecordedPlaces(city, names, positions))
    worker = TripUnderstandingWorker(repository, full_pipeline=pipeline)
    routes = TwentyMinuteRoutes()
    map_worker = MapRenderWorker(repository, renderer=MapRenderer(routes))

    created = client.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": source}},
                          headers={"Idempotency-Key": "create"})
    assert created.status_code == 202, created.text
    assert "HttpOnly" in created.headers["set-cookie"]
    url = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
    assert asyncio.run(worker.run_once("controlled-model"))

    def result():
        response = client.get(url + "/result")
        assert response.status_code == 200, response.text
        return response

    def post(path, body=None, key=None, etag=None):
        return client.post(url + path, json=body, headers={
            "If-Match": etag or result().headers["etag"], "Idempotency-Key": key or uuid4().hex,
        })

    def drain_map():
        while asyncio.run(map_worker.run_once("controlled-map")):
            pass

    initial = result()
    cards = initial.json()["days"][0]["activities"]
    assert len(cards) == 2
    assert initial.json()["days"][0]["label"] == "9月12日"
    assert cards[0]["visit_duration_minutes"] == 120
    assert initial.json()["ownership"] == "ANONYMOUS"
    outsider = TestClient(app)
    assert outsider.get(url + "/result").status_code in {403, 404}

    if scenario == "ambiguous":
        assert cards[0]["status"] == "NEEDS_CONFIRMATION"
        async def candidates(**_kwargs):
            return [CandidatePlace(canonical_place_id="amap:controlled-choice", city=city,
                name=names[0], category="景点", area_or_address=f"{city}已记录地址",
                position=GCJ02Position(longitude=positions[0][0], latitude=positions[0][1]))]
        app.dependency_overrides[api.get_place_candidate_search] = lambda: candidates
        found = post("/place-candidates", {"activity_token": cards[0]["activity_token"], "query": names[0]})
        assert found.status_code == 200, found.text
        choice = found.json()["candidates"][0]
        confirmed = post("/commands", {"command_type": "PLACE_CONFIRM", "activity_token": cards[0]["activity_token"],
                                         "candidate_token": choice["candidate_token"]})
        assert confirmed.status_code == 200, confirmed.text
        assert result().json()["days"][0]["activities"][0]["status"] == "READY"
        assert routes.calls == 0
        assert post("/map-renders").status_code == 202

    drain_map()
    map_view = client.get(url + "/map-renders/latest")
    assert map_view.status_code == 200
    assert map_view.json()["status"] == "AVAILABLE", map_view.text
    assert len(map_view.json()["points"]) == 2
    assert map_view.json()["days"][0]["label"] == "9月12日"
    edge = map_view.json()["days"][0]["routes"][0]
    assert edge["from_activity_token"] == result().json()["days"][0]["activities"][0]["activity_token"]

    if scenario == "schedule_conflict":
        checked = post("/materialize")
        assert checked.status_code == 200, checked.text
        checks = client.get(url + "/checks")
        assert checks.status_code == 200, checks.text
        conflict = next(item for item in checks.json()["items"] if item["title"] == "这段时间来不及")
        assert conflict["label"] == "必须调整" and conflict["can_preview"]
        before = result()
        preview = post("/changes/preview", {"check_token": conflict["check_token"]})
        assert preview.status_code == 200, preview.text
        assert result().headers["etag"] == before.headers["etag"]
        assert preview.json()["changes"][0]["after"]["start_time"] == "12:20"
        applied = post("/changes/adopt", {"change_token": preview.json()["change_token"]})
        assert applied.status_code == 200, applied.text
        assert result().json()["days"][0]["activities"][1]["start_time"] == "12:20"
        assert result().json()["map"]["status"] == "NEEDS_UPDATE"
        assert post("/changes/adopt", {"change_token": preview.json()["change_token"]}, etag=before.headers["etag"]).status_code == 409

    if scenario == "edit_recovery":
        before = result()
        call_count = routes.calls
        command = {"command_type": "ACTIVITY_TIME_SET", "activity_token": cards[0]["activity_token"], "start_time": "09:00"}
        first = post("/commands", command, key="edit", etag=before.headers["etag"])
        replay = post("/commands", command, key="edit", etag=before.headers["etag"])
        assert first.status_code == replay.status_code == 200
        assert replay.headers["Idempotency-Replayed"] == "true"
        assert post("/commands", command, etag=before.headers["etag"]).status_code == 409
        assert routes.calls == call_count
        assert result().json()["map"]["status"] == "NEEDS_UPDATE"
        assert post("/commands", {"command_type": "UNDO"}).status_code == 200
        # Undo restores visible content in a new version; activity capabilities
        # rotate so an old open editor cannot target a resurrected activity.
        def visible_days(body):
            return [{"label": day["label"], "activities": [
                {key: value for key, value in card.items() if key != "activity_token"}
                for card in day["activities"]]} for day in body["days"]]
        assert visible_days(result().json()) == visible_days(before.json())
        assert result().headers["etag"] != before.headers["etag"]
        assert post("/commands", command).status_code == 409
        assert routes.calls == call_count
        assert post("/map-renders").status_code == 202
        drain_map()
        assert client.get(url + "/map-renders/latest").json()["status"] == "AVAILABLE"

    # Refresh recovery and deletion are part of every scenario.
    restored_client = TestClient(app)
    restored_client.cookies.update(client.cookies)
    assert restored_client.get(url + "/result").json() == result().json()
    deleted = client.delete(url, headers={"Idempotency-Key": "delete"})
    assert deleted.status_code == 204, deleted.text
    assert client.get(url + "/result").status_code == 410
