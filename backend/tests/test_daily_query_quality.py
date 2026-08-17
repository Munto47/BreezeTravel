import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from app.agents.nodes import critic, router
from app.agents.nodes import synthesizer
from app.agents.nodes.router import _has_sufficient_place_evidence
from app.agents.nodes.tool_executor import _merge_unique_places
from app.constraints.location import (
    extract_district_constraint,
    extract_explicit_district_constraint,
    place_is_human_suitable,
)
from app.constraints.evidence import build_constraint_evidence
from app.constraints.recommendation_intent import (
    build_category_search_plan,
    build_place_search_queries,
    filter_places_for_request,
    infer_requested_categories,
    is_closed_landmark_request,
    extract_budget_ceiling,
    extract_landmark_groups,
    rank_places_for_request,
    requested_category_argument,
    request_has_all_landmarks,
)
from app.constraints.recommendation_plan import build_recommendation_plan
from app.tools.amap_tool import _compile_provider_keyword
from app.constraints.candidate_selection import (
    _attach_delivered_attraction_evidence,
    _attach_low_transfer_core_evidence,
    _attach_shared_anchor_evidence,
    _drop_obviously_remote_meals,
    _filter_by_requested_open_time,
    _filter_category_identity_conflicts,
    _filter_low_transfer_candidates,
    _filter_time_sensitive_hub_candidates,
    extract_user_cuisine_constraint,
    select_eligible_places,
)
from app.tools.amap_tool import _run_amap_search
from app.schemas.place import (
    Coordinates,
    EvidenceStatus,
    GeoEvidence,
    Place,
    PlaceCategory,
    PlaceSource,
)
from app.schemas.api import ChatRequest
from app.memory.policy import should_use_long_term_memory
from app.tools.runtime import SearchPlacesArgs
from scripts.run_daily_query_eval import _materialize_response, _summarize, deterministic_checks, validate_dataset
from scripts.run_daily_query_eval import JUDGE_PROMPT
from scripts import run_daily_query_eval as daily_eval_runner


ROOM_OPENING_PROMPT = (
    "你好！欢迎来到 北京 3 天行程的协同规划房间 ✨ "
    "请帮我搭建初版推荐清单，**总数控制在 15 个以内**（无论我几天几人），"
    "三类各约 5 个，宁缺毋滥： 🏞 美景：约 5 个必去地标（覆盖核心片区即可，不要堆数量） "
    "🍜 美食：约 5 家代表性餐厅（本地老字号 / 高分性价比为主，不要重复分店） "
    "🏨 美梦：约 5 家不同价位的酒店或民宿（标注大致价位与所在片区） "
    "每个地点一句话特色描述。优先高评分、知名度高的，剩余可在用户追问时再补充。"
)


def _place(place_id: str, name: str, category: PlaceCategory, district: str = "闵行区", rating: float = 4.5) -> Place:
    return Place(
        place_id=place_id,
        name=name,
        category=category,
        address=f"{district}{name}地址",
        coords=Coordinates(lng=121.4, lat=31.1),
        city="上海",
        district=district,
        source=PlaceSource.AMAP_POI,
        amap_rating=rating,
    )


def test_living_in_district_is_not_hotel_intent():
    categories = infer_requested_categories("我住在闵行区，有什么美食？")
    assert categories == {PlaceCategory.FOOD}
    assert requested_category_argument("我住在闵行区，有什么美食？") == "美食"


def test_three_city_dataset_has_exactly_fifty_real_scenarios_each():
    path = Path(__file__).resolve().parents[1] / "eval_data" / "daily_queries" / "cases.json"
    cases = validate_dataset(json.loads(path.read_text(encoding="utf-8")))
    assert len(cases) == 150
    assert {city: sum(case["city"] == city for case in cases) for city in ("北京", "上海", "杭州")} == {
        "北京": 50, "上海": 50, "杭州": 50,
    }
    assert all(case.get("persona") and case.get("dimensions") for case in cases)


def test_reserved_runtime_identities_do_not_use_long_term_memory():
    for user_id in (None, "", "anonymous", "tool-call", "eval"):
        assert not should_use_long_term_memory(user_id)
    assert should_use_long_term_memory("real-user-id")


def test_judge_rubric_rewards_honest_actionable_unknown_without_calling_it_verified():
    assert "应给 4" in JUDGE_PROMPT
    assert "因尚未证实不能给 5" in JUDGE_PROMPT
    assert "静态硬约束" in JUDGE_PROMPT


def test_hangzhou_cuisine_before_negative_safety_clause_keeps_food_intent():
    query = "湖滨附近吃杭州菜，但同行有人坚果严重过敏，不要替餐厅保证安全。"
    assert infer_requested_categories(query) == {PlaceCategory.FOOD}
    assert build_place_search_queries(query) == ["湖滨 杭帮菜"]


def test_provider_alias_resolves_shanghai_tower_observation_deck():
    query = "我想去中国第一高楼看看。"
    places = [_place("tower", "上海之巅观光厅", PlaceCategory.ATTRACTION, "浦东新区")]
    assert request_has_all_landmarks(places, query)


def test_required_attraction_can_be_grounded_by_provider_address():
    case = {
        "id": "address_grounded_entity",
        "city": "北京",
        "intent": "attraction",
        "expected": {"min_places": 1, "required_place_groups": [["798"]]},
    }
    place = _place("ucca", "UCCA尤伦斯当代艺术中心", PlaceCategory.ATTRACTION, "朝阳区")
    place = place.model_copy(update={"address": "酒仙桥路4号798艺术区内"})
    result = {
        "places": [place.model_dump(mode="json")],
        "text": "",
        "thinking": [],
        "errors": [],
        "done": {"retrieval_audits": [], "tool_failures": [], "tool_receipts": []},
        "canonical_duplicate_count": 0,
    }
    checks = deterministic_checks(case, result)
    assert not any(reason.startswith("缺少指定地点") for reason in checks["failures"])


