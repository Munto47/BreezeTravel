"""Evidence-first selection and geographic contract tests."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langchain_core.messages import HumanMessage

from app.agents.nodes import synthesizer
from app.constraints.evidence_resolver import resolve_candidate_evidence
from app.constraints.geo_routes import RouteResult, _route_semaphore, enrich_geo_route_evidence
from app.constraints.recommendation_plan import bind_geo_anchor_evidence, build_recommendation_plan
from app.constraints.selection_policy import select_evidence_eligible_candidates
from app.schemas.place import Coordinates, EvidenceStatus, Place, PlaceCategory, PlaceSource


def _place(place_id: str, name: str, category: PlaceCategory, lng: float, lat: float, slots=(), phone=None):
    return Place(
        place_id=place_id,
        name=name,
        category=category,
        address="测试地址",
        coords=Coordinates(lng=lng, lat=lat),
        city="杭州",
        district="西湖区",
        source=PlaceSource.AMAP_POI,
        recommendation_slot_ids=list(slots),
        canonical_entity_names=[name],
        phone=phone,
    )


def test_evidence_resolver_runs_geo_contract_before_selection():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    anchor = _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, 120.15, 30.25, ["slot-02"])
    nearby = _place("near", "湖滨餐厅", PlaceCategory.FOOD, 120.16, 30.25, ["slot-03"])
    far = _place("far", "远郊餐厅", PlaceCategory.FOOD, 120.35, 30.25, ["slot-03"])
    resolved = resolve_candidate_evidence([anchor, nearby, far], plan.user_request, plan=plan)
    selected = select_evidence_eligible_candidates(resolved)
    assert "near" in {place.place_id for place in selected}
    assert "far" not in {place.place_id for place in selected}
    near_geo = next(place for place in resolved if place.place_id == "near").geo_evidence
    radius_evidence = next(item for item in near_geo if item.constraint_kind == "proximity")
    route_evidence = next(item for item in near_geo if item.constraint_kind == "route")
    assert radius_evidence.status == EvidenceStatus.VERIFIED
    assert radius_evidence.satisfies_constraint is True
    assert route_evidence.status == EvidenceStatus.UNKNOWN
    assert route_evidence.estimated_travel_minutes is None


def test_provider_anchor_audit_is_bound_into_plan_and_verifies_radius():
    plan = build_recommendation_plan(
        "早上七点从北京南站坐车，六点左右附近哪里能快速吃早餐？",
        "北京",
    )
    audits = [
        {
            "slot_id": slot.slot_id,
            "location": "116.378517,39.865246",
            "anchor_place_id": "B000A83AJN",
            "anchor_response_hash": "a" * 64,
            "retrieved_at": "2026-08-13T00:00:00Z",
        }
        for slot in plan.slots
    ]
    bound = bind_geo_anchor_evidence(plan, audits)
    assert all(slot.geo.anchor_coords is not None for slot in bound.slots)
    assert all(slot.geo.anchor_place_id == "B000A83AJN" for slot in bound.slots)

    breakfast = _place(
        "breakfast", "测试早餐店", PlaceCategory.FOOD,
        116.376243, 39.845043, [bound.slots[0].slot_id],
    )
    resolved = resolve_candidate_evidence(
        [breakfast], bound.user_request, plan=bound,
    )[0]
    radius = next(item for item in resolved.geo_evidence if item.constraint_kind == "proximity")
    assert radius.status == EvidenceStatus.VERIFIED
    assert radius.satisfies_constraint is True
    assert radius.straight_line_distance_km is not None


def test_local_patronage_stays_unverified_without_review_source():
    restaurant = _place(
        "food", "社区家常菜", PlaceCategory.FOOD, 116.4, 39.9,
        phone="010-12345678",
    )
    resolved = resolve_candidate_evidence(
        [restaurant], "想吃居民常去的馆子", plan=None,
    )[0]
    evidence = next(item for item in resolved.constraint_evidence if item.constraint == "local_patronage")
    assert evidence.status == EvidenceStatus.REQUIRES_CONFIRMATION
    assert resolved.selection_evidence_status == EvidenceStatus.REQUIRES_CONFIRMATION
    assert any("居民常去或本地客群" in action for action in resolved.confirmation_actions)


def test_seasonal_scenery_stays_unverified_without_dated_source():
    attraction = _place(
        "park", "杭州花圃", PlaceCategory.ATTRACTION, 120.13, 30.25,
    )
    resolved = resolve_candidate_evidence(
        [attraction], "秋天想看桂花和秋色", plan=None,
    )[0]
    evidence = next(item for item in resolved.constraint_evidence if item.constraint == "seasonal_scenery")
    assert evidence.status == EvidenceStatus.REQUIRES_CONFIRMATION
    assert resolved.confirmation_actions


def test_hotel_solo_request_does_not_emit_food_portion_notice():
    hotel = _place("hotel", "车站酒店", PlaceCategory.HOTEL, 116.38, 39.86)
    response = synthesizer._build_demo_response(
        [hotel], "北京", {}, None,
        "女生一个人晚上十一点到北京南站，只住一晚。",
    )
    assert "一人套餐" not in response


def test_response_exposes_verified_hours_and_anchor_distance():
    plan = build_recommendation_plan(
        "早上七点从北京南站坐车，六点左右附近哪里能快速吃早餐？", "北京",
    )
    slot = plan.slots[0]
    bound = bind_geo_anchor_evidence(plan, [{
        "slot_id": slot.slot_id,
        "location": "116.378517,39.865246",
        "retrieved_at": "2026-08-13T00:00:00Z",
    }])
    breakfast = _place(
        "food", "六点早餐店", PlaceCategory.FOOD,
        116.376243, 39.845043, [slot.slot_id],
    ).model_copy(update={"opening_hours": "06:00-20:00", "amap_price": 25})
    resolved = resolve_candidate_evidence(
        [breakfast], bound.user_request, plan=bound,
    )
    response = synthesizer._build_demo_response(
        resolved, "北京", {}, None, bound.user_request,
    )
    assert "高德记录营业 06:00-20:00" in response
    assert "距锚点直线约" in response
    assert "时间约束回执：1/1" in response
    assert "出发前请用地图或电话逐家复核" in response


def test_response_only_claims_budget_for_structured_prices():
    priced = _place("priced", "平价餐厅", PlaceCategory.FOOD, 116.4, 39.9).model_copy(
        update={"amap_price": 80}
    )
    unknown = _place("unknown", "价格未知餐厅", PlaceCategory.FOOD, 116.4, 39.9)
    response = synthesizer._build_demo_response(
        [priced, unknown], "北京", {}, None, "学生预算，人均一百以内。",
    )
    assert "1 个有高德参考价的候选均不高于 100 元" in response
    assert "1 个候选缺少可核验价格，不能视为已满足预算" in response


def test_response_emits_recomputable_exclusion_receipt():
    viewpoint = _place("view", "外滩观景平台", PlaceCategory.ATTRACTION, 121.49, 31.24)
    response = synthesizer._build_demo_response(
        [viewpoint], "上海", {}, None,
        "想拍外滩日出，别给照相馆。",
    )
    assert "排除项回执：最终 1 张地点卡中不含摄影器材或照相服务场所" in response


def test_response_limits_named_chain_exclusion_to_recomputable_brand_names():
    snack = _place("snack", "老上海馄饨铺", PlaceCategory.FOOD, 121.49, 31.24)
    response = synthesizer._build_demo_response(
        [snack], "上海", {}, None,
        "不要肯德基、星巴克这种连锁。",
    )
    assert "不含用户点名的肯德基/星巴克" in response
    assert "其他品牌是否为全国连锁不作无证据扩大判定" in response


def test_route_limiter_is_isolated_per_thread_event_loop():
    async def resolve_limiter_identity() -> tuple[int, int]:
        first = _route_semaphore()
        await asyncio.sleep(0)
        return id(asyncio.get_running_loop()), id(first)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(asyncio.run, resolve_limiter_identity()) for _ in range(2)]
        identities = [future.result() for future in futures]

    assert identities[0][0] != identities[1][0]
    assert identities[0][1] != identities[1][1]


def test_live_route_upgrades_unknown_time_and_rejects_slow_candidate():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    anchor = _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, 120.15, 30.25, ["slot-02"])
    nearby = _place("near", "湖滨餐厅", PlaceCategory.FOOD, 120.16, 30.25, ["slot-03"])
    resolved = resolve_candidate_evidence([anchor, nearby], plan.user_request, plan=plan)

    async def slow_route(*_args, **_kwargs):
        return RouteResult(
            status="ok", duration_minutes=35, distance_km=1.2,
            source="amap_walking_route", response_hash="a" * 64,
        )

    with patch("app.constraints.geo_routes.fetch_amap_route", side_effect=slow_route):
        enriched = asyncio.run(enrich_geo_route_evidence(resolved, plan))
    candidate = next(place for place in enriched if place.place_id == "near")
    route_evidence = next(item for item in candidate.geo_evidence if item.constraint_kind == "route")
    assert route_evidence.status == EvidenceStatus.VERIFIED
    assert route_evidence.estimated_travel_minutes == 35
    assert route_evidence.satisfies_constraint is False
    assert "near" not in {
        place.place_id for place in select_evidence_eligible_candidates(enriched)
    }


def test_failed_route_remains_unknown_with_auditable_reason_and_action():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    anchor = _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, 120.15, 30.25, ["slot-02"])
    nearby = _place("near", "湖滨餐厅", PlaceCategory.FOOD, 120.16, 30.25, ["slot-03"])
    resolved = resolve_candidate_evidence([anchor, nearby], plan.user_request, plan=plan)

    async def failed_route(*_args, **_kwargs):
        return RouteResult(
            status="unknown", source="amap_route", failure_reason="TimeoutError",
        )

    with patch("app.constraints.geo_routes.fetch_amap_route", side_effect=failed_route):
        enriched = asyncio.run(enrich_geo_route_evidence(resolved, plan))
    candidate = next(place for place in enriched if place.place_id == "near")
    route_evidence = next(item for item in candidate.geo_evidence if item.constraint_kind == "route")
    assert route_evidence.status == EvidenceStatus.UNKNOWN
    assert route_evidence.failure_reason == "TimeoutError"
    assert any("实际通勤时间和路线" in action for action in candidate.confirmation_actions)


def test_high_risk_unknown_is_secondary_and_has_contact_action():
    hotel = _place("hotel", "测试酒店", PlaceCategory.HOTEL, 120.15, 30.25, phone="0571-12345678")
    resolved = resolve_candidate_evidence([hotel], "要无障碍客房和接驳班车", plan=None)
    item = resolved[0]
    assert item.selection_evidence_status == EvidenceStatus.REQUIRES_CONFIRMATION
    assert all(e.status == EvidenceStatus.REQUIRES_CONFIRMATION for e in item.constraint_evidence)
    assert len(item.confirmation_actions) == 1
    assert "无台阶入口" in item.confirmation_actions[0]
    assert "电梯尺寸" in item.confirmation_actions[0]
    assert "接驳或班车" in item.confirmation_actions[0]


def test_hotel_style_search_signal_is_not_treated_as_verified_evidence():
    hotel = _place("hotel", "北京后海鼓楼四合院漫心府", PlaceCategory.HOTEL, 120.15, 30.25)
    resolved = resolve_candidate_evidence(
        [hotel], "想住有北京胡同氛围的精品酒店", plan=None,
    )[0]
    evidence = {item.constraint: item for item in resolved.constraint_evidence}
    assert evidence["lodging_style"].status == EvidenceStatus.REQUIRES_CONFIRMATION
    assert resolved.confirmation_actions


def test_unknown_attribute_does_not_become_verified_from_name_or_tags():
    hotel = _place("hotel", "无障碍亲子家庭房酒店", PlaceCategory.HOTEL, 120.15, 30.25)
    hotel = hotel.model_copy(update={"tags": ["无障碍", "家庭房"]})
    resolved = resolve_candidate_evidence([hotel], "要无障碍家庭房", plan=None)
    assert {item.status for item in resolved[0].constraint_evidence} == {
        EvidenceStatus.REQUIRES_CONFIRMATION,
    }
    assert resolved[0].confirmation_actions


def test_unknown_price_and_geo_both_produce_explicit_actions():
    hotel = _place("hotel", "测试酒店", PlaceCategory.HOTEL, 120.15, 30.25)
    price_only = resolve_candidate_evidence([hotel], "每晚预算五百元", plan=None)[0]
    assert price_only.selection_evidence_status == EvidenceStatus.UNKNOWN
    assert price_only.confirmation_actions == [
        "通过场所官方电话或预订页面联系确认：住宿参考价格、参考价不高于 500 元"
    ]

    plan = build_recommendation_plan("想住西湖周边的酒店", "杭州")
    geo_only = resolve_candidate_evidence([hotel.model_copy(update={
        "recommendation_slot_ids": [plan.slots[0].slot_id],
    })], plan.user_request, plan=plan)[0]
    assert geo_only.selection_evidence_status == EvidenceStatus.UNKNOWN
    assert any("实际通勤时间和路线" in action for action in geo_only.confirmation_actions)


def test_pending_high_risk_evidence_blocks_generated_affirmative_card_copy():
    hotel = _place("hotel", "测试酒店", PlaceCategory.HOTEL, 120.15, 30.25)
    llm = AsyncMock()
    llm.ainvoke.return_value = SimpleNamespace(content=json.dumps({
        "place_updates": [{
            "place_id": "hotel",
            "description": "已经确认提供无障碍客房和接驳班车。",
            "tags": ["无障碍", "接驳车"],
        }],
        "response_text": "测试酒店已经确认满足全部无障碍和接驳需求。",
    }, ensure_ascii=False))
    state = {
        "messages": [HumanMessage(content="需要无障碍客房和接驳班车")],
        "trip_city": "杭州",
        "trip_district": None,
        "amap_places": [hotel],
        "rag_chunks": [],
        "working_context": {},
        "recommendation_plan": None,
        "user_id": "eval",
    }
    with (
        patch.object(synthesizer.settings, "demo_mode", False),
        patch.object(synthesizer, "_get_llm", return_value=llm),
    ):
        result = asyncio.run(synthesizer.run(state))
    delivered = result["synthesized_places"][0]
    assert "已经确认" not in (delivered.description or "")
    assert "无障碍" not in delivered.tags
    assert "接驳" not in delivered.tags
    assert "逐项确认" in result["final_response"]
