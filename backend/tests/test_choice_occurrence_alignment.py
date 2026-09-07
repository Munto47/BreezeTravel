"""A repeated short label may move only within a wholly unselected choice."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)


SOURCE = (
    "Day1：澄湖公园。\n"
    "Day2：枫林寺。\n"
    "Day3：城内 / 海边二选一\n"
    "### 方案 A：江边\n青溪滩滨江。\n"
    "### 方案 B：街区\n星河坊简单逛一圈即可。傍晚青溪滩看落日。"
)


def activity(name, day, role="OPTIONAL", occurrence=1):
    return {"source_quote": name, "place_name": name, "role": role,
            "day_index": day, "occurrence": occurrence}


def activities():
    return [
        activity("澄湖公园", 1, "PLANNED"),
        activity("枫林寺", 2, "PLANNED"),
        activity("青溪滩滨江", 3),
        activity("青溪滩", 3),
        activity("星河坊", 3),
    ]


def propose(source=SOURCE, rows=None):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "上海", "activities": activities() if rows is None else rows,
    }))


def names(proposal):
    return [mention.atomic_place_name for mention in proposal.mentions]


def assert_missing_visit(source, rows=None):
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(source, rows)
    assert "MISSING_EXPLICIT_VISIT_PLACE" in {issue["category"] for issue in error.value.issues}


def test_unique_shadowed_short_name_moves_to_its_visit_and_only_that_group_is_sorted():
    proposal = propose()
    assert names(proposal) == ["澄湖公园", "枫林寺", "青溪滩滨江", "星河坊", "青溪滩"]
    assert len(proposal.mentions) == len(activities())
    assert [mention.day_index for mention in proposal.mentions] == [1, 2, 3, 3, 3]
    assert [mention.role.value for mention in proposal.mentions] == ["PLANNED", "PLANNED", "OPTIONAL", "OPTIONAL", "OPTIONAL"]
    short = proposal.mentions[-1]
    assert short.span_start == SOURCE.rindex("青溪滩")
    assert SOURCE[short.span_start:short.span_end] == short.raw_text == "青溪滩"
    assert [mention.sequence_index for mention in proposal.mentions[2:]] == [0, 1, 2]


def test_correct_occurrence_does_not_authorize_unsolicited_sorting():
    rows = activities()
    rows[3]["occurrence"] = 2
    proposal = propose(rows=rows)
    assert names(proposal) == ["澄湖公园", "枫林寺", "青溪滩滨江", "青溪滩", "星河坊"]
    assert proposal.mentions[3].span_start == SOURCE.rindex("青溪滩")


def test_ordinary_non_choice_day_keeps_model_order_when_anchors_are_already_valid():
    source = SOURCE.replace("城内 / 海边二选一", "当天确定安排")
    rows = activities()
    rows[3]["occurrence"] = 2
    proposal = propose(source, rows)
    assert names(proposal) == ["澄湖公园", "枫林寺", "青溪滩滨江", "青溪滩", "星河坊"]
    assert proposal.mentions[3].span_start == source.rindex("青溪滩")


def test_ordinary_non_choice_day_cannot_relocate_an_invalid_short_anchor():
    assert_missing_visit(SOURCE.replace("城内 / 海边二选一", "当天确定安排"))


@pytest.mark.parametrize("correction", [
    "更正：两套方案还需要调整。",
    "方案B改期到下一天。",
    "最后把方案B按逆序执行。",
])
def test_changed_or_reversed_plans_are_not_reanchored_or_sorted(correction):
    assert_missing_visit(SOURCE + "\n" + correction)


def test_more_than_two_literal_occurrences_cannot_choose_one_actual_revisit():
    source = SOURCE + "当天还要再到青溪滩一次。"
    assert source.count("青溪滩") == 3
    assert_missing_visit(source)


def test_a_second_long_name_prefix_is_not_an_independent_short_visit():
    source = SOURCE.replace("傍晚青溪滩看落日", "参观青溪滩观景台")
    proposal = propose(source)
    assert names(proposal) == ["澄湖公园", "枫林寺", "青溪滩滨江", "青溪滩", "星河坊"]
    assert proposal.mentions[3].span_start == source.index("青溪滩滨江")


@pytest.mark.parametrize("role", ["REFERENCE", "EXCLUDED"])
def test_target_claimed_by_another_role_is_never_stolen(role):
    rows = activities() + [activity("青溪滩", 3, role, occurrence=2)]
    proposal = propose(rows=rows)
    assert len(proposal.mentions) == len(rows)
    assert names(proposal) == ["澄湖公园", "枫林寺", "青溪滩滨江", "青溪滩", "星河坊", "青溪滩"]
    short_mentions = [mention for mention in proposal.mentions if mention.atomic_place_name == "青溪滩"]
    assert [mention.span_start for mention in short_mentions] == [SOURCE.index("青溪滩滨江"), SOURCE.rindex("青溪滩")]


def test_a_non_contiguous_choice_group_cannot_move_past_another_day_activity():
    source = SOURCE.replace("Day2：枫林寺。", "Day2：枫林寺，然后瑶光公园。")
    rows = activities()
    rows.insert(4, activity("瑶光公园", 2, "PLANNED"))
    assert_missing_visit(source, rows)


def test_a_group_containing_a_planned_item_is_not_wholly_unselected():
    rows = activities()
    rows[2]["role"] = "PLANNED"
    assert_missing_visit(SOURCE, rows)


HEADING_REPLACEMENT_SOURCE = (
    "Day1：澄湖公园。\nDay2：枫林寺。\n"
    "Day3：城内 / 郊外二选一\n"
    "### 方案 A：看展\n云岚博物馆。\n"
    "### 方案 B：散步\n星河公园。\n"
    "### 如果想去云岛：\n直接把 Day3 替换云岛一整天。"
)


def replacement_activities():
    return [
        activity("澄湖公园", 1, "PLANNED"),
        activity("枫林寺", 2, "PLANNED"),
        activity("云岛", 3),
        activity("云岚博物馆", 3),
        activity("星河公园", 3),
    ]


def test_optional_heading_name_moves_only_to_its_unique_whole_day_alternative_body():
    source = HEADING_REPLACEMENT_SOURCE
    rows = replacement_activities()
    proposal = propose(source, rows)
    assert names(proposal) == ["澄湖公园", "枫林寺", "云岚博物馆", "星河公园", "云岛"]
    assert len(proposal.mentions) == len(rows)
    assert [mention.day_index for mention in proposal.mentions] == [1, 2, 3, 3, 3]
    assert [mention.role.value for mention in proposal.mentions] == ["PLANNED", "PLANNED", "OPTIONAL", "OPTIONAL", "OPTIONAL"]
    assert [mention.sequence_index for mention in proposal.mentions[2:]] == [0, 1, 2]
    relocated = proposal.mentions[-1]
    assert relocated.span_start == source.rindex("云岛")
    assert source[relocated.span_start:relocated.span_end] == relocated.raw_text == "云岛"


@pytest.mark.parametrize("source", [
    HEADING_REPLACEMENT_SOURCE + "\n云岛也是介绍中的名字。",
    HEADING_REPLACEMENT_SOURCE.replace("### 如果想去云岛：", "如果想去云岛："),
])
def test_third_occurrence_or_ordinary_intro_cannot_reanchor_a_whole_day_alternative(source):
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(source, replacement_activities())
    assert "MISSING_EXPLICIT_OPTIONAL_PLACE" in {issue["category"] for issue in error.value.issues}


def test_heading_occurrence_is_unchanged_when_the_other_name_is_only_a_reference():
    source = HEADING_REPLACEMENT_SOURCE.replace(
        "直接把 Day3 替换云岛一整天。", "云岛是介绍中的一个参考名称。",
    )
    rows = replacement_activities()
    proposal = propose(source, rows)
    assert names(proposal) == [row["place_name"] for row in rows]
    assert len(proposal.mentions) == len(rows)
    assert proposal.mentions[2].span_start == source.index("云岛")


@pytest.mark.parametrize("preparation", ["门票提前买好。", "提前7天20点抢票。"])
@pytest.mark.parametrize("heading", [False, True])
def test_ticket_preparation_does_not_disable_source_occurrence_alignment(preparation, heading):
    source = (HEADING_REPLACEMENT_SOURCE if heading else SOURCE) + "\n" + preparation
    rows = replacement_activities() if heading else activities()
    proposal = propose(source, rows)
    expected = ["澄湖公园", "枫林寺", "云岚博物馆", "星河公园", "云岛"] if heading else [
        "澄湖公园", "枫林寺", "青溪滩滨江", "星河坊", "青溪滩",
    ]
    assert names(proposal) == expected
    assert len(proposal.mentions) == len(rows)
    assert [mention.day_index for mention in proposal.mentions] == [1, 2, 3, 3, 3]
    assert [mention.role.value for mention in proposal.mentions] == ["PLANNED", "PLANNED", "OPTIONAL", "OPTIONAL", "OPTIONAL"]
    assert proposal.mentions[-1].span_start == source.rindex(expected[-1])


@pytest.mark.parametrize("heading", [False, True])
def test_advancing_the_last_day_still_disables_source_occurrence_alignment(heading):
    source = (HEADING_REPLACEMENT_SOURCE if heading else SOURCE) + "\n末日提前一天。"
    rows = replacement_activities() if heading else activities()
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(source, rows)
    missing_category = "MISSING_EXPLICIT_OPTIONAL_PLACE" if heading else "MISSING_EXPLICIT_VISIT_PLACE"
    assert missing_category in {issue["category"] for issue in error.value.issues}