def test_eval_runner_keeps_authenticated_identity_but_disables_long_term_memory():
    captured: list[tuple[str, dict]] = []

    class Response:
        def raise_for_status(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def fake_post(url, *, json, **_kwargs):
        captured.append((url, json))
        return Response()

    case = {"id": "bj_eval_identity", "city": "北京", "query": "故宫附近吃什么"}
    with (
        patch.object(daily_eval_runner.requests, "post", side_effect=fake_post),
        patch.object(daily_eval_runner, "_read_sse", return_value=[]),
    ):
        daily_eval_runner._run_case("http://test", "real-user-id", "token", case)

    assert captured[0][1]["user_id"] == "real-user-id"
    assert captured[1][1]["user_id"] == "real-user-id"
    assert captured[1][1]["use_long_term_memory"] is False


def test_checkpoint_publish_retries_transient_windows_file_lock(tmp_path):
    target = tmp_path / "report.json"
    real_replace = daily_eval_runner.os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporary sharing violation")
        return real_replace(source, destination)

    with (
        patch.object(daily_eval_runner.os, "replace", side_effect=flaky_replace),
        patch.object(daily_eval_runner.time, "sleep"),
    ):
        daily_eval_runner._write_report(target, {"complete": True})

    assert attempts == 3
    assert json.loads(target.read_text(encoding="utf-8")) == {"complete": True}


def test_negated_landmark_does_not_create_attraction_intent_for_budget_hotel():
    query = "学生来上海，酒店每晚三百多，靠地铁、正规干净就行，不必住外滩。"
    assert infer_requested_categories(query) == {PlaceCategory.HOTEL}
    assert build_place_search_queries(query) == ["经济型酒店 地铁"]
    assert extract_budget_ceiling(query) == 300


def test_accessibility_search_uses_destination_keywords_not_toilet_keywords():
    query = "坐轮椅想看北京经典景点，优先无障碍路线。"
    assert build_place_search_queries(query) == ["故宫博物院", "天坛公园", "什刹海"]
    assert place_is_human_suitable(_place("wc", "无障碍卫生间", PlaceCategory.ATTRACTION)) is False


def test_rainy_family_query_preserves_both_venue_and_food_categories():
    query = "下雨带孩子在上海玩半天，想去室内场馆，再就近吃饭。"
    assert infer_requested_categories(query) == {PlaceCategory.ATTRACTION, PlaceCategory.FOOD}
    assert build_place_search_queries(query) == [
        "上海自然博物馆", "上海科技馆", "上海博物馆", "家常菜", "亲子餐厅",
    ]


def test_compound_night_view_uses_bounded_public_destinations():
    query = "晚上想看看北京夜景再吃点本地夜宵，最好地铁还能回酒店。"
    assert build_place_search_queries(query, "北京")[:3] == [
        "什刹海", "前门大街", "北京奥林匹克公园",
    ]


def test_lacto_ovo_vegetarian_compiles_to_bounded_vegetarian_queries():
    query = "我吃蛋奶素，东城区想找清淡点的正餐，不要把咖啡店当一顿饭。"
    assert build_place_search_queries(query, "北京") == ["素食餐厅", "蔬食餐厅", "素菜馆"]


@pytest.mark.parametrize("query", [
    "豆汁", "炒肝", "卤煮", "生煎", "锅贴", "粢饭",
    "24小时餐厅", "亲子餐厅", "家常菜", "社区小馆",
    "商务餐厅", "小吃快餐",
])
def test_precise_food_provider_keyword_is_not_erased(query):
    assert _compile_provider_keyword(query, "美食") != "餐厅"


def test_explicit_breakfast_and_late_supper_hours_are_hard_filtered():
    early = _place("early", "早店", PlaceCategory.FOOD)
    late = _place("late", "晚店", PlaceCategory.FOOD)
    unknown = _place("unknown", "待确认店", PlaceCategory.FOOD)
    early = early.model_copy(update={"opening_hours": "06:30-12:00"})
    late = late.model_copy(update={"opening_hours": "11:00-02:00"})
    breakfast = _filter_by_requested_open_time(
        [early, late, unknown], "早餐最好七点能吃到",
    )
    assert [place.place_id for place in breakfast] == ["early", "unknown"]
    supper = _filter_by_requested_open_time(
        [early, late, unknown], "十点半后吃夜宵",
    )
    assert [place.place_id for place in supper] == ["late", "unknown"]


def test_landmark_is_only_a_location_anchor_for_hotel_requests():
    disney = "带四岁孩子去迪士尼，想住附近，有家庭房或接驳车更好。"
    west_lake = "老人睡眠浅，想住西湖周边但不要酒吧夜生活最吵的位置。"
    assert infer_requested_categories(disney) == {PlaceCategory.HOTEL}
    assert infer_requested_categories(west_lake) == {PlaceCategory.HOTEL}
    assert build_place_search_queries(disney) == ["迪士尼附近亲子酒店"]
    assert build_place_search_queries(west_lake) == ["西湖附近 酒店"]

    family = "带四岁孩子看西湖，想住附近，最好有家庭房、吃饭方便。"
    assert infer_requested_categories(family) == {PlaceCategory.HOTEL}
    assert build_place_search_queries(family) == ["西湖附近亲子酒店"]


def test_photography_and_local_walk_use_real_destination_anchors():
    assert build_place_search_queries("想拍北京中轴线日出和城市天际线，不要摄影器材店") == [
        "景山公园", "钟鼓楼", "永定门公园",
    ]
    assert build_place_search_queries("上海来过几次，想逛有生活气的里弄和梧桐街区") == [
        "武康路历史文化名街", "思南露天博物馆", "愚园路历史名人墙",
    ]


def test_explicit_local_dishes_are_not_erased_by_student_budget():
    query = "学生预算，在人民广场附近想吃生煎、小笼这类上海小吃，人均八十以内。"
    plan = build_category_search_plan(query, "上海")
    assert plan[PlaceCategory.FOOD] == ["生煎", "小笼"]
    assert set(extract_user_cuisine_constraint(query)) == {"生煎", "小笼", "小笼包"}


def test_low_budget_attraction_discovery_uses_bounded_low_cost_venues():
    assert build_place_search_queries(
        "住西湖区，学生预算，想找免费或便宜、能逛两三个小时的地方。", "杭州",
    ) == ["杭州西湖风景名胜区", "中国湿地博物馆", "浙江省博物馆之江馆"]


def test_mixed_named_landmarks_are_not_replaced_by_generic_attraction_query():
    query = "先去灵隐寺，再到西湖，最后在湖滨吃饭，按这个方向给地点。"
    assert build_place_search_queries(query) == ["灵隐寺", "杭州西湖风景名胜区", "湖滨 本地特色餐厅"]


def test_landmark_coverage_cannot_be_satisfied_by_a_restaurant_branch_name():
    case = {
        "id": "mixed-landmark",
        "city": "杭州",
        "intent": "mixed",
        "query": "先去灵隐寺，再到西湖，最后在湖滨吃饭。",
        "expected": {
            "min_places": 3,
            "required_place_groups": [["灵隐寺", "灵隐"], ["西湖"]],
            "ordered_place_groups": [["灵隐寺", "灵隐"], ["西湖"]],
        },
    }
    result = {
        "places": [
            {"name": "灵隐寺", "category": "attraction", "city": "杭州", "district": "西湖区"},
            {"name": "老杭州菜（西湖湖滨店）", "category": "food", "city": "杭州", "district": "上城区"},
            {"name": "新白鹿（湖滨店）", "category": "food", "city": "杭州", "district": "上城区"},
        ],
        "errors": [], "text": "候选地点", "thinking": [],
    }
    checked = deterministic_checks(case, result)
    assert not checked["passed"]
    assert "缺少指定地点：西湖" in checked["failures"]


def test_high_risk_dynamic_constraint_gets_confirmation_notice():
    text = synthesizer.ensure_dynamic_constraint_notice("推荐两家候选酒店。", "坐轮椅，需要无障碍客房。")
    assert "逐项核实" in text


def test_dynamic_hotel_notice_names_the_requested_unverified_facilities():
    text = synthesizer.ensure_dynamic_constraint_notice(
        "推荐以下候选酒店。", "带孩子住迪士尼附近，需要家庭房和接驳车。",
    )
    assert "家庭房" in text and "接驳车" in text and "预订前" in text

    family_only = synthesizer.ensure_dynamic_constraint_notice(
        "推荐以下候选酒店。", "带孩子看西湖，最好有家庭房、吃饭方便。",
    )
    assert "家庭房" in family_only and "周边餐饮便利" in family_only
    assert "接驳车" not in family_only


def test_museum_night_and_train_requests_get_explicit_operational_notices():
    museum = synthesizer.ensure_dynamic_constraint_notice(
        "推荐两个室内场馆。", "下雨想去两个博物馆，也提醒哪些可能要预约。",
    )
    night = synthesizer.ensure_dynamic_constraint_notice(
        "推荐夜景候选。", "女生一个人晚上散步看夜景，回地铁方便。",
    )
    train = synthesizer.ensure_dynamic_constraint_notice(
        "推荐附近早餐。", "早上七点坐高铁，六点吃早餐，别误车。",
    )
    assert "官网或官方小程序逐一核实" in museum
    assert "照明、人流、末班车和返程路线" in night
    assert "进站、安检" in train and "只从候选中选一个" in train


def test_hotel_dynamic_attributes_are_structured_as_requires_confirmation():
    hotel = _place("hotel", "候选酒店", PlaceCategory.HOTEL, "浦东新区").model_copy(update={
        "description": "AI 生成：有家庭房、接驳车、宠物友好、带厨房和无障碍客房",
        "tags": ["家庭房", "宠物友好", "无障碍"],
        "phone": "021-12345678",
    })
    evidence = build_constraint_evidence(
        hotel,
        "带孩子和小狗住迪士尼附近，需要家庭房、无障碍客房、接驳、停车、洗衣和厨房。",
        "浦东新区",
    )
    by_key = {item.constraint: item for item in evidence}
    assert by_key["district"].status.value == "VERIFIED"
    for key in ("family_room", "accessible_room", "shuttle", "pet_policy", "parking", "laundry", "kitchen"):
        assert by_key[key].status.value == "REQUIRES_CONFIRMATION"
        assert by_key[key].source == "unavailable_in_poi"


def test_budget_evidence_is_verified_only_when_structured_price_exists():
    with_price = _place("priced", "有价格酒店", PlaceCategory.HOTEL).model_copy(update={"amap_price": 399})
    without_price = _place("unknown", "无价格酒店", PlaceCategory.HOTEL)
    verified = build_constraint_evidence(with_price, "每晚预算五百元")
    unknown = build_constraint_evidence(without_price, "每晚预算五百元")
    verified_by_key = {item.constraint: item for item in verified}
    unknown_by_key = {item.constraint: item for item in unknown}
    assert verified_by_key["nightly_price"].status.value == "VERIFIED"
    assert verified_by_key["budget_ceiling"].status.value == "VERIFIED"
    assert verified_by_key["budget_ceiling"].value["satisfies_constraint"] is True
    assert verified_by_key["nightly_price"].source == "amap_poi"
    assert unknown_by_key["nightly_price"].status.value == "UNKNOWN"
    assert unknown_by_key["budget_ceiling"].status.value == "UNKNOWN"
    assert unknown_by_key["nightly_price"].source == "unavailable_in_poi"


def test_budget_and_requested_time_evidence_record_failed_comparisons():
    expensive = _place("expensive", "超预算餐厅", PlaceCategory.FOOD).model_copy(
        update={"amap_price": 180, "opening_hours": "07:00-21:00"}
    )
    by_key = {
        item.constraint: item for item in build_constraint_evidence(
            expensive, "人均一百以内，早餐六点要能吃到。",
        )
    }
    assert by_key["budget_ceiling"].status.value == "UNKNOWN"
    assert by_key["budget_ceiling"].value["satisfies_constraint"] is False
    assert by_key["requested_open_time"].status.value == "UNKNOWN"
    assert by_key["requested_open_time"].value["satisfies_constraint"] is False


def test_requested_time_evidence_verifies_provider_hours_covering_target():
    early = _place("early", "六点早餐店", PlaceCategory.FOOD).model_copy(
        update={"opening_hours": "05:30-10:00"}
    )
    by_key = {
        item.constraint: item for item in build_constraint_evidence(
            early, "早餐六点要能吃到。",
        )
    }
    assert by_key["requested_open_time"].status.value == "VERIFIED"
    assert by_key["requested_open_time"].value["requested_time"] == "06:00"


def test_food_and_attraction_risks_use_the_same_structured_evidence_contract():
    restaurant = _place("food", "候选餐厅", PlaceCategory.FOOD).model_copy(update={
        "description": "AI 生成：绝对无花生并提供植物奶，早上六点营业",
        "tags": ["无过敏原", "植物奶"],
    })
    attraction = _place("park", "候选景点", PlaceCategory.ATTRACTION)
    food_evidence = {
        item.constraint: item for item in build_constraint_evidence(
            restaurant, "花生严重过敏且乳糖不耐，早上六点用餐。",
        )
    }
    attraction_evidence = {
        item.constraint: item for item in build_constraint_evidence(
            attraction, "同行有人坐轮椅，需要无障碍路线。",
        )
    }
    assert food_evidence["allergen_handling"].status.value == "REQUIRES_CONFIRMATION"
    assert food_evidence["dairy_free"].status.value == "REQUIRES_CONFIRMATION"
    assert food_evidence["opening_hours"].status.value == "REQUIRES_CONFIRMATION"
    assert attraction_evidence["attraction_accessibility"].status.value == "REQUIRES_CONFIRMATION"


def test_explicit_provider_category_overrides_landmark_words_in_query():
    places = [
        _place("temple", "天坛公园", PlaceCategory.ATTRACTION, "东城区"),
        _place("food", "天坛附近北京菜", PlaceCategory.FOOD, "东城区"),
        _place("hotel", "故宫附近酒店", PlaceCategory.HOTEL, "东城区"),
    ]
    assert [place.place_id for place in filter_places_for_request(places, "天坛附近北京菜", "美食")] == ["food"]
    assert [place.place_id for place in filter_places_for_request(places, "故宫附近亲子酒店", "住宿")] == ["hotel"]


def test_router_repairs_only_missing_category_after_first_parallel_batch():
    state = {
        "messages": [HumanMessage(content="上午逛天坛，之后在附近吃北京菜。")],
        "trip_city": "北京",
        "trip_district": "东城区",
        "react_iterations": 1,
        "amap_places": [_place("temple", "天坛公园", PlaceCategory.ATTRACTION, "东城区")],
    }
    output = asyncio.run(router.run(state))
    calls = output["messages"][0].tool_calls
    assert calls
    assert {call["args"]["category"] for call in calls} == {"美食"}
    assert output["routing_signals"] == ["missing:food"]


def test_persona_notices_explain_low_walk_and_photo_evidence_boundaries():
    elder = synthesizer.ensure_dynamic_constraint_notice("推荐以下候选。", "带七十五岁老人，走不了太久。")
    photo = synthesizer.ensure_dynamic_constraint_notice("推荐以下候选。", "想拍西湖日出和倒影。")
    assert "不应在同一天全部串联" in elder and "休息点" in elder
    assert "日出朝向" in photo and "具体机位" in photo

    local_walk = synthesizer.ensure_dynamic_constraint_notice(
        "推荐三个街区。", "想逛有生活气的里弄和梧桐街区。",
    )
    assert "不同街区的漫步起点" in local_walk
    assert "近期照片、评论或街景" in local_walk
    assert "核实沿街店铺构成" in local_walk


def test_photo_request_filters_commercial_photo_services():
    places = [
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区"),
        _place("studio", "寻迹万物写真馆", PlaceCategory.ATTRACTION, "上城区"),
    ]
    kept = filter_places_for_request(places, "想拍西湖日出，别给摄影店。")
    assert [place.name for place in kept] == ["杭州西湖风景名胜区"]


def test_city_landmark_anchors_become_hard_districts():
    assert extract_district_constraint("国贸附近约客户午饭") == "朝阳区"
    assert extract_district_constraint("武康路附近喝咖啡") == "徐汇区"
    assert extract_district_constraint("湖滨附近吃杭州菜") == "上城区"
    assert extract_district_constraint("想逛有生活气的里弄和梧桐街区") is None
    assert extract_district_constraint("不逛商场，想逛老社区、吃居民常去的馆子") is None
    assert extract_district_constraint("想看看老社区") is None
    assert extract_district_constraint("想住法租界氛围的中心区") is None
    assert extract_district_constraint("住虹口，我们吃清真") == "虹口区"
    assert extract_district_constraint("陆家嘴约客户午餐") == "浦东新区"
    assert extract_district_constraint("钱江新城约客户午饭") == "上城区"
    assert extract_district_constraint("我住海淀，想找能逛两小时的地方") == "海淀区"
    assert extract_district_constraint("周末在杨浦逛有文化感的地方") == "杨浦区"
    assert extract_district_constraint("周末在拱墅逛运河文化") == "拱墅区"
    assert extract_explicit_district_constraint("先逛故宫，再去景山") is None
    assert extract_explicit_district_constraint("只在东城区逛") == "东城区"
    assert extract_district_constraint("杭州西湖风景名胜区") is None
    assert extract_district_constraint("798艺术区") is None


def test_not_only_west_lake_does_not_reserve_west_lake_for_art_query():
    query = "杭州不只想看西湖，这次想找当代艺术、设计或新建筑空间。"
    assert build_place_search_queries(query) == [
        "浙江美术馆", "中国美术学院美术馆", "天目里",
    ]
    assert extract_landmark_groups(query) == []


def test_room_city_guides_queries_when_message_omits_city_name():
    assert build_place_search_queries(
        "带七十五岁老人看西湖，走不了太久，想选能坐车或随时休息的几个点。",
        "杭州",
    ) == ["杭州西湖风景名胜区", "浙江省博物馆", "柳浪闻莺"]
    assert build_place_search_queries(
        "想拍西湖日出和倒影，推荐真正适合取景的公共地点。",
        "杭州",
    ) == ["杭州西湖风景名胜区", "北山街", "集贤亭"]


def test_named_poi_search_does_not_append_broad_category_keyword():
    captured = {}

    async def fake_run(state):
        captured["query"] = state["query_rewrite"]
        captured["district"] = state["trip_district"]
        captured["typecodes"] = state["search_typecodes"]
        return {"amap_places": []}

    with patch("app.agents.nodes.amap_search.run", side_effect=fake_run):
        asyncio.run(_run_amap_search(
            "浙江美术馆", "杭州", district="西湖区", category="景点",
            typecodes=["110000"],
        ))
    assert captured["query"] == "浙江美术馆"
    assert captured["district"] == "西湖区"
    assert captured["typecodes"] == ["110000"]


def test_dynamic_hotel_attributes_are_not_sent_as_provider_keywords():
    captured = {}

    async def fake_run(state):
        captured["query"] = state["query_rewrite"]
        return {"amap_places": [], "retrieval_audits": []}

    with patch("app.agents.nodes.amap_search.run", side_effect=fake_run):
        asyncio.run(_run_amap_search(
            "外滩附近 酒店 无障碍客房", "上海", district="黄浦区",
            category="住宿", typecodes=["100000"],
        ))
    assert captured["query"] == "酒店"


def test_stable_hotel_style_keywords_survive_provider_compilation():
    from app.tools.amap_tool import _compile_provider_keyword

    assert _compile_provider_keyword("四合院 酒店", "住宿") == "四合院 酒店"
    assert _compile_provider_keyword("历史建筑 酒店", "住宿") == "历史建筑 酒店"
    assert _compile_provider_keyword("客栈 民宿", "住宿") == "客栈 民宿"
    assert _compile_provider_keyword("酒店 无障碍客房 接驳车", "住宿") == "酒店"


def test_autumn_language_is_an_attraction_intent():
    query = "十月底在北京看秋色，不自驾，想选地铁公交能到的地方。"
    assert infer_requested_categories(query) == {PlaceCategory.ATTRACTION}
    assert build_place_search_queries(query) == ["颐和园", "香山公园", "地坛公园"]


def test_negative_chain_constraint_filters_provider_candidates():
    places = [
        _place("local", "前门老北京小吃", PlaceCategory.FOOD, "东城区"),
        _place("chain", "肯德基(前门店)", PlaceCategory.FOOD, "东城区"),
    ]
    kept = filter_places_for_request(places, "前门附近想吃北京小吃，不要肯德基、麦当劳这类全国连锁。")
    assert [place.name for place in kept] == ["前门老北京小吃"]


def test_nationwide_chain_request_rejects_provider_chain_label():
    places = [
        _place("local", "护国寺小吃", PlaceCategory.FOOD, "东城区"),
        _place("chain", "南门铜锅涮肉", PlaceCategory.FOOD, "东城区").model_copy(
            update={"tags": ["连锁品牌"]},
        ),
    ]
    kept = filter_places_for_request(
        places,
        "前门附近想吃北京小吃，别给我肯德基、麦当劳这类全国连锁。",
    )
    assert [place.name for place in kept] == ["护国寺小吃"]


def test_report_summary_separates_judge_error_from_scored_failure():
    rows = [
        {"id": "a", "passed": True, "evaluation_status": "completed", "judge": {"scores": {"intent_relevance": 5}}},
        {"id": "b", "passed": False, "evaluation_status": "judge_error", "judge": {"error": "402 Payment Required"}},
    ]
    summary = _summarize(rows, {"a": {"city": "北京", "intent": "food"}, "b": {"city": "北京", "intent": "food"}})
    assert summary["judged"] == 1
    assert summary["judge_errors"] == 1
    assert summary["judged_pass_rate"] == 1.0


def test_infrastructure_failure_is_excluded_from_semantic_judge(tmp_path):
    dataset_path = Path(__file__).resolve().parents[1] / "eval_data" / "daily_queries" / "cases.json"
    failed_result = {
        "places": [], "raw_place_count": 0, "canonical_duplicate_count": 0,
        "text": "", "thinking": [], "errors": [],
        "done": {
            "retrieval_audits": [{
                "query": "景点", "city": "北京", "provider": "amap",
                "execution_mode": "live", "retrieved_at": "2026-08-12T00:00:00Z",
                "response_hash": None, "result_count": 0, "status": "blocked",
                "attempted": False, "fallback_reason": "circuit_open",
                "error_category": "circuit_open", "provider_health_failure": False,
            }],
            "tool_failures": [{"tool": "search_places", "reason": "circuit_open"}],
        },
    }
    with (
        patch.object(daily_eval_runner, "_register", return_value=("u", "t")),
        patch.object(daily_eval_runner, "verify_live_server", return_value={
            "runtime_profile": "local_real", "demo_mode": False,
            "amap_mock": False, "amap_configured": True,
        }),
        patch.object(daily_eval_runner, "_run_case", return_value=failed_result),
        patch.object(daily_eval_runner, "_read_service_metrics", return_value={"model_calls": {}, "model_usage": {}}),
    ):
        report = daily_eval_runner.run(
            "http://example.invalid", dataset_path,
            {"bj_01_first_time_landmarks"}, None, True,
            workers=1, checkpoint_path=tmp_path / "checkpoint.json", allow_paid_generation=True,
        )
    row = report["cases"][0]
    assert row["evaluation_status"] == "infrastructure_error"
    assert row["judge"]["skip_reason"] == "circuit_open"
    assert report["summary"]["retrieval_availability"]["availability_rate"] == 0.0
    assert report["summary"]["recommendation_quality_under_valid_retrieval"]["eligible"] == 0
    assert report["summary"]["end_to_end_success"]["pass_rate"] == 0.0


def test_resume_rejects_legacy_rows_without_live_retrieval_integrity(tmp_path):
    dataset_path = Path(__file__).resolve().parents[1] / "eval_data" / "daily_queries" / "cases.json"
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(json.dumps({"cases": [
        {
            "id": "bj_01_first_time_landmarks", "passed": True,
            "deterministic": {"passed": True, "failures": []},
            "judge": {"scores": {"intent_relevance": 5}, "passed": True},
            "output": {"places": [], "text": "", "thinking": [], "errors": [], "done": {}},
        },
        {"id": "bj_02_elder_low_walk", "passed": False, "error": "old judge error"},
    ]}), encoding="utf-8")
    system_result = {"places": [], "text": "", "thinking": [], "errors": [], "done": {}}
    with (
        patch.object(daily_eval_runner, "_register", return_value=("u", "t")),
        patch.object(daily_eval_runner, "verify_live_server", return_value={
            "runtime_profile": "local_real", "demo_mode": False,
            "amap_mock": False, "amap_configured": True,
        }),
        patch.object(daily_eval_runner, "_run_case", return_value=system_result) as run_case,
        patch.object(daily_eval_runner, "_read_service_metrics", return_value={"model_calls": {}, "model_usage": {}}),
    ):
        report = daily_eval_runner.run(
            "http://example.invalid", dataset_path,
            {"bj_01_first_time_landmarks", "bj_02_elder_low_walk"}, None, True,
            workers=1, resume_path=resume_path, allow_paid_generation=True,
        )
    assert run_case.call_count == 2
    assert report["summary"]["total"] == 2


def test_llm_judge_is_disabled_even_when_api_keys_are_configured():
    case = {"query": "北京景点", "persona": "游客", "dimensions": [], "intent": "attraction", "expected": {"semantic_requirement": "推荐北京景点"}}
    result = {"places": [], "text": "候选", "thinking": [], "errors": [], "done": {}}
    with (
        patch.object(daily_eval_runner.settings, "deepseek_api_key", "primary-key"),
        patch.object(daily_eval_runner.requests, "post") as post,
    ):
        with __import__("pytest").raises(RuntimeError, match="API LLM-as-Judge 已禁用"):
            daily_eval_runner.llm_judge(case, result)
    post.assert_not_called()


def test_live_eval_requires_explicit_paid_generation_authorization():
    dataset_path = Path(__file__).resolve().parents[1] / "eval_data" / "daily_queries" / "cases.json"
    with pytest.raises(RuntimeError, match="--allow-paid-generation"):
        daily_eval_runner.run(
            "http://example.invalid", dataset_path, set(), 1, True,
        )


def test_model_call_metrics_delta_is_label_bounded():
    before = {"model_calls": {"deepseek-chat:router": 3}}
    after = {"model_calls": {"deepseek-chat:router": 5, "deepseek-chat:synthesizer": 1}}
    assert daily_eval_runner._numeric_mapping_delta(before, after, "model_calls") == {
        "deepseek-chat:router": 2,
        "deepseek-chat:synthesizer": 1,
    }


def test_report_summary_separates_deterministic_and_llm_semantic_failures():
    rows = [
        {
            "id": "pass", "passed": True, "evaluation_status": "completed",
            "deterministic": {"passed": True, "failures": []},
            "judge": {"passed": True, "scores": {"intent_relevance": 5}, "judge_provider": "deepseek", "judge_model": "deepseek-chat"},
        },
        {
            "id": "det", "passed": False, "evaluation_status": "completed",
            "deterministic": {"passed": False, "failures": ["缺少必需品类：hotel"]},
            "judge": {"passed": True, "scores": {"intent_relevance": 5}, "judge_provider": "deepseek", "judge_model": "deepseek-chat"},
        },
        {
            "id": "llm", "passed": False, "evaluation_status": "completed",
            "deterministic": {"passed": True, "failures": []},
            "judge": {"passed": False, "scores": {"intent_relevance": 2}, "judge_provider": "deepseek", "judge_model": "deepseek-chat"},
        },
        {
            "id": "both", "passed": False, "evaluation_status": "completed",
            "deterministic": {"passed": False, "failures": ["地点数 0 小于最低要求 2"]},
            "judge": {"passed": False, "scores": {"intent_relevance": 1}, "judge_provider": "deepseek", "judge_model": "deepseek-chat"},
        },
    ]
    cases = {
        "pass": {"city": "北京", "intent": "attraction"},
        "det": {"city": "上海", "intent": "hotel"},
        "llm": {"city": "杭州", "intent": "mixed"},
        "both": {"city": "杭州", "intent": "all"},
    }
    summary = _summarize(rows, cases)
    assert summary["failure_type_counts"] == {
        "passed": 1,
        "deterministic_only": 1,
        "deterministic_failed_total": 2,
        "llm_semantic_only": 1,
        "llm_semantic_failed_total": 2,
        "deterministic_and_llm": 1,
    }
    assert summary["by_intent_group"]["compound"] == {"total": 2, "passed": 0, "pass_rate": 0.0}
    assert summary["judge_provider_distribution"] == {"deepseek": 4}


def test_landmark_aliases_infer_district_without_overriding_explicit_area():
    assert extract_district_constraint("我住七宝，明早找早餐") == "闵行区"
    assert extract_district_constraint("迪士尼附近吃饭") == "浦东新区"
    assert extract_district_constraint("不要浦东新区，改成闵行区") == "闵行区"


def test_room_opening_prompt_does_not_treat_generic_area_copy_as_district():
    assert extract_district_constraint(ROOM_OPENING_PROMPT) is None
    assert extract_district_constraint("覆盖核心片区，标注大致价位与所在片区") is None
    assert extract_district_constraint("带孩子在浦东玩一天") == "浦东新区"


def test_room_opening_prompt_expands_to_bounded_provider_queries():
    queries = build_place_search_queries(ROOM_OPENING_PROMPT)
    assert queries == ["必去地标 景点", "本地老字号 高分餐厅", "不同价位 酒店 民宿"]
    for query in queries:
        parsed = SearchPlacesArgs.model_validate({"query": query, "city": "北京"})
        assert parsed.query == query


def test_router_builds_three_valid_calls_for_real_room_opening_prompt():
    state = {
        "messages": [HumanMessage(content=ROOM_OPENING_PROMPT)],
        "trip_city": "北京",
        "trip_district": extract_district_constraint(ROOM_OPENING_PROMPT),
        "react_iterations": 0,
        "amap_places": [],
    }
    with patch.object(router.settings, "demo_mode", False), patch.object(
        router.settings, "deterministic_routing_enabled", True
    ):
        output = asyncio.run(router.run(state))

    calls = output["messages"][0].tool_calls
    assert [call["args"]["query"] for call in calls] == build_place_search_queries(ROOM_OPENING_PROMPT)
    assert all("district" not in call["args"] for call in calls)
    assert [call["args"]["typecodes"] for call in calls] == [
        ["110000", "140100", "140200", "140400", "140500"],
        ["050000"],
        ["100000"],
    ]
    for call in calls:
        SearchPlacesArgs.model_validate(call["args"])


def test_semantic_location_and_ordered_landmarks_expand_to_bounded_searches():
    assert build_place_search_queries("我住在浦东新区南边的海边，附近有哪些吃的") == [
        "滴水湖附近餐厅", "南汇新城餐厅", "芦潮港海鲜餐厅",
    ]
    assert build_place_search_queries("我想先去迪士尼乐园，再去上海野生动物园") == [
        "上海迪士尼乐园", "上海野生动物园",
    ]
    assert build_place_search_queries("我要去中国第一高楼那里") == ["上海中心大厦"]
    assert build_place_search_queries("晚上到虹桥机场，附近找个方便住一晚的酒店") == [
        "虹桥机场T2附近酒店", "虹桥枢纽酒店", "虹桥机场酒店",
    ]
    assert build_place_search_queries("迪士尼玩完以后，附近有什么适合一家人吃饭的地方") == [
        "迪士尼小镇餐厅", "上海迪士尼度假区餐厅", "比斯特上海购物村餐厅",
    ]


def test_broad_landmark_request_keeps_complementary_destinations():
    query = "第一次到杭州只有一天，想看西湖和最有杭州味道的地方。"
    assert build_place_search_queries(query) == [
        "杭州西湖风景名胜区", "灵隐寺", "京杭大运河杭州景区",
    ]
    places = [
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区"),
        _place("temple", "灵隐寺", PlaceCategory.ATTRACTION, "西湖区"),
        _place("canal", "京杭大运河杭州景区", PlaceCategory.ATTRACTION, "拱墅区"),
    ]
    assert [place.place_id for place in filter_places_for_request(places, query)] == [
        "lake", "temple", "canal",
    ]


def test_exact_provider_query_keeps_only_the_named_poi_group():
    places = [
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区"),
        _place("museum", "杭州博物馆", PlaceCategory.ATTRACTION, "上城区"),
        _place("park", "西溪国家湿地公园", PlaceCategory.ATTRACTION, "西湖区"),
    ]
    kept = filter_places_for_request(places, "杭州西湖风景名胜区", "景点")
    assert [place.place_id for place in kept] == ["lake"]


def test_ranking_keeps_one_landmark_then_prioritises_complementary_places():
    query = "第一次到上海，想看外滩、老城和现代天际线。"
    places = [
        _place("bund", "外滩", PlaceCategory.ATTRACTION, "黄浦区", 4.8),
        _place("bund-view", "外滩观景平台", PlaceCategory.ATTRACTION, "黄浦区", 4.9),
        _place("museum", "上海博物馆", PlaceCategory.ATTRACTION, "黄浦区", 4.7),
        _place("tower", "上海中心大厦", PlaceCategory.ATTRACTION, "浦东新区", 4.7),
    ]
    ranked = rank_places_for_request(places, query)
    assert ranked[0].name == "外滩"
    assert [place.name for place in ranked[1:3]] == ["上海博物馆", "上海中心大厦"]


def test_mixed_landmark_coverage_requires_real_attraction_cards():
    query = "先去灵隐寺，再到西湖，最后在湖滨吃饭。"
    places = [
        _place("temple", "灵隐寺", PlaceCategory.ATTRACTION, "西湖区"),
        _place("food", "老杭州菜（西湖湖滨店）", PlaceCategory.FOOD, "上城区"),
    ]
    assert request_has_all_landmarks(places, query) is False


def test_beijing_landmark_sequence_is_split_and_constrained_in_user_order():
    assert build_place_search_queries("我要去长城然后去奥林匹克公园") == [
        "长城", "北京奥林匹克公园",
    ]
    places = [
        _place("forest", "奥林匹克森林公园", PlaceCategory.ATTRACTION, "朝阳区", 4.9),
        _place("park", "北京奥林匹克公园", PlaceCategory.ATTRACTION, "朝阳区", 4.7),
        _place("wall", "长城（八达岭）", PlaceCategory.ATTRACTION, "延庆区", 4.8),
        _place("park-centre", "奥林匹克公园中心区", PlaceCategory.ATTRACTION, "朝阳区", 4.6),
    ]
    result = filter_places_for_request(places, "我要去长城然后去奥林匹克公园")
    assert [place.place_id for place in result] == ["wall", "park"]


def test_synthesizer_treats_only_latest_human_turn_as_current_request():
    messages = [
        HumanMessage(content=ROOM_OPENING_PROMPT),
        HumanMessage(content="我要去长城然后去奥林匹克公园"),
    ]
    assert synthesizer.latest_user_request(messages) == "我要去长城然后去奥林匹克公园"


def test_explicit_landmark_response_only_claims_grounded_final_places():
    places = [
        _place("wall", "长城（八达岭）", PlaceCategory.ATTRACTION, "延庆区", 4.8),
        _place("park", "北京奥林匹克公园", PlaceCategory.ATTRACTION, "朝阳区", 4.7),
    ]
    result = synthesizer.ground_explicit_landmark_response(
        "北京三天共15个地点，长城也一并放入。",
        places,
        "我要去长城然后去奥林匹克公园",
    )
    assert result == (
        "已按你的顺序找到 2 个地点：长城（八达岭） → 北京奥林匹克公园。"
        "卡片与地图点位使用同一份高德 POI 数据。"
    )


def test_explicit_landmark_response_discloses_missing_entity():
    places = [_place("park", "北京奥林匹克公园", PlaceCategory.ATTRACTION, "朝阳区", 4.7)]
    result = synthesizer.ground_explicit_landmark_response(
        "都找到了。", places, "我要去长城然后去奥林匹克公园"
    )
    assert "未找到：长城" in result
    assert "没有用相似地点凑数" in result


def test_broad_landmark_request_keeps_multi_card_summary():
    places = [
        _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区"),
        _place("museum", "浙江省博物馆", PlaceCategory.ATTRACTION, "西湖区"),
    ]
    candidate = "共找到 2 个互补地点。"
    assert synthesizer.ground_explicit_landmark_response(
        candidate, places, "带七十五岁老人看西湖，想选几个能休息的点。",
    ) == candidate


def test_explicit_food_request_filters_transport_and_attractions():
    places = [
        _place("f", "本帮菜馆", PlaceCategory.FOOD),
        _place("a", "七宝古镇", PlaceCategory.ATTRACTION),
        _place("t", "七宝(地铁站)", PlaceCategory.TRANSPORT),
    ]
    result = filter_places_for_request(places, "我住在闵行区，有什么美食？")
    assert [place.place_id for place in result] == ["f"]


def test_named_landmarks_are_ranked_in_user_order_ahead_of_high_rating_noise():
    places = [
        _place("noise", "东方明珠", PlaceCategory.ATTRACTION, "浦东新区", 5.0),
        _place("zoo", "上海野生动物园", PlaceCategory.ATTRACTION, "浦东新区", 4.6),
        _place("disney", "上海迪士尼乐园", PlaceCategory.ATTRACTION, "浦东新区", 4.7),
    ]
    result = rank_places_for_request(places, "先去迪士尼乐园，再去上海野生动物园")
    assert [place.place_id for place in result[:2]] == ["disney", "zoo"]


def test_single_want_to_visit_landmark_is_a_closed_entity_request():
    query = "我想去中国第一高楼看看。"
    groups = extract_landmark_groups(query)
    assert groups
    assert is_closed_landmark_request(query, groups)


def test_explicit_snack_semantics_rank_a_matching_poi_ahead_of_generic_high_rating_food():
    generic = _place(
        "generic", "高分铜锅涮肉(前门店)", PlaceCategory.FOOD, "东城区", 4.9,
    ).model_copy(update={"tags": ["京味小吃拼盘"]})
    snack = _place("snack", "护国寺小吃", PlaceCategory.FOOD, "东城区", 4.3)
    ranked = rank_places_for_request(
        [generic, snack],
        "前门附近想吃北京小吃，别给我全国连锁。",
    )
    assert [place.place_id for place in ranked[:2]] == ["snack", "generic"]


def test_no_mall_exclusion_checks_address_as_well_as_name():
    community = _place("community", "老社区活动中心", PlaceCategory.ATTRACTION, "东城区")
    hidden_mall = _place("mall", "城市会客厅", PlaceCategory.ATTRACTION, "东城区").model_copy(
        update={"address": "某购物中心三层"}
    )
    kept = filter_places_for_request([community, hidden_mall], "不想逛商场，想看看老社区。")
    assert [place.place_id for place in kept] == ["community"]

    branded_mall = _place("branded", "小吃店", PlaceCategory.FOOD, "东城区").model_copy(
        update={"address": "王府井喜悦B1层"}
    )
    kept = filter_places_for_request([branded_mall], "不想进商场，想吃居民常去的馆子。")
    assert kept == []

    department_store = _place("department", "社区小馆", PlaceCategory.FOOD, "静安区").model_copy(
        update={"address": "南京西路1601号芮欧百货4层"}
    )
    assert filter_places_for_request(
        [department_store], "不想逛商场，想吃居民日常的小馆。"
    ) == []


def test_provider_category_identity_conflicts_are_removed_before_delivery():
    lobby = _place("lobby", "国贸大酒店大堂", PlaceCategory.HOTEL, "朝阳区")
    hotel = _place("hotel", "国贸大酒店", PlaceCategory.HOTEL, "朝阳区")
    kept = _filter_category_identity_conflicts([lobby, hotel])
    assert [place.place_id for place in kept] == ["hotel"]

    scenic_as_food = _place(
        "scenic-food", "杭州西湖风景名胜区-石屋洞", PlaceCategory.FOOD, "西湖区"
    )
    restaurant_in_park = _place(
        "park-food", "公园餐厅", PlaceCategory.FOOD, "西湖区"
    )
    kept = _filter_category_identity_conflicts([scenic_as_food, restaurant_in_park])
    assert [place.place_id for place in kept] == ["park-food"]


def test_explicit_two_attractions_keep_pair_evidence_and_are_not_rendered_as_backups():
    first = _place("museum-a", "甲博物馆", PlaceCategory.ATTRACTION, "东城区").model_copy(
        update={"coords": Coordinates(lng=116.397, lat=39.908)}
    )
    second = _place("museum-b", "乙科技馆", PlaceCategory.ATTRACTION, "东城区").model_copy(
        update={"coords": Coordinates(lng=116.407, lat=39.908)}
    )
    query = "北京下雨，想安排两个室内文化场馆，也提醒我预约。"
    evidenced = _attach_delivered_attraction_evidence([first, second], query)
    pair = next(
        item for item in evidenced[1].geo_evidence
        if item.constraint_kind == "delivered_attraction_proximity"
    )
    assert pair.anchor_place == "甲博物馆"
    assert pair.straight_line_distance_km is not None
    response = synthesizer._build_demo_response(evidenced, "北京", {}, user_request=query)
    assert "两处均为本次目标，不是互斥备选" in response
    assert "不合适时换" not in response


def test_ordered_landmarks_are_rendered_in_user_order_before_food():
    palace = _place("palace", "故宫博物院", PlaceCategory.ATTRACTION, "东城区")
    hill = _place("hill", "景山公园", PlaceCategory.ATTRACTION, "西城区").model_copy(
        update={"coords": Coordinates(lng=116.397, lat=39.925)}
    )
    food = _place("duck", "景山烤鸭店", PlaceCategory.FOOD, "西城区").model_copy(
        update={"coords": Coordinates(lng=116.398, lat=39.925)}
    )
    query = "先故宫再景山，走完想在附近吃北京菜，地点顺序别弄反。"
    selected = _attach_shared_anchor_evidence([palace, hill, food], query)
    selected = _attach_delivered_attraction_evidence(selected, query)
    response = synthesizer._build_demo_response(selected, "北京", {}, user_request=query)
    assert "故宫博物院 → 景山公园 → 景山烤鸭店" in response
    assert "其他景点如果没有与主点的路线证据，仅作备选" not in response


def test_obviously_remote_meals_fail_closed_for_nearby_after_visit_request():
    lake = _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区").model_copy(
        update={"coords": Coordinates(lng=120.130, lat=30.250)}
    )
    remote = _place("remote", "远处夜宵", PlaceCategory.FOOD, "西湖区").model_copy(
        update={"coords": Coordinates(lng=120.200, lat=30.300)}
    )
    query = "晚上看完西湖夜景想吃点杭州夜宵。"
    selected = _attach_shared_anchor_evidence([lake, remote], query)
    selected = _drop_obviously_remote_meals(selected, query)
    assert [place.place_id for place in selected] == ["lake"]


