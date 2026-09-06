"""Synthetic end-to-end proposals for bounded guide repairs; no external calls."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.guide_choices import choice_scopes
from app.trip_understanding.pipeline import EvidenceCompiler


def row(name, **values):
    return {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1, **values}


def propose(source, rows):
    return proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": rows}))


def branches(first, second="青溪公园。"):
    return f"Day1：二选一\n### 方案 A：城中\n{first}\n### 方案 B：林间\n{second}"


def identities(proposal):
    return [(m.atomic_place_name, m.role.value) for m in proposal.mentions]


def test_a_bold_list_missing_its_last_member_is_completed_in_source_order():
    source = "Day1：上午**星河街、云岭路**。"
    proposal = propose(source, [row("星河街")])
    assert identities(proposal) == [("星河街", "PLANNED"), ("云岭路", "PLANNED")]
    assert [m.day_index for m in proposal.mentions] == [1, 1]
    assert [m.raw_text for m in proposal.mentions] == ["星河街", "云岭路"]
    assert all(source[m.span_start:m.span_end] == m.raw_text for m in proposal.mentions)


def test_bold_completion_keeps_the_first_booking_without_copying_it_to_the_sibling():
    source = "Day1：09:00 已预约**星河博物馆、云岭公园**。"
    proposal = propose(source, [row("星河博物馆", start_time="09:00", locked=True,
                                   fixed_commitment=True, timing_source="TEXT", time_evidence=source)])
    first, second = proposal.mentions
    assert (first.atomic_place_name, first.start_time, first.locked) == ("星河博物馆", "09:00", True)
    assert second.atomic_place_name == "云岭公园"
    assert second.start_time is None and second.end_time is None and not second.locked


@pytest.mark.parametrize("source", [
    "Day1：星河街、云岭路。",
    "Day1：**星河街 / 云岭路**。",
    "Day1：不去**星河街、云岭路**。",
    "Day1：二选一：**星河街、云岭路**。",
    "Day1：备选**星河街、云岭路**。",
    "Day1：**星河街、云岭路**。\n更正：当天只安排星河街。",
    "Day1：**星河街、云岭路**。\n取消云岭路。",
])
def test_unsupported_missing_lists_do_not_inherit_a_visit(source):
    try:
        proposal = propose(source, [row("星河街")])
    except SourceAnchorValidationError as exc:
        assert "MISSING_EXPLICIT_PARALLEL_PLACE" in str(exc)
    else:
        # Existing completeness checks can request repair. When they leave an
        # unsupported phrase to inference, this new repair must not add a stop.
        assert identities(proposal) == [("星河街", "PLANNED")]


def test_a_reference_cannot_lend_a_planned_role_to_its_bold_sibling():
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        propose("Day1：**星河街、云岭路**。", [row("星河街", role="REFERENCE")])


def test_an_earlier_same_name_quote_cannot_anchor_a_later_bold_list():
    source = "Day1：介绍星河街。\n**星河街、云岭路**。"
    with pytest.raises(SourceAnchorValidationError, match="MISSING_EXPLICIT_PARALLEL_PLACE"):
        propose(source, [row("星河街", occurrence=1)])


@pytest.mark.parametrize("sibling_role", ["PLANNED", "REFERENCE", "EXCLUDED"])
def test_a_separately_classified_sibling_keeps_its_role_without_a_duplicate(sibling_role):
    source = "Day1：**星河街、云岭路**。"
    proposal = propose(source, [row("星河街"), row("云岭路", role=sibling_role)])
    assert identities(proposal) == [("星河街", "PLANNED"), ("云岭路", sibling_role)]


@pytest.mark.parametrize("draft_role", ["PLANNED", "OPTIONAL"])
def test_a_choice_area_caption_is_reference_but_its_named_children_remain_options(draft_role):
    source = branches("星河湾，逛星河湾美术馆、云岭书院。")
    proposal = propose(source, [row(name, role=draft_role) for name in ["星河湾", "星河湾美术馆", "云岭书院", "青溪公园"]])
    assert identities(proposal) == [("星河湾", "REFERENCE"), ("星河湾美术馆", "OPTIONAL"),
                                     ("云岭书院", "OPTIONAL"), ("青溪公园", "OPTIONAL")]
    assert proposal.mentions[0].raw_text == "星河湾"
    assert not any(m.eligible_for_place_search for m in EvidenceCompiler().compile(source, proposal)[0])


def test_a_caption_outside_an_unselected_choice_cannot_remove_an_actual_visit():
    source = "Day1：星河湾，逛星河湾美术馆、云岭书院。"
    proposal = propose(source, [row(name) for name in ["星河湾", "星河湾美术馆", "云岭书院"]])
    assert identities(proposal) == [("星河湾", "PLANNED"), ("星河湾美术馆", "PLANNED"), ("云岭书院", "PLANNED")]


def test_unrelated_following_stops_do_not_make_a_region_merely_a_caption():
    source = branches("星河湾，逛海棠美术馆、云岭书院。")
    proposal = propose(source, [row(name) for name in ["星河湾", "海棠美术馆", "云岭书院", "青溪公园"]])
    assert identities(proposal)[0] == ("星河湾", "OPTIONAL")


def test_an_absent_child_is_not_invented_to_justify_removing_a_region():
    source = branches("星河湾，逛星河湾美术馆、云岭书院。")
    proposal = propose(source, [row("星河湾"), row("星河湾美术馆"), row("青溪公园")])
    assert identities(proposal) == [("星河湾", "OPTIONAL"), ("星河湾美术馆", "OPTIONAL"), ("青溪公园", "OPTIONAL")]


def test_a_separate_earlier_region_visit_survives_a_later_area_caption():
    source = branches("早上星河湾。\n星河湾，逛星河湾美术馆、云岭书院。")
    proposal = propose(source, [row("星河湾", occurrence=1), row("星河湾", occurrence=2),
                               row("星河湾美术馆"), row("云岭书院"), row("青溪公园")])
    assert identities(proposal)[:2] == [("星河湾", "OPTIONAL"), ("星河湾", "REFERENCE")]
    assert proposal.mentions[0].span_start < proposal.mentions[1].span_start


def test_repeated_meal_area_keeps_an_unnamed_optional_meal_and_its_source():
    source = branches("上午星河路。\n中午星河路周边吃饭。")
    proposal = propose(source, [row("星河路"), row("星河路周边"), row("青溪公园")])
    assert identities(proposal) == [("星河路", "OPTIONAL"), (None, "OPTIONAL"), ("青溪公园", "OPTIONAL")]
    meal = proposal.mentions[1]
    assert meal.category_hint == "餐饮" and meal.raw_text == "星河路周边"
    assert source[meal.span_start:meal.span_end] == "星河路周边"
    assert not EvidenceCompiler().compile(source, proposal)[0][1].eligible_for_place_search


@pytest.mark.parametrize("return_word", ["再去", "返回", "重访", "回到"])
def test_an_explicit_return_to_a_meal_area_keeps_its_location(return_word):
    source = branches(f"上午星河路。\n中午{return_word}星河路周边吃饭。")
    proposal = propose(source, [row("星河路"), row("星河路周边"), row("青溪公园")])
    assert identities(proposal)[1] == ("星河路周边", "OPTIONAL")


def test_a_meal_area_without_an_earlier_standalone_road_keeps_its_name():
    source = branches("中午星河路周边吃饭。")
    proposal = propose(source, [row("星河路周边"), row("青溪公园")])
    assert identities(proposal)[0] == ("星河路周边", "OPTIONAL")


def test_a_real_restaurant_branch_is_not_replaced_by_an_unnamed_meal():
    source = branches("上午星河路。\n中午云汀餐厅（星河路店）吃饭。")
    proposal = propose(source, [row("星河路"), row("云汀餐厅（星河路店）", category="餐饮"), row("青溪公园")])
    assert identities(proposal)[1] == ("云汀餐厅（星河路店）", "OPTIONAL")
    assert proposal.mentions[1].category_hint == "餐饮"


def test_a_visit_in_another_exclusive_branch_cannot_erase_this_branches_meal_area():
    source = branches("上午星河路。", "中午星河路周边吃饭。")
    proposal = propose(source, [row("星河路"), row("星河路周边")])
    assert identities(proposal) == [("星河路", "OPTIONAL"), ("星河路周边", "OPTIONAL")]


def test_the_second_branch_can_reuse_its_own_earlier_standalone_road():
    source = branches("青溪公园。", "上午星河路。\n中午星河路周边吃饭。")
    proposal = propose(source, [row("青溪公园"), row("星河路"), row("星河路周边")])
    assert identities(proposal) == [("青溪公园", "OPTIONAL"), ("星河路", "OPTIONAL"), (None, "OPTIONAL")]
    assert proposal.mentions[2].category_hint == "餐饮"


def test_a_reference_to_a_road_is_not_a_standalone_optional_visit():
    source = branches("参考：星河路。\n中午星河路周边吃饭。")
    proposal = propose(source, [row("星河路", role="REFERENCE"), row("星河路周边"), row("青溪公园")])
    assert identities(proposal) == [("星河路", "REFERENCE"), ("星河路周边", "OPTIONAL"), ("青溪公园", "OPTIONAL")]


def test_a_cross_day_note_keeps_the_models_day_and_same_branch_context_only():
    source = (
        "Day3：二选一\n### 方案 A：城中\n"
        "星河湾，逛星河湾美术馆、云岭书院。\n上午星河路。\n中午星河路周边吃饭。\n"
        "### 方案 B：林间\n中午星河路周边吃饭。\n这个会减少第二天的部分安排。"
    )
    assert [day for _, _, day in choice_scopes(source)] == [None]
    rows = [row(name, day_index=3) for name in ["星河湾", "星河湾美术馆", "云岭书院", "星河路", "星河路周边"]]
    rows.append(row("星河路周边", occurrence=2, day_index=3))
    proposal = propose(source, rows)
    assert identities(proposal) == [
        ("星河湾", "REFERENCE"), ("星河湾美术馆", "OPTIONAL"), ("云岭书院", "OPTIONAL"),
        ("星河路", "OPTIONAL"), (None, "OPTIONAL"), ("星河路周边", "OPTIONAL"),
    ]
    assert all(m.day_index == 3 for m in proposal.mentions)
    assert proposal.mentions[4].category_hint == "餐饮"
    assert source[proposal.mentions[5].span_start:proposal.mentions[5].span_end] == "星河路周边"
    assert proposal.mentions[4].span_start != proposal.mentions[5].span_start
    assert not any(m.eligible_for_place_search for m in EvidenceCompiler().compile(source, proposal)[0])


def test_an_explicit_scope_day_mismatch_does_not_reclassify_area_or_meal():
    source = (
        "Day3：二选一\n### 方案 A：城中\n"
        "星河湾，逛星河湾美术馆、云岭书院。\n上午星河路。\n中午星河路周边吃饭。\n"
        "### 方案 B：林间\n青溪公园。"
    )
    assert [day for _, _, day in choice_scopes(source)] == [3]
    names = ["星河湾", "星河湾美术馆", "云岭书院", "星河路", "星河路周边", "青溪公园"]
    proposal = propose(source, [row(name, day_index=2) for name in names])
    # Existing choice classification can mark these unselected. The new
    # context repair cannot use a conflicting day to erase a named place.
    assert identities(proposal) == [(name, "OPTIONAL") for name in names]
    assert all(m.atomic_place_name is not None for m in proposal.mentions)
