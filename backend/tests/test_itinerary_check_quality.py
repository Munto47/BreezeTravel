import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx
import app.trip_understanding.stay as stay_module

from app.audit.models import AuditFinding, AuditSeverity, AuditStatus, EvidenceFact, EvidenceSnapshot
from app.itineraries.models import ItineraryDay, ItineraryRevision, ItineraryStop, TripDateRange
from app.trip_understanding.g03 import (
    CalendarEvidenceRule,
    MealBreakRule,
    RepeatVisitRule,
    RouteAvailabilityRule,
    StayCommuteRule,
    calendar_profile,
    command_for_finding,
    preview_for_finding,
    public_checks,
    run_g03_audit,
)
from app.trip_understanding.schedule_checks import ScheduleFeasibilityRule
from app.trip_understanding.errors import ResourceNotFoundError, ResourceNotReadyError
from app.trip_understanding.map_repository import plan_with_stay_anchor
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.stay import (
    AmapStayCandidateProvider,
    ControlledStayRouteProvider,
    StayRecommendationEngine,
    assess_stay_commute,
    stay_plan_from_map,
)
from app.trip_understanding.stay_repository import _candidate_view
from tests.test_g02_map_stay import OneHotelProvider, _map_plan
from tests.test_experience_text_fidelity import DraftProvider, RecordingResolver, activity
from tests.test_experience_v3_journey import create, finish, repository_for


NOW = datetime(2026, 9, 6, 10, tzinfo=timezone.utc)


def stop(name, *, category="attraction", start=None, end=None, place=None):
    return ItineraryStop(stop_id=name, place_id=place or name, raw_name=name,
        day_index=0, order_index=0, category=category, start_time=start, end_time=end,
        resolution_status="AUTO_MATCHED")


def context(stops, facts=(), *, days=None, dated=False):
    day_groups = days or [stops]
    revision = ItineraryRevision(itinerary_id="quality-trip", workspace_id="quality-workspace",
        revision=1, source_type="IMPORT", city="北京", date_range=TripDateRange(),
        days=[ItineraryDay(day_index=index, stops=[item.model_copy(update={"day_index": index, "order_index": order})
            for order, item in enumerate(group)]) for index, group in enumerate(day_groups)],
        created_by="synthetic", created_at=NOW, content_hash="a" * 64)
    snapshot = EvidenceSnapshot(snapshot_id="quality-snapshot", workspace_id=revision.workspace_id,
        itinerary_revision=1, policy_version="quality-test", facts=list(facts), created_at=NOW)
    return SimpleNamespace(revision=revision, evidence_snapshot=snapshot, now=NOW,
        task_spec=SimpleNamespace(date_range=SimpleNamespace(start=NOW.date() if dated else None)))


def route(minutes=20, **updates):
    values = {"walking": "AVAILABLE", "transit": "AVAILABLE", "selected_mode": "walking",
        "selected_duration_minutes": minutes}
    values.update(updates.pop("value", {}))
    return EvidenceFact(fact_id="route", snapshot_id="quality-snapshot", subject_type="ROUTE_EDGE",
        subject_id="a->b", fact_type="ROUTE_MODE_SET", value=values, provider="synthetic",
        observed_at=NOW, valid_until=NOW + timedelta(hours=1), response_hash="b" * 64,
        confidence=1, freshness_status="FRESH").model_copy(update=updates)


@pytest.mark.parametrize("minutes", [None, 0, -1, True, 12.5, "20"])
def test_available_flag_never_proves_missing_or_invalid_duration(minutes):
    findings = RouteAvailabilityRule().evaluate(context([stop("a"), stop("b")], [route(minutes)]))
    assert [item.status for item in findings] == [AuditStatus.UNKNOWN]


@pytest.mark.parametrize("updates", [
    {"value": {"selected_mode": "driving"}},
    {"value": {"selected_mode": None}},
    {"value": {"selected_mode": []}},
    {"value": {"walking": "UNAVAILABLE"}},
    {"freshness_status": "STALE"},
    {"freshness_status": "CONFLICTING"},
    {"valid_until": NOW},
    {"valid_until": None},
    {"valid_from": NOW + timedelta(minutes=1)},
])
def test_wrong_mode_or_noncurrent_route_is_unknown(updates):
    findings = RouteAvailabilityRule().evaluate(context([stop("a"), stop("b")], [route(**updates)]))
    assert findings[0].status == AuditStatus.UNKNOWN