def test_elder_portfolio_uses_pairwise_compact_core_not_external_center_radius():
    attraction_near = _place("a-near", "近景点", PlaceCategory.ATTRACTION, "上城区").model_copy(
        update={
            "coords": Coordinates(lng=120.160, lat=30.250),
            "geo_evidence": [GeoEvidence(
                slot_id="portfolio", anchor_place="参考中心",
                constraint_kind="portfolio_compactness", status=EvidenceStatus.VERIFIED,
                straight_line_distance_km=0.2,
            )],
        }
    )
    attraction_far = _place("a-far", "远景点", PlaceCategory.ATTRACTION, "上城区").model_copy(
        update={"coords": Coordinates(lng=120.230, lat=30.320)}
    )
    food_near = _place("f-near", "清淡餐厅", PlaceCategory.FOOD, "上城区").model_copy(
        update={"coords": Coordinates(lng=120.161, lat=30.251)}
    )
    food_far = _place("f-far", "远餐厅", PlaceCategory.FOOD, "上城区").model_copy(
        update={"coords": Coordinates(lng=120.240, lat=30.320)}
    )
    hotel_near = _place("h-near", "近地铁酒店", PlaceCategory.HOTEL, "上城区").model_copy(
        update={"coords": Coordinates(lng=120.162, lat=30.252)}
    )
    hotel_far = _place("h-far", "远酒店", PlaceCategory.HOTEL, "上城区").model_copy(
        update={"coords": Coordinates(lng=120.250, lat=30.320)}
    )
    selected = _attach_low_transfer_core_evidence(
        [attraction_far, attraction_near, food_far, food_near, hotel_far, hotel_near],
        "带两位老人，少走路、少折腾，给景点、清淡餐厅和酒店候选。",
    )
    core_edges = [
        item for place in selected for item in place.geo_evidence
        if item.constraint_kind == "low_transfer_core_proximity"
    ]
    assert len(core_edges) == 3
    assert max(item.straight_line_distance_km or 99 for item in core_edges) < 0.5
    response = synthesizer._build_demo_response(
        selected, "杭州", {}, user_request="带两位老人，少走路、少折腾，给景点、清淡餐厅和酒店候选。",
    )
    assert "三者两两直线距离的最大值" in response
    assert "近景点 + 清淡餐厅 + 近地铁酒店" in response


