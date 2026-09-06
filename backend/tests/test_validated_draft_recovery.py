"""A failed timing repair may use only an independently revalidated place draft."""
from __future__ import annotations

import copy
import json

import pytest

from app.trip_understanding import experience_inference as inference
from app.trip_understanding.errors import InferenceProviderUnavailableError
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
async def test_a_first_draft_with_place_or_role_errors_is_never_a_recovery_candidate(defect):
    source = SOURCE
    first = bad_timing_payload()
    if defect == "missing_place":
        first["activities"].pop(1)
        expected = "MISSING_EXPLICIT_PARALLEL_PLACE"
    else:
        source += "最终修改为：云岭书院取消。"
        expected = "EXPLICIT_CANCELLATION_CONFLICT"
    client = client_for(first, bad_quote_payload())
    with pytest.raises(InferenceProviderUnavailableError) as error:
        await provider(client).propose(source)
    binding = error.value.provider_binding
    assert_two_calls(binding, client)
    assert binding["fallback_used"] is False
    assert expected in {issue["category"] for issue in binding["calls"][0]["validation_errors"]}


@pytest.mark.asyncio
async def test_a_candidate_rejected_by_full_revalidation_cannot_be_returned(monkeypatch):
    original_validate = inference.proposal_from_draft
    rejected_candidates = []

    def require_full_revalidation(source, draft):
        candidate = [item.source_quote for item in draft.activities] == NAMES and all(
            item.start_time is None and item.end_time is None and item.visit_duration_minutes is None
            for item in draft.activities
        )
        if candidate:
            # Fault injection at the full-validation boundary: clearing time
            # alone cannot certify a candidate that this boundary rejects.
            rejected_candidates.append(copy.deepcopy(draft.model_dump()))
            raise inference.SourceAnchorValidationError([
                {"field": "activities[0].place_name", "category": "PLACE_NOT_IN_SOURCE_QUOTE"},
            ])
        return original_validate(source, draft)

    monkeypatch.setattr(inference, "proposal_from_draft", require_full_revalidation)
    client = client_for(bad_timing_payload(), bad_quote_payload())
    with pytest.raises(InferenceProviderUnavailableError) as error:
        await provider(client).propose(SOURCE)
    assert rejected_candidates, "The cleaned candidate must pass through full validation before it can be saved."
    assert error.value.provider_binding["fallback_used"] is False
    assert_two_calls(error.value.provider_binding, client)
