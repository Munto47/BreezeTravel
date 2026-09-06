"""Synthetic area-caption omissions must request repair, never invent visits."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler


REGION = "青溪湾"
CHILDREN = ("青溪湾天主教堂", "星河城商圈")


def row(name, **values):
    return {"source_quote": name, "place_name": name, "role": "OPTIONAL", "day_index": 3, **values}


def source_for(clause):
    return f"Day3：二选一\n### 方案 A：城中\n{clause}\n### 方案 B：林间\n云岭公园。"


def propose(source, rows):
    return proposal_from_draft(source, SemanticDraft.model_validate({"destination": "北京", "activities": rows}))


@pytest.mark.parametrize("numbering", ["", "③"])
@pytest.mark.parametrize("kept_child_count", [0, 1])
def test_an_area_prefix_cannot_hide_missing_explicit_child_visits(numbering, kept_child_count):
    source = source_for(f"{numbering}{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。")
    rows = [row(REGION), *(row(name) for name in CHILDREN[:kept_child_count]), row("云岭公园")]
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose(source, rows)
    assert any(issue["category"] == "MISSING_EXPLICIT_PARALLEL_PLACE" for issue in raised.value.issues)
    assert all(name in raised.value.repair_hints for name in CHILDREN[kept_child_count:])
    assert all(name not in str(raised.value) for name in CHILDREN)


@pytest.mark.parametrize("numbering", ["", "③"])
def test_complete_children_remain_separate_options_and_the_area_is_reference(numbering):
    source = source_for(f"{numbering}{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。")
    proposal = propose(source, [row(REGION), *(row(name) for name in CHILDREN), row("云岭公园")])
    assert [(m.atomic_place_name, m.role.value) for m in proposal.mentions] == [
        (REGION, "REFERENCE"), (CHILDREN[0], "OPTIONAL"), (CHILDREN[1], "OPTIONAL"), ("云岭公园", "OPTIONAL"),
    ]
    assert [m.raw_text for m in proposal.mentions] == [REGION, *CHILDREN, "云岭公园"]
    assert all(m.day_index == 3 for m in proposal.mentions)
    assert all(source[m.span_start:m.span_end] == m.raw_text for m in proposal.mentions)
    assert not any(m.eligible_for_place_search for m in EvidenceCompiler().compile(source, proposal)[0])


@pytest.mark.parametrize("clause", [
    f"旅行达人说{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"旅行达人说：{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"介绍{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"说明：{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"去年{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"去年行程：{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"原计划{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"取消：{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"{REGION}附近，逛{CHILDREN[0]}、{CHILDREN[1]}。",
    f"{REGION}，附近有{CHILDREN[0]}、{CHILDREN[1]}。",
    f"{REGION}，仅眺望{CHILDREN[0]}、{CHILDREN[1]}。",
])
def test_narrative_history_cancellation_and_observed_neighbors_do_not_require_extra_places(clause):
    source = source_for(clause)
    proposal = propose(source, [row(REGION, role="REFERENCE"), row("云岭公园")])
    assert [(m.atomic_place_name, m.role.value) for m in proposal.mentions] == [
        (REGION, "REFERENCE"), ("云岭公园", "OPTIONAL"),
    ]


def test_a_park_interior_description_does_not_require_individual_child_visits():
    name = "青溪湾公园"
    source = source_for(f"{name}，园内有{name}教堂、星河桥。")
    proposal = propose(source, [row(name), row("云岭公园")])
    assert [m.atomic_place_name for m in proposal.mentions] == [name, "云岭公园"]
    assert all(m.role.value == "OPTIONAL" for m in proposal.mentions)


def test_a_fabricated_child_is_not_accepted_as_a_repair_for_the_missing_list():
    source = source_for(f"{REGION}，逛{CHILDREN[0]}、{CHILDREN[1]}。")
    with pytest.raises(SourceAnchorValidationError) as raised:
        propose(source, [row(REGION), row(CHILDREN[0]), row("月泉城商圈"), row("云岭公园")])
    categories = {issue["category"] for issue in raised.value.issues}
    assert "SOURCE_QUOTE_NOT_FOUND" in categories
    assert "MISSING_EXPLICIT_PARALLEL_PLACE" in categories
    assert CHILDREN[1] in raised.value.repair_hints