def test_short_station_layover_keeps_one_route_verified_attraction():
    near = _place("near", "近站展馆", PlaceCategory.ATTRACTION, "上城区").model_copy(update={
        "geo_evidence": [GeoEvidence(
            slot_id="a", anchor_place="杭州东站", status=EvidenceStatus.VERIFIED,
            estimated_travel_minutes=12, satisfies_constraint=True,
        )],
    })
    farther = _place("far", "较远展馆", PlaceCategory.ATTRACTION, "西湖区").model_copy(update={
        "geo_evidence": [GeoEvidence(
            slot_id="a", anchor_place="杭州东站", status=EvidenceStatus.VERIFIED,
            estimated_travel_minutes=18, satisfies_constraint=True,
        )],
    })
    result = _filter_time_sensitive_hub_candidates(
        [farther, near], "杭州东站换乘只有两小时，想看点东西，别让我误车。",
    )
    assert [place.place_id for place in result] == ["near"]


def test_business_lunch_excludes_dinner_only_primary_candidate():
    lunch = _place("lunch", "午餐餐厅", PlaceCategory.FOOD, "浦东新区").model_copy(
        update={"opening_hours": "11:30-14:00 17:30-21:00"},
    )
    dinner = _place("dinner", "晚餐餐厅", PlaceCategory.FOOD, "浦东新区", 4.9).model_copy(
        update={"opening_hours": "17:00-21:00"},
    )
    result = _filter_by_requested_open_time(
        [dinner, lunch], "陆家嘴约客户午餐，人均四百左右。",
    )
    assert [place.place_id for place in result] == ["lunch"]