def test_competing_route_facts_never_select_the_last_optimistic_value():
    for facts in ([route(120), route(10).model_copy(update={"fact_id": "other"})],
                  [route(10), route(120).model_copy(update={"fact_id": "other"})]):
        findings = RouteAvailabilityRule().evaluate(context([stop("a"), stop("b")], facts))
        assert findings[0].status == AuditStatus.UNKNOWN


@pytest.mark.parametrize("facts, reason", [([], "ROUTE_CONFIRMATION_REQUIRED"), ([route(120)], "ROUTE_TOO_LONG")])
def test_meal_placeholder_preserves_real_route_findings(facts, reason):
    before = context([stop("a"), stop("b")], facts)
    after = context([stop("a"), stop("meal", category="meal_break"), stop("b")], facts)
    assert [item.reason_code for item in RouteAvailabilityRule().evaluate(before)] == [reason]
    findings = RouteAvailabilityRule().evaluate(after)
    assert [item.reason_code for item in findings] == [reason]
    assert findings[0].affected_stop_ids == ["a", "b"]


def test_explicit_overlap_survives_missing_routes_and_stale_map():
    ctx = context([stop("a", start="10:00", end="12:00"), stop("b", start="11:00", end="13:00")])
    finding = ScheduleFeasibilityRule().evaluate(ctx)[0]
    assert finding.reason_code == "SCHEDULE_TIME_OVERLAP" and finding.status == AuditStatus.VIOLATED
    assert not finding.repairable and finding.evidence_fact_ids == []
    view = public_checks(SimpleNamespace(findings=[finding]), ctx.evidence_snapshot,
        check_tokens={finding.finding_id: "check-quality-0000000001"}, routes_current=False, now=NOW)
    assert view.items[0].label == "必须调整" and not view.items[0].can_preview
    assert not view.items[0].depends_on_routes


@pytest.mark.parametrize("stops", [
    [stop("a", start="10:00", end="12:00"), stop("b", start="11:00", end="13:00")],
    [stop("a"), stop("b")],
])
def test_rechecking_same_revision_creates_snapshot_specific_finding_ids(stops):
    first = context(stops)
    second = context(stops)
    second.evidence_snapshot = second.evidence_snapshot.model_copy(update={"snapshot_id": "refreshed-snapshot"})
    before, after = (ScheduleFeasibilityRule().evaluate(item) for item in (first, second))
    assert before and after
    assert {item.reason_code for item in before} == {item.reason_code for item in after}
    assert {item.finding_id for item in before}.isdisjoint(item.finding_id for item in after)


def test_missing_times_and_dates_are_scope_notes_instead_of_three_unusable_tasks():
    ctx = context([], days=[[stop(f"{day}a"), stop(f"{day}b")] for day in range(4)])
    findings = ScheduleFeasibilityRule().evaluate(ctx) + CalendarEvidenceRule().evaluate(ctx)
    assert sum(item.reason_code == "SCHEDULE_TIMES_MISSING" for item in findings) == 1
    view = public_checks(SimpleNamespace(findings=findings), ctx.evidence_snapshot,
        check_tokens={item.finding_id: "check-quality-0000000001" for item in findings}, now=NOW)
    assert view.items == [] and view.status == "STILL_NEEDS_CONFIRMATION"
    assert "未提供的活动时间" in view.message and "日期" in view.message
    assert "补充活动时间" not in view.message


def test_breakfast_is_not_a_full_day_meal_and_insertion_does_not_invent_times():
    ctx = context([stop("早餐", category="dining", start="08:00", end="09:00"), stop("a"), stop("b")])
    finding = MealBreakRule().evaluate(ctx)[0]
    assert finding.reason_code == "MEAL_BREAK_MISSING"
    command = command_for_finding(finding)
    assert command.start_time is None and command.end_time is None
    assert command.time_hint is None and command.visit_duration_minutes is None
    preview = preview_for_finding(finding, change_token="change-quality-00000001")
    assert "12:30" not in preview.model_dump_json()
    morning = context([stop("早餐", category="dining", start="08:00", end="09:00"), stop("a", start="09:30", end="11:00")])
    assert MealBreakRule().evaluate(morning)[0].status == AuditStatus.SATISFIED


