"""Synthetic title alternatives need model names, never sliced descriptions."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler


SOURCE = "Day1：澄湖公园。\n## Day2｜郊游（二选一：云岭坡人少，星河湾更出名）参考说明"


def activity(name, day, role):
    return {"source_quote": name, "place_name": name, "day_index": day, "role": role}


def propose(rows, source=SOURCE):
    return proposal_from_draft(source, SemanticDraft.model_validate({
        "destination": "北京", "activities": [activity("澄湖公园", 1, "PLANNED"), *rows],
    }))


def test_two_literal_reference_names_become_optional_without_search_or_description_names():
    proposal = propose([activity("云岭坡", 2, "REFERENCE"), activity("星河湾", 2, "REFERENCE")])
    assert len(proposal.mentions) == 3
    assert [mention.atomic_place_name for mention in proposal.mentions] == ["澄湖公园", "云岭坡", "星河湾"]
    assert [mention.role.value for mention in proposal.mentions] == ["PLANNED", "OPTIONAL", "OPTIONAL"]
    assert [mention.day_index for mention in proposal.mentions] == [1, 2, 2]
    for mention in proposal.mentions[1:]:
        assert SOURCE[mention.span_start:mention.span_end] == mention.raw_text == mention.atomic_place_name
    compiled = EvidenceCompiler().compile(SOURCE, proposal)[0]
    assert [item.eligible_for_place_search for item in compiled] == [True, False, False]


@pytest.mark.parametrize("included", [[], [activity("云岭坡", 2, "OPTIONAL")]])
def test_missing_one_or_both_binary_names_requires_repair_instead_of_fabricated_mentions(included):
    with pytest.raises(SourceAnchorValidationError) as error:
        propose(included)
    missing = [issue for issue in error.value.issues if issue["category"] == "MISSING_EXPLICIT_OPTIONAL_PLACE"]
    assert len(missing) == 2 - len(included)


def test_excluded_candidate_is_not_silently_promoted_by_a_binary_title():
    with pytest.raises(SourceAnchorValidationError) as error:
        propose([activity("云岭坡", 2, "EXCLUDED"), activity("星河湾", 2, "OPTIONAL")])
    assert "MISSING_EXPLICIT_OPTIONAL_PLACE" in {issue["category"] for issue in error.value.issues}


def test_a_description_fragment_cannot_itself_be_the_model_supplied_place_name():
    with pytest.raises(SourceAnchorValidationError):
        propose([activity("云岭坡人少", 2, "REFERENCE"), activity("星河湾更出名", 2, "REFERENCE")])