def test_low_transfer_portfolio_does_not_drop_below_three_candidates():
    core_a = _place("a", "市区酒店A", PlaceCategory.HOTEL, "东城区")
    core_b = _place("b", "市区酒店B", PlaceCategory.HOTEL, "西城区")
    remote = _place("r", "远郊酒店", PlaceCategory.HOTEL, "密云区")
    result = _filter_low_transfer_candidates(
        [remote, core_a, core_b], "带老人去北京，酒店靠地铁，少折腾。",
    )
    assert {place.place_id for place in result} == {"a", "b", "r"}


def test_three_city_low_transfer_portfolio_uses_a_bounded_district_cluster():
    places = [
        _place(f"a-{index}", f"景点{index}", PlaceCategory.ATTRACTION, district)
        for index, district in enumerate(("东城区", "西城区", "朝阳区"))
    ] + [
        _place(f"f-{index}", f"餐厅{index}", PlaceCategory.FOOD, district)
        for index, district in enumerate(("东城区", "东城区", "朝阳区", "西城区"))
    ] + [
        _place(f"h-{index}", f"酒店{index}", PlaceCategory.HOTEL, district)
        for index, district in enumerate(("朝阳区", "朝阳区", "西城区"))
    ] + [_place("remote", "古北口远郊酒店", PlaceCategory.HOTEL, "密云区")]
    places = [place.model_copy(update={"city": "北京"}) for place in places]
    result = _filter_low_transfer_candidates(
        places, "带两位老人去北京，先给景点、清淡餐厅和住宿，少折腾。",
    )
    assert len(result) == 6
    assert all(place.district in {"东城区", "西城区", "朝阳区"} for place in result)
    assert all(
        any(item.constraint_kind == "portfolio_compactness" for item in place.geo_evidence)
        for place in result
    )
    assert all(
        any(item.constraint_kind == "portfolio_route" and item.status == EvidenceStatus.UNKNOWN
            for item in place.geo_evidence)
        for place in result
    )
    assert all(any("实际通勤时间和路线" in action for action in place.confirmation_actions)
               for place in result)
    response = synthesizer._build_demo_response(
        result,
        "北京",
        {},
        None,
        "带两位老人去北京，先给景点、清淡餐厅和住宿，少折腾。",
    )
    assert "六张卡是备选，不建议全部走完" in response
    assert "优先核验组合" in response
    assert "实际驾车/网约车路线仍未证实" in response
    assert "不建议老人步行串联" in response