def test_untyped_opening_payload_and_missing_calendar_never_imply_pass():
    opening = route().model_copy(update={"subject_id": "a", "subject_type": "PLACE", "fact_type": "OPENING_HOURS",
        "value": {"closed": True}})
    assert all(item.status == AuditStatus.UNKNOWN for item in CalendarEvidenceRule().evaluate(context([stop("a")], [opening], dated=True)))
    assert CalendarEvidenceRule().evaluate(context([stop("a")]))[0].status == AuditStatus.UNKNOWN


def test_same_day_repeat_is_advisory_and_preserves_an_intentional_return():
    ctx = context([stop("a", place="museum"), stop("b"), stop("return", place="museum")])
    before = ctx.revision.model_dump_json()
    finding = RepeatVisitRule().evaluate(ctx)[0]
    assert finding.reason_code == "REPEATED_VISIT_REVIEW" and not finding.repairable
    assert "有意再次到访可以保留" in finding.message
    assert ctx.revision.model_dump_json() == before
    assert RepeatVisitRule().evaluate(context([], days=[[stop("a", place="museum")], [stop("return", place="museum")]])) == []


def stay_row():
    return {"public_candidate_token": "stay-quality-00000000001", "name": "汉庭酒店（合成店）", "brand": "汉庭",
        "area_or_address": "合成地址", "max_single_leg_minutes": 120, "transfer_count": 8, "missing_leg_count": 1}


def stay_leg(minutes=20, *, status="AVAILABLE", expires=NOW + timedelta(hours=1)):
    return {"selected_mode": "walking", "walking": {"mode": "walking", "status": status,
        "duration_minutes": minutes, "transfer_count": 0, "observed_at": NOW, "expires_at": expires}}


def test_legacy_partial_stay_recovers_only_verified_legs_and_never_exposes_120():
    assessment = assess_stay_commute([stay_leg(), {"selected_mode": None}], now=NOW, expected_missing=1)
    view = _candidate_view(stay_row(), assessment=assessment)
    assert view.max_single_leg_minutes == 20 and "已核对的路段中" in view.commute_summary
    assert "120" not in view.model_dump_json() and not assessment.complete
    legacy = _candidate_view(stay_row())
    assert legacy.max_single_leg_minutes is None and "120" not in legacy.model_dump_json()
    expired = _candidate_view(stay_row(), assessment=assess_stay_commute([stay_leg(expires=NOW)], now=NOW))
    assert expired.max_single_leg_minutes is None


def test_partial_or_legacy_stay_evidence_cannot_produce_verified_long_commute():
    for complete in (None, False):
        fact = route().model_copy(update={"fact_type": "STAY_COMMUTE", "subject_type": "STAY",
            "value": {"max_single_leg_minutes": 120, "complete": complete}})
        finding = StayCommuteRule().evaluate(context([stop("a")], [fact]))[0]
        assert finding.status == AuditStatus.UNKNOWN and finding.reason_code == "STAY_COMMUTE_UNKNOWN"


def test_historical_stay_report_is_downgraded_without_rewriting_stored_evidence():
    fact = route().model_copy(update={"fact_type": "STAY_COMMUTE", "subject_type": "STAY",
        "value": {"max_single_leg_minutes": 120}})
    ctx = context([stop("a")], [fact])
    finding = AuditFinding(finding_id="old-stay", rule_id="g03.stay_commute", rule_version="1.0.0",
        status=AuditStatus.VIOLATED, severity=AuditSeverity.MEDIUM, reason_code="STAY_COMMUTE_LONG",
        message="old", evidence_fact_ids=[fact.fact_id])
    view = public_checks(SimpleNamespace(findings=[finding]), ctx.evidence_snapshot,
        check_tokens={finding.finding_id: "historical-stay-check-0001"}, now=NOW)
    assert view.items[0].label == "需要确认" and view.items[0].basis_status == "NEEDS_RECHECK"
    assert "120" not in view.model_dump_json() and not view.items[0].can_preview
    assert fact.value["max_single_leg_minutes"] == 120


@pytest.mark.asyncio
async def test_partial_hotel_score_keeps_missing_leg_penalty_out_of_actual_minutes():
    class PartialRoutes:
        async def route(self, origin, destination, mode, *, observed_at):
            fact = await ControlledStayRouteProvider().route(origin, destination, mode, observed_at=observed_at)
            if origin.name.startswith("汉庭"):
                return fact.model_copy(update={"duration_minutes": 20})
            return fact.model_copy(update={"status": "UNAVAILABLE", "duration_minutes": None})

    plan = stay_plan_from_map(_map_plan())
    engine = StayRecommendationEngine(OneHotelProvider(), PartialRoutes())
    output = await engine.recommend(plan, observed_at=NOW)
    assert output.status == "PARTIAL" and output.candidates
    scored = output.candidates[0]
    assert scored.missing_leg_count > 0 and scored.max_single_leg_minutes == 20
    assert scored.total_score > scored.max_single_leg_minutes


