"""An omitted source-explicit branch revisit is never a default selected stop."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    _retain_explicit_branch_revisits,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler


SOURCE = (
    "Day1：澄湖公园。\n"
    "Day2：城中 / 海岸二选一\n"
    "### 方案 A：江边\n青溪滩滨江。云岭路。\n"
    "### 方案 B：街区\n星河坊简单逛一圈即可。\n傍晚青溪滩看落日。"
)


def activity(name, day=2, role="OPTIONAL", occurrence=1):
    return {"source_quote": name, "place_name": name, "day_index": day, "role": role, "occurrence": occurrence}


def rows():
    return [activity("澄湖公园", 1, "PLANNED"), activity("青溪滩滨江"), activity("云岭路"), activity("星河坊")]


def draft(items=None):
    return SemanticDraft.model_validate({"activities": rows() if items is None else items})


def assert_unchanged(source, items=None):
    original = draft(items)
    recovered = _retain_explicit_branch_revisits(source, original)
    assert recovered.model_dump() == original.model_dump()


def test_missing_abbreviated_tail_visit_is_retained_as_one_unconfirmed_same_day_option():
    original = draft()
    recovered = _retain_explicit_branch_revisits(SOURCE, original)
    assert recovered.activities[:-1] == original.activities
    assert len(recovered.activities) == len(original.activities) + 1
    added = recovered.activities[-1]
    assert (added.place_name, added.source_quote, added.occurrence, added.role.value, added.day_index) == (
        "青溪滩", "青溪滩", 2, "OPTIONAL", 2,
    )
    assert added.city is None and added.city_evidence is None
    assert added.start_time is None and added.end_time is None and added.visit_duration_minutes is None
    assert added.time_evidence is None
    assert added.locked is False and added.fixed_commitment is False
    assert _retain_explicit_branch_revisits(SOURCE, recovered).model_dump() == recovered.model_dump()

    proposal = proposal_from_draft(SOURCE, original)
    assert [item.atomic_place_name for item in proposal.mentions] == [
        "澄湖公园", "青溪滩滨江", "云岭路", "星河坊", "青溪滩",
    ]
    assert [item.day_index for item in proposal.mentions] == [1, 2, 2, 2, 2]
    assert [item.role.value for item in proposal.mentions] == ["PLANNED", "OPTIONAL", "OPTIONAL", "OPTIONAL", "OPTIONAL"]
    added_mention = proposal.mentions[-1]
    assert added_mention.span_start == SOURCE.rindex("青溪滩")
    assert SOURCE[added_mention.span_start:added_mention.span_end] == added_mention.raw_text == "青溪滩"
    assert added_mention.city_hint is None
    assert not EvidenceCompiler().compile(SOURCE, proposal)[0][-1].eligible_for_place_search


def test_explicit_optional_short_label_can_recur_after_another_branchs_qualified_label():
    source = SOURCE.replace("青溪滩滨江", "星河咖啡（云岭店）").replace(
        "傍晚青溪滩看落日。", "星河咖啡可以打卡特调。",
    )
    items = rows()
    items[1] = activity("星河咖啡（云岭店）")
    proposal = proposal_from_draft(source, draft(items))
    assert len(proposal.mentions) == len(items) + 1
    added = proposal.mentions[-1]
    assert added.atomic_place_name == added.raw_text == "星河咖啡"
    assert added.role.value == "OPTIONAL" and added.day_index == 2
    assert added.span_start == source.rindex("星河咖啡")
    assert not EvidenceCompiler().compile(source, proposal)[0][-1].eligible_for_place_search


@pytest.mark.parametrize("correction", [
    "取消这次海岸安排。",
    "方案B改期到另一天。",
    "最终选定方案A。",
    "本次已选方案A，方案B不执行。",
    "已选定方案B。",
    "末日提前一天。",
    "最后将方案B按逆序执行。",
    "两套方案先后顺序需要对调。",
])
def test_cancelled_settled_rescheduled_or_reversed_source_cannot_add_a_revisit(correction):
    source = SOURCE.replace("### 方案 A", correction + "\n### 方案 A")
    assert_unchanged(source)


@pytest.mark.parametrize("tail", [
    "引用：傍晚青溪滩看落日。",
    "> 傍晚青溪滩看落日。",
    "作者说：“傍晚青溪滩看落日。”",
    "傍晚青溪滩看落日的照片挂在墙上。",
    "不去傍晚青溪滩看落日。",
    "https://example.test/傍晚青溪滩看落日",
])
def test_quotation_negation_and_descriptive_action_are_not_a_missing_visit(tail):
    assert_unchanged(SOURCE.replace("傍晚青溪滩看落日。", tail))


@pytest.mark.parametrize("mutation", ["no_choice", "one_branch", "another_day", "no_existing_branch_stop", "reverse_draft"])
def test_missing_choice_structure_day_or_source_order_never_authorizes_an_addition(mutation):
    source, items = SOURCE, rows()
    if mutation == "no_choice":
        source = source.replace("城中 / 海岸二选一", "城中和海岸确定安排")
    elif mutation == "one_branch":
        source = source.replace("### 方案 B：街区", "### 街区")
    elif mutation == "another_day":
        source = source.replace("傍晚青溪滩看落日。", "Day3：\n傍晚青溪滩看落日。")
    elif mutation == "no_existing_branch_stop":
        items.pop()
    elif mutation == "reverse_draft":
        items[1], items[2] = items[2], items[1]
    assert_unchanged(source, items)


@pytest.mark.parametrize("role", ["OPTIONAL", "PLANNED", "REFERENCE", "EXCLUDED"])
def test_an_existing_short_name_is_never_duplicated_to_hide_a_wrong_occurrence(role):
    items = rows()
    items.insert(2, activity("青溪滩", role=role, occurrence=1))
    assert_unchanged(SOURCE, items)


def test_full_same_name_in_another_branch_does_not_authorize_an_extra_occurrence():
    source = SOURCE.replace("青溪滩滨江", "青溪滩")
    items = rows()
    items[1] = activity("青溪滩")
    assert_unchanged(source, items)


def test_a_claimed_tail_quote_cannot_generate_another_activity():
    items = rows() + [{"source_quote": "傍晚青溪滩看落日", "place_name": None, "day_index": 2, "role": "REFERENCE"}]
    assert_unchanged(SOURCE, items)


def test_other_branch_must_already_supply_a_literal_full_name():
    source = SOURCE.replace("青溪滩滨江", "松岚公园")
    items = rows()
    items[1] = activity("松岚公园")
    assert_unchanged(source, items)
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(source, draft(items))
    assert "MISSING_EXPLICIT_VISIT_PLACE" in {issue["category"] for issue in error.value.issues}