def test_requested_attraction_time_drops_explicitly_closed_candidates():
    dawn = _place("dawn", "河畔步道", PlaceCategory.ATTRACTION, "上城区").model_copy(
        update={"opening_hours": "24小时营业"},
    )
    late = _place("late", "城市阳台", PlaceCategory.ATTRACTION, "上城区").model_copy(
        update={"opening_hours": "09:00-16:00"},
    )
    result = select_eligible_places([late, dawn], "杭州晚上散步看夜景，想找人多一点的地方。")
    assert [place.place_id for place in result] == ["dawn"]


def test_small_portion_transit_ticket_and_room_space_get_per_place_actions():
    food = _place("food", "候选餐厅", PlaceCategory.FOOD, "西湖区")
    attraction = _place("a", "候选景点", PlaceCategory.ATTRACTION, "西湖区")
    hotel = _place("h", "候选酒店", PlaceCategory.HOTEL, "上城区")
    solo = {item.constraint for item in build_constraint_evidence(food, "一个人吃，想要小份。")}
    transit = {
        item.constraint for item in build_constraint_evidence(
            attraction, "学生预算，想找免费、公共交通方便的景点，不自驾。",
        )
    }
    room = {
        item.constraint for item in build_constraint_evidence(
            hotel, "一家三口住一周，房间别太局促。",
        )
    }
    child_diet = {
        item.constraint for item in build_constraint_evidence(
            food, "带孩子吃饭，孩子不太能吃辣。",
        )
    }
    assert "solo_portion" in solo
    assert {"public_transit_access", "ticket_affordability"} <= transit
    assert {"family_room", "room_space"} <= room
    assert {"family_dining", "dietary_policy"} <= child_diet


def test_meal_slots_reject_drink_only_spaces_when_real_restaurants_exist():
    places = [
        _place("tea", "小慢居书茶院", PlaceCategory.FOOD, "上城区").model_copy(
            update={"tags": ["茶座", "品茶"]},
        ),
        _place("meal-1", "日月轩中餐厅", PlaceCategory.FOOD, "上城区"),
        _place("meal-2", "德悦海鲜餐厅", PlaceCategory.FOOD, "上城区"),
    ]
    result = select_eligible_places(places, "杭州钱江新城约客户午饭，希望安静。")
    assert {place.place_id for place in result} == {"meal-1", "meal-2"}


def test_named_area_and_local_city_food_filters_keep_direct_evidence():
    dessert = [
        _place("near-1", "鼓楼甜品店", PlaceCategory.FOOD, "西城区").model_copy(
            update={"city": "北京", "coords": Coordinates(lng=116.390, lat=39.945)},
        ),
        _place("near-2", "什刹海糖水铺", PlaceCategory.FOOD, "西城区").model_copy(
            update={"city": "北京", "coords": Coordinates(lng=116.385, lat=39.942)},
        ),
        _place("far", "前门甜品店", PlaceCategory.FOOD, "西城区"),
    ]
    bounded = select_eligible_places(dessert, "北京逛完什刹海想吃甜品。")
    assert {place.place_id for place in bounded} == {"near-1", "near-2"}
    assert all(
        any(item.constraint_kind == "named_area_proximity" for item in place.geo_evidence)
        for place in bounded
    )

    supper = [
        _place("local-1", "东泰祥生煎馆", PlaceCategory.FOOD, "黄浦区"),
        _place("local-2", "顶特勒粥面馆", PlaceCategory.FOOD, "黄浦区"),
        _place("other", "重庆老火锅", PlaceCategory.FOOD, "黄浦区"),
    ]
    local = select_eligible_places(supper, "上海夜宵想吃点本地味道。")
    assert {place.place_id for place in local} == {"local-1", "local-2"}


def test_hotel_cleanliness_and_transit_are_explicit_confirmation_constraints():
    hotel = _place("hotel", "经济酒店", PlaceCategory.HOTEL, "黄浦区")
    constraints = {
        item.constraint
        for item in build_constraint_evidence(hotel, "正规干净、靠地铁的酒店。")
    }
    assert {"hotel_cleanliness_safety", "hotel_transit_access"} <= constraints


def test_nearby_food_is_bound_to_a_delivered_landmark_when_plan_edge_is_missing():
    lake = _place("lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区")
    near = _place("near", "湖边面馆", PlaceCategory.FOOD, "西湖区")
    result = _attach_shared_anchor_evidence([lake, near], "看完西湖夜景后想就近吃宵夜。")
    food = next(place for place in result if place.category == PlaceCategory.FOOD)
    assert any(item.constraint_kind == "shared_anchor_proximity" for item in food.geo_evidence)
    assert any(item.constraint_kind == "shared_anchor_route" for item in food.geo_evidence)
    assert any("实际通勤时间和路线" in action for action in food.confirmation_actions)


def test_open_visit_then_eat_request_selects_the_closest_delivered_anchor():
    far = _place("far", "远端夜景台", PlaceCategory.ATTRACTION, "朝阳区").model_copy(
        update={"coords": Coordinates(lng=116.60, lat=39.90)},
    )
    near = _place("near", "湖边夜景台", PlaceCategory.ATTRACTION, "西城区").model_copy(
        update={"coords": Coordinates(lng=116.39, lat=39.94)},
    )
    food = _place("food", "本地夜宵店", PlaceCategory.FOOD, "西城区").model_copy(
        update={"coords": Coordinates(lng=116.391, lat=39.941)},
    )
    result = _attach_shared_anchor_evidence(
        [far, near, food], "晚上看完夜景再吃点本地夜宵。",
    )
    delivered_food = next(place for place in result if place.category == PlaceCategory.FOOD)
    proximity = next(
        item for item in delivered_food.geo_evidence
        if item.constraint_kind == "shared_anchor_proximity"
    )
    assert proximity.anchor_place == "湖边夜景台"
    assert proximity.satisfies_constraint is None
    assert proximity.straight_line_distance_km < 1


def test_incidental_neighborhood_meal_uses_the_same_anchor_mechanism():
    lane = _place("lane", "武康路历史街区", PlaceCategory.ATTRACTION, "徐汇区").model_copy(
        update={"coords": Coordinates(lng=121.44, lat=31.21)},
    )
    local = _place("local", "街坊小馆", PlaceCategory.FOOD, "徐汇区").model_copy(
        update={"coords": Coordinates(lng=121.441, lat=31.211)},
    )
    result = _attach_shared_anchor_evidence(
        [lane, local], "不想逛商场，想去老街区走走，顺便吃一家社区小馆。",
    )
    food = next(place for place in result if place.category == PlaceCategory.FOOD)
    assert any(
        item.anchor_place == lane.name and item.constraint_kind == "shared_anchor_proximity"
        for item in food.geo_evidence
    )


def test_shared_anchor_refresh_removes_stale_undelivered_anchor_and_action():
    delivered = _place("delivered", "世界技能博物馆", PlaceCategory.ATTRACTION, "杨浦区")
    food = _place("food", "学生面馆", PlaceCategory.FOOD, "杨浦区").model_copy(update={
        "geo_evidence": [
            GeoEvidence(
                slot_id="old",
                anchor_place="未交付旧景点",
                constraint_kind="shared_anchor_proximity",
                status=EvidenceStatus.VERIFIED,
                satisfies_constraint=None,
                straight_line_distance_km=0.2,
                source="amap_delivered_poi_coordinates",
            ),
            GeoEvidence(
                slot_id="old",
                anchor_place="未交付旧景点",
                constraint_kind="shared_anchor_route",
                status=EvidenceStatus.UNKNOWN,
                satisfies_constraint=None,
                straight_line_distance_km=0.2,
                source="route_time_not_queried",
                failure_reason="route_time_not_queried",
            ),
        ],
        "confirmation_actions": ["打开地图路线功能核实与未交付旧景点之间的实际通勤时间和路线"],
    })
    result = _attach_shared_anchor_evidence(
        [delivered, food], "逛完有文化感的地方，再吃顿学生预算的饭。",
    )
    refreshed = next(place for place in result if place.category == PlaceCategory.FOOD)
    assert {item.anchor_place for item in refreshed.geo_evidence} == {delivered.name}
    assert all("未交付旧景点" not in action for action in refreshed.confirmation_actions)


def test_local_community_and_small_eatery_request_receives_a_delivered_anchor():
    lane = _place("lane", "老社区街区", PlaceCategory.ATTRACTION, "黄浦区")
    food = _place("food", "居民小馆", PlaceCategory.FOOD, "黄浦区")
    result = _attach_shared_anchor_evidence(
        [lane, food], "想看看老社区、吃居民日常的小馆。",
    )
    delivered_food = next(place for place in result if place.category == PlaceCategory.FOOD)
    assert any(item.anchor_place == lane.name for item in delivered_food.geo_evidence)


