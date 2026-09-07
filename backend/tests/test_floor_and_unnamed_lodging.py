"""Literal floor restrictions and unnamed check-in activities stay source-bound."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler


def propose(source, quote, name, *, occurrence=1, role="PLANNED", category="地点"):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "上海",
        "activities": [{
            "source_quote": quote, "place_name": name, "occurrence": occurrence,
            "role": role, "day_index": 1, "category": category,
        }],
    }))


@pytest.mark.parametrize("annotation,expected", [
    ("（星穹大厦52楼）", "（星穹大厦52楼）"),
    ("(星穹大厦52层)", "(星穹大厦52层)"),
    ("（星穹大厦 52 楼，咖啡看城市全景）", "（星穹大厦52楼）"),
])
@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
def test_short_model_name_keeps_the_exact_attached_building_floor_and_original_evidence(annotation, expected, role):
    literal = "云汀书院" + annotation
    source = f"Day1：{literal}。"
    proposal = propose(source, "云汀书院", "云汀书院", role=role)
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == "云汀书院" + expected
    assert source[mention.span_start:mention.span_end] == mention.raw_text == literal
    assert mention.span_start == source.index(literal)
    assert mention.role.value == role
    assert mention.day_index == 1
    compiled = EvidenceCompiler().compile(source, proposal)[0][0]
    assert compiled.eligible_for_place_search is (role == "PLANNED")


def test_repeated_base_name_uses_the_requested_occurrence_without_swapping_floors():
    source = "Day1：云汀书院（星穹大厦31楼）。随后云汀书院（星穹大厦52楼）。"
    proposal = propose(source, "云汀书院", "云汀书院", occurrence=2)
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == mention.raw_text == "云汀书院（星穹大厦52楼）"
    assert mention.span_start == source.rindex("云汀书院")
    assert source[mention.span_start:mention.span_end] == mention.raw_text


@pytest.mark.parametrize("wrong_name", [
    "云汀书院（星穹大厦51楼）",
    "云汀书院（星海大厦52楼）",
    "星河书院",
])
def test_wrong_floor_building_or_base_name_requires_repair_instead_of_being_overwritten(wrong_name):
    literal = "云汀书院（星穹大厦52楼）"
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(f"Day1：{literal}。", literal, wrong_name)
    assert "PLACE_NOT_IN_SOURCE_QUOTE" in {issue["category"] for issue in error.value.issues}


def test_unknown_floor_note_is_not_silently_removed_to_make_a_qualified_place():
    literal = "云汀书院（星穹大厦52楼，隔壁还有其他店）"
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(f"Day1：{literal}。", "云汀书院", "云汀书院")
    assert "PLACE_QUALIFIER_OMITTED" in {issue["category"] for issue in error.value.issues}


@pytest.mark.parametrize("quote", ["14:00 入住放行李", "办理入住，放行李。", "寄存行李后休息"])
def test_literal_unnamed_check_in_keeps_an_unnamed_lodging_activity(quote):
    source = f"Day1：{quote}"
    proposal = propose(source, quote, "酒店")
    assert len(proposal.mentions) == 1
    mention = proposal.mentions[0]
    assert mention.atomic_place_name is None
    assert mention.category_hint == "住宿"
    assert mention.role.value == "PLANNED"
    assert mention.day_index == 1
    assert source[mention.span_start:mention.span_end] == mention.raw_text == quote
    assert not EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search


def test_unnamed_lodging_retains_the_exact_repeated_quote_occurrence():
    source = "Day1：先放行李，然后拍照，最后放行李。"
    proposal = propose(source, "放行李", "酒店", occurrence=2)
    mention = proposal.mentions[0]
    assert mention.atomic_place_name is None
    assert mention.span_start == source.rindex("放行李")
    assert source[mention.span_start:mention.span_end] == mention.raw_text == "放行李"


@pytest.mark.parametrize("quote", ["14:00 看展和休息", "入住以后去云岭路散步"])
def test_unknown_description_cannot_be_cleared_into_an_unnamed_hotel(quote):
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(f"Day1：{quote}。", quote, "酒店")
    assert "PLACE_NOT_IN_SOURCE_QUOTE" in {issue["category"] for issue in error.value.issues}


def test_named_hotel_is_preserved_when_the_original_quote_names_it():
    source = "Day1：14:00 入住云岚酒店，放行李。"
    proposal = propose(source, "云岚酒店", "云岚酒店", category="住宿")
    mention = proposal.mentions[0]
    assert mention.atomic_place_name == mention.raw_text == "云岚酒店"
    assert mention.category_hint == "住宿"
    assert source[mention.span_start:mention.span_end] == "云岚酒店"
    assert EvidenceCompiler().compile(source, proposal)[0][0].eligible_for_place_search


def test_invented_named_hotel_is_rejected_instead_of_being_cleared():
    quote = "14:00 入住放行李"
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(f"Day1：{quote}。", quote, "云岚酒店")
    assert "PLACE_NOT_IN_SOURCE_QUOTE" in {issue["category"] for issue in error.value.issues}


def test_a_nonliteral_check_in_quote_still_requires_source_anchor_validation():
    with pytest.raises(SourceAnchorValidationError) as error:
        propose("Day1：14:00 看展。", "14:00 入住放行李", "酒店")
    assert "SOURCE_QUOTE_NOT_FOUND" in {issue["category"] for issue in error.value.issues}
