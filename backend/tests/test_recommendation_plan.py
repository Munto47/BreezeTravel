"""RecommendationPlan slot contracts and targeted-repair regression tests."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agents.nodes import critic, router, tool_executor
from app.agents.nodes.tool_executor import _execute_tool_call, _merge_unique_places
from app.constraints.recommendation_plan import (
    build_recommendation_plan,
    missing_slot_ids,
    reserve_places_for_plan,
    slot_coverage,
)
from app.constraints.recommendation_intent import (
    filter_places_for_request,
    infer_requested_categories,
)
from app.constraints.candidate_selection import extract_user_cuisine_constraint
from app.constraints.city_knowledge import provider_query_for_geo_anchor
from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource
from app.tools.runtime import ToolReceipt


def _place(place_id: str, name: str, category: PlaceCategory, slots=()) -> Place:
    return Place(
        place_id=place_id,
        name=name,
        category=category,
        address="测试地址",
        coords=Coordinates(lng=120.1, lat=30.2),
        city="杭州",
        district="西湖区",
        source=PlaceSource.AMAP_POI,
        recommendation_slot_ids=list(slots),
    )


def test_compound_request_becomes_ordered_entity_and_food_slots():
    plan = build_recommendation_plan(
        "上午先去灵隐寺，再到西湖，最后在湖滨附近找两家餐厅",
        "杭州",
    )
    assert [(slot.category, slot.entity_name) for slot in plan.slots] == [
        (PlaceCategory.ATTRACTION, "灵隐寺"),
        (PlaceCategory.ATTRACTION, "杭州西湖风景名胜区"),
        (PlaceCategory.FOOD, None),
    ]
    assert [slot.order for slot in plan.slots] == [1, 2, 3]
    assert plan.slots[2].min_results == 2
    assert plan.slots[2].geo.anchor_place == "湖滨"
    assert plan.slots[2].query.startswith("湖滨附近")


def test_current_base_does_not_create_a_hotel_slot_for_food_request():
    plan = build_recommendation_plan(
        "我们吃清真，住牛街附近，想尝北京本地味道。",
        "北京",
    )
    assert [slot.category for slot in plan.slots] == [PlaceCategory.FOOD]
    assert extract_user_cuisine_constraint(
        "想尝北京本地味道，别推荐普通火锅店"
    ) == []


def test_landmark_then_nearby_food_keeps_landmark_and_food_only():
    plan = build_recommendation_plan(
        "上午带老人逛天坛，之后想在附近吃清淡点的北京菜。",
        "北京",
    )
    assert [(slot.category, slot.entity_name) for slot in plan.slots] == [
        (PlaceCategory.ATTRACTION, "天坛公园"),
        (PlaceCategory.FOOD, None),
    ]


def test_open_photo_and_art_requests_compile_independent_provider_slots():
    photo = build_recommendation_plan("想拍北京中轴线日出和城市天际线", "北京")
    assert [slot.query for slot in photo.slots] == ["景山公园", "钟鼓楼", "永定门公园"]
    art = build_recommendation_plan("只想在北京看当代艺术和工业改造空间", "北京")
    assert [slot.query for slot in art.slots] == [
        "798艺术区", "UCCA尤伦斯当代艺术中心", "红砖美术馆",
    ]


def test_named_photo_anchor_keeps_only_photo_relevant_complements():
    plan = build_recommendation_plan(
        "想拍外滩日出和浦东天际线，推荐实际能取景的公共地点。", "上海",
    )
    assert [slot.entity_name for slot in plan.slots] == [
        "外滩", "北外滩滨江", "徐汇滨江",
    ]


def test_explicit_attraction_count_bounds_open_discovery_slots():
    plan = build_recommendation_plan(
        "带六岁孩子在西湖区玩一天，想去两个有趣的地方再找附近吃饭。", "杭州",
    )
    attraction_slots = [slot for slot in plan.slots if slot.category == PlaceCategory.ATTRACTION]
    assert [slot.query for slot in attraction_slots] == ["杭州动物园", "中国湿地博物馆"]


def test_child_science_request_uses_bounded_real_venues():
    plan = build_recommendation_plan(
        "带八岁孩子来北京，想找能动手体验、下雨也能去的科普场馆。",
        "北京",
    )
    assert [slot.query for slot in plan.slots] == [
        "中国科学技术馆", "北京科学中心", "中国地质博物馆",
    ]


def test_safe_night_walk_uses_central_bounded_destinations():
    plan = build_recommendation_plan(
        "女生一个人晚上在北京想散步看看夜景，不喝酒，也不想去太偏的地方。", "北京",
    )
    assert [slot.query for slot in plan.slots] == [
        "什刹海", "前门大街", "北京奥林匹克公园",
    ]


def test_first_time_landmark_request_adds_diverse_city_complements():
    plan = build_recommendation_plan(
        "第一次到杭州只有一天，想看西湖和最有杭州味道的地方，别给十个同质化公园。", "杭州",
    )
    assert [slot.entity_name for slot in plan.slots] == [
        "杭州西湖风景名胜区", "灵隐寺", "京杭大运河杭州景区",
    ]


def test_semantic_area_and_business_commute_become_geo_anchors():
    beijing = build_recommendation_plan(
        "老人睡眠浅，想住二环内但不要酒吧街和夜生活最吵的位置。", "北京",
    )
    assert beijing.slots[0].geo.anchor_place == "二环内"
    assert beijing.slots[0].geo.max_radius_km == 6.0

    shanghai = build_recommendation_plan(
        "想住法租界氛围的中心区，但我睡眠浅，不要正对酒吧街。", "上海",
    )
    assert shanghai.slots[0].geo.anchor_place == "法租界"
    assert shanghai.slots[0].geo.max_radius_km == 4.0

    commute = build_recommendation_plan(
        "在陆家嘴出差三天，预算九百左右，希望走路或一站地铁到公司。", "上海",
    )
    assert commute.slots[0].geo.anchor_place == "陆家嘴"
    assert commute.slots[0].geo.max_radius_km == 3.0


def test_open_neighborhood_compound_request_preserves_attraction_and_food_slots():
    plan = build_recommendation_plan(
        "来上海第三次，不想逛商场，想看看老社区、吃居民日常的小馆。",
        "上海",
    )
    assert [slot.query for slot in plan.slots[:3]] == [
        "武康路历史文化名街", "思南露天博物馆", "愚园路历史名人墙",
    ]
    assert plan.slots[-1].category == PlaceCategory.FOOD


def test_landmark_inferred_district_does_not_filter_cross_boundary_ordered_entity():
    plan = build_recommendation_plan("先逛故宫，再去景山", "北京")
    assert [slot.geo.administrative_district for slot in plan.slots] == [None, None]


def test_regional_cuisine_constraint_accepts_concrete_local_dish_names():
    terms = extract_user_cuisine_constraint("天坛附近吃清淡点的北京菜")
    assert "白水羊头" in terms
    assert "炸酱面" in terms


@pytest.mark.parametrize(("query", "category"), [
    ("带爸妈看皇家园林，少爬坡", PlaceCategory.ATTRACTION),
    ("学生预算，想找免费或便宜、能逛两三个小时的地方", PlaceCategory.ATTRACTION),
    ("坐轮椅想看经典城市景观", PlaceCategory.ATTRACTION),
    ("孩子学近代史，想沿建筑看城市变迁", PlaceCategory.ATTRACTION),
    ("孩子学南宋史，想看和课本对上的地方", PlaceCategory.ATTRACTION),
    ("蛋奶素，想找清淡点的正餐", PlaceCategory.FOOD),
    ("逛完想吃甜品，有没有植物奶", PlaceCategory.FOOD),
    ("钱江新城约客户午饭", PlaceCategory.FOOD),
    ("学生预算每晚三百上下，靠地铁", PlaceCategory.HOTEL),
])
def test_natural_place_discovery_language_creates_a_category_slot(query, category):
    assert infer_requested_categories(query) == {category}
    plan = build_recommendation_plan(query, "杭州")
    assert plan.slots
    assert all(slot.category == category for slot in plan.slots)


def test_negated_cuisine_is_not_compiled_as_a_positive_provider_keyword():
    plan = build_recommendation_plan(
        "住徐家汇，想吃够辣的湘菜，不要全推本帮菜。",
        "上海",
    )
    food = next(slot for slot in plan.slots if slot.category == PlaceCategory.FOOD)
    assert "湘菜" in food.query
    assert "本帮菜" not in food.query


def test_tea_or_coffee_is_an_alternative_not_a_mandatory_coffee_filter():
    from app.constraints.candidate_selection import extract_user_cuisine_constraint

    query = "龙井村附近想喝茶或咖啡，我乳糖不耐。"
    plan = build_recommendation_plan(query, "杭州")
    food = next(slot for slot in plan.slots if slot.category == PlaceCategory.FOOD)
    assert any(term in food.query for term in ("茶馆", "咖啡"))
    assert extract_user_cuisine_constraint(query) == []


@pytest.mark.parametrize("query", [
    "三个大学生周末在海淀玩，想逛有文化氛围的地方再吃顿饭。",
    "来北京第三次了，不想进商场，想逛老社区、吃居民常去的馆子。",
])
def test_compound_stroll_language_keeps_attraction_and_food_slots(query):
    assert infer_requested_categories(query) == {
        PlaceCategory.ATTRACTION,
        PlaceCategory.FOOD,
    }


def test_completed_landmark_visit_is_not_a_new_attraction_request():
    query = "带孩子逛完西湖，附近找不太辣、适合一家人坐下吃饭的地方。"
    assert infer_requested_categories(query) == {PlaceCategory.FOOD}


def test_transport_hub_attraction_query_is_bounded_and_airport_uses_driving():
    airport = build_recommendation_plan(
        "首都机场转机空档四小时，想出去看一眼北京。", "北京",
    )
    assert [slot.query for slot in airport.slots] == ["民航博物馆", "罗红摄影艺术馆"]
    assert all(slot.geo.transport_mode == "driving" for slot in airport.slots)
    assert all(slot.geo.anchor_place == "首都机场" for slot in airport.slots)
    assert all(slot.geo.max_radius_km == 25.0 for slot in airport.slots)
    assert all(slot.geo.max_travel_minutes == 30 for slot in airport.slots)

    station = build_recommendation_plan(
        "杭州东站换乘只有两小时，想在附近看点东西再吃饭。", "杭州",
    )
    attraction = next(slot for slot in station.slots if slot.category == PlaceCategory.ATTRACTION)
    assert attraction.query == "景点"


def test_curated_discovery_slots_are_exact_entities_without_broad_type_filter():
    plan = build_recommendation_plan(
        "上海想看当代艺术和工业改造空间。", "上海",
    )
    assert [slot.entity_name for slot in plan.slots] == [
        "西岸美术馆", "龙美术馆", "浦东美术馆",
    ]
    assert all(slot.provider_typecodes == [] for slot in plan.slots)


def test_provider_query_registry_keeps_canonical_entity_separate():
    plan = build_recommendation_plan("先看外滩，再去上海中心", "上海")
    tower = plan.slots[1]
    assert tower.entity_name == "上海中心大厦"
    assert tower.query == "上海中心大厦观光厅"
    assert tower.provider_typecodes == []


def test_city_scoped_area_anchor_compiles_to_stable_provider_poi_query():
    assert provider_query_for_geo_anchor("杭州", "湖滨") == "杭州湖滨步行街"
    assert provider_query_for_geo_anchor("北京", "湖滨") == "湖滨"
    assert provider_query_for_geo_anchor("北京", "二环内") == "天安门广场"
    assert provider_query_for_geo_anchor("上海", "法租界") == "武康路历史文化名街"


def test_named_landmark_plus_open_discovery_keeps_an_extra_attraction_slot():
    shanghai = build_recommendation_plan(
        "第一次到上海只有一天，想看外滩、老城和现代天际线，别全给商场。",
        "上海",
    )
    assert [slot.entity_name for slot in shanghai.slots] == [
        "外滩", "上海博物馆", "豫园",
    ]

    hangzhou = build_recommendation_plan(
        "第一次到杭州只有一天，想看西湖和最有杭州味道的地方。",
        "杭州",
    )
    assert [slot.entity_name for slot in hangzhou.slots] == [
        "杭州西湖风景名胜区", "灵隐寺", "京杭大运河杭州景区",
    ]


def test_hotel_style_language_does_not_create_an_attraction_slot():
    plan = build_recommendation_plan(
        "想住有北京胡同氛围的精品酒店，两个人，晚上希望安静。",
        "北京",
    )
    assert [slot.category for slot in plan.slots] == [PlaceCategory.HOTEL]
    assert plan.slots[0].query == "四合院 酒店"

    shanghai = build_recommendation_plan(
        "情侣想住有老上海建筑感的精品酒店，晚上别太吵。",
        "上海",
    )
    assert shanghai.slots[0].query == "历史建筑 酒店"


def test_district_name_is_not_promoted_to_landmark_entity():
    plan = build_recommendation_plan(
        "带六岁孩子在西湖区玩一天，想去两个有趣的地方再找附近吃饭。",
        "杭州",
    )
    assert all(slot.entity_name is None for slot in plan.slots)
    attraction_slots = [slot for slot in plan.slots if slot.category == PlaceCategory.ATTRACTION]
    assert [slot.query for slot in attraction_slots] == [
        "杭州动物园", "中国湿地博物馆",
    ]
    assert all(slot.min_results == 1 for slot in attraction_slots)
    assert all(slot.entity_name is None for slot in attraction_slots)
    assert all(slot.provider_match_aliases == [slot.query] for slot in attraction_slots)


def test_compound_nearby_food_anchors_to_first_bounded_attraction():
    plan = build_recommendation_plan(
        "下雨带孩子在杭州玩半天，想去室内场馆，再就近吃饭。", "杭州",
    )
    food = next(slot for slot in plan.slots if slot.category == PlaceCategory.FOOD)
    assert food.geo.anchor_place == "浙江自然博物院杭州馆"
    assert food.geo.max_radius_km == 3.0
    assert [slot.query for slot in plan.slots if slot.category == PlaceCategory.ATTRACTION] == [
        "浙江自然博物院杭州馆", "浙江省科技馆", "中国京杭大运河博物馆",
    ]


def test_multi_food_slots_inherit_transport_anchor_district():
    plan = build_recommendation_plan(
        "早上七点从北京南站坐车，六点左右附近哪里能快速吃早餐？", "北京",
    )
    assert len(plan.slots) == 3
    assert all(slot.geo.anchor_place == "北京南站" for slot in plan.slots)
    assert all(slot.geo.administrative_district == "丰台区" for slot in plan.slots)


def test_district_child_registry_stays_inside_requested_area():
    plan = build_recommendation_plan(
        "带六岁孩子在朝阳区玩一天，想要两三个好玩的地方加附近吃饭，尽量少换乘。",
        "北京",
    )
    assert [slot.query for slot in plan.slots if slot.category == PlaceCategory.ATTRACTION] == [
        "中国科学技术馆", "中国电影博物馆", "中国铁道博物馆东郊展馆",
    ]


def test_generic_nearby_phrase_anchors_to_previous_landmark():
    plan = build_recommendation_plan("先故宫再景山，走完想在附近吃北京菜", "北京")
    assert plan.slots[-1].geo.anchor_place == "景山公园"
    assert plan.slots[-1].query == "景山公园附近 北京菜"


def test_connector_nearby_phrase_uses_last_landmark_not_previous_clause():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    assert plan.slots[-1].geo.anchor_place == "杭州西湖风景名胜区"
    assert plan.slots[-1].query == "杭州西湖风景名胜区附近 本地特色餐厅"


def test_explicit_final_area_without_nearby_becomes_geo_anchor():
    plan = build_recommendation_plan("先看外滩，再去上海中心，最后在陆家嘴吃饭", "上海")
    assert plan.slots[-1].geo.anchor_place == "陆家嘴"
    assert plan.slots[-1].query == "陆家嘴附近 本地特色餐厅"


def test_plain_language_before_nearby_uses_resolved_registry_venue_not_prose():
    plan = build_recommendation_plan(
        "带六岁孩子在朝阳区玩一天，想要两三个好玩的地方加附近吃饭，尽量少换乘。",
        "北京",
    )
    food = next(slot for slot in plan.slots if slot.category == PlaceCategory.FOOD)
    assert food.geo.anchor_place == "中国科学技术馆"
    assert food.geo.anchor_place != "好玩的地方加"
    assert food.geo.administrative_district == "朝阳区"


def test_known_landmark_survives_connective_prose_as_nearby_anchor():
    plan = build_recommendation_plan(
        "情侣下午去798看展，想顺便找附近适合坐下来聊天的餐厅或咖啡馆。",
        "北京",
    )
    food = next(slot for slot in plan.slots if slot.category == PlaceCategory.FOOD)
    assert food.geo.anchor_place == "798艺术区"


def test_transport_hub_can_anchor_multiple_nearby_slots():
    plan = build_recommendation_plan(
        "北京南站换乘只有两小时，想在附近看点东西再吃饭，别让我误车。",
        "北京",
    )
    by_category = {slot.category: slot for slot in plan.slots}
    assert by_category[PlaceCategory.ATTRACTION].geo.anchor_place == "北京南站"
    assert by_category[PlaceCategory.FOOD].geo.anchor_place == "北京南站"
    assert by_category[PlaceCategory.ATTRACTION].geo.max_travel_minutes == 30


def test_low_transfer_request_compiles_transit_contract():
    plan = build_recommendation_plan(
        "北京南站附近看点东西再吃饭，尽量少换乘。",
        "北京",
    )
    assert all(
        slot.geo.transport_mode == "transit" and slot.geo.max_transfers == 1
        for slot in plan.slots
    )


def test_disney_and_bund_are_resolved_landmark_anchors_not_verb_phrases():
    disney = build_recommendation_plan(
        "带四岁孩子去迪士尼，想住附近，有家庭房或接驳车更好。",
        "上海",
    )
    hotel = next(slot for slot in disney.slots if slot.category == PlaceCategory.HOTEL)
    assert hotel.geo.anchor_place == "上海迪士尼乐园"
    assert [slot.category for slot in disney.slots] == [PlaceCategory.HOTEL]
    assert hotel.provider_typecodes == ["100000"]

    bund = build_recommendation_plan(
        "坐轮椅住外滩附近，需要无障碍客房和电梯，哪些酒店可以先问？",
        "上海",
    )
    hotel = next(slot for slot in bund.slots if slot.category == PlaceCategory.HOTEL)
    assert hotel.geo.anchor_place == "外滩"
    assert [slot.category for slot in bund.slots] == [PlaceCategory.HOTEL]


def test_slot_coverage_counts_canonical_candidates_not_total_places():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    places = [
        _place("lingyin", "灵隐寺", PlaceCategory.ATTRACTION, ["slot-01"]),
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, ["slot-02"]),
    ]
    assert missing_slot_ids(plan, places) == ["slot-03"]
    assert slot_coverage(plan, places)["slot-03"]["actual"] == 0


def test_plan_reservation_prevents_first_slot_from_consuming_category_cap_order():
    plan = build_recommendation_plan("先灵隐寺再西湖", "杭州")
    noisy = [_place(f"l{i}", f"灵隐寺子景点{i}", PlaceCategory.ATTRACTION, ["slot-01"]) for i in range(5)]
    lake = _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, ["slot-02"])
    ordered = reserve_places_for_plan([*noisy, lake], plan)
    assert ordered[0].recommendation_slot_ids == ["slot-01"]
    assert ordered[1].place_id == "lake"


def test_entity_dedupe_unions_slot_provenance():
    first = _place("one", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, ["slot-01"])
    duplicate = first.model_copy(update={"place_id": "two", "recommendation_slot_ids": ["slot-02"]})
    merged = _merge_unique_places([], [first, duplicate])
    assert len(merged) == 1
    assert merged[0].recommendation_slot_ids == ["slot-01", "slot-02"]


def test_tool_result_keeps_slot_id_on_places_and_audit():
    candidate = _place("food", "湖滨餐厅", PlaceCategory.FOOD)
    audit = {"query": "湖滨餐厅", "provider": "amap", "result_count": 1}
    call = {
        "name": "search_places",
        "args": {"query": "湖滨餐厅", "city": "杭州", "slot_id": "slot-03"},
        "id": "call-1",
    }
    state = {"messages": [HumanMessage(content="附近找餐厅")], "trip_city": "杭州"}
    with patch(
        "app.tools.amap_tool._run_amap_search_with_audit",
        new=AsyncMock(return_value=([candidate.model_copy(update={"recommendation_slot_ids": ["slot-03"]})], [{**audit, "slot_id": "slot-03"}]))
    ):
        _, places, _, audits = asyncio.run(_execute_tool_call(call, state))
    assert places[0].recommendation_slot_ids == ["slot-03"]
    assert audits[0]["slot_id"] == "slot-03"


def test_slot_coverage_is_computed_after_delivery_filters():
    plan = build_recommendation_plan("想吃火锅", "杭州")
    slot = plan.slots[0]
    raw_food = _place("fast", "网红快餐", PlaceCategory.FOOD, [slot.slot_id])
    receipt = ToolReceipt(
        call_id="call-1", trace_id="trace-1", tool="search_places",
        status="ok", duration_ms=1, result_count=1,
    )
    state = {
        "messages": [
            HumanMessage(content="想吃火锅"),
            AIMessage(content="", tool_calls=[{
                "name": "search_places",
                "args": {"query": slot.query, "city": "杭州", "slot_id": slot.slot_id},
                "id": "call-1",
            }]),
        ],
        "trip_city": "杭州",
        "trip_district": None,
        "recommendation_plan": plan.model_dump(mode="json"),
        "amap_places": [],
        "rag_chunks": [],
        "citations": [],
        "tool_failures": [],
        "tool_receipts": [],
        "retrieval_audits": [],
        "slot_coverage": {},
    }
    with patch(
        "app.agents.nodes.tool_executor._execute_with_runtime",
        new=AsyncMock(return_value=(("ok", [raw_food], [], []), receipt)),
    ):
        result = asyncio.run(tool_executor.run(state))
    assert [place.name for place in result["amap_places"]] == ["网红快餐"]
    assert result["eligible_amap_places"] == []
    assert result["slot_coverage"][slot.slot_id]["actual"] == 0

    routed = asyncio.run(router.run({
        **state,
        **result,
        "react_iterations": 1,
        "missing_slot_ids": [],
    }))
    assert routed["messages"][0].tool_calls[0]["args"]["slot_id"] == slot.slot_id


def test_restaurant_or_cafe_is_not_compiled_as_mandatory_coffee_filter():
    plan = build_recommendation_plan(
        "情侣下午去798看展，想顺便找附近适合聊天的餐厅或咖啡馆。",
        "北京",
    )
    food_slot = next(slot for slot in plan.slots if slot.category == PlaceCategory.FOOD)
    restaurant = _place("food", "那家小馆(798艺术区店)", PlaceCategory.FOOD, [food_slot.slot_id])
    from app.constraints.candidate_selection import select_eligible_places

    selected = select_eligible_places([restaurant], plan.user_request, recommendation_plan=plan)
    assert [place.place_id for place in selected] == ["food"]


def test_slot_category_wins_over_whole_compound_query_category_hint():
    candidate = _place("palace", "故宫博物院", PlaceCategory.ATTRACTION)
    call = {
        "name": "search_places",
        "args": {"query": "故宫博物院", "city": "北京", "category": "景点", "slot_id": "slot-01"},
        "id": "call-entity",
    }
    state = {
        "messages": [HumanMessage(content="先故宫再景山，之后附近吃饭")],
        "trip_city": "北京",
    }
    with patch(
        "app.tools.amap_tool._run_amap_search_with_audit",
        new=AsyncMock(return_value=([candidate], [])),
    ) as search:
        asyncio.run(_execute_tool_call(call, state))
    assert search.await_args.kwargs["category"] == "景点"


def test_exact_entity_slot_keeps_only_main_poi_and_does_not_certify_subpois():
    plan = build_recommendation_plan(
        "首都机场转机空档四小时，想出去看一眼北京。", "北京",
    )
    slot = plan.slots[1]
    main = _place("main", "罗红摄影艺术馆", PlaceCategory.ATTRACTION, [slot.slot_id])
    child = _place(
        "child", "罗红摄影艺术馆-水池景观(打卡点)",
        PlaceCategory.ATTRACTION, [slot.slot_id],
    )
    call = {
        "name": "search_places",
        "args": {
            "query": slot.query, "city": "北京", "category": "景点",
            "slot_id": slot.slot_id,
        },
        "id": "call-entity",
    }
    state = {
        "messages": [HumanMessage(content=plan.user_request)],
        "trip_city": "北京",
        "recommendation_plan": plan.model_dump(mode="json"),
    }
    with patch(
        "app.tools.amap_tool._run_amap_search_with_audit",
        new=AsyncMock(return_value=([child, main], [])),
    ):
        _, places, _, _ = asyncio.run(_execute_tool_call(call, state))

    assert [place.place_id for place in places] == ["main"]
    assert places[0].canonical_entity_names == ["罗红摄影艺术馆"]


def test_plan_categories_keep_entity_slots_in_mixed_request_filter():
    places = [
        _place("palace", "故宫博物院", PlaceCategory.ATTRACTION, ["slot-01"]),
        _place("food", "北京菜馆", PlaceCategory.FOOD, ["slot-03"]),
    ]
    filtered = filter_places_for_request(
        places,
        "先故宫再景山，之后附近吃北京菜",
        explicit_category=[PlaceCategory.ATTRACTION, PlaceCategory.FOOD],
    )
    assert [place.place_id for place in filtered] == ["palace", "food"]


def test_router_repairs_only_missing_slot_and_preserves_plan():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    places = [
        _place("lingyin", "灵隐寺", PlaceCategory.ATTRACTION, ["slot-01"]),
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, ["slot-02"]),
    ]
    result = asyncio.run(router.run({
        "messages": [HumanMessage(content=plan.user_request), AIMessage(content="")],
        "trip_city": "杭州",
        "trip_district": None,
        "react_iterations": 1,
        "amap_places": places,
        "recommendation_plan": plan.model_dump(mode="json"),
        "slot_coverage": {},
        "missing_slot_ids": [],
    }))
    calls = result["messages"][0].tool_calls
    assert len(calls) == 1
    assert calls[0]["args"]["slot_id"] == "slot-03"


def test_critic_requests_targeted_slot_repair_without_clearing_candidates():
    plan = build_recommendation_plan("先灵隐寺再西湖，之后附近吃饭", "杭州")
    places = [
        _place("lingyin", "灵隐寺", PlaceCategory.ATTRACTION, ["slot-01"]),
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, ["slot-02"]),
    ]
    result = asyncio.run(critic.run({
        "messages": [HumanMessage(content=plan.user_request)],
        "synthesized_places": places,
        "amap_places": places,
        "rag_chunks": [],
        "recommendations": [],
        "working_context": {},
        "critic_iterations": 0,
        "recommendation_plan": plan.model_dump(mode="json"),
    }))
    assert result["critic_retry"] is True
    assert result["missing_slot_ids"] == ["slot-03"]
    assert "amap_places" not in result


def test_generic_landmark_proximity_uses_radius_without_invented_route_ceiling():
    plan = build_recommendation_plan(
        "城隍庙附近想吃上海小吃，不要肯德基。", "上海",
    )
    slot = next(item for item in plan.slots if item.category == PlaceCategory.FOOD)
    assert slot.geo.anchor_place == "城隍庙"
    assert slot.geo.max_radius_km == 3.0
    assert slot.geo.max_travel_minutes is None


def test_transport_hub_proximity_keeps_route_time_contract():
    plan = build_recommendation_plan(
        "早上七点从北京南站坐车，六点左右附近哪里能吃早餐？", "北京",
    )
    slot = next(item for item in plan.slots if item.category == PlaceCategory.FOOD)
    assert slot.geo.anchor_place == "北京南站"
    assert slot.geo.max_travel_minutes == 20
