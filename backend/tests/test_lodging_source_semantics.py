"""Hotel event metadata is source bound and never invents nightly visits."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import EvidenceCompiler, TripUnderstandingPipeline
from app.trip_understanding.repository import _persisted_proposal
from tests.test_experience_inference import Client, provider
from tests.test_semantic_partial_recovery import activity


@pytest.mark.parametrize("event,clause", [
    ("CHECK_OUT", "上午在星河酒店退房"),
    ("DEPARTURE", "清晨从星河酒店出发"),
    ("LUGGAGE_PICKUP", "返程前回星河酒店取行李"),
    ("OVERNIGHT", "今晚入住星河酒店"),
])
@pytest.mark.asyncio
async def test_explicit_hotel_events_keep_source_evidence_and_public_metadata(event, clause):
    source = "Day1：" + clause + "。Day2：月光桥。"
    draft = {"activities": [activity("星河酒店", category="住宿", lodging_event=event,
        lodging_scope="DAY" if event == "OVERNIGHT" else None, lodging_evidence=clause), activity("月光桥", 2)]}
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(draft))), ControlledSnapshotPlaceResolver()).run(source)
    mention = output.proposal.mentions[0]
    card = output.public_result.days[0].activities[0]
    assert mention.lodging_event == card.lodging_event == event
    assert card.lodging_scope == ("DAY" if event == "OVERNIGHT" else None)
    assert source[mention.lodging_evidence_start:mention.lodging_evidence_end] == clause
    assert any(claim.claim_type == "ROLE" and claim.quote == clause for claim in output.claims)
    retained = _persisted_proposal(output)
    assert retained["structure"][0]["lodging_event"] == event
    assert "lodging_evidence" not in json.dumps(retained)
    assert clause not in output.public_result.model_dump_json()


def test_repeated_hotel_overnight_and_final_luggage_pickup_remain_separate_events():
    source = "Day1：今晚入住星河酒店。Day2：返程前回星河酒店取行李。"
    draft = SemanticDraft.model_validate({"activities": [
        activity("星河酒店", category="住宿", lodging_event="OVERNIGHT", lodging_scope="DAY", lodging_evidence="今晚入住星河酒店"),
        activity("星河酒店", 2, occurrence=2, category="住宿", lodging_event="LUGGAGE_PICKUP", lodging_evidence="返程前回星河酒店取行李")]})
    result = proposal_from_draft(source, draft)
    assert [(item.day_index, item.lodging_event, item.lodging_scope) for item in result.mentions] == [
        (1, "OVERNIGHT", "DAY"), (2, "LUGGAGE_PICKUP", None)]


def test_whole_trip_hotel_is_one_constraint_but_first_night_is_only_one_night():
    source = "全程住星河酒店。Day1：月光桥。Day2：晨光湖。"
    draft = SemanticDraft.model_validate({"activities": [
        activity("星河酒店", day=None, category="住宿", lodging_event="OVERNIGHT", lodging_scope="WHOLE_TRIP", lodging_evidence="全程住星河酒店"),
        activity("月光桥"), activity("晨光湖", 2)]})
    result = proposal_from_draft(source, draft)
    assert result.unprocessed_count == 0
    assert [(item.atomic_place_name, item.day_index, item.lodging_scope) for item in result.mentions] == [
        ("星河酒店", 1, "WHOLE_TRIP"), ("月光桥", 1, None), ("晨光湖", 2, None)]
    assert len(EvidenceCompiler().compile(source, result)[0]) == 3


@pytest.mark.parametrize("evidence", [None, "星河酒店", "第二晚入住星河酒店", "今晚入住其他酒店"])
def test_invalid_event_metadata_does_not_drop_valid_hotel_or_borrow_another_occurrence(evidence):
    source = "Day1：星河酒店。Day2：第二晚入住星河酒店。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河酒店", category="住宿",
        lodging_event="OVERNIGHT", lodging_scope="WHOLE_TRIP", lodging_evidence=evidence)]})
    result = proposal_from_draft(source, draft)
    assert result.mentions[0].atomic_place_name == "星河酒店"
    assert result.mentions[0].lodging_event is result.mentions[0].lodging_scope is None
    assert any(issue.category == "LODGING_EVIDENCE_SCOPE_MISMATCH" for issue in result.diagnostics)
    assert result.unprocessed_count == 1


@pytest.mark.parametrize("role", ["OPTIONAL", "EXCLUDED", "REFERENCE"])
def test_unselected_hotel_can_never_become_a_whole_trip_stay_constraint(role):
    source = "Day1：不住星河酒店，可能改别处。"
    draft = SemanticDraft.model_validate({"activities": [activity("星河酒店", category="住宿", role=role,
        lodging_event="OVERNIGHT", lodging_scope="WHOLE_TRIP", lodging_evidence="不住星河酒店")]})
    result = proposal_from_draft(source, draft)
    assert result.mentions[0].lodging_event is result.mentions[0].lodging_scope is None


def test_hotel_name_alone_has_no_inferred_event_or_scope():
    source = "Day1：星河酒店。"
    result = proposal_from_draft(source, SemanticDraft.model_validate({"activities": [activity("星河酒店", category="住宿")]}))
    assert result.mentions[0].lodging_event is result.mentions[0].lodging_scope is None
    assert result.mentions[0].lodging_role_uncertain and result.unprocessed_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["星河酒店", "全程住星河酒店"])
async def test_trip_header_hotel_is_not_reanchored_to_later_luggage_visit(quote):
    source = "北京三日游。全程住星河酒店。\nDay1：月光桥。\nDay2：晨光湖。\nDay3：晚霞公园，回星河酒店取行李。"
    draft = {"activities": [
        activity("星河酒店", source_quote=quote, category="住宿", lodging_event="OVERNIGHT", lodging_scope="WHOLE_TRIP", lodging_evidence="全程住星河酒店"),
        activity("月光桥"), activity("晨光湖", 2), activity("晚霞公园", 3),
        activity("星河酒店", 3, source_quote="星河酒店取行李", category="住宿", lodging_event="LUGGAGE_PICKUP", lodging_evidence="回星河酒店取行李")]}
    client = Client(json.dumps(draft))
    result = await provider(client).propose(source)
    hotels = [item for item in result.mentions if item.category_hint == "住宿"]
    assert [(item.day_index, item.lodging_event) for item in hotels] == [(1, "OVERNIGHT"), (3, "LUGGAGE_PICKUP")]
    assert hotels[0].span_end < hotels[1].span_start
    assert len(client.calls) == 1 and result.unprocessed_count == 0


@pytest.mark.asyncio
async def test_explicit_lodging_gap_has_no_fake_hotel_card_or_missing_event_diagnostic():
    source = "北京一日游。Day1：故宫博物院，今晚酒店未确定。"
    draft = {"destination": "北京", "activities": [activity("故宫博物院", category="景点"),
        activity("今晚酒店未确定", place_name=None, category="住宿", lodging_event="OVERNIGHT", lodging_scope="DAY")]}
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(draft))), ControlledSnapshotPlaceResolver()).run(source)
    assert [card.name for card in output.public_result.days[0].activities] == ["故宫博物院"]
    assert output.proposal.mentions[1].atomic_place_name is None
    assert output.proposal.unprocessed_count == 0
    assert output.public_result.coverage.complete