@pytest.mark.parametrize("logical_start", [None,
    datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2040, 1, 1, tzinfo=timezone.utc)])
@pytest.mark.parametrize("clock_status", ["current", "future", "expired"])
@pytest.mark.asyncio
async def test_stay_route_response_clock_accepts_network_elapsed_but_rejects_future_and_expired(logical_start, clock_status):
    class ResponseClockRoutes:
        async def route(self, origin, destination, mode, *, observed_at):
            requested = time.perf_counter()
            await asyncio.sleep(0.02)
            response_at = (datetime.now(timezone.utc) if logical_start is None else
                observed_at + timedelta(seconds=time.perf_counter() - requested))
            if clock_status == "future":
                response_at += timedelta(hours=1)
            fact = await ControlledStayRouteProvider().route(origin, destination, mode, observed_at=response_at)
            return fact.model_copy(update={"expires_at": response_at - timedelta(seconds=1)}) if clock_status == "expired" else fact

    output = await StayRecommendationEngine(OneHotelProvider(), ResponseClockRoutes()).recommend(
        stay_plan_from_map(_map_plan()), observed_at=logical_start)
    if clock_status != "current":
        assert output.status == "UNAVAILABLE" and output.candidates == []
        return
    assert output.candidates and output.candidates[0].missing_leg_count == 0
    assert output.finished_at > output.started_at
    if logical_start is not None:
        assert output.started_at == logical_start and output.finished_at.year == logical_start.year
    assert all(output.started_at < fact.observed_at <= output.finished_at
        for leg in output.candidates[0].legs for fact in (leg.walking, leg.transit))


@pytest.mark.asyncio
async def test_concurrent_stay_recommendations_keep_their_own_logical_clocks():
    class LogicalResponseRoutes:
        async def route(self, origin, destination, mode, *, observed_at):
            requested = time.perf_counter()
            await asyncio.sleep(0.002)
            return await ControlledStayRouteProvider().route(origin, destination, mode,
                observed_at=observed_at + timedelta(seconds=time.perf_counter() - requested))

    engine = StayRecommendationEngine(OneHotelProvider(), LogicalResponseRoutes())
    starts = [datetime(year, 1, 1, tzinfo=timezone.utc) for year in (2020, 2040)]
    outputs = await asyncio.gather(*(engine.recommend(stay_plan_from_map(_map_plan()), observed_at=started) for started in starts))
    for output, started in zip(outputs, starts, strict=True):
        assert output.candidates and output.started_at == started
        assert all(leg.walking.observed_at.year == started.year for leg in output.candidates[0].legs)


@pytest.mark.asyncio
async def test_real_stay_clock_reads_wall_time_after_response_when_counters_diverge(monkeypatch):
    wall = SimpleNamespace(now=NOW)

    class WallClock:
        @classmethod
        def now(cls, tz=None):
            return wall.now

    class ResponseClockRoutes:
        async def route(self, origin, destination, mode, *, observed_at):
            await asyncio.sleep(0)
            # Deterministically reproduce wall-clock ticks advancing beyond
            # elapsed performance-counter time; no arbitrary grace interval.
            wall.now += timedelta(seconds=1)
            return await ControlledStayRouteProvider().route(origin, destination, mode, observed_at=wall.now)

    monkeypatch.setattr(stay_module, "datetime", WallClock)
    output = await StayRecommendationEngine(OneHotelProvider(), ResponseClockRoutes()).recommend(stay_plan_from_map(_map_plan()))
    assert output.candidates and output.candidates[0].missing_leg_count == 0
    assert output.started_at == NOW and output.finished_at == wall.now
    assert all(fact.status == "AVAILABLE" and output.started_at < fact.observed_at <= output.finished_at
        for leg in output.candidates[0].legs for fact in (leg.walking, leg.transit))


def hotel_poi():
    return {"id": "synthetic-hotel", "name": "汉庭酒店（合成店）", "cityname": "北京市", "pname": "北京市",
        "typecode": "100100", "type": "住宿服务;宾馆酒店", "location": "116.397,39.917", "address": ["合成路", "1号"]}


