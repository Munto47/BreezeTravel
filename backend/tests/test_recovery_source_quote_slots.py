"""A bad repaired name cannot displace a source-validated atomic visit."""
from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.semantic_recovery import merge_preserved_activities
from tests.test_experience_inference import provider
from tests.test_semantic_day_sections import RecordingPlaces
from tests.test_validated_draft_recovery import client_for


def activity(name, *, occurrence=1, day=1, quote=None):
    return dict(place_name=name, source_quote=quote or name, occurrence=occurrence,
                day_index=day, role="PLANNED", category="景点")


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_index", [0, 2])
async def test_repaired_bad_name_keeps_its_own_repeat_visit_and_public_order(broken_index):
    source = "Day1：云岭书院、星河公园，再访云岭书院。\nDay2：青岚展厅。"
    complete = [activity("云岭书院"), activity("星河公园"),
                activity("云岭书院", occurrence=2), activity("青岚展厅", day=2)]
    first = {"activities": [row for index, row in enumerate(complete) if index != 1]}
    second = {"activities": [dict(row) for row in complete]}
    second["activities"][broken_index]["place_name"] = "不存在的云岭分馆"
    client = client_for(first, second)
    result = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(source)
    assert [card.name for day in result.public_result.days for card in day.activities] == [
        "云岭书院", "星河公园", "云岭书院", "青岚展厅"]
    assert [item.day_index for item in result.proposal.mentions] == [1, 1, 1, 2]
    assert all(item.role.value == "PLANNED" for item in result.proposal.mentions)
    repeated = [item for item in result.proposal.mentions if item.atomic_place_name == "云岭书院"]
    assert [item.span_start for item in repeated] == [source.index("云岭书院"), source.rindex("云岭书院")]
    assert result.public_result.coverage.confirmed_place_count == 4
    assert len(client.calls) == result.inference_binding["external_calls"] == 2


@pytest.mark.parametrize("defect", ["broad_quote", "duplicate_row", "other_occurrence", "other_day", "other_role", "other_parent"])
def test_invalid_name_cannot_claim_an_ambiguous_or_different_source_slot(defect):
    source = "Day1：云岭书院、星河公园，再访云岭书院。\nDay2：青岚展厅。"
    first = activity("云岭书院", quote="云岭书院、星河公园" if defect == "broad_quote" else None)
    original = SemanticDraft.model_validate({"activities": [first, activity("青岚展厅", day=2)]})
    validated = proposal_from_draft(source, original, allow_partial=True)
    bad = dict(first, place_name="不存在的云岭分馆")
    if defect == "other_occurrence":
        bad["occurrence"] = 2
    elif defect == "other_day":
        bad["day_index"] = 2
    elif defect == "other_role":
        bad["role"] = "OPTIONAL"
    elif defect == "other_parent":
        bad["parent_source_quote"] = "星河公园"
    rows = [bad, activity("星河公园"), activity("青岚展厅", day=2)]
    if defect == "duplicate_row":
        rows.insert(1, dict(bad))
    repaired = SemanticDraft.model_validate({"activities": rows})
    merged = merge_preserved_activities(source, original, validated, repaired)
    # The invalid row remains untrusted instead of being assigned a guessed
    # original visit. The independently verified first visit is still kept.
    assert merged.activities[0].model_dump() == repaired.activities[0].model_dump()
    kept = [row for row in merged.activities if row.place_name == "云岭书院"]
    assert len(kept) == 1
    assert kept[0].model_dump() == original.activities[0].model_dump()
