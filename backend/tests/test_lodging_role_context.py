"""Controlled regressions for uncertain hotel roles; no live quality claims."""
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.map_repository import map_view_with_points, plan_with_source_lodging, plan_with_stay_anchor
from app.trip_understanding.map_render import MapRenderView
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.overnight_context import overnight_segments, stay_context_hash
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.stay import ControlledStayRouteProvider, StayRecommendationEngine, stay_plan_from_map
from app.trip_understanding.stay_repository import _overnight_metadata, _segmented_view
from tests.test_g02_map_stay import _map_plan, _test_registry
from tests.test_experience_inference import Client, provider
from tests.test_experience_text_fidelity import DraftProvider, RecordingResolver
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_partial_recovery import activity
from tests.test_stay_overnight_segments import CityHotels


@pytest.mark.parametrize("day", [1, 2, 3])
@pytest.mark.asyncio
async def test_uncertain_named_hotel_blocks_only_affected_nights_without_claiming_a_booking(day):
    base = _map_plan()
    hotel = base.stops[0].model_copy(update={"name": "具体的原酒店", "canonical_place_id": "source-hotel",
        "category": "住宿", "day_index": day, "day_label": f"Day {day}", "sequence_index": 0,
        "lodging_role_uncertain": True})
    plan = base.model_copy(update={"stops": [hotel, *[s.model_copy(update={"sequence_index": s.sequence_index + 1})
        if s.day_index == day else s for s in base.stops]]})
    contexts = overnight_segments(plan)
    expected_affected = {1} if day == 1 else {1, 2} if day == 2 else {2}
    affected = {n for s in contexts if s.pending_lodging_roles for n in s.overnight_days}
    assert affected == expected_affected
    assert all(not s.preserved_hotels for s in contexts)
    assert all(s.uncertain and len(s.missing_boundaries) == 2
        and {m["reason"] for m in s.missing_boundaries} == {"LODGING_ROLE_UNCONFIRMED"}
        for s in contexts if s.pending_lodging_roles)
    result = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert not any(set(scored.candidate.provider_binding["overnight_days"]) & expected_affected for scored in result.candidates)
    for metadata in result.provider_binding["segments"]:
        if metadata["pending_lodging_roles"]:
            assert metadata["status"] == "LIMITED" and hotel.name in metadata["message"]
    view = _segmented_view(_overnight_metadata(plan), [])
    assert all(segment.status == "LIMITED" and "用途还需确认" in segment.message
        for segment in view.segments if set(int(d.split()[1]) for d in segment.overnight_days) & expected_affected)
    mapped = plan_with_stay_anchor(plan, selected_place_id="other", selected_name="新酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    assert hotel in mapped.stops
    assert not any(s.is_stay_anchor and s.day_index == n and s.sequence_index > 0
        for s in mapped.stops for n in expected_affected)
    assert plan_with_source_lodging(plan).stops == plan.stops
    legacy = plan.model_copy(update={"stops": [s.model_copy(update={"lodging_role_uncertain": False}) for s in plan.stops]})
    assert stay_context_hash(plan) != stay_context_hash(legacy)
    if day < 3:
        assert any(hotel.name in s.preserved_hotels for s in overnight_segments(legacy))


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_failed_metadata_repair_keeps_named_hotel_and_limited_night_after_storage(kind):
    source = "北京三日游。Day1：上午从北京饭店退房，然后去故宫博物院。Day2：天坛公园。Day3：颐和园。"
    draft = {"destination": "北京", "activities": [activity("北京饭店", category="住宿", lodging_event="CHECK_OUT"),
        activity("故宫博物院", category="景点"), activity("天坛公园", 2, category="景点"), activity("颐和园", 3, category="景点")]}
    client = Client(json.dumps(draft), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), RecordingResolver()).run(source)
    assert output.public_result.days[0].activities[0].lodging_role_uncertain
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="uncertain-hotel",
            request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="uncertain-hotel", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        plan, _ = await repo.get_current_place_plan(resource)
        hotel = next(s for s in plan.stops if s.name == "北京饭店")
        assert hotel.lodging_role_uncertain and hotel.lodging_event is None
        assert hotel.canonical_place_id and not hotel.source_place_is_placeholder and not hotel.is_stay_anchor
        contexts = overnight_segments(plan)
        assert contexts[0].pending_lodging_roles == [hotel.name] and not contexts[0].preserved_hotels
        assert not contexts[1].uncertain
        stay_job = await repo.claim_next_stay(worker_id="uncertain-hotel-stay", now=now, lease_seconds=60)
        stay_plan = await repo.load_stay_plan(stay_job)
        recommendation = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
            stay_plan, observed_at=now)
        await repo.complete_stay_job(stay_job, recommendation, now=now)
        stay = await repo.get_stay_view(resource)
        assert stay.segments[0].status == "LIMITED" and "用途还需确认" in stay.segments[0].message


