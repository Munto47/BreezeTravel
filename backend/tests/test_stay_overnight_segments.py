"""Original controlled scenarios; no live claims or external provider requests."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from app.trip_understanding.map_repository import plan_with_stay_anchor
from app.trip_understanding.overnight_context import overnight_segments, stay_context_hash
from app.trip_understanding.stay import (ControlledStayRouteProvider, HotelBrandRegistry,
    AmapStayCandidateProvider, StayCandidate, StayRecommendationEngine, StaySearchRows, rank_stay_candidates, stay_plan_from_map)
from app.trip_understanding.models import UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_g02_map_stay import _map_plan
from tests.test_experience_v3_journey import repository_for
from tests.test_experience_text_fidelity import DraftProvider, RecordingResolver, activity


def test_three_day_nights_include_final_morning_and_exclude_arrival_morning():
    plan = _map_plan()
    before = plan.model_dump()
    stay = stay_plan_from_map(plan)
    assert [(a.day_index, a.direction) for a in stay.anchors] == [
        (1, "LAST_TO_STAY"), (2, "STAY_TO_FIRST"), (2, "LAST_TO_STAY"), (3, "STAY_TO_FIRST")]
    selected = plan_with_stay_anchor(plan, selected_place_id="hotel", selected_name="合成酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    assert [(s.day_index, s.sequence_index) for s in selected.stops if s.is_stay_anchor] == [(1, 2), (2, 0), (2, 2), (3, 0)]
    assert stay_context_hash(selected) == stay_context_hash(plan)
    assert plan.model_dump() == before


def test_known_hotel_is_preserved_and_missing_day_does_not_invent_an_overnight():
    plan = _map_plan()
    known = plan.stops[1].model_copy(update={"name": "已订的酒店", "category": "住宿"})
    plan = plan.model_copy(update={"stops": [plan.stops[0], known, *plan.stops[2:]]})
    assert overnight_segments(plan)[0].preserved_hotels == ["已订的酒店"]
    changed = plan_with_stay_anchor(plan, selected_place_id="new", selected_name="别的酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1])
    assert changed.stops == plan.stops
    gap = plan.model_copy(update={"stops": [s for s in plan.stops if s.day_index != 2]})
    assert all(s.uncertain or s.preserved_hotels for s in overnight_segments(gap))


def test_official_branch_identity_requires_name_city_and_street_number():
    registry = HotelBrandRegistry()
    for name in ("汉庭旁民宿", "全季风景公寓", "全季酒店附近公寓", "全季旁酒店"):
        assert registry.match(name) is None
    hotel = StayCandidate(canonical_place_id="synthetic", name="汉庭酒店(北京前门大街店)", category="住宿",
        city="北京", area_or_address="西经路1号", longitude=116.4, latitude=39.9)
    assert registry.identity(hotel, "汉庭")["property_identity"] == "OFFICIAL_NAME_ADDRESS"
    assert registry.identity(hotel.model_copy(update={"area_or_address": "西经路2号"}), "汉庭")["property_identity"] == "NAME_ONLY"
    closed = hotel.model_copy(update={"name": "全季酒店(杭州西湖湖滨店)", "city": "杭州", "area_or_address": "开元路66号"})
    assert registry.identity(closed, "全季")["property_status"] == "SUSPENDED"
    for item in (hotel.model_copy(update={"area_or_address": "西经路2号"}),
        hotel.model_copy(update={"name": "新全季悦享酒店", "area_or_address": "另一条街8号"})):
        identity = registry.identity(item, registry.match(item.name))
        assert identity == {"brand_group": None, "brand_priority": 1, "property_identity": "NAME_ONLY"}
    assert HotelBrandRegistry({"brands": [{"brand": "汉庭", "aliases": ["汉庭"], "priority": 0, "group": "华住"}],
        "properties": [{"brand": "汉庭", "city": "北京", "name_tokens": [], "address_tokens": ["西经路1号"]}]
    }).identity(hotel, "汉庭") == {"brand_group": None, "brand_priority": 1, "property_identity": "NAME_ONLY"}


class CityHotels:
    async def search(self, *, city, longitude, latitude, radius_m):
        return StaySearchRows([StayCandidate(canonical_place_id=f"{city}-{n}", name=f"汉庭酒店(合成{n}店)",
            category="住宿", city=city, area_or_address=f"测试路{n}号", longitude=longitude + n * .002,
            latitude=latitude, provider_binding={"external_calls": 1}) for n in range(12)], external_calls=1)


class CountedRoutes(ControlledStayRouteProvider):
    def __init__(self):
        self.calls = self.active = self.peak = 0

    async def route(self, *args, **kwargs):
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0)
        value = await super().route(*args, **kwargs)
        self.active -= 1
        return value.model_copy(update={"external_call_count": 1})


@pytest.mark.asyncio
async def test_official_property_recall_survives_nearby_name_only_rows_without_promoting_impostors():
    class OfficialRecall(CityHotels):
        async def search_property(self, **kwargs):
            assert kwargs["name"] == "汉庭北京前门大街酒店"
            return StaySearchRows([
                StayCandidate(canonical_place_id="official", name="汉庭酒店(北京前门大街店)", category="住宿", city="北京",
                    area_or_address="西经路1号", longitude=116.6, latitude=39.9),
                StayCandidate(canonical_place_id="impostor", name="全季悦享酒店", category="住宿", city="北京",
                    area_or_address="陌生路9号", longitude=116.4, latitude=39.9)], external_calls=1)
    output = await StayRecommendationEngine(OfficialRecall(), ControlledStayRouteProvider()).recommend(
        stay_plan_from_map(_map_plan()), observed_at=datetime.now(timezone.utc))
    assert output.provider_binding["candidate_provider_calls"] == 2
    assert "official" in {c.candidate.canonical_place_id for c in output.candidates[:3]}
    assert "impostor" not in {c.candidate.canonical_place_id for c in output.candidates}
    official = next(c.candidate for c in output.candidates if c.candidate.canonical_place_id == "official")
    assert official.brand == "汉庭" and official.provider_binding["brand_group"] == "华住"
    assert all(c.candidate.brand is None and c.candidate.provider_binding["brand_group"] is None
        for c in output.candidates if c.candidate.canonical_place_id != "official")


@pytest.mark.asyncio
async def test_budget_counts_requests_and_cache_reuses_actual_endpoints():
    now = datetime.now(timezone.utc)
    routes = CountedRoutes()
    engine = StayRecommendationEngine(CityHotels(), routes, task_route_budget=12)
    output = await engine.recommend(stay_plan_from_map(_map_plan()), observed_at=now)
    assert routes.calls == output.provider_binding["route_external_calls"] == 12
    assert routes.peak <= 4 and len(output.candidates) <= 6
    assert output.provider_binding["candidate_provider_calls"] == 1  # Not 12 rows.
    assert output.status == "PARTIAL"
    repeat = await engine.recommend(stay_plan_from_map(_map_plan()), observed_at=now + timedelta(seconds=1))
    assert repeat.provider_binding["route_cache_hits"] >= 12
    assert repeat.provider_binding["route_external_calls"] <= 12


@pytest.mark.asyncio
async def test_long_trip_enforces_each_segment_and_whole_task_limits():
    base = _map_plan()
    plan = base.model_copy(update={"stops": [base.stops[0].model_copy(update={
        "day_index": day, "day_label": f"Day {day}", "canonical_place_id": f"stop-{day}",
        "city": "北京" if day <= 7 else "上海", "longitude": 116.3 + day * .006 if day <= 7 else 121.3 + day * .006,
        "latitude": 39.9 if day <= 7 else 31.2}) for day in range(1, 15)]})
    routes = CountedRoutes()
    output = await StayRecommendationEngine(CityHotels(), routes, task_route_budget=160).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert sorted(output.provider_binding["segment_route_attempts"].values()) == [64, 96]
    assert routes.calls == output.provider_binding["route_external_calls"] == 160
    assert routes.peak <= 4 and output.status == "PARTIAL"


@pytest.mark.parametrize("preferred_minutes,first", [(20, "preferred"), (21, "nearby")])
@pytest.mark.asyncio
async def test_brand_preference_uses_ten_minute_average_leg_boundary_and_keeps_both_options(preferred_minutes, first):
    output = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider()).recommend(
        stay_plan_from_map(_map_plan()), observed_at=datetime.now(timezone.utc))
    template = output.candidates[0]
    def candidate(identifier, minutes, priority):
        return template.model_copy(update={"candidate": template.candidate.model_copy(update={
            "canonical_place_id": identifier, "provider_binding": {"brand_priority": priority}}),
            "total_score": minutes * 4, "missing_leg_count": 0, "legs": [leg.model_copy(update={
                "selected_mode": "walking", "walking": leg.walking.model_copy(update={"duration_minutes": minutes})})
                for leg in template.legs]})
    ranked = rank_stay_candidates([candidate("nearby", 10, 1), candidate("second", 11, 1),
        candidate("third", 12, 1), candidate("preferred", preferred_minutes, 0)])
    assert ranked[0].candidate.canonical_place_id == first
    assert {"nearby", "preferred"}.issubset({c.candidate.canonical_place_id for c in ranked[:3]})


@pytest.mark.parametrize("city,code,district,district_code,lng,lat", [
    ("广州", "440100", "越秀区", "440104", 113.27, 23.13),
    ("深圳", "440300", "福田区", "440304", 114.06, 22.54),
])
@pytest.mark.asyncio
async def test_southern_city_hotel_scope_checks_admin_fields_and_reuses_scope_cache(city, code, district, district_code, lng, lat):
    requests = []
    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/district"):
            keyword = request.url.params["keywords"]
            row = {"name": "广东省", "level": "province", "adcode": "440000"} if keyword == "440000" else {
                "name": city + "市", "level": "city", "adcode": code,
                "polyline": f"{lng-.1},{lat-.1};{lng+.1},{lat-.1};{lng+.1},{lat+.1}",
                "districts": [{"name": district, "level": "district", "adcode": district_code}]}
            return httpx.Response(200, json={"status": "1", "districts": [row]})
        hotel = {"id": "valid", "name": "汉庭酒店(合成店)", "typecode": "100100", "type": "住宿服务;宾馆酒店",
            "pname": "广东省", "cityname": city + "市", "adname": district, "adcode": district_code,
            "location": f"{lng},{lat}", "address": "合成路1号"}
        return httpx.Response(200, json={"status": "1", "pois": [hotel, {**hotel, "id": "wrong-admin", "adcode": "110101"}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = AmapStayCandidateProvider(api_key="synthetic-unused", client=client)
        first = await provider.search(city=city, longitude=lng, latitude=lat, radius_m=2000)
        second = await provider.search_area(city=city, area="合成区域", longitude=lng, latitude=lat)
    assert [row.canonical_place_id for row in first] == ["valid"]
    assert [row.canonical_place_id for row in second] == ["valid"]
    assert first.administrative_calls == 2 and second.administrative_calls == 0
    assert first.external_calls + second.external_calls == 2 and len(requests) == 4


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_two_city_selection_is_independent_replayable_and_undoable(kind):
    source = "北京、上海四日行程\nDay 1 北京\n故宫博物院\nDay 2 北京\n天坛公园\nDay 3 上海\n外滩\nDay 4 上海\n豫园"
    places, cities = ["故宫博物院", "天坛公园", "外滩", "豫园"], ["北京", "北京", "上海", "上海"]
    provider = DraftProvider([activity(n, day+1, city=c, city_evidence=f"Day {day+1} {c}") for day, (n, c) in enumerate(zip(places, cities))])
    provider.draft = provider.draft.model_copy(update={"destination": "北京、上海"})
    output = await TripUnderstandingPipeline(provider, RecordingResolver()).run(source)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="segments",
            request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="segments", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        stay_job = await repo.claim_next_stay(worker_id="segments-stay", now=now, lease_seconds=60)
        plan = await repo.load_stay_plan(stay_job)
        stay = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider()).recommend(plan, observed_at=now)
        await repo.complete_stay_job(stay_job, stay, now=now)
        service = TripUnderstandingApplicationService(repo)
        first = await repo.get_stay_view(resource)
        assert [(s.city, s.overnight_days) for s in first.segments] == [("北京", ["Day 1"]), (None, ["Day 2"]), ("上海", ["Day 3"])]
        assert first.segments[1].status == "LIMITED" and not first.segments[1].candidates
        jobs_before = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        for index in (0, 2):
            resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
            current = await repo.get_result(resource)
            view = await repo.get_stay_view(resource)
            candidate = view.segments[index].candidates[0]
            selected = await service.select_stay(resource, candidate_token=candidate.candidate_token,
                expected_etag=current.opaque_etag, idempotency_key=f"select-{index}", now=now)
            replay = await service.select_stay(resource, candidate_token=candidate.candidate_token,
                expected_etag=current.opaque_etag, idempotency_key=f"select-{index}", now=now)
            assert replay.replayed and replay.opaque_etag == selected.opaque_etag
        final = await repo.get_stay_view(resource)
        assert final.segments[0].candidates[0].selected and final.segments[2].candidates[0].selected
        jobs_after = len(repo.map_jobs) if kind == "memory" else await repo._pool.fetchval("SELECT count(*) FROM trip_map_render_jobs")
        assert jobs_before == jobs_after
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        current = await repo.get_result(resource)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=current.opaque_etag,
            idempotency_key="undo-second-stay", now=now)
        undone = await repo.get_stay_view(resource)
        assert undone.segments[0].candidates[0].selected
        assert not any(c.selected for c in undone.segments[2].candidates)
