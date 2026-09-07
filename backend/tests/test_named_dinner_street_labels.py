"""An explicit dinner street survives a model's unnamed meal draft."""
import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.guide_choices import explicit_visit_labels


def _proposal(source, quote, *, day=1, role="PLANNED", more=()):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "上海", "activities": [
            {"source_quote": quote, "place_name": None, "role": role,
             "day_index": day, "category": "餐饮"}, *more,
        ],
    }))


@pytest.mark.parametrize("action", ["吃本帮菜", "吃粤菜", "吃面", "吃咖啡简餐", "吃当地特色", "吃点家常菜"])
@pytest.mark.parametrize("layout", ["晚上：青禾路{action}，推荐春晓食堂、青叶饭店。",
                                    "  - 晚上：**青禾路**{action}，推荐春晓食堂、青叶饭店。\r\n"])
def test_explicit_dinner_street_is_recovered_without_turning_recommendations_into_stops(action, layout):
    quote = layout.format(action=action)
    source = "Day1：城区闲逛\r\n" + quote
    spans = explicit_visit_labels(source)
    assert [source[start:end] for start, end in spans] == ["青禾路"]
    proposal = _proposal(source, quote.strip())
    assert len(proposal.mentions) == 1
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == mention.raw_text == "青禾路"
    assert mention.category_hint == "地点" and mention.role.value == "PLANNED"
    assert mention.day_index == 1
    assert source[mention.span_start:mention.span_end] == "青禾路"


@pytest.mark.parametrize("line", [
    "晚上：青禾路附近吃本帮菜。", "晚上：青禾路周边吃粤菜。",
    "晚上：青禾路 / 星雨街吃本帮菜。", "晚上：青禾路吃亏。",
    "晚上：青禾路吃惊后回去。", "晚上：青禾路吃闭门羹。",
    "晚上：青禾路吃苦耐劳。", "晚上：青禾路吃饱了回家。", "晚上：青禾路吃饭后不去。",
    "晚上：青禾路吃不惯当地菜。", "晚上：青禾路吃了苦头。",
    "晚上：青禾路吃力地走完。", "晚上：青禾路吃饭，后来取消了。",
    "晚上：青禾路不吃本帮菜。", "晚上：不去青禾路吃本帮菜。",
    "晚上：如果有空，青禾路吃本帮菜。", "如果天气好，晚上：青禾路吃本帮菜。",
    "晚上：青禾路吃本帮菜（可选）。", "晚上：青禾路吃本帮菜，改天再去。",
    "> 晚上：青禾路吃本帮菜。", "引用：\n晚上：青禾路吃本帮菜。",
    "```text\n晚上：青禾路吃本帮菜。\n```", "“晚上：青禾路吃本帮菜。”",
    "晚上：推荐青禾路吃本帮菜。", "晚上：附近街吃本帮菜。",
    "晚上：青禾餐厅吃本帮菜。", "晚上：青禾路吃完饭再决定去哪儿。",
])
def test_dinner_labels_do_not_promote_areas_references_conditions_negations_or_eating_figures_of_speech(line):
    assert explicit_visit_labels("Day1\n" + line) == []


def test_a_named_dinner_in_an_unselected_day_branch_stays_optional():
    source = "Day1：二选一\n方案 A：城区\n晚上：青禾路吃本帮菜。\n方案 B：郊外\n星雨公园。"
    proposal = _proposal(source, "青禾路吃本帮菜", role="OPTIONAL")
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].atomic_place_name == "青禾路"
    assert proposal.mentions[0].role.value == "OPTIONAL"


def test_an_existing_named_street_does_not_gain_a_duplicate_dinner_stop():
    source = "Day1\n晚上：青禾路吃本帮菜，推荐春晓食堂。"
    named = {"source_quote": "青禾路", "place_name": "青禾路", "role": "PLANNED", "day_index": 1}
    proposal = _proposal(source, "青禾路吃本帮菜", more=[named])
    assert [mention.atomic_place_name for mention in proposal.mentions] == [None, "青禾路"]


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
@pytest.mark.parametrize("label", ["中午", "午餐", "晚餐", "晚上"])
def test_a_bare_meal_clause_keeps_original_offsets_for_both_line_endings(line_ending, label):
    source = f"Day1{line_ending}  - {label}：青禾路吃粤菜{line_ending}"
    proposal = _proposal(source, "青禾路吃粤菜")
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["青禾路"]
    mention = proposal.mentions[0]
    assert source[mention.span_start:mention.span_end] == "青禾路"


@pytest.mark.parametrize("clause", [
    "吃饭，计划已撤销。", "吃饭的计划已作废。", "吃饭，若有空再去。", "吃饭，改到明天。",
    "吃粤菜，有空才去。", "吃面，假如有时间才安排。", "吃本帮菜，延期到后天。",
])
def test_a_retracted_conditional_or_postponed_dinner_does_not_gain_a_named_main_stop(clause):
    source = "Day1\n晚上：青禾路" + clause
    assert explicit_visit_labels(source) == []
    proposal = _proposal(source, "青禾路" + clause)
    assert len(proposal.mentions) == 1
    assert proposal.mentions[0].atomic_place_name is None
