"""Synthetic regressions for verbose guides, not retained owner documents."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.experience_inference import SourceAnchorIndex
from app.trip_understanding.pipeline import _model_activity_cities, atomic_place_rejection_reason
from app.trip_understanding.pipeline import EvidenceCompiler


def draft(source, name, *, destination="北京", city="北京", evidence=None):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": destination, "activities": [{
            "source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1,
            "city": city, "city_evidence": evidence,
        }],
    }))


def test_unselected_branch_places_never_become_a_default_main_route():
    source = "Day1：自由活动。\nDay2：二选一\n### 方案 A：城中\n云岭公园。\n### 方案 B：郊区\n青溪古镇。\nDay3：星河湖。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "目的地待确认", "activities": [
            {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day}
            for name, day in [("云岭公园", 2), ("青溪古镇", 2), ("星河湖", 3)]
        ],
    }))
    assert [(m.atomic_place_name, m.role.value, m.day_index) for m in proposal.mentions] == [
        ("云岭公园", "OPTIONAL", 2), ("青溪古镇", "OPTIONAL", 2), ("星河湖", "PLANNED", 3),
    ]
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [m.eligible_for_place_search for m in compiled] == [False, False, True]


def test_explicit_replacement_bold_place_cannot_be_silently_omitted():
    source = "Day1：云岭公园。\n> 如果下雨，替换方案：**星河博物馆**。"
    with pytest.raises(ValueError, match="MISSING_EXPLICIT_OPTIONAL_PLACE"):
        draft(source, "云岭公园", city=None)


def test_next_day_main_visit_keeps_its_own_occurrence_after_a_prior_day_alternative():
    source = "Day1：市区\n> 如果下雨，替换方案：**青溪公园**。\nDay2：返程\n上午青溪公园。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "目的地待确认", "activities": [
            {"source_quote": "青溪公园", "place_name": "青溪公园", "role": "PLANNED", "day_index": day}
            for day in (1, 2)
        ],
    }))
    assert [(m.day_index, m.role.value) for m in proposal.mentions] == [(1, "OPTIONAL"), (2, "PLANNED")]
    assert proposal.mentions[1].span_start == source.rindex("青溪公园")


@pytest.mark.parametrize("literal", ["顺着云岭路慢慢走", "星河坊简单逛一圈即可", "傍晚青溪滩看落日"])
def test_explicit_visit_instruction_cannot_disappear_from_a_draft(literal):
    source = f"Day1：城市\n先去青溪公园，{literal}。"
    with pytest.raises(ValueError, match="MISSING_EXPLICIT_VISIT_PLACE"):
        draft(source, "青溪公园", city=None)


def test_brief_visit_is_main_but_the_same_syntax_in_an_unselected_branch_stays_optional():
    source = "Day1：城市\n星河坊简单逛一圈即可。\nDay2：二选一\n### 方案A：\n傍晚青溪滩看落日。\n### 方案B：\n云岭公园。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "目的地待确认", "activities": [
            {"source_quote": name, "place_name": name, "role": role, "day_index": day}
            for name, role, day in [("星河坊", "OPTIONAL", 1), ("青溪滩", "REFERENCE", 2), ("云岭公园", "PLANNED", 2)]
        ],
    }))
    assert [(m.atomic_place_name, m.role.value) for m in proposal.mentions] == [
        ("星河坊", "PLANNED"), ("青溪滩", "OPTIONAL"), ("云岭公园", "OPTIONAL"),
    ]


def test_ambiguous_repeat_cannot_borrow_a_different_days_source_span():
    source = "Day1：市区\n青溪公园。\nDay2：返程\n上午青溪公园，晚上再回青溪公园。"
    with pytest.raises(ValueError, match="SOURCE_DAY_QUOTE_MISMATCH"):
        proposal_from_draft(source, SemanticDraft.model_validate({
            "destination": "目的地待确认", "activities": [{
                "source_quote": "青溪公园", "place_name": "青溪公园", "role": "PLANNED", "day_index": 2,
            }],
        }))


@pytest.mark.parametrize("label", ["星河街", "青溪路", "云岭胡同", "星河寺周边"])
def test_meal_area_is_not_turned_into_a_restaurant(label):
    source = f"Day1：中午在{label}吃饭。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "目的地待确认", "activities": [{
            "source_quote": label, "place_name": label, "role": "PLANNED", "category": "餐饮", "day_index": 1,
        }],
    }))
    assert proposal.mentions[0].category_hint == "地点"
    assert proposal.mentions[0].atomic_place_name == label


@pytest.mark.parametrize("suffix", ["摩天轮", "露台", "周边"])
def test_an_attached_subvenue_or_surroundings_cannot_be_silently_removed(suffix):
    source = f"Day1：云岭中心{suffix}。"
    proposal = draft(source, "云岭中心", city=None)
    assert proposal.mentions[0].atomic_place_name == "云岭中心" + suffix
    assert source[proposal.mentions[0].span_start:proposal.mentions[0].span_end] == "云岭中心" + suffix


def test_fare_is_not_a_named_transport_facility():
    assert atomic_place_rejection_reason("3元轮渡") == "FARE_DESCRIPTION"
    assert atomic_place_rejection_reason("青溪轮渡码头") is None


@pytest.mark.parametrize("literal,quote", [
    ("云汀书院（星城中心 62 楼，咖啡看城市全景）", "云汀书院（星城中心 62 楼）"),
    ("M28 创意园", "M28创意园"),
    ("江城博物馆（人民广场馆，免费）", "江城博物馆（人民广场馆）"),
])
def test_unique_closed_label_normalization_retains_the_full_original_anchor(literal, quote):
    source = f"Day1：{literal}。"
    proposal = draft(source, quote, city=None)
    mention = proposal.mentions[0]
    assert mention.atomic_place_name is not None
    assert source[mention.span_start:mention.span_end] == literal
    assert EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search


@pytest.mark.parametrize("source,quote", [
    ("云汀书院（星城中心62楼，另去其他店）", "云汀书院（星城中心62楼）"),
    ("云汀书院（星城中心63楼，咖啡看城市全景）", "云汀书院（星城中心62楼）"),
    ("江 城博物馆", "江城博物馆"),
    ("M28 创意园，另一站M28 创意园", "M28创意园"),
    ("AM28 创意园", "M28创意园"),
    ("M128 创意园", "28创意园"),
])
def test_source_normalization_refuses_different_identity_prose_and_ambiguous_occurrences(source, quote):
    with pytest.raises(ValueError, match="SOURCE_QUOTE_NOT_FOUND"):
        SourceAnchorIndex(source).locate(quote)


def test_unbolded_bridge_list_before_caption_is_not_allowed_to_lose_its_second_stop():
    source = "Day1：云岭公园，走到青溪桥、星河桥（欣赏夜景）。"
    with pytest.raises(ValueError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        draft(source, "云岭公园", city=None)


@pytest.mark.parametrize("evidence", [None, "", "  "])
def test_missing_city_evidence_does_not_poison_a_safe_single_city_assumption(evidence):
    source = "Day1：先到颐和园。"
    proposal = draft(source, "颐和园", evidence=evidence)
    mention = proposal.mentions[0]
    assert mention.city_hint is None and mention.city_evidence is None
    assert _model_activity_cities(source, proposal, mention) == ("北京",)
    assert proposal.unprocessed_count == 1


@pytest.mark.parametrize("source,name", [
    ("Day1：上海星河博物馆。", "上海星河博物馆"),
    ("Day1：云岭公园，晚上吃老上海风味菜。", "云岭公园"),
])
def test_city_tokens_in_a_name_or_food_style_are_still_a_soft_destination(source, name):
    proposal = draft(source, name, destination="上海", city=None)
    assert proposal.destination_basis.value == "SOFT_ASSUMPTION"


@pytest.mark.parametrize("source,name,destination,city,evidence", [
    ("广州行程：北京路步行街。", "北京路步行街", "北京", "北京", None),
    ("北京路步行街。", "北京路步行街", "北京", "北京", None),
    ("第1天北京；第2天上海：外滩。", "外滩", "北京", "北京", None),
    ("北京行程：颐和园。", "颐和园", "北京", "上海", "上海行程"),
])
def test_clearing_absent_city_evidence_cannot_bypass_conflicting_or_embedded_city(source, name, destination, city, evidence):
    proposal = draft(source, name, destination=destination, city=city, evidence=evidence)
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)


@pytest.mark.parametrize("instruction", ["索道上、滑道下", "缆车上、缆车下", "索道上山、索道下山"])
def test_bold_transport_instructions_do_not_demand_nonexistent_place_cards(instruction):
    source = f"Day1：游览云岭公园。省力方式：**{instruction}**，园内游玩。"
    proposal = draft(source, "云岭公园", city=None)
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["云岭公园"]
    assert all(atomic_place_rejection_reason(part) == "TRANSPORT_ACTION" for part in instruction.split("、"))


@pytest.mark.parametrize("name", ["云岭索道站", "云岭缆车站", "云岭滑道售票处"])
def test_named_transport_facilities_are_not_rejected_as_directional_actions(name):
    assert atomic_place_rejection_reason(name) is None


@pytest.mark.parametrize("model_name", ["江城博物馆（人民广场馆，免费）", "江城博物馆（人民广场馆）"])
def test_campus_and_source_span_survive_parenthetical_fee_cleanup(model_name):
    literal = "江城博物馆（人民广场馆，免费）"
    source = f"Day1：{literal}。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "上海", "activities": [{"source_quote": literal, "place_name": model_name,
            "role": "PLANNED", "day_index": 1}],
    }))
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == "江城博物馆（人民广场馆）"
    assert mention.raw_text == source[mention.span_start:mention.span_end] == literal
    compiled, claims, _ = EvidenceCompiler().compile(source, proposal)
    assert compiled[0].eligible_for_place_search
    assert claims[0].quote == literal


def test_campus_with_fee_cannot_silently_shrink_to_parent_name():
    with pytest.raises(ValueError, match="PLACE_QUALIFIER_OMITTED"):
        draft("Day1：江城博物馆（人民广场馆，免费）。", "江城博物馆", city=None)


def test_unknown_annotation_cannot_authorize_a_normalized_unverbatim_name():
    literal = "江城博物馆（南馆，改到北馆）"
    with pytest.raises(ValueError, match="PLACE_NOT_IN_SOURCE_QUOTE"):
        proposal_from_draft(literal, SemanticDraft.model_validate({
            "destination": "上海", "activities": [{"source_quote": literal, "place_name": "江城博物馆（南馆）",
                "role": "PLANNED", "day_index": 1}],
        }))


@pytest.mark.parametrize("evidence", ["某公园说明", "老北京风味", "并不存在的城市标题"])
def test_invalid_repeat_of_soft_destination_is_not_hard_city_evidence(evidence):
    source = "Day1：颐和园，里面有苏州街，晚上尝老北京风味。"
    proposal = draft(source, "颐和园", evidence=evidence)
    assert proposal.mentions[0].city_hint is None
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("北京",)


def test_discarded_evidence_still_cannot_resolve_unassigned_multiple_cities():
    source = "第1天北京颐和园，第2天上海外滩。"
    proposal = draft(source, "颐和园", evidence="错误标题")
    assert _model_activity_cities(source, proposal, proposal.mentions[0]) == ("目的地待确认",)


def test_bold_prose_and_later_shopping_are_not_parallel_places_or_attached_branches():
    source = "Day1：星河创意园逛艺术涂鸦小店。**不用一定要登塔，江边散步就很好**。"
    proposal = draft(source, "星河创意园", city=None)
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["星河创意园"]


def test_explicit_bundled_names_expand_in_order_without_losing_a_real_revisit():
    source = "Day1：星河公园。Day2：星河公园 / 枫林步行街。"
    proposal = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": [
        {"place_name": "星河公园", "source_quote": "星河公园", "role": "PLANNED", "day_index": 1},
        {"place_name": "星河公园 / 枫林步行街", "source_quote": "星河公园 / 枫林步行街", "role": "PLANNED", "day_index": 2},
    ]}))
    assert [(m.day_index, m.atomic_place_name) for m in proposal.mentions] == [(1,"星河公园"),(2,"星河公园"),(2,"枫林步行街")]
    assert proposal.mentions[1].span_start > proposal.mentions[0].span_end


def test_unknown_parenthetical_description_does_not_become_a_fake_multiple_place_error():
    literal = "云汀书院（星城中心52楼，看看城市全景）"
    proposal = draft(literal, literal, city=None)
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].atomic_place_name is None
    assert not EvidenceCompiler().compile(literal, proposal)[0][0].eligible_for_place_search


@pytest.mark.parametrize("literal,expected", [("M28 创意园","M28创意园"),("ABC 湖畔金融中心露台","ABC湖畔金融中心露台"),("城市中心 108 层","城市中心108层"),("星河路逛街","星河路")])
def test_literal_mixed_labels_remain_searchable_without_their_original_spelling_being_lost(literal, expected):
    proposal = draft(literal, literal, city=None)
    assert proposal.mentions[0].atomic_place_name == expected
    assert proposal.mentions[0].raw_text == literal
    assert EvidenceCompiler().compile(literal, proposal)[0][0].eligible_for_place_search
