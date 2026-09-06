"""Descriptions stay non-executing while independent literal recovery composes."""
import json

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    _align_choice_label_occurrences,
    _retain_explicit_branch_revisits,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import EvidenceCompiler
from tests.test_experience_inference import Client, provider


SOURCE = ("Day1：城中\n星河公园。\nDay2：二选一\n"
          "### 方案 A：江边\n青溪滩滨江。远眺云岚塔。\n"
          "### 方案 B：街区\n枫林坊简单逛一圈即可。傍晚青溪滩看落日。")


def activity(name, day=2, role="OPTIONAL", **fields):
    return {"source_quote": name, "place_name": name, "day_index": day, "role": role, **fields}


@pytest.mark.parametrize("reference_day", [None, 1, 2])
@pytest.mark.parametrize("include_short", [False, True])
def test_a_viewed_tower_does_not_hide_the_other_branchs_revisit(reference_day, include_short):
    rows = [activity("星河公园", 1, "PLANNED"), activity("青溪滩滨江"),
            activity("云岚塔", reference_day, "REFERENCE")]
    if include_short:
        rows.append(activity("青溪滩"))  # The model points at the longer label in A.
    rows.append(activity("枫林坊"))
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": rows})
    proposal = proposal_from_draft(SOURCE, draft)
    optional = [item for item in proposal.mentions if item.role == "OPTIONAL"]
    assert [item.atomic_place_name for item in optional] == ["青溪滩滨江", "枫林坊", "青溪滩"]
    assert all(item.day_index == 2 for item in optional)
    assert optional[-1].span_start == SOURCE.rindex("青溪滩")
    reference = next(item for item in proposal.mentions if item.atomic_place_name == "云岚塔")
    assert reference.role == "REFERENCE" and reference.day_index == reference_day
    assert [item.mention.atomic_place_name for item in EvidenceCompiler().compile(SOURCE, proposal)[0]
            if item.eligible_for_place_search] == ["星河公园"]


@pytest.mark.asyncio
async def test_time_cleanup_uses_the_same_indices_after_a_literal_option_was_inserted():
    source = "Day1：市区\n星河公园。云岭咖啡可以打卡。青溪桥。"
    rows = [activity("星河公园", 1, "PLANNED"), activity("青溪桥", 1, "PLANNED",
            start_time="10:00", time_evidence="没有原文依据的时间")]
    payload = json.dumps({"destination": "北京", "activities": rows}, ensure_ascii=False)
    result = await provider(Client(payload, payload)).propose(source)
    assert [(item.atomic_place_name, item.role.value) for item in result.mentions] == [
        ("星河公园", "PLANNED"), ("云岭咖啡", "OPTIONAL"), ("青溪桥", "PLANNED")]
    assert all(item.start_time is None for item in result.mentions)
    assert result.binding["degraded_timing_activities"] == 1
    assert result.binding["outcome"] == "PARTIAL_RESULT"
    assert result.unprocessed_count >= 1


def _draft_with_partially_claimed_visit(*, include_short):
    rows = [activity("星河公园", 1, "PLANNED"), activity("青溪滩滨江"),
            activity("云岚塔", None, "REFERENCE")]
    if include_short:
        rows.append(activity("青溪滩"))
    rows.extend([activity("枫林坊"),
                 activity(None, None, "REFERENCE", source_quote="傍晚青溪")])
    return SemanticDraft.model_validate({"destination": "北京", "activities": rows})


def test_a_missing_branch_visit_cannot_borrow_part_of_an_existing_reference():
    draft = _draft_with_partially_claimed_visit(include_short=False)
    retained = _retain_explicit_branch_revisits(SOURCE, draft)
    assert retained == draft
    assert retained.activities[-1].source_quote == "傍晚青溪"
    assert retained.activities[-1].role == "REFERENCE"
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(SOURCE, draft)
    assert "MISSING_EXPLICIT_VISIT_PLACE" in {issue["category"] for issue in error.value.issues}


def test_an_existing_branch_label_cannot_move_into_a_partially_claimed_reference():
    draft = _draft_with_partially_claimed_visit(include_short=True)
    aligned = _align_choice_label_occurrences(SOURCE, draft)
    assert aligned == draft
    assert next(item for item in aligned.activities if item.place_name == "青溪滩").occurrence == 1
    assert aligned.activities[-1].source_quote == "傍晚青溪"
    assert aligned.activities[-1].role == "REFERENCE"
    with pytest.raises(SourceAnchorValidationError) as error:
        proposal_from_draft(SOURCE, draft)
    assert "MISSING_EXPLICIT_VISIT_PLACE" in {issue["category"] for issue in error.value.issues}
