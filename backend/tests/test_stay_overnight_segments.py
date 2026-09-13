"""Original controlled scenarios; no live claims or external provider requests."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from app.trip_understanding.map_repository import _plan_for_result, plan_with_stay_anchor, plan_with_source_lodging
from app.trip_understanding.overnight_context import overnight_segments, stay_context_hash
from app.trip_understanding.stay import (ControlledStayRouteProvider, HotelBrandRegistry,
    AmapStayCandidateProvider, StayCandidate, StayRecommendationEngine, StaySearchRows, rank_stay_candidates, stay_plan_from_map)
from app.trip_understanding.stay_repository import _overnight_metadata, _segmented_view
from app.trip_understanding.models import ActivityCardView, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_g02_map_stay import _map_plan, _test_registry
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


@pytest.mark.asyncio
async def test_real_beijing_shape_unresolved_qianmen_is_not_replaced_by_temple_as_last_stop():
    base = _map_plan()
    unknown = base.stops[2].model_copy(update={"name": "前门大街", "sequence_index": 1,
        "canonical_place_id": None, "resolution_status": "UNRESOLVED", "longitude": None, "latitude": None})
    plan = base.model_copy(update={"day_count": 3, "stops": [*base.stops, unknown]})
    contexts = overnight_segments(plan)
    assert [c.overnight_days for c in contexts] == [[1], [2]]
    assert not contexts[0].uncertain and contexts[1].uncertain
    assert contexts[1].missing_boundaries == [{"day": 2, "direction": "LAST_TO_STAY", "name": "前门大街", "reason": "UNCONFIRMED_PLACE"}]
    assert not any(day == 2 and direction == "LAST_TO_STAY" for day, direction, _ in contexts[1].anchors)
    result = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert result.status == "PARTIAL" and result.candidates
    assert result.provider_binding["expected_boundary_count"] == 4
    assert result.provider_binding["missing_boundary_count"] == 1
    assert all(c.candidate.provider_binding["overnight_days"] == [1] for c in result.candidates)
    selected = plan_with_stay_anchor(plan, selected_place_id="hotel", selected_name="已选酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    assert [(s.day_index, s.sequence_index) for s in selected.stops if s.is_stay_anchor] == [(1, 2), (2, 0)]


@pytest.mark.parametrize("unknown_city", [False, True])
def test_unknown_next_morning_boundary_does_not_skip_to_later_confirmed_stop(unknown_city):
    base = _map_plan()
    first = base.stops[2].model_copy(update={"name": "待确认的早晨地点", "city": None if unknown_city else "北京",
        **({} if unknown_city else {"canonical_place_id": None, "resolution_status": "UNRESOLVED", "longitude": None, "latitude": None})})
    later = base.stops[2].model_copy(update={"sequence_index": 1})
    plan = base.model_copy(update={"day_count": 3, "stops": [*base.stops[:2], first, later, base.stops[3]]})
    contexts = overnight_segments(plan)
    assert contexts[0].uncertain and not contexts[1].uncertain
    assert any(issue["name"] == "待确认的早晨地点" for issue in contexts[0].missing_boundaries)
    assert not any(day == 2 and direction == "STAY_TO_FIRST" and stop.name == later.name for day, direction, stop in contexts[0].anchors)


@pytest.mark.parametrize("empty", ["first", "last", "all"])
def test_empty_days_preserve_each_night_in_boundary_denominator(empty):
    base = _map_plan()
    stops = [] if empty == "all" else [s for s in base.stops if s.day_index != (1 if empty == "first" else 3)]
    contexts = overnight_segments(base.model_copy(update={"stops": stops, "day_count": 3}))
    assert sum(len(s.overnight_days) for s in contexts) == 2
    assert sum(s.expected_boundary_count for s in contexts) == 4
    assert any(issue["reason"] == "EMPTY_DAY" for s in contexts for issue in s.missing_boundaries)


@pytest.mark.parametrize("event", ["CHECK_OUT", "DEPARTURE", "LUGGAGE_PICKUP"])
def test_first_day_hotel_visit_does_not_reserve_that_night_or_block_new_stay(event):
    base = _map_plan()
    hotel = base.stops[0].model_copy(update={"canonical_place_id": "source-hotel", "name": "原酒店",
        "category": "住宿", "lodging_event": event, "sequence_index": 0})
    plan = base.model_copy(update={"stops": [hotel, *[s.model_copy(update={"sequence_index": s.sequence_index + 1})
        if s.day_index == 1 else s for s in base.stops]]})
    assert all(not segment.preserved_hotels for segment in overnight_segments(plan))
    mapped = plan_with_stay_anchor(plan, selected_place_id="new-hotel", selected_name="新选酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    first = [s for s in mapped.stops if s.day_index == 1]
    assert first[0].name == "原酒店" and first[-1].name == "新选酒店"
    assert first[0].lodging_event == event and not first[0].is_stay_anchor
    assert len([s for s in mapped.stops if s.is_stay_anchor]) == 4
    assert stay_context_hash(plan) != stay_context_hash(plan.model_copy(update={"stops": [hotel.model_copy(update={"lodging_event": "OVERNIGHT"}), *plan.stops[1:]]}))


def test_last_day_explicit_luggage_visit_stays_on_map_without_inventing_another_night():
    base = _map_plan()
    luggage = base.stops[-1].model_copy(update={"canonical_place_id": "source-hotel", "name": "原酒店",
        "category": "住宿", "lodging_event": "LUGGAGE_PICKUP", "sequence_index": 1})
    plan = base.model_copy(update={"stops": [*base.stops, luggage]})
    assert [n for s in overnight_segments(plan) for n in s.overnight_days] == [1, 2]
    mapped = plan_with_stay_anchor(plan, selected_place_id="new-hotel", selected_name="新选酒店", selected_city="北京",
        longitude=116.4, latitude=39.9, overnight_days=[1, 2])
    last = [s for s in mapped.stops if s.day_index == 3]
    assert [s.name for s in last] == ["新选酒店", "颐和园", "原酒店"]
    assert not last[-1].is_stay_anchor and last[-1].lodging_event == "LUGGAGE_PICKUP"


@pytest.mark.parametrize("event", ["CHECK_OUT", "DEPARTURE"])
def test_next_morning_source_hotel_applies_to_previous_night_only(event):
    base = _map_plan()
    hotel = base.stops[2].model_copy(update={"canonical_place_id": "source-hotel", "name": "原酒店",
        "category": "住宿", "lodging_event": event, "sequence_index": 0})
    plan = base.model_copy(update={"stops": [*base.stops[:2], hotel,
        base.stops[2].model_copy(update={"sequence_index": 1}), base.stops[3]]})
    segments = overnight_segments(plan)
    assert segments[0].overnight_days == [1] and segments[0].preserved_hotels == ["原酒店"]
    assert segments[1].overnight_days == [2] and not segments[1].preserved_hotels


def test_whole_trip_source_hotel_is_a_night_constraint_without_invented_first_morning_or_last_return():
    base = _map_plan()
    hotel = base.stops[0].model_copy(update={"canonical_place_id": "source-hotel", "name": "全程原酒店",
        "category": "住宿", "lodging_event": "OVERNIGHT", "lodging_scope": "WHOLE_TRIP"})
    plan = base.model_copy(update={"lodging_constraints": [hotel]})
    assert all(s.preserved_hotels == ["全程原酒店"] for s in overnight_segments(plan))
    mapped = plan_with_source_lodging(plan)
    assert [s.name for s in mapped.stops if s.day_index == 1] == ["故宫博物院", "景山公园", "全程原酒店"]
    assert [s.name for s in mapped.stops if s.day_index == 2] == ["全程原酒店", "天坛公园", "全程原酒店"]
    assert [s.name for s in mapped.stops if s.day_index == 3] == ["全程原酒店", "颐和园"]
    assert stay_context_hash(mapped) == stay_context_hash(plan) != stay_context_hash(base)
    # A Beijing constraint cannot silently reserve a Shanghai night.
    elsewhere = base.model_copy(update={"lodging_constraints": [hotel], "stops": [s.model_copy(update={"city": "上海"}) for s in base.stops]})
    assert all(not s.preserved_hotels for s in overnight_segments(elsewhere))


def test_one_explicit_overnight_adds_only_next_morning_source_hotel_endpoint():
    base = _map_plan()
    hotel = base.stops[0].model_copy(update={"canonical_place_id": "source-hotel", "name": "当晚原酒店",
        "category": "住宿", "lodging_event": "OVERNIGHT", "lodging_scope": "DAY", "sequence_index": 2})
    plan = base.model_copy(update={"stops": [*base.stops[:2], hotel, *base.stops[2:]]})
    mapped = plan_with_source_lodging(plan)
    assert [s.name for s in mapped.stops if s.day_index == 1] == ["故宫博物院", "景山公园", "当晚原酒店"]
    assert [s.name for s in mapped.stops if s.day_index == 2] == ["当晚原酒店", "天坛公园"]
    assert [s.name for s in mapped.stops if s.day_index == 3] == ["颐和园"]
    assert len([s for s in mapped.stops if s.is_stay_anchor]) == 1
    assert [segment.preserved_hotels for segment in overnight_segments(mapped)] == [["当晚原酒店"], []]
    assert stay_context_hash(mapped) == stay_context_hash(plan)


@pytest.mark.parametrize("hotel_name", ["地点待确认", "已订但尚未匹配的北门酒店", "酒店待定宾馆"])
@pytest.mark.asyncio
async def test_source_placeholder_hotel_does_not_claim_a_booking_but_unmatched_specific_hotels_remain(hotel_name):
    # Accommodation shape captured by complex-lodging-real-ui-v1: unnamed accommodation
    # becomes a NOT_ELIGIBLE card with the projector's reserved placeholder name.
    source = "北京三日游。Day1：故宫博物院、景山公园。Day2：天坛公园。Day3：颐和园。"
    rows = [activity("故宫博物院"), activity("景山公园"), activity("天坛公园", 2), activity("颐和园", 3)]
    output = await TripUnderstandingPipeline(DraftProvider(rows), RecordingResolver()).run(source)
    bindings = {a.compiled.public_activity_token: (a.place.canonical_place_id if a.place else None,
        a.resolution_status.value, a.resolver_receipt) for a in output.activities}
    card = ActivityCardView(activity_token="synthetic-unnamed-hotel-0000000", name=hotel_name, category="住宿",
        city=None, area_or_address="地点待确认", status="NEEDS_CONFIRMATION", available_actions=["DELETE", "REPLACE"])
    result = output.public_result.model_copy(deep=True)
    result.days[0].activities.append(card)
    bindings[card.activity_token] = (None, "NOT_ELIGIBLE" if hotel_name == "地点待确认" else "NEEDS_CONFIRMATION", {})
    plan = _plan_for_result("synthetic-trip", 2, result, bindings, city="北京")
    projected = next(stop for stop in plan.stops if stop.activity_token == card.activity_token)
    segments = overnight_segments(plan)
    if hotel_name == "地点待确认":
        assert projected.source_place_is_placeholder
        assert len(segments) == 1 and segments[0].overnight_days == [1, 2]
        assert not segments[0].preserved_hotels and not segments[0].uncertain
        assert segments[0].expected_boundary_count == 4
        recommendation = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
            stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
        assert recommendation.candidates
    else:
        assert not projected.source_place_is_placeholder
        assert segments[0].preserved_hotels == [hotel_name] and segments[0].overnight_days == [1]
        assert segments[0].uncertain and all(issue["reason"] == "HOTEL_UNCONFIRMED" for issue in segments[0].missing_boundaries)
        view = _segmented_view(_overnight_metadata(plan), [])
        assert view.segments[0].status == "LIMITED" and "还需确认" in view.segments[0].message
        assert view.segments[0].preserved_hotels == [hotel_name]
        assert segments[1].preserved_hotels == [] and segments[1].overnight_days == [2]


def test_known_whole_trip_hotel_is_kept_when_other_places_have_unknown_destination_placeholder():
    base = _map_plan()
    hotel = base.stops[0].model_copy(update={"canonical_place_id": "known-hotel", "name": "已订全程酒店",
        "category": "住宿", "lodging_event": "OVERNIGHT", "lodging_scope": "WHOLE_TRIP"})
    plan = base.model_copy(update={"lodging_constraints": [hotel], "stops": [stop.model_copy(update={
        "city": "目的地待确认", "canonical_place_id": None, "resolution_status": "NEEDS_CONFIRMATION",
        "longitude": None, "latitude": None}) for stop in base.stops]})
    contexts = overnight_segments(plan)
    assert all(s.city == "北京" and s.preserved_hotels == [hotel.name] and s.uncertain for s in contexts)
    assert sum(len(s.missing_boundaries) for s in contexts) == 4
    assert not any(stop.is_stay_anchor for stop in plan_with_source_lodging(plan).stops)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("whole_trip", [False, True])
@pytest.mark.asyncio
async def test_source_hotel_semantics_roundtrip_to_nightly_map_endpoints(kind, whole_trip):
    hotel = "北京饭店"
    if whole_trip:
        clause = f"全程住{hotel}"
        source = f"北京三日游。{clause}。\nDay1：故宫博物院。\nDay2：天坛公园。\nDay3：颐和园。返程前回{hotel}取行李。"
        rows = [{**activity(hotel, None), "category": "住宿", "lodging_event": "OVERNIGHT",
            "lodging_scope": "WHOLE_TRIP", "lodging_evidence": clause},
            activity("故宫博物院"), activity("天坛公园", 2), activity("颐和园", 3),
            {**activity(hotel, 3), "occurrence": 2, "category": "住宿", "lodging_event": "LUGGAGE_PICKUP",
             "lodging_evidence": f"返程前回{hotel}取行李"}]
    else:
        clause = f"今晚入住{hotel}"
        source = f"北京三日游。\nDay1：故宫博物院。{clause}。\nDay2：上午在{hotel}退房。天坛公园。\nDay3：颐和园。"
        rows = [activity("故宫博物院"),
            {**activity(hotel), "category": "住宿", "lodging_event": "OVERNIGHT",
             "lodging_scope": "DAY", "lodging_evidence": clause},
            {**activity(hotel, 2), "occurrence": 2, "category": "住宿", "lodging_event": "CHECK_OUT",
             "lodging_evidence": f"上午在{hotel}退房"}, activity("天坛公园", 2), activity("颐和园", 3)]
    output = await TripUnderstandingPipeline(DraftProvider(rows), RecordingResolver()).run(source)
    assert all(card.status == "READY" for day in output.public_result.days for card in day.activities)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        created = await repo.create_demo(capability_hash="a"*64, source_text=source, idempotency_key="source-lodging",
            request_hash=canonical_sha256({"text": source}), now=now, ttl_hours=24)
        job = await repo.claim_next(worker_id="source-lodging", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash="a"*64, now=now)
        plan, _ = await repo.get_current_place_plan(resource)
        assert [stop.name for stop in plan.stops if stop.day_index == 1] == ["故宫博物院", hotel]
        if whole_trip:
            assert len(plan.lodging_constraints) == 1 and plan.lodging_constraints[0].lodging_scope == "WHOLE_TRIP"
            assert [stop.name for stop in plan.stops if stop.day_index == 2] == [hotel, "天坛公园", hotel]
            assert [stop.name for stop in plan.stops if stop.day_index == 3] == [hotel, "颐和园", hotel]
            assert plan.stops[-1].lodging_event == "LUGGAGE_PICKUP" and not plan.stops[-1].is_stay_anchor
            assert all(segment.preserved_hotels == [hotel] for segment in overnight_segments(plan))
        else:
            assert plan.lodging_constraints == []
            assert [stop.name for stop in plan.stops if stop.day_index == 2] == [hotel, "天坛公园"]
            assert [stop.lodging_event for stop in plan.stops if stop.day_index == 2][0] == "CHECK_OUT"
            assert [segment.preserved_hotels for segment in overnight_segments(plan)] == [[hotel], []]
        assert plan_with_source_lodging(plan).stops == plan.stops


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


@pytest.mark.parametrize("id_prefix", ["", "amap:"])
def test_verified_five_city_branch_maps_require_exact_id_full_name_address_and_city(id_prefix):
    registry = HotelBrandRegistry()
    counts = {}
    other_chains = {}
    for prop in registry.properties:
        for mapping in prop.get("provider_matches", []):
            lng, lat = map(float, mapping["location"].split(","))
            candidate = StayCandidate(canonical_place_id=f"{id_prefix}{mapping['poi_id']}", name=mapping["name"],
                city=prop["city"], category="住宿", area_or_address=mapping["address"], longitude=lng, latitude=lat)
            assert registry.match(candidate.name) == prop["brand"]
            identity = registry.identity(candidate, prop["brand"])
            assert identity["identity_method"] == "EXPLICIT_AMAP_BRANCH"
            assert identity["brand_group"] in {"华住", "首旅如家"}
            assert identity["brand_priority"] == (0 if identity["brand_group"] == "华住" else 1)
            group_counts = counts if identity["brand_group"] == "华住" else other_chains
            group_counts[prop["city"]] = group_counts.get(prop["city"], 0) + 1
            for update in ({"canonical_place_id": "amap:other-property"}, {"name": candidate.name + "悦享"},
                {"area_or_address": candidate.area_or_address + "另栋"}, {"city": "苏州"}):
                rejected = registry.identity(candidate.model_copy(update=update), prop["brand"])
                assert rejected == {"brand_group": None, "brand_priority": 1, "property_identity": "NAME_ONLY"}
    assert set(counts) == {"北京", "上海", "广州", "深圳", "杭州"}
    assert all(count >= 3 for count in counts.values())
    assert set(other_chains) == set(counts) and all(count >= 1 for count in other_chains.values())
    # Width/whitespace normalization is safe; branch spelling is not globally fuzzy.
    mapped = next(p for p in registry.properties if p["name"] == "汉庭上海南京路步行街中心酒店")
    assert mapped["name_tokens"] == ["汉庭", "南京路步行街", "中心"]
    assert "南京东路" in mapped["provider_matches"][0]["name"]


def test_official_seed_uses_nearest_verified_property_not_missing_first_record():
    registry = HotelBrandRegistry()
    assert registry.property_seeds("北京", 116.396148, 39.896348)[0]["name"] == "汉庭北京天安门广场前门酒店"
    assert registry.property_seeds("深圳", 113.921068, 22.526719)[0]["name"] == "全季深圳南山地铁站酒店"


@pytest.mark.parametrize("name,brand", [("桔子水晶合成店酒店", "桔子水晶"), ("桔子合成店酒店", "桔子")])
def test_crystal_and_orange_are_distinct_brands_but_neither_name_proves_branch_identity(name, brand):
    registry = HotelBrandRegistry()
    assert registry.match(name) == brand
    candidate = StayCandidate(canonical_place_id="unverified", name=name, category="住宿", city="北京",
        area_or_address="未核实地址1号", longitude=116.4, latitude=39.9)
    assert registry.identity(candidate, brand) == {"brand_group": None, "brand_priority": 1, "property_identity": "NAME_ONLY"}


@pytest.mark.asyncio
async def test_name_only_hotel_hints_never_fill_verified_chain_recommendations_or_route_budget():
    class UnverifiedHotels:
        async def search(self, **kwargs):
            return StaySearchRows([StayCandidate(canonical_place_id=f"unverified-{n}", name=name,
                category="住宿", city="北京", area_or_address="未核实街道1号", longitude=116.4, latitude=39.9)
                for n, name in enumerate(("北京温馨如家宾馆", "全季悦享酒店", "全季旁酒店"))], external_calls=1)
    routes = CountedRoutes()
    output = await StayRecommendationEngine(UnverifiedHotels(), routes).recommend(stay_plan_from_map(_map_plan()))
    assert output.status == "UNAVAILABLE" and output.candidates == [] and routes.calls == 0
    assert output.provider_binding["candidate_provider_calls"] == 4
    assert output.provider_binding["segments"][0]["unverified_branch_count"] == 2
    assert "仅0家" in output.provider_binding["segments"][0]["message"]


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
            assert kwargs["name"]
            return StaySearchRows([
                StayCandidate(canonical_place_id="official", name="汉庭酒店(北京前门大街店)", category="住宿", city="北京",
                    area_or_address="西经路1号", longitude=116.6, latitude=39.9),
                StayCandidate(canonical_place_id="impostor", name="全季悦享酒店", category="住宿", city="北京",
                    area_or_address="陌生路9号", longitude=116.4, latitude=39.9)], external_calls=1)
    output = await StayRecommendationEngine(OfficialRecall(), ControlledStayRouteProvider()).recommend(
        stay_plan_from_map(_map_plan()), observed_at=datetime.now(timezone.utc))
    assert output.provider_binding["candidate_provider_calls"] == 6
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
    engine = StayRecommendationEngine(CityHotels(), routes, brand_registry=_test_registry(), task_route_budget=12)
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
    output = await StayRecommendationEngine(CityHotels(), routes, brand_registry=_test_registry(), task_route_budget=160).recommend(
        stay_plan_from_map(plan), observed_at=datetime.now(timezone.utc))
    assert sorted(output.provider_binding["segment_route_attempts"].values()) == [64, 96]
    assert routes.calls == output.provider_binding["route_external_calls"] == 160
    assert routes.peak <= 4 and output.status == "PARTIAL"


@pytest.mark.parametrize("preferred_minutes,first", [(20, "preferred"), (21, "nearby")])
@pytest.mark.asyncio
async def test_brand_preference_uses_ten_minute_average_leg_boundary_and_keeps_both_options(preferred_minutes, first):
    output = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(
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
        stay = await StayRecommendationEngine(CityHotels(), ControlledStayRouteProvider(), brand_registry=_test_registry()).recommend(plan, observed_at=now)
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
