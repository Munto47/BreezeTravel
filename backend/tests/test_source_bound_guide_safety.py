"""Independent synthetic checks for source list and area-caption recovery."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import EvidenceCompiler, TripUnderstandingPipeline


def activity(name, role="PLANNED"):
    return {"source_quote": name, "place_name": name, "role": role,
            "day_index": 1, "occurrence": 1, "category": "景点"}


def draft_for(rows):
    return SemanticDraft.model_validate({"destination": "北京", "activities": rows})


@pytest.mark.parametrize("qualification", [
    "两个只选星河公园。",
    "只是参考，实际只去星河公园。",
    "，星河公园入园游玩，云岭公园仅路过。",
    "，星河公园入园游玩，云岭公园只是途经。",
    "，以星河公园替换云岭公园。",
    "中只参观星河公园。",
])
def test_first_list_member_cannot_lend_a_planned_role_to_a_non_visit(qualification):
    source = "Day1：**星河公园、云岭公园**" + qualification
    try:
        proposal = proposal_from_draft(source, draft_for([activity("星河公园")]))
    except SourceAnchorValidationError as exc:
        # A partial semantic draft may request repair, but cannot fabricate a
        # second confirmed intention just to satisfy the list completeness guard.
        assert exc.issues
        return
    assert [item.atomic_place_name for item in proposal.mentions if item.role == ActivityRole.PLANNED] == ["星河公园"]
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [item.mention.atomic_place_name for item in compiled if item.eligible_for_place_search] == ["星河公园"]


def test_an_unqualified_explicit_list_still_recovers_both_planned_names():
    source = "Day1：**星河公园、云岭公园**。"
    proposal = proposal_from_draft(source, draft_for([activity("星河公园")]))
    compiled = EvidenceCompiler().compile(source, proposal)[0]
    assert [item.mention.atomic_place_name for item in compiled if item.eligible_for_place_search] == ["星河公园", "云岭公园"]
    assert all(item.mention.role == ActivityRole.PLANNED for item in compiled)


def test_a_siblings_explicit_non_visit_role_is_never_overwritten():
    source = "Day1：**星河公园、云岭公园**，后者只作参考。"
    proposal = proposal_from_draft(source, draft_for([
        activity("星河公园"), activity("云岭公园", "REFERENCE"),
    ]))
    assert [(item.atomic_place_name, item.role.value) for item in proposal.mentions] == [
        ("星河公园", "PLANNED"), ("云岭公园", "REFERENCE"),
    ]
    assert [item.mention.atomic_place_name for item in EvidenceCompiler().compile(source, proposal)[0]
            if item.eligible_for_place_search] == ["星河公园"]


def venue_choice(parent, child, instruction):
    source = ("Day1：方案A或方案B二选一。\n"
              f"方案A：{instruction}{parent}，逛{child}、青溪公园。\n"
              "方案B：云岭公园。")
    names = [parent, child, "青溪公园", "云岭公园"]
    return source, names, draft_for([activity(name, "OPTIONAL") for name in names])


@pytest.mark.parametrize("parent,child", [
    ("星河博物馆", "星河博物馆东馆"),
    ("清溪公园", "清溪公园展览馆"),
    ("照月寺", "照月寺文物馆"),
    ("青岩书院", "青岩书院西馆"),
])
@pytest.mark.parametrize("instruction", ["先游览", ""])
def test_an_explicit_parent_venue_remains_optional_beside_its_child(parent, child, instruction):
    source, names, draft = venue_choice(parent, child, instruction)
    proposal = proposal_from_draft(source, draft)
    assert [item.atomic_place_name for item in proposal.mentions] == names
    assert all(item.role == ActivityRole.OPTIONAL for item in proposal.mentions)
    assert all(not item.eligible_for_place_search for item in EvidenceCompiler().compile(source, proposal)[0])


@pytest.mark.asyncio
async def test_the_parent_venue_remains_visible_in_unselected_alternatives():
    source, names, draft = venue_choice("星河博物馆", "星河博物馆东馆", "先游览")

    class DraftProvider:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    class NoSearch:
        async def resolve(self, **values):
            raise AssertionError("Unselected alternatives must not trigger a place search")

    output = await TripUnderstandingPipeline(DraftProvider(), NoSearch()).run(source)
    assert output.public_result.days[0].activities == []
    assert [item.name for item in output.public_result.days[0].alternatives] == names