def test_final_shared_anchor_never_names_a_candidate_removed_by_portfolio_clustering():
    query = "三个大学生周末在杨浦逛有文化感的地方，再吃顿人均八十以内的饭。"
    attractions = [
        _place(f"a-{index}", f"文化博物馆{index}", PlaceCategory.ATTRACTION, "杨浦区", 4.9 - index * 0.1).model_copy(
            update={"coords": Coordinates(lng=121.50 + index * 0.01, lat=31.28)},
        )
        for index in range(6)
    ]
    foods = [
        _place(f"f-{index}", f"学生面馆{index}", PlaceCategory.FOOD, "杨浦区", 4.8 - index * 0.1).model_copy(
            update={
                "coords": Coordinates(lng=121.501 + index * 0.005, lat=31.281),
                "amap_price": 40 + index,
                "tags": ["面馆"],
            },
        )
        for index in range(5)
    ]
    delivered = select_eligible_places(
        [*attractions, *foods], query,
        recommendation_plan=build_recommendation_plan(query, "上海"),
    )
    delivered_attractions = {place.name for place in delivered if place.category == PlaceCategory.ATTRACTION}
    anchors = {
        item.anchor_place
        for place in delivered
        for item in place.geo_evidence
        if item.constraint_kind.startswith("shared_anchor")
    }
    assert anchors
    assert anchors <= delivered_attractions


def test_response_exposes_an_actionable_verified_anchor_pair():
    attraction = _place("anchor", "科技馆", PlaceCategory.ATTRACTION, "浦东新区")
    food = _place("food", "科技馆旁餐厅", PlaceCategory.FOOD, "浦东新区").model_copy(
        update={
            "geo_evidence": [GeoEvidence(
                slot_id="slot-food",
                anchor_place="科技馆",
                constraint_kind="route",
                status=EvidenceStatus.VERIFIED,
                satisfies_constraint=True,
                straight_line_distance_km=0.8,
                estimated_travel_minutes=12,
                transport_mode="walking",
                source="amap_walking_route",
            )],
        },
    )
    response = synthesizer._build_demo_response(
        [attraction, food], "上海", {}, "浦东新区", "带孩子玩后附近吃饭，少换乘。",
    )
    assert "落地使用顺序" in response
    assert "先以“科技馆”为主点" in response
    assert "高德步行路线约 12 分钟" in response


def test_response_uses_the_exact_shared_anchor_when_radius_has_no_cutoff():
    anchored = _place("anchored", "自然博物馆", PlaceCategory.ATTRACTION, "拱墅区")
    unbound = _place("unbound", "运河博物馆", PlaceCategory.ATTRACTION, "拱墅区")
    food = _place("food", "博物馆旁餐厅", PlaceCategory.FOOD, "拱墅区").model_copy(
        update={
            "geo_evidence": [GeoEvidence(
                slot_id="shared-delivered-anchor",
                anchor_place="自然博物馆",
                constraint_kind="shared_anchor_proximity",
                status=EvidenceStatus.VERIFIED,
                satisfies_constraint=None,
                straight_line_distance_km=0.4,
                transport_mode="walking",
                source="amap_delivered_poi_coordinates",
            )],
        },
    )
    response = synthesizer._build_demo_response(
        [unbound, anchored, food], "杭州", {}, "拱墅区", "雨天带孩子去室内场馆再附近吃饭。",
    )
    assert "先以“自然博物馆”为主点" in response
    assert "两者直线约 0.4 km" in response


def test_response_does_not_claim_one_district_for_cross_district_results():
    airport = _place("airport", "机场公园", PlaceCategory.ATTRACTION, "顺义区")
    museum = _place("museum", "民航博物馆", PlaceCategory.ATTRACTION, "朝阳区")
    response = synthesizer._build_demo_response(
        [airport, museum], "北京", {}, "顺义区", "机场转机空档去一个景点。",
    )
    assert "筛出的北京候选" in response
    assert "筛出的顺义区候选" not in response


def test_layover_response_exposes_verified_route_time_and_round_trip_budget():
    museum = _place("museum", "民航博物馆", PlaceCategory.ATTRACTION, "朝阳区").model_copy(
        update={"geo_evidence": [GeoEvidence(
            slot_id="layover",
            anchor_place="首都机场",
            constraint_kind="route",
            status=EvidenceStatus.VERIFIED,
            satisfies_constraint=True,
            estimated_travel_minutes=19,
            transport_mode="driving",
            source="amap_driving_route",
        )]},
    )
    response = synthesizer._build_demo_response(
        [museum], "北京", {}, "朝阳区", "首都机场转机空档四小时，想出去看一眼北京。",
    )
    assert "单程驾车约 19 分钟" in response
    assert "往返路上至少按 38 分钟估算" in response
    assert "实时路况倒推最晚返程时间" in response


def test_layover_response_fails_closed_when_route_time_is_missing():
    park = _place("park", "机场附近公园", PlaceCategory.ATTRACTION, "闵行区")
    response = synthesizer._build_demo_response(
        [park], "上海", {}, "闵行区", "虹桥机场转机只有三小时，想出去走走。",
    )
    assert "转机路线证据仍不完整" in response
    assert "核实“虹桥机场”到“机场附近公园”的实时单程和返程时间" in response


def test_hotel_and_negated_photo_requests_do_not_receive_unrelated_route_notices():
    hotel = synthesizer.ensure_dynamic_constraint_notice(
        "住宿候选。", "老人睡眠浅，想住安静酒店，晚上别太吵。",
    )
    history = synthesizer.ensure_dynamic_constraint_notice(
        "建筑候选。", "想看城市历史，不想只去网红拍照点。",
    )
    assert "园区内观光车" not in hotel and "末班车" not in hotel
    assert "日出朝向" not in history


def test_low_transfer_delivery_cap_is_four_per_category():
    assert synthesizer.delivery_per_category_cap("带两位老人，少走路") == 4
    assert synthesizer.delivery_per_category_cap("普通美食推荐") == 5


def test_cuisine_noun_phrase_filters_first_visit_food_candidates():
    query = "情侣第一次去上海三天，给一版景点、本帮餐厅和住宿候选。"
    local = _place("local", "人和馆", PlaceCategory.FOOD, "徐汇区").model_copy(
        update={"tags": ["上海菜", "本帮菜"]}
    )
    unrelated = _place("other", "北京涮肉", PlaceCategory.FOOD, "黄浦区").model_copy(
        update={"tags": ["北京菜"]}
    )
    hotel = _place("hotel", "中心酒店", PlaceCategory.HOTEL, "黄浦区")
    result = select_eligible_places([local, unrelated, hotel], query)
    assert [place.place_id for place in result] == ["local", "hotel"]


def test_west_lake_edge_ranking_prefers_direct_address_evidence():
    query = "学生预算，西湖边想吃片儿川或杭州面，人均六十以内。"
    direct = _place("direct", "家烧面馆", PlaceCategory.FOOD, "西湖区", 4.5).model_copy(
        update={"address": "南山路38号", "amap_price": 36}
    )
    broad = _place("broad", "高分面馆", PlaceCategory.FOOD, "西湖区", 4.9).model_copy(
        update={"address": "转塘街道", "amap_price": 30}
    )
    assert rank_places_for_request([broad, direct], query)[0].place_id == "direct"


def test_west_lake_food_only_decision_uses_visible_edge_address_for_primary():
    query = "学生预算，西湖边想吃片儿川或杭州面，人均六十以内。"
    direct = _place("direct", "家烧面馆", PlaceCategory.FOOD, "西湖区", 4.5).model_copy(
        update={"address": "南山路38号", "amap_price": 36},
    )
    broad = _place("broad", "高分面馆", PlaceCategory.FOOD, "西湖区", 4.9).model_copy(
        update={"address": "莫干山路493号", "amap_price": 30},
    )
    response = synthesizer._build_demo_response([broad, direct], "杭州", {}, "西湖区", query)
    assert "先向“家烧面馆”核实" in response


def test_west_lake_edge_hard_filter_uses_two_direct_address_matches():
    query = "学生预算，西湖边想吃片儿川或杭州面，人均六十以内。"
    direct_a = _place("a", "南山面馆", PlaceCategory.FOOD, "西湖区").model_copy(
        update={"address": "南山路38号", "amap_price": 36, "tags": ["片儿川", "杭州面"]}
    )
    direct_b = _place("b", "湖滨面馆", PlaceCategory.FOOD, "上城区").model_copy(
        update={"address": "龙翔桥地铁口", "amap_price": 30, "tags": ["片儿川", "杭州面"]}
    )
    remote = _place("remote", "郊区面馆", PlaceCategory.FOOD, "萧山区").model_copy(
        update={"amap_price": 20, "tags": ["片儿川", "杭州面"]}
    )
    result = select_eligible_places(
        [remote, direct_a, direct_b], query,
        recommendation_plan=build_recommendation_plan(query, "杭州"),
    )
    assert {place.place_id for place in result} == {"a", "b"}


def test_west_lake_edge_filter_falls_back_to_lake_districts_not_remote_citywide():
    query = "学生预算，西湖边想吃片儿川或杭州面，人均六十以内。"
    west_lake = _place("west", "西湖面馆", PlaceCategory.FOOD, "西湖区").model_copy(
        update={"amap_price": 36, "tags": ["片儿川"]}
    )
    shangcheng = _place("upper", "上城面馆", PlaceCategory.FOOD, "上城区").model_copy(
        update={"amap_price": 30, "tags": ["杭州面"]}
    )
    remote = _place("remote", "萧山面馆", PlaceCategory.FOOD, "萧山区").model_copy(
        update={"amap_price": 20, "tags": ["片儿川", "杭州面"]}
    )
    result = select_eligible_places(
        [remote, west_lake, shangcheng], query,
        recommendation_plan=build_recommendation_plan(query, "杭州"),
    )
    assert {place.place_id for place in result} == {"west", "upper"}


def test_no_remote_suburb_request_keeps_supported_central_city_candidates():
    query = "下午半天想看杭州人文，不去乐园，也不想跑远郊。"
    central_a = _place("a", "杭州博物馆", PlaceCategory.ATTRACTION, "上城区")
    central_b = _place("b", "浙江自然博物院", PlaceCategory.ATTRACTION, "拱墅区")
    remote = _place("remote", "临安博物馆", PlaceCategory.ATTRACTION, "临安区")
    result = select_eligible_places(
        [remote, central_a, central_b], query,
        recommendation_plan=build_recommendation_plan(query, "杭州"),
    )
    assert {place.place_id for place in result} == {"a", "b"}


