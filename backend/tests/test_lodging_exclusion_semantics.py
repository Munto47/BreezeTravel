"""Explicit hotel replacement is source bound to a concrete hotel and nights."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.repository import _persisted_proposal
from tests.test_experience_inference import Client, provider
from tests.test_semantic_partial_recovery import activity


def hotel(**fields):
    return activity("星河酒店", category="住宿", lodging_event="CHECK_OUT",
        lodging_evidence="上午从星河酒店退房", **fields)


def draft(row):
    return {"activities": [row, activity("月光桥"), activity("晨光湖", 2), activity("晚霞公园", 3)]}


def source(clause):
    return f"Day1：{clause}。游览月光桥。Day2：晨光湖。Day3：晚霞公园。"


@pytest.mark.parametrize("clause,nights", [
    ("上午从星河酒店退房，今晚另找一家酒店", [1]),
    ("上午从星河酒店退房。今晚另找一家酒店", [1]),
    ("上午从星河酒店退房。\n今晚另找一家酒店", [1]),
    ("上午从星河酒店退房。第二晚另找一家酒店", [2]),
    ("上午从星河酒店退房。第1至2晚另找一家酒店", [1, 2]),
    ("上午从星河酒店退房。今晚不再住原店，另找一家酒店", [1]),
])
def test_only_source_bound_same_day_or_numbered_nights_are_retained(clause, nights):
    result = proposal_from_draft(source(clause), SemanticDraft.model_validate(draft(hotel(
        lodging_excluded_nights=nights, lodging_exclusion_evidence=clause))))
    mention = result.mentions[0]
    assert mention.lodging_excluded_nights == nights
    assert not mention.lodging_role_uncertain and result.unprocessed_count == 0
    assert mention.lodging_event == "CHECK_OUT" and mention.day_index == 1
    assert source(clause)[mention.lodging_exclusion_evidence_start:mention.lodging_exclusion_evidence_end] == clause


@pytest.mark.parametrize("tail,nights", [
    ("然后出门游览", [1]),
    ("如果不满意，今晚另找一家酒店", [1]),
    ("今晚考虑另找一家酒店", [1]),
    ("今晚不再另找一家酒店", [1]),
    ("今晚不会另找一家酒店", [1]),
    ("今晚没有必要另找一家酒店", [1]),
    ("第二晚另找一家酒店", [1]),
    ("今晚另找一家酒店", [2]),
    ("明晚另找一家酒店", [1]),
    ("今晚另找一家餐厅", [1]),
])
def test_checkout_conditions_negation_and_wrong_nights_cannot_create_an_exclusion(tail, nights):
    clause = "上午从星河酒店退房。" + tail
    result = proposal_from_draft(source(clause), SemanticDraft.model_validate(draft(hotel(
        lodging_excluded_nights=nights, lodging_exclusion_evidence=clause))))
    mention = result.mentions[0]
    assert mention.lodging_excluded_nights == [] and mention.lodging_role_uncertain
    assert mention.lodging_event == "CHECK_OUT" and result.unprocessed_count == 1
    assert mention.atomic_place_name == "星河酒店"


@pytest.mark.parametrize("evidence", ["月光酒店退房，今晚另找一家酒店", "星河酒店", "Day2：星河酒店，今晚另找一家酒店"])
def test_evidence_cannot_borrow_another_hotel_or_occurrence(evidence):
    text = "Day1：上午从星河酒店退房。月光酒店退房，今晚另找一家酒店。Day2：星河酒店，今晚另找一家酒店。"
    result = proposal_from_draft(text, SemanticDraft.model_validate({"activities": [hotel(
        lodging_excluded_nights=[1], lodging_exclusion_evidence=evidence)]}))
    assert result.mentions[0].lodging_excluded_nights == [] and result.mentions[0].lodging_role_uncertain


@pytest.mark.asyncio
@pytest.mark.parametrize("tail", ["然后出门游览", "如果不满意，今晚另找一家酒店", "今晚不会另找一家酒店", "今晚另找一家餐厅"])
async def test_ordinary_checkout_or_unselected_change_has_no_exclusion_and_no_extra_call(tail):
    client = Client(json.dumps(draft(hotel())))
    result = await provider(client).propose(source("上午从星河酒店退房。" + tail))
    assert result.mentions[0].lodging_excluded_nights == [] and not result.mentions[0].lodging_role_uncertain
    assert len(client.calls) == 1 and result.unprocessed_count == 0


@pytest.mark.asyncio
async def test_omitted_cross_sentence_replacement_gets_one_patch_without_rewriting_valid_event_or_sequence():
    clause = "上午从星河酒店退房。今晚另找一家酒店"
    patch = {"index": 0, "lodging_excluded_nights": [1], "lodging_exclusion_evidence": clause,
        "lodging_event": None, "lodging_scope": None, "lodging_evidence": None}
    client = Client(json.dumps(draft(hotel())), json.dumps({"activities": [patch]}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(source(clause))
    mention = output.proposal.mentions[0]
    assert mention.lodging_excluded_nights == [1] and mention.lodging_event == "CHECK_OUT"
    assert not mention.lodging_role_uncertain and output.proposal.unprocessed_count == 0
    assert [(m.atomic_place_name, m.day_index) for m in output.proposal.mentions] == [
        ("星河酒店", 1), ("月光桥", 1), ("晨光湖", 2), ("晚霞公园", 3)]
    assert len(client.calls) == 2 and "今晚另找一家酒店" in client.calls[1]["messages"][1]["content"]
    assert "Day2" not in client.calls[1]["messages"][1]["content"]
    assert output.public_result.days[0].activities[0].lodging_excluded_nights == [1]
    assert any(c.claim_type == "ROLE" and c.quote == clause for c in output.claims)
    retained = _persisted_proposal(output)
    assert retained["structure"][0]["lodging_excluded_nights"] == [1]
    assert "lodging_exclusion_evidence" not in json.dumps(retained)
    assert clause not in output.public_result.model_dump_json() and clause not in json.dumps(retained, ensure_ascii=False)


@pytest.mark.asyncio
async def test_missing_or_invalid_exclusion_patch_keeps_the_hotel_and_pending_intent():
    clause = "上午从星河酒店退房。今晚另找一家酒店"
    client = Client(json.dumps(draft(hotel())), json.dumps({"activities": []}))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(source(clause))
    hotel_card = output.public_result.days[0].activities[0]
    assert len(client.calls) == 2 and hotel_card.name == "星河酒店" and hotel_card.lodging_event == "CHECK_OUT"
    assert hotel_card.lodging_excluded_nights == [] and hotel_card.lodging_role_uncertain
    assert output.proposal.unprocessed_count == 1 and not output.public_result.coverage.complete
    assert output.public_result.coverage.unclassified_mention_count == 0


@pytest.mark.asyncio
async def test_exclusion_patch_preserves_already_verified_whole_trip_scope():
    text = "全程住星河酒店。第二晚另找一家酒店。Day1：月光桥。Day2：晨光湖。Day3：晚霞公园。"
    row = activity("星河酒店", day=None, category="住宿", lodging_event="OVERNIGHT", lodging_scope="WHOLE_TRIP",
        lodging_evidence="全程住星河酒店")
    patch = {"index": 0, "lodging_excluded_nights": [2], "lodging_exclusion_evidence": "全程住星河酒店。第二晚另找一家酒店"}
    client = Client(json.dumps(draft(row)), json.dumps({"activities": [patch]}))
    result = await provider(client).propose(text)
    mention = result.mentions[0]
    assert mention.lodging_scope == "WHOLE_TRIP" and mention.lodging_event == "OVERNIGHT"
    assert mention.lodging_excluded_nights == [2] and not mention.lodging_role_uncertain
    assert result.unprocessed_count == 0 and len(client.calls) == 2


@pytest.mark.asyncio
async def test_explicit_empty_patch_can_remove_an_invalid_conditional_exclusion_without_changing_the_visit():
    clause = "上午从星河酒店退房。如果不满意，今晚另找一家酒店"
    row = hotel(lodging_excluded_nights=[1], lodging_exclusion_evidence=clause)
    client = Client(json.dumps(draft(row)), json.dumps({"activities": [{"index": 0, "lodging_excluded_nights": []}]}))
    result = await provider(client).propose(source(clause))
    assert result.mentions[0].lodging_excluded_nights == [] and not result.mentions[0].lodging_role_uncertain
    assert result.mentions[0].lodging_event == "CHECK_OUT" and result.unprocessed_count == 0
