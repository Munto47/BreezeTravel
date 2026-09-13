"""Provided action evidence identifies visits; duplicated model rows do not."""
import json
from pathlib import Path

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_inference import Client, provider
from tests.test_semantic_day_sections import RecordingPlaces


def proposal(source, activities):
    return _proposal_from_live_draft(source, SemanticDraft.model_validate(dict(
        destination="杭州", activities=activities)), allow_partial=True)


def hotel(quote, evidence, event, *, day=1, role="PLANNED"):
    return dict(source_quote=quote, place_name="星河酒店", lodging_evidence=evidence,
                lodging_event=event, category="住宿", role=role, day_index=day)


@pytest.mark.asyncio
async def test_observed_revisit_keeps_the_actual_pickup_after_longjing():
    sample = json.loads((Path(__file__).parent / "fixtures/live_lodging_revisit.json").read_text(encoding="utf-8"))
    content = json.dumps(sample["model_response"], ensure_ascii=False)
    client = Client(content, content)
    output = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(sample["source"])
    named = [item.compiled.mention for item in output.activities
             if item.compiled.mention.role.value == "PLANNED" and item.compiled.mention.atomic_place_name]
    assert [[m.day_index, m.atomic_place_name] for m in named] == sample["expected_planned"]
    hotels = [m for m in named if m.atomic_place_name == "杭州西湖国宾馆"]
    assert [(m.day_index, m.lodging_event) for m in hotels] == [(1, "OVERNIGHT"), (2, "LUGGAGE_PICKUP")]
    assert hotels[0].span_start != hotels[1].span_start
    unnamed = [a.compiled.mention for a in output.activities if a.compiled.mention.atomic_place_name is None]
    assert any(m.day_index == 2 and m.raw_text == "退房后把行李寄存在酒店" for m in unnamed)
    assert [[card.name for card in day.activities] for day in output.public_result.days] == [
        [name for day, name in sample["expected_planned"] if day == index] for index in (1, 2)]
    # The unsupported hotel name on the unnamed checkout stays local and is
    # not counted as verified merely because all nine named visits survive.
    assert output.public_result.coverage.unprocessed_count == 1
    assert output.inference_binding["semantic_diagnostic_counts"] == {"UNBOUND_LODGING_REFERENCE": 1}
    assert [day.unprocessed_count for day in output.public_result.days] == [0, 1]
    assert len(client.calls) == 1


def test_distinct_same_day_action_quotes_preserve_both_visits():
    source = "杭州一天。早上从星河酒店出发。下午回星河酒店取行李。如果下雨就早点回家。"
    result = proposal(source, [hotel("星河酒店", "早上从星河酒店出发", "DEPARTURE"),
        hotel("星河酒店", "下午回星河酒店取行李", "LUGGAGE_PICKUP")])
    assert [(m.atomic_place_name, m.lodging_event) for m in result.mentions] == [
        ("星河酒店", "DEPARTURE"), ("星河酒店", "LUGGAGE_PICKUP")]
    assert result.mentions[0].span_start != result.mentions[1].span_start
    assert result.unprocessed_count == 0


def test_duplicate_rows_for_one_actual_action_stay_one_visit():
    source = "杭州一天。下午回星河酒店取行李。"
    item = hotel("星河酒店", "下午回星河酒店取行李", "LUGGAGE_PICKUP")
    result = proposal(source, [item, dict(item)])
    assert len(result.mentions) == 1
    assert result.mentions[0].lodging_event == "LUGGAGE_PICKUP"


def test_short_action_evidence_cannot_erase_a_literal_name_in_the_same_clause():
    source = "杭州一天。下午回星河酒店取行李。"
    result = proposal(source, [hotel("星河酒店", "取行李", "LUGGAGE_PICKUP")])
    assert len(result.mentions) == 1
    assert result.mentions[0].atomic_place_name == "星河酒店"
    assert result.mentions[0].lodging_role_uncertain


def test_unverbatim_lodging_evidence_never_reanchors_to_an_invented_action():
    source = "杭州一天。上午在星河酒店休息。"
    result = proposal(source, [hotel("星河酒店", "下午回星河酒店取行李", "LUGGAGE_PICKUP")])
    assert result.mentions[0].raw_text == "星河酒店"
    assert result.mentions[0].lodging_role_uncertain
    assert result.unprocessed_count > 0


def test_conditional_hotel_action_stays_optional_when_anchored():
    source = "杭州一天。早上从星河酒店出发。如果有空，下午回星河酒店取行李。"
    result = proposal(source, [hotel("星河酒店", "早上从星河酒店出发", "DEPARTURE"),
        hotel("星河酒店", "如果有空，下午回星河酒店取行李", "LUGGAGE_PICKUP", role="OPTIONAL")])
    assert [(m.atomic_place_name, m.role.value) for m in result.mentions] == [("星河酒店", "PLANNED"), ("星河酒店", "OPTIONAL")]
    assert result.mentions[0].span_start != result.mentions[1].span_start