def test_first_visit_multi_day_ranking_prefers_core_city_districts():
    query = "情侣第一次去上海三天，给一版景点、本帮餐厅和住宿候选。"
    remote = _place("remote", "郊区高分酒店", PlaceCategory.HOTEL, "奉贤区", 4.9)
    central = _place("central", "市区酒店", PlaceCategory.HOTEL, "虹口区", 4.5)
    assert rank_places_for_request([remote, central], query)[0].place_id == "central"


def test_single_category_evidence_does_not_force_category_diversity():
    foods = [_place(str(i), f"餐厅{i}", PlaceCategory.FOOD) for i in range(3)]
    assert _has_sufficient_place_evidence(foods, "闵行区有什么美食") is True


def test_location_anchor_is_not_required_as_a_food_result_card():
    foods = [_place(str(i), f"迪士尼小镇餐厅{i}", PlaceCategory.FOOD, "浦东新区") for i in range(3)]
    assert _has_sufficient_place_evidence(foods, "迪士尼附近有什么吃饭的地方") is True


def test_vegetarian_request_requires_visible_provider_evidence():
    places = [
        _place("v", "一叶一世界藏茶素食", PlaceCategory.FOOD),
        _place("m", "普通烤肉店", PlaceCategory.FOOD),
    ]
    result = filter_places_for_request(places, "闵行区适合素食者的餐厅")
    assert [place.place_id for place in result] == ["v"]


def test_administrative_or_area_names_are_not_visit_destinations():
    assert place_is_human_suitable(_place("d", "闵行区", PlaceCategory.ATTRACTION)) is False
    assert place_is_human_suitable(_place("q", "七宝", PlaceCategory.ATTRACTION)) is False


def test_sse_materialization_removes_preview_rejected_by_synthesizer():
    food = _place("f", "餐厅", PlaceCategory.FOOD).model_dump(mode="json")
    station = _place("t", "七宝地铁站", PlaceCategory.TRANSPORT).model_dump(mode="json")
    result = _materialize_response([
        {"event": "place", "data": {"place": station}},
        {"event": "place", "data": {"place": food}},
        {"event": "place_remove", "data": {"place_id": "t"}},
    ])
    assert [place["place_id"] for place in result["places"]] == ["f"]


def test_deterministic_gate_rejects_transport_in_food_results():
    case = {
        "city": "上海",
        "expected": {
            "allowed_categories": ["food"],
            "required_category_coverage": ["food"],
            "district": "闵行区",
            "min_places": 1,
        },
    }
    output = {
        "places": [
            _place("f", "餐厅", PlaceCategory.FOOD).model_dump(mode="json"),
            _place("t", "七宝地铁站", PlaceCategory.TRANSPORT).model_dump(mode="json"),
        ],
        "errors": [],
    }
    result = deterministic_checks(case, output)
    assert result["passed"] is False
    assert any("意图外品类" in failure for failure in result["failures"])


def test_deterministic_gate_records_honest_missing_category_as_bounded_warning():
    case = {
        "city": "杭州",
        "expected": {
            "allowed_categories": ["attraction", "food"],
            "required_category_coverage": ["attraction", "food"],
            "min_places": 3,
        },
    }
    attraction = _place(
        "lake", "杭州西湖风景名胜区", PlaceCategory.ATTRACTION, "西湖区"
    ).model_dump(mode="json")
    safe_text = (
        "安全降级回执：当前没有通过身份、范围与证据门禁的餐饮候选；"
        "未用错误品类或明显远距离地点凑数。"
    )
    safe = deterministic_checks(case, {
        "places": [attraction], "text": safe_text, "thinking": [], "errors": [],
    })
    assert safe["passed"] is True
    assert safe["missing_required_categories"] == ["food"]
    assert safe["safe_coverage_degradation"] is True
    assert any("缺少必需品类" in warning for warning in safe["coverage_warnings"])

    unsafe = deterministic_checks(case, {
        "places": [attraction], "text": "没有餐饮。", "thinking": [], "errors": [],
    })
    assert unsafe["passed"] is False
    assert any("缺少必需品类" in failure for failure in unsafe["failures"])


def test_room_opening_deterministic_gate_enforces_user_visible_boundaries():
    places = []
    for category in (PlaceCategory.ATTRACTION, PlaceCategory.FOOD, PlaceCategory.HOTEL):
        for index in range(5):
            updates = {"description": f"{category.value}特色描述{index}"}
            if category == PlaceCategory.HOTEL:
                updates["amap_price"] = 300.0 + index * 100
            places.append(
                _place(f"{category.value}-{index}", f"{category.value}地点{index}", category).model_copy(
                    update=updates
                ).model_dump(mode="json")
            )
    case = {
        "city": "北京",
        "expected": {
            "allowed_categories": ["attraction", "food", "hotel"],
            "required_category_coverage": ["attraction", "food", "hotel"],
            "min_places": 9,
            "max_places": 15,
            "min_per_category": 3,
            "max_per_category": 5,
            "unique_names": True,
            "require_descriptions": True,
            "require_district_categories": ["hotel"],
            "require_price_categories": ["hotel"],
            "forbidden_text_keywords": ["没有找到位于大致价位与所在片区"],
            "forbidden_thinking_keywords": ["高德地点搜索暂时不可用"],
        },
    }
    result = deterministic_checks(case, {
        "places": places,
        "text": "已按三类整理初版清单。",
        "thinking": [],
        "errors": [],
    })
    assert result["passed"] is True

    result["failures"].clear()
    broken = deterministic_checks(case, {
        "places": places + [places[0]],
        "text": "没有找到位于大致价位与所在片区且符合条件的地点",
        "thinking": ["高德地点搜索暂时不可用"],
        "errors": [],
    })
    assert broken["passed"] is False
    assert any("超过上限" in failure for failure in broken["failures"])
    assert any("错误降级提示" in failure for failure in broken["failures"])


def test_parallel_search_batch_is_deduplicated_before_synthesis():
    first = _place("same", "迪士尼小镇餐厅", PlaceCategory.FOOD, "浦东新区")
    duplicate = first.model_copy(update={"amap_rating": 4.9})
    other = _place("other", "另一家餐厅", PlaceCategory.FOOD, "浦东新区")
    merged = _merge_unique_places([], [first, duplicate, other])
    assert [place.place_id for place in merged] == ["same", "other"]


def test_critic_reports_exhaustion_instead_of_false_quality_pass():
    result = asyncio.run(critic.run({
        "messages": [HumanMessage(content=ROOM_OPENING_PROMPT)],
        "synthesized_places": [],
        "rag_chunks": [],
        "recommendations": [],
        "working_context": {},
        "critic_iterations": 1,
    }))
    assert result["critic_retry"] is False
    assert result["critic_exhausted"] is True
    assert "未达标" in result["critic_reason"]


def test_synthesizer_reserves_deadline_for_grounded_fallback():
    place = _place("fallback", "受控景点", PlaceCategory.ATTRACTION, "东城区")

    class SlowLLM:
        async def ainvoke(self, _messages):
            await asyncio.sleep(10)

    state = {
        "messages": [HumanMessage(content="北京景点推荐")],
        "amap_places": [place],
        "rag_chunks": [],
        "trip_city": "北京",
        "trip_district": None,
        "working_context": {},
        "deadline_monotonic": time.monotonic() + 0.2,
    }
    with patch.object(synthesizer.settings, "demo_mode", False), patch.object(
        synthesizer.settings, "amap_mock", False
    ), patch.object(synthesizer, "_get_llm", return_value=SlowLLM()):
        started = time.monotonic()
        result = asyncio.run(synthesizer.run(state))

    assert time.monotonic() - started < 1
    assert result["synthesized_places"][0].place_id == "fallback"
    assert "东城区" in result["synthesized_places"][0].description
    assert result["final_response"]


def test_sse_does_not_repeat_cumulative_failures_or_claim_false_pass():
    from app.api import chat as chat_api

    failure = {"tool": "search_places", "reason": "invalid_payload"}

    class FakeGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_chain_end",
                "name": "tool_executor",
                "data": {"output": {"tool_failures": [failure]}},
            }
            yield {
                "event": "on_chain_end",
                "name": "tool_executor",
                "data": {"output": {"tool_failures": [failure]}},
            }
            yield {
                "event": "on_chain_end",
                "name": "critic",
                "data": {"output": {
                    "critic_retry": False,
                    "critic_exhausted": True,
                    "critic_reason": "结果仍未达标，已达自动重试上限",
                }},
            }

    async def collect():
        request = ChatRequest(
            thread_id="boundary-thread",
            user_id="anonymous",
            message=ROOM_OPENING_PROMPT,
            trip_city="北京",
        )
        http_request = AsyncMock()
        http_request.is_disconnected = AsyncMock(return_value=False)
        with patch.object(chat_api, "get_graph_with_persistence", AsyncMock(return_value=FakeGraph())):
            return [chunk async for chunk in chat_api._event_stream(request, "trace-boundary", http_request)]

    events = []
    for chunk in asyncio.run(collect()):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    summaries = [event["data"]["summary"] for event in events if event.get("event") == "thinking"]
    assert sum("高德地点搜索暂时不可用" in summary for summary in summaries) == 1
    assert any("质量仍未达标" in summary for summary in summaries)
    assert "质量检查通过" not in summaries


def test_sse_deadline_after_poi_search_returns_degraded_cards_not_error():
    from app.api import chat as chat_api

    place = _place("deadline-poi", "超时保留景点", PlaceCategory.ATTRACTION, "东城区")

    class TimeoutAfterToolsGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_chain_end",
                "name": "tool_executor",
                "data": {"output": {"amap_places": [place], "tool_failures": []}},
            }
            raise TimeoutError("controlled graph deadline")

    async def collect():
        request = ChatRequest(
            thread_id="deadline-thread",
            user_id="anonymous",
            message="北京景点推荐",
            trip_city="北京",
        )
        http_request = AsyncMock()
        http_request.is_disconnected = AsyncMock(return_value=False)
        with patch.object(
            chat_api,
            "get_graph_with_persistence",
            AsyncMock(return_value=TimeoutAfterToolsGraph()),
        ):
            return [chunk async for chunk in chat_api._event_stream(request, "trace-timeout", http_request)]

    events = []
    for chunk in asyncio.run(collect()):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

    assert not [event for event in events if event.get("event") == "error"]
    updates = [event for event in events if event.get("event") == "place_update"]
    assert updates and "东城区" in updates[0]["data"]["fields"]["description"]
    done = next(event for event in events if event.get("event") == "done")
    assert done["data"]["total_places"] == 1
    assert done["data"]["degraded"] is True
