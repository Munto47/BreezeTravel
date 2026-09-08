"""A failed timing repair may use only an independently revalidated place draft."""
from __future__ import annotations

import copy
import json

import pytest

from app.trip_understanding import experience_inference as inference
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_inference import Client, provider


SOURCE = "Day1：云岭书院、星河公园。\nDay2：青岚展厅。"
NAMES = ["云岭书院", "星河公园", "青岚展厅"]


def valid_payload():
    return {"destination": "上海", "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day, "category": "景点"}
        for name, day in zip(NAMES, [1, 1, 2], strict=True)
    ]}


def bad_timing_payload():
    payload = valid_payload()
    for item in payload["activities"][:2]:
        item.update(category="地点", start_time="08:00", end_time="09:00", visit_duration_minutes=60,
                    timing_source="TEXT", locked=True, fixed_commitment=True, time_evidence="08:00已经确认预约")
    return payload


def bad_quote_payload():
    payload = valid_payload()
    payload["activities"][0]["source_quote"] = "完全不存在的地点引文"
    return payload


def client_for(*payloads):
    return Client(*(json.dumps(payload, ensure_ascii=False) for payload in payloads))


def assert_two_calls(binding, client):
    assert len(client.calls) == 2
    assert binding["external_calls"] == 2
    assert binding["repair_call_count"] == 1
    assert len(binding["calls"]) == 2


@pytest.mark.asyncio
async def test_validated_first_place_draft_survives_a_second_answer_that_breaks_source_anchors():
    client = client_for(bad_timing_payload(), bad_quote_payload())
    result = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    proposal = result.proposal
    assert [item.atomic_place_name for item in proposal.mentions] == NAMES
    assert [item.day_index for item in proposal.mentions] == [1, 1, 2]
    assert proposal.day_count == 2
    assert len(proposal.mentions) == 3
    assert all(item.role.value == "PLANNED" for item in proposal.mentions)
    for item in proposal.mentions:
        assert SOURCE[item.span_start:item.span_end] == item.raw_text == item.atomic_place_name
        assert item.start_time is None and item.end_time is None and item.visit_duration_minutes is None
        assert item.locked is False and item.fixed_commitment is False
    assert proposal.binding["outcome"] == "PARTIAL_RESULT"
    assert proposal.binding["fallback_used"] is True
    assert proposal.binding["degraded_timing_activities"] == 2
    assert proposal.unprocessed_count >= 2
    assert result.public_result.status == "PARTIAL_RESULT"
    assert [item.name for day in result.public_result.days for item in day.activities] == NAMES
    assert_two_calls(proposal.binding, client)
    assert all(call["outcome"] != "SUCCESS" for call in proposal.binding["calls"])


@pytest.mark.asyncio
async def test_a_valid_second_answer_takes_precedence_over_the_saved_partial_candidate():
    client = client_for(bad_timing_payload(), valid_payload())
    proposal = await provider(client).propose(SOURCE)
    assert [item.atomic_place_name for item in proposal.mentions] == NAMES
    assert all(item.category_hint == "景点" for item in proposal.mentions)
    assert proposal.binding["outcome"] == "SUCCESS"
    assert proposal.binding["fallback_used"] is False
    assert proposal.binding["degraded_timing_activities"] == 0
    assert proposal.unprocessed_count == 0
    assert_two_calls(proposal.binding, client)


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing_place", "cancelled_role"])
async def test_place_or_role_errors_keep_only_safe_original_parts_and_remain_incomplete(defect):
    from tests.test_semantic_day_sections import RecordingPlaces

    source = SOURCE
    first = bad_timing_payload()
    if defect == "missing_place":
        first["activities"].pop(1)
        expected = "MISSING_EXPLICIT_PARALLEL_PLACE"
    else:
        source += "最终修改为：云岭书院取消。"
        expected = "EXPLICIT_CANCELLATION_CONFLICT"
    client = client_for(first, bad_quote_payload())
    result = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(source)
    expected_names = NAMES if defect == "missing_place" else NAMES[1:]
    assert [item.atomic_place_name for item in result.proposal.mentions] == expected_names
    assert [card.name for day in result.public_result.days for card in day.activities] == expected_names
    assert [item.day_index for item in result.proposal.mentions] == ([1, 1, 2] if defect == "missing_place" else [1, 2])
    assert all(source[item.span_start:item.span_end] == item.raw_text == item.atomic_place_name
               and item.role.value == "PLANNED" for item in result.proposal.mentions)
    assert all(item.start_time is None and item.end_time is None and item.visit_duration_minutes is None
               and not item.locked and not item.fixed_commitment for item in result.proposal.mentions)
    assert result.public_result.status == "PARTIAL_RESULT"
    assert result.public_result.coverage.complete is False
    assert result.public_result.coverage.unprocessed_count > 0
    binding = result.inference_binding
    assert_two_calls(binding, client)
    assert binding["outcome"] == "PARTIAL_RESULT"
    assert expected in {issue["category"] for issue in binding["calls"][0]["validation_errors"]}