@pytest.mark.parametrize("radius", [2000, 4000, 8000, None])
@pytest.mark.asyncio
async def test_live_stay_adapter_uses_one_chain_keyword_and_preserves_bounded_query(radius):
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [hotel_poi()]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapStayCandidateProvider(api_key="synthetic-unused-key", client=client)
        rows = await provider.search(city="北京", longitude=116.397, latitude=39.916, radius_m=radius)
    assert len(requests) == 1 and len(rows) == 1
    params = requests[0].url.params
    assert params["keywords"] == "连锁酒店" and "|" not in params["keywords"]
    assert params["region"] == "北京" and params["city_limit"] == "true"
    assert params["page_size"] == "25" and params["page_num"] == "1" and params["types"] == "100000"
    assert requests[0].url.path.endswith("/text" if radius is None else "/around")
    if radius is None:
        assert "radius" not in params and "location" not in params
    else:
        assert params["radius"] == str(radius) and params["location"] == "116.397000,39.916000"
    assert rows[0].area_or_address == "合成路1号" and rows[0].brand is None


@pytest.mark.parametrize("change", [
    {"cityname": "上海市"}, {"typecode": "050100", "type": "餐饮服务"},
    {"type": "住宿服务;宾馆酒店;餐饮"}, {"type": ""},
    {"location": "121.47,31.23"}, {"location": "116.60,39.90"}, {"location": "NaN,39.917"},
    {"name": "https://example.test/汉庭酒店"}, {"name": "汉庭酒店需要提前预约"},
    {"name": "01012345678"}, {"name": ""}, {"id": ""},
])
@pytest.mark.asyncio
async def test_stay_query_keyword_does_not_override_identity_category_or_coordinate_guards(change):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request:
        httpx.Response(200, json={"status": "1", "pois": [{**hotel_poi(), **change}]}))) as client:
        provider = AmapStayCandidateProvider(api_key="synthetic-unused-key", client=client)
        assert await provider.search(city="北京", longitude=116.397, latitude=39.916, radius_m=2000) == []


@pytest.mark.asyncio
async def test_chain_keyword_is_not_brand_evidence_and_never_expands_search_count():
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"status": "1", "pois": [hotel_poi(),
            {**hotel_poi(), "id": "unregistered", "name": "合成连锁酒店"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        output = await StayRecommendationEngine(
            AmapStayCandidateProvider(api_key="synthetic-unused-key", client=client), ControlledStayRouteProvider(),
        ).recommend(stay_plan_from_map(_map_plan()), observed_at=NOW)
    assert len(requests) == 4
    assert [request.url.params.get("radius") for request in requests] == ["2000", "4000", "8000", None]
    assert [item.candidate.name for item in output.candidates] == ["汉庭酒店（合成店）"]
    assert output.candidates[0].candidate.brand == "汉庭"


@pytest.mark.asyncio
async def test_citywide_stay_search_preserves_city_bounds_without_a_radius_limit():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request:
        httpx.Response(200, json={"status": "1", "pois": [
            {**hotel_poi(), "location": "116.60,39.90"},
            {**hotel_poi(), "id": "outside-city", "location": "121.47,31.23"},
        ]}))) as client:
        rows = await AmapStayCandidateProvider(api_key="synthetic-unused-key", client=client).search(
            city="北京", longitude=116.397, latitude=39.916, radius_m=None)
    assert [item.canonical_place_id for item in rows] == ["synthetic-hotel"]


def test_full_report_keeps_unknown_scope_even_when_available_routes_have_no_conflict():
    ctx = context([stop("a", category="dining"), stop("b")], [route()])
    report = run_g03_audit(revision=ctx.revision, snapshot=ctx.evidence_snapshot,
        profile=calendar_profile([], day_count=1), room_id="synthetic", now=NOW)
    assert report.overall_status == AuditStatus.UNKNOWN


def multicity_plan(plan):
    return plan.model_copy(update={"stops": [stop.model_copy(update={"city": "上海"})
        if stop.day_index == 2 else stop for stop in plan.stops]})


