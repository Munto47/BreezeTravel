import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import trip_understandings_v3 as api
from app.audit.models import AuditStatus, EvidenceFreshness
from app.trip_understanding import candidates
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.schedule_checks import ScheduleFeasibilityRule


@pytest.mark.parametrize("case", ["missing", "inconsistent", "suggested", "locked"])
def test_uncertain_times_never_become_pass_and_appointments_are_not_shifted(case):
    left = SimpleNamespace(stop_id="left", raw_name="前一站", start_time="10:00", end_time=None,
        visit_duration_minutes=120, locked=False, fixed_commitment=False)
    right = SimpleNamespace(stop_id="right", raw_name="后一站", start_time="11:00", end_time=None,
        visit_duration_minutes=60, locked=case == "locked", fixed_commitment=False)
    if case == "missing":
        left.visit_duration_minutes = None
    if case == "inconsistent":
        left.end_time = "11:00"
    context = SimpleNamespace(revision=SimpleNamespace(workspace_id="test", revision=1,
        change_summary={"timing_sources": {"left": "SUGGESTED" if case == "suggested" else "TEXT"}},
        days=[SimpleNamespace(day_index=0, stops=[left, right])]),
        evidence_snapshot=SimpleNamespace(snapshot_id="timing-test", facts=[SimpleNamespace(fact_id="route", fact_type="ROUTE_MODE_SET",
            subject_id="left->right", value={"selected_duration_minutes": 20}, freshness_status=EvidenceFreshness.FRESH)]))
    findings = ScheduleFeasibilityRule().evaluate(context)
    assert findings
    assert all(item.status == (AuditStatus.VIOLATED if case == "locked" else AuditStatus.UNKNOWN) for item in findings)
    assert not any(item.repairable for item in findings)


@pytest.mark.asyncio
async def test_candidate_search_rejects_wrong_city_category_sentence_and_coordinates(monkeypatch):
    row = {"id": "actual-poi", "name": "故宫博物院", "location": "116.397026,39.918058",
        "type": "风景名胜;风景名胜相关;旅游景点", "typecode": "110202", "pname": "北京市",
        "cityname": "北京市", "adname": "东城区", "address": "景山前街4号", "adcode": "110101"}
    rows = [row, {**row, "id": "wrong-city", "cityname": "上海市"},
        {**row, "id": "wrong-category", "type": "餐饮服务;中餐厅;中餐厅", "typecode": "050100"},
        {**row, "id": "sentence", "name": "记得预约。"},
        {**row, "id": "bad-position", "location": "120.1,30.2"}]
    async def query(self, **kwargs):
        return rows, {}
    monkeypatch.setattr(candidates, "get_settings", lambda: SimpleNamespace(amap_api_key="test-key", trip_understanding_provider_mode="live"))
    monkeypatch.setattr(candidates.AmapPlaceResolver, "_query_provider", query)
    results = await candidates.search_candidates(city="北京", query="故宫", category_hint="景点")
    assert len(results) == 1
    assert results[0].name == "故宫博物院"
    assert results[0].position.coordinate_system == "GCJ02"


def test_anonymous_http_checks_preview_adopt_candidates_and_undo():
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repository
    async def search(**kwargs):
        return [CandidatePlace(canonical_place_id="amap:private-id", city="北京", name="故宫博物院",
            category="景点", area_or_address="景山前街4号", position=GCJ02Position(longitude=116.397, latitude=39.918))]
    app.dependency_overrides[api.get_place_candidate_search] = lambda: search
    client = TestClient(app)
    created = client.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": DEMO_SOURCE_TEXT}}, headers={"Idempotency-Key": "anonymous-http"})
    assert created.status_code == 202
    resource_id = created.json()["public_resource_id"]
    base = f"/api/v3/trip-understandings/{resource_id}"
    async def finish():
        now = datetime.now(timezone.utc)
        job = await repository.claim_next(worker_id="http-test", now=now, lease_seconds=30)
        await repository.complete_job(job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=now)
    asyncio.run(finish())
    result = client.get(base + "/result")
    assert result.json()["ownership"] == "ANONYMOUS"
    etag = result.headers["etag"]
    materialized = client.post(base + "/materialize", headers={"If-Match": etag, "Idempotency-Key": "materialize"})
    assert materialized.status_code == 200
    checks = client.get(base + "/checks")
    assert checks.status_code == 200
    check = next(item for item in checks.json()["items"] if item["can_preview"])
    preview = client.post(base + "/changes/preview", json={"check_token": check["check_token"]}, headers={"Idempotency-Key": "preview"})
    assert preview.status_code == 200
    adopted = client.post(base + "/changes/adopt", json={"change_token": preview.json()["change_token"]}, headers={"If-Match": etag, "Idempotency-Key": "adopt"})
    assert adopted.status_code == 200
    undone = client.post(base + "/commands", json={"command_type": "UNDO"}, headers={"If-Match": adopted.headers["etag"], "Idempotency-Key": "undo"})
    assert undone.status_code == 200
    result = client.get(base + "/result")
    activity_token = result.json()["days"][0]["activities"][0]["activity_token"]
    candidates_response = client.post(base + "/place-candidates", json={"activity_token": activity_token, "query": "故宫"})
    assert candidates_response.status_code == 200
    selected = candidates_response.json()["candidates"][0]
    assert "canonical_place_id" not in selected
    assert "private-id" not in str(selected)
    confirmed = client.post(base + "/commands", json={"command_type": "PLACE_CONFIRM", "activity_token": activity_token,
        "candidate_token": selected["candidate_token"]}, headers={"If-Match": result.headers["etag"], "Idempotency-Key": "confirm"})
    assert confirmed.status_code == 200
    stranger = TestClient(app)
    assert stranger.get(base + "/checks").status_code == 404