@pytest.mark.asyncio
async def test_a_candidate_rejected_by_full_revalidation_cannot_be_returned(monkeypatch):
    original_validate = inference.proposal_from_draft
    rejected_candidates = []

    def require_full_revalidation(source, draft, *, allow_partial=False):
        result = original_validate(source, draft, allow_partial=allow_partial)
        candidate = [item.atomic_place_name for item in result.mentions] == NAMES and all(
            item.start_time is None and item.end_time is None and item.visit_duration_minutes is None
            for item in result.mentions
        )
        if candidate:
            # Reject the same cleaned result at both strict and partial
            # validation boundaries, so partial mode cannot bypass rejection.
            rejected_candidates.append((allow_partial, copy.deepcopy(result.model_dump())))
            raise inference.SourceAnchorValidationError([
                {"field": "activities[0].place_name", "category": "PLACE_NOT_IN_SOURCE_QUOTE"},
            ])
        return result

    monkeypatch.setattr(inference, "proposal_from_draft", require_full_revalidation)
    client = client_for(bad_timing_payload(), bad_quote_payload())
    from tests.test_semantic_day_sections import RecordingPlaces

    result = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(SOURCE)
    assert rejected_candidates, "The cleaned candidate must pass through full validation before it can be saved."
    assert any(partial for partial, _candidate in rejected_candidates)
    assert any(not partial for partial, _candidate in rejected_candidates)
    # A rejected candidate cannot reappear through a partial-validation route.
    # Independently valid other items remain useful under the current contract.
    assert [item.atomic_place_name for item in result.proposal.mentions] == NAMES[1:]
    assert [card.name for day in result.public_result.days for card in day.activities] == NAMES[1:]
    assert result.public_result.coverage.complete is False
    assert result.public_result.coverage.unprocessed_count > 0
    assert result.inference_binding["outcome"] == "PARTIAL_RESULT"
    assert_two_calls(result.inference_binding, client)


@pytest.mark.asyncio
async def test_valid_second_repair_is_complete_after_confirmed_place_readback():
    from tests.test_semantic_day_sections import RecordingPlaces

    client = client_for(bad_timing_payload(), valid_payload())
    result = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(SOURCE)
    assert [card.name for day in result.public_result.days for card in day.activities] == NAMES
    assert result.public_result.coverage.confirmed_place_count == 3
    assert result.public_result.coverage.complete is True
    assert result.proposal.unprocessed_count == 0
    assert all(item.category_hint == "景点" and item.start_time is None for item in result.proposal.mentions)
    assert result.inference_binding["fallback_used"] is False
    assert_two_calls(result.inference_binding, client)


@pytest.mark.asyncio
async def test_partial_repair_keeps_a_restored_first_stop_before_the_new_second_stop():
    from tests.test_semantic_day_sections import RecordingPlaces

    first = bad_timing_payload()
    first["activities"].pop(1)
    client = client_for(first, bad_quote_payload())
    result = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(SOURCE)
    assert [card.name for day in result.public_result.days for card in day.activities] == NAMES
    assert [item.day_index for item in result.proposal.mentions] == [1, 1, 2]
    assert all(item.role.value == "PLANNED" for item in result.proposal.mentions)
    assert all(SOURCE[item.span_start:item.span_end] == item.atomic_place_name for item in result.proposal.mentions)
    assert result.public_result.coverage.confirmed_place_count == 3
    assert result.public_result.coverage.complete is False
    assert result.inference_binding["outcome"] == "PARTIAL_RESULT"
    assert_two_calls(result.inference_binding, client)


def test_same_day_revisit_cannot_replace_a_broken_quote_by_name_alone():
    from app.trip_understanding.semantic_recovery import merge_preserved_activities

    source = "Day1：云岭书院，星河公园，再访云岭书院。"
    original = inference.SemanticDraft.model_validate({"activities": [
        dict(source_quote="云岭书院", place_name="云岭书院", occurrence=1, day_index=1, role="PLANNED"),
        dict(source_quote="星河公园", place_name="星河公园", day_index=1, role="PLANNED"),
        dict(source_quote="云岭书院", place_name="云岭书院", occurrence=2, day_index=1, role="PLANNED"),
    ]})
    validated = inference.proposal_from_draft(source, original)
    repaired = original.model_copy(deep=True)
    repaired.activities[0] = repaired.activities[0].model_copy(update={"source_quote": "不存在的引文"})
    merged = merge_preserved_activities(source, original, validated, repaired)
    # Keep the unidentified repair row untrusted. Preserve both original visits
    # with their own anchors instead of assigning either one to that row.
    assert merged.activities[0].source_quote == "不存在的引文"
    known = [item for item in merged.activities if item.source_quote == "云岭书院"]
    assert [item.occurrence for item in known] == [1, 2]


@pytest.mark.asyncio
async def test_time_repair_cannot_change_a_previously_specific_category_day_or_role():
    first = bad_timing_payload()
    first["activities"][0]["category"] = "景点"
    second = valid_payload()
    second["activities"][0].update(category="餐饮", role="OPTIONAL", day_index=2)
    client = client_for(first, second)
    result = await provider(client).propose(SOURCE)
    kept = result.mentions[0]
    assert (kept.atomic_place_name, kept.category_hint, kept.day_index, kept.role.value) == (
        "云岭书院", "景点", 1, "PLANNED")
    assert [item.atomic_place_name for item in result.mentions] == NAMES
    assert_two_calls(result.binding, client)