def test_single_hotel_plan_and_map_anchor_require_one_matching_city():
    plan = _map_plan()
    assert stay_plan_from_map(plan) is not None
    assert stay_plan_from_map(multicity_plan(plan)) is None
    normalized = plan.model_copy(update={"stops": [stop.model_copy(update={"city": "北京市"})
        if index == 0 else stop for index, stop in enumerate(plan.stops)]})
    assert stay_plan_from_map(normalized) is not None
    kwargs = dict(selected_place_id="hotel", selected_name="合成酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    assert len(plan_with_stay_anchor(plan, **kwargs).stops) == len(plan.stops) + 4
    assert plan_with_stay_anchor(multicity_plan(plan), **kwargs).stops == multicity_plan(plan).stops
    assert plan_with_stay_anchor(plan, **{**kwargs, "selected_city": "杭州"}).stops == plan.stops


@pytest.mark.asyncio
async def test_legacy_multicity_stay_plan_cannot_call_hotel_or_route_providers():
    provider = OneHotelProvider()
    plan = stay_plan_from_map(_map_plan())
    anchors = [anchor.model_copy(update={"stop": anchor.stop.model_copy(update={"city": "上海"})})
        if anchor.day_index == 2 else anchor for anchor in plan.anchors]
    with pytest.raises(ValueError, match="cannot span cities"):
        await StayRecommendationEngine(provider).recommend(plan.model_copy(update={"anchors": anchors}), observed_at=NOW)
    assert provider.scopes == []


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_multicity_stay_is_explicitly_limited_and_never_enqueues_hotel_search(kind):
    source = "上海、杭州三日行程\nDay 1 上海\n外滩\nDay 2 杭州\n西湖\nDay 3 杭州\n灵隐寺"
    rows = [activity("外滩", city="上海", city_evidence="Day 1 上海"),
        activity("西湖", 2, city="杭州", city_evidence="Day 2 杭州"),
        activity("灵隐寺", 3, city="杭州", city_evidence="Day 3 杭州")]
    provider = DraftProvider(rows)
    provider.draft = provider.draft.model_copy(update={"destination": "上海、杭州"})
    output = await TripUnderstandingPipeline(provider, RecordingResolver()).run(source)
    assert [day.activities[0].city for day in output.public_result.days] == ["上海", "杭州", "杭州"]
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a" * 64, source_text=source,
            idempotency_key="multicity-stay", request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="multicity-text", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a" * 64, now=now)
        view = await repo.get_stay_view(resource)
        assert view.status == "LIMITED" and view.message == "跨城行程请按过夜城市分别选择住宿"
        assert not view.candidates and not view.available_actions
        assert (await repo.get_result(resource)).result.stay == view
        assert await repo.claim_next_stay(worker_id="no-hotel-search", now=now, lease_seconds=60) is None
        job_count = len(repo.stay_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT COUNT(*) FROM trip_stay_recommendation_jobs")
        assert job_count == 0


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_persisted_hotel_candidates_and_selection_cannot_bypass_multicity_guard(kind, monkeypatch):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "legacy-stay", now), now)
        worker = MapRenderWorker(repo)
        assert await worker.run_once("legacy-map", now=now)
        assert await worker.run_once("legacy-stay", now=now)
        stored = await repo.get_result(resource)
        candidate = (await repo.get_stay_view(resource)).candidates[0]
        service = TripUnderstandingApplicationService(repo)
        selected = await service.select_stay(resource, candidate_token=candidate.candidate_token,
            expected_etag=stored.opaque_etag, idempotency_key="legacy-selection", now=now)
        resource = await repo.authorize(resource.public_resource_id, capability_hash="a" * 64, now=now)
        assert (await repo.get_stay_view(resource)).candidates[0].selected
        # Replay stored single-hotel records against a now-detected cross-city
        # plan, without rewriting immutable business rows in PostgreSQL.
        if kind == "memory":
            original = repo._memory_plan
            monkeypatch.setattr(repo, "_memory_plan", lambda *args: multicity_plan(original(*args)))
        else:
            original = repo._read_map_plan
            async def cross_city(*args):
                return multicity_plan(await original(*args))
            monkeypatch.setattr(repo, "_read_map_plan", cross_city)
        view = await repo.get_stay_view(resource)
        assert view.status == "LIMITED" and "按过夜城市分别选择住宿" in view.message
        assert view.candidates == [] and (await repo.get_result(resource)).result.stay == view
        for key in ("legacy-selection", "new-attempt"):
            with pytest.raises((ResourceNotReadyError, ResourceNotFoundError)):
                await service.select_stay(resource, candidate_token=candidate.candidate_token,
                    expected_etag=selected.opaque_etag, idempotency_key=key, now=now)
        assert (await repo.get_result(resource)).opaque_etag == selected.opaque_etag
