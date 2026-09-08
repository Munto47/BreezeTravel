"""Bare city/duration headings must scope visits without borrowing other cities."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.models import ActivityRole, DestinationBasis
from app.trip_understanding.pipeline import _model_activity_cities, source_destination_cities


def propose(source, city, evidence, names):
    draft = SemanticDraft.model_validate({
        "destination": city,
        "activities": [
            {"source_quote": name, "place_name": name, "role": "PLANNED",
             "category": "景点", "day_index": 1, "city": city, "city_evidence": evidence}
            for name in names
        ],
    })
    return proposal_from_draft(source, draft)


@pytest.mark.parametrize("city,heading,names", [
    ("上海", "上海一天。", ["上海博物馆东馆", "上海虹桥站"]),
    ("杭州", "杭州一日。", ["灵隐寺", "曲院风荷"]),
    ("广州", "广州两天。", ["陈家祠", "越秀公园"]),
    ("上海", "上海 1 天：", ["外滩", "豫园"]),
    ("杭州", "杭州市二日一晚\n", ["西湖", "灵隐寺"]),
])
@pytest.mark.parametrize("quote_city_only", [False, True])
def test_bare_city_duration_heading_keeps_source_bound_city(city, heading, names, quote_city_only):
    source = heading + "上午去" + names[0] + "，下午去" + names[1] + "。"
    evidence = city if quote_city_only else heading.strip()
    proposal = propose(source, city, evidence, names)
    assert source_destination_cities(source) == (city,)
    assert proposal.destination_basis == DestinationBasis.EXPLICIT
    assert proposal.unprocessed_count == 0
    assert [item.city_hint for item in proposal.mentions] == [city, city]
    assert [_model_activity_cities(source, proposal, item) for item in proposal.mentions] == [(city,), (city,)]


def test_city_repair_does_not_claim_to_correct_the_models_conditional_role_error():
    source = ("上海一天。上午去上海博物馆东馆，下午去外滩散步，晚上到上海虹桥站返程。"
              "如果下午太累，就改去南京东路，不去外滩。上海中心这次取消，不去了。")
    draft = SemanticDraft.model_validate({"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": role, "day_index": 1,
         "category": category, "city": "上海", "city_evidence": "上海一天"}
        for name, role, category in [
            ("上海博物馆东馆", "PLANNED", "景点"), ("外滩", "OPTIONAL", "景点"),
            ("南京东路", "OPTIONAL", "地点"), ("上海虹桥站", "PLANNED", "交通节点"),
            ("上海中心", "EXCLUDED", "景点"),
        ]
    ]})
    proposal = proposal_from_draft(source, draft)
    planned = [item for item in proposal.mentions if item.role == ActivityRole.PLANNED]
    assert [item.atomic_place_name for item in planned] == ["上海博物馆东馆", "上海虹桥站"]
    assert [_model_activity_cities(source, proposal, item) for item in planned] == [("上海",), ("上海",)]
    assert next(item for item in proposal.mentions if item.atomic_place_name == "外滩").role == ActivityRole.OPTIONAL
    assert not any(item.category == "UNSUPPORTED_CITY_REMOVED" for item in proposal.diagnostics)


@pytest.mark.parametrize("source,evidence", [
    ("上海一天太短，只是预算比较。上午去测试园。", "上海一天"),
    ("上海一天的消费只是参考。上午去测试园。", "上海一天"),
    ("参考上海一天。上午去测试园。", "上海一天"),
    ("如果去上海一天。上午去测试园。", "上海一天"),
    ("去年在上海一天。上午去测试园。", "上海一天"),
    ("杭州一日。说明牌写着上海一天。上午去测试园。", "上海一天"),
])
def test_city_in_comparison_condition_history_or_body_is_not_a_trip_heading(source, evidence):
    proposal = propose(source, "上海", evidence, ["测试园"])
    assert "上海" not in source_destination_cities(source)
    assert proposal.mentions[0].city_hint is None
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)


@pytest.mark.parametrize("source", [
    "上海一天。下午到杭州，去西湖。",
    "上海一天。上海这次取消，改去杭州游览西湖。",
    "上海一天。比较杭州的路线后决定去杭州游览西湖。",
])
def test_single_city_heading_cannot_assign_a_later_citys_visit(source):
    proposal = propose(source, "上海", "上海一天", ["西湖"])
    assert proposal.mentions[0].city_hint is None
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)


def test_invented_heading_evidence_cannot_borrow_the_real_heading():
    source = "上海一天。上午去外滩。"
    proposal = propose(source, "上海", "上海一日", ["外滩"])
    assert proposal.mentions[0].city_hint is None
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)