def test_lodging_markers_are_separate_scoped_deduplicated_and_do_not_expose_internal_ids():
    plan = _map_plan().model_copy(update={"understanding_id": "private-aggregate-6aabff8e5f8244dab046530744250156"})
    mapped = plan_with_stay_anchor(plan, selected_place_id="provider-secret-hotel", selected_name="已选酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    empty = MapRenderView(status="AVAILABLE", message="地图可用")
    view = map_view_with_points(empty, mapped)
    assert [(p.day_label, p.name) for p in view.lodging_points] == [(f"Day {n}", "已选酒店") for n in [1, 2, 3]]
    assert view.lodging_points == map_view_with_points(empty, mapped).lodging_points
    assert len({p.point_token for p in view.lodging_points}) == 3
    serialized = json.dumps([p.model_dump() for p in view.lodging_points])
    assert "provider-secret-hotel" not in serialized and "activity_token" not in serialized
    assert "canonical_place_id" not in serialized and "private-aggregate" not in serialized
    assert empty.lodging_points == [] and map_view_with_points(empty, plan).lodging_points == []
    other = mapped.model_copy(update={"understanding_id": "different-private-aggregate"})
    revised = mapped.model_copy(update={"plan_ref": mapped.plan_ref.model_copy(update={"revision": mapped.plan_ref.revision + 1})})
    assert view.lodging_points[0].point_token != map_view_with_points(empty, other).lodging_points[0].point_token
    assert view.lodging_points[0].point_token != map_view_with_points(empty, revised).lodging_points[0].point_token
    # Old snapshots parse compatibly. No marker from unresolved or unselected cards.
    assert MapRenderView.model_validate({"status": "AVAILABLE", "message": "旧快照", "points": []}).lodging_points == []
    unconfirmed = mapped.model_copy(update={"stops": [s.model_copy(update={"resolution_status": "NEEDS_CONFIRMATION"})
        if s.is_stay_anchor else s for s in mapped.stops]})
    assert map_view_with_points(view, unconfirmed).lodging_points == []


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_terminal_map_worker_failure_keeps_unknown_calls_and_no_exception_secrets(kind, caplog, monkeypatch):
    class FailingRenderer:
        async def render(self, *args, **kwargs):
            raise RuntimeError("synthetic-secret-in-provider-url-must-not-be-recorded")

    source = "北京三日游。Day1：故宫博物院。Day2：天坛公园。Day3：颐和园。"
    output = await TripUnderstandingPipeline(DraftProvider([activity("故宫博物院", category="景点"),
        activity("天坛公园", 2, category="景点"), activity("颐和园", 3, category="景点")]), RecordingResolver()).run(source)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="failed-map",
            request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="failed-map", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        worker = MapRenderWorker(repo, renderer=FailingRenderer())
        worker.lease_takeover_renderer = FailingRenderer()
        for index in range(3):
            assert await worker.run_once("failed-map", now=now+timedelta(seconds=4*index))
        if kind == "memory":
            snapshot = next(iter(repo.map_snapshots.values()))
            binding, failure = snapshot.provider_binding, snapshot.failure
        else:
            pool = await repo._get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow("""SELECT s.provider_binding_json,s.failure_json FROM trip_map_render_snapshots s
                    JOIN trip_map_render_jobs j ON j.map_job_id=s.map_job_id WHERE j.understanding_id=$1""", resource.understanding_id)
            binding, failure = json.loads(row["provider_binding_json"]), json.loads(row["failure_json"])
            # Run both private read-only report queries against this disposable
            # test database. No experience database or provider is contacted.
            monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
            import read_private_measurement as usage_helper
            import read_private_lodging_measurement as lodging_helper

            monkeypatch.setattr(usage_helper.experience, "read_env", lambda _path: {})
            monkeypatch.setattr(usage_helper.experience, "environment", lambda _values: {"DATABASE_URL": pool._connect_args[0]})
            usage = await usage_helper.read(resource.public_resource_id)
            lodging = await lodging_helper.read(resource.public_resource_id)
            assert usage["status"] == lodging["status"] == "READ"
            assert usage["stay_jobs"][0]["status"] == "QUEUED"
            assert usage["stay_jobs"][0]["route_external_calls"] is None
            assert not usage["stay_jobs"][0]["all_attempt_metrics_complete"]
            assert lodging["map_jobs"][0]["failure_category"] == "MAP_RENDER_ERROR"
            assert lodging["map_jobs"][0]["metrics"]["external_calls"] is None
            assert not lodging["map_jobs"][0]["all_attempt_metrics_complete"]
            assert usage["map_jobs"] == lodging["map_jobs"]
        assert binding == {"execution_mode": "UNKNOWN", "external_calls": None, "metrics_complete": False}
        assert failure == {"category": "MAP_RENDER_ERROR"}
        assert "synthetic-secret" not in json.dumps(binding) + json.dumps(failure) + caplog.text
