"""A general repair may fill hotel evidence without rewriting retained facts."""
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.semantic_recovery import improves_only_lodging_evidence, merge_preserved_activities
from tests.test_experience_inference import Client, provider
from tests.test_semantic_partial_recovery import activity


SOURCE = "Day1：晚上入住星河酒店。Day2：先去月光桥，傍晚回星河酒店取行李。"
EVIDENCE = "回星河酒店取行李"


def draft():
    return SemanticDraft.model_validate({"activities": [
        activity("星河酒店", category="住宿", lodging_event="OVERNIGHT", lodging_scope="DAY",
            lodging_evidence="晚上入住星河酒店"),
        activity("月光桥", 2),
        activity("星河酒店", 2, occurrence=2, category="住宿", lodging_event="LUGGAGE_PICKUP"),
    ]})


def merge(original, repaired):
    return merge_preserved_activities(SOURCE, original, proposal_from_draft(SOURCE, original), repaired)


def test_missing_evidence_survives_preservation_and_original_order_restoration():
    original = draft()
    pickup = original.activities[-1].model_copy(update={"lodging_evidence": EVIDENCE})
    repaired = original.model_copy(update={"activities": [pickup, *original.activities[:-1]]})
    result = merge(original, repaired)
    assert result.activities[-1].lodging_evidence == EVIDENCE
    assert [a.model_dump(exclude={"lodging_evidence"}) for a in result.activities] == [
        a.model_dump(exclude={"lodging_evidence"}) for a in original.activities]
    proposal = proposal_from_draft(SOURCE, result)
    assert proposal.mentions[-1].lodging_event == "LUGGAGE_PICKUP"
    assert not proposal.mentions[-1].lodging_role_uncertain
    assert proposal.unprocessed_count == 0


@pytest.mark.parametrize("fields", [
    {"day_index": 1}, {"role": "OPTIONAL"}, {"category": "地点"},
    {"lodging_event": "OVERNIGHT", "lodging_scope": "DAY"},
    {"lodging_evidence": "晚上入住星河酒店"},
    {"lodging_evidence": "星河酒店"},
    {"lodging_evidence": "原文中不存在的取行李证据"},
])
def test_changed_semantics_or_unbound_evidence_cannot_modify_preserved_activity(fields):
    original = draft()
    pickup = original.activities[-1].model_copy(update={"lodging_evidence": EVIDENCE, **fields})
    repaired = original.model_copy(update={"activities": [*original.activities[:-1], pickup]})
    assert merge(original, repaired).activities == original.activities


def test_existing_valid_evidence_is_never_replaced():
    original = draft()
    original.activities[-1].lodging_evidence = EVIDENCE
    pickup = original.activities[-1].model_copy(update={"lodging_evidence": "傍晚回星河酒店取行李"})
    repaired = original.model_copy(update={"activities": [*original.activities[:-1], pickup]})
    assert merge(original, repaired).activities == original.activities


def test_a_missing_action_is_not_inferred_by_the_evidence_merge():
    original = draft()
    original.activities[-1].lodging_event = None
    pickup = original.activities[-1].model_copy(update={"lodging_event": "LUGGAGE_PICKUP", "lodging_evidence": EVIDENCE})
    repaired = original.model_copy(update={"activities": [*original.activities[:-1], pickup]})
    assert merge(original, repaired).activities == original.activities


@pytest.mark.asyncio
async def test_source_repair_keeps_completed_pickup_evidence_but_does_not_resolve_hotel_pronoun():
    source = "Day1：晚上入住星河酒店。Day2：退房后把行李寄存在酒店，先去月光桥，傍晚回星河酒店取行李。"
    original = draft()
    unresolved = activity("星河酒店", 2, source_quote="退房后把行李寄存在酒店", category="住宿", lodging_event="CHECK_OUT")
    first = original.model_dump()
    first["activities"].insert(1, unresolved)
    second = json.loads(json.dumps(first))
    second["activities"][-1]["lodging_evidence"] = EVIDENCE
    second["activities"][1]["lodging_evidence"] = "退房后把行李寄存在酒店"
    client = Client(json.dumps(first), json.dumps(second))
    result = await provider(client).propose(source)
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [
        ("星河酒店", 1), ("月光桥", 2), ("星河酒店", 2)]
    assert result.mentions[-1].lodging_event == "LUGGAGE_PICKUP"
    assert not result.mentions[-1].lodging_role_uncertain
    assert result.unprocessed_count == 1
    assert len(client.calls) == 2


@pytest.mark.parametrize("quote", ["回星河酒店取行李", "傍晚回星河酒店取行李"])
@pytest.mark.asyncio
async def test_explicit_pickup_quote_can_supply_its_own_missing_evidence_without_another_call(quote):
    source = f"Day1：{quote}。"
    payload = {"activities": [activity("星河酒店", source_quote=quote, category="住宿", lodging_event="LUGGAGE_PICKUP")]}
    client = Client(json.dumps(payload), json.dumps({"activities": []}))
    result = await provider(client).propose(source)
    assert result.mentions[0].lodging_event == "LUGGAGE_PICKUP"
    assert not result.mentions[0].lodging_role_uncertain
    assert result.mentions[0].lodging_evidence == quote
    assert result.unprocessed_count == 0
    assert len(client.calls) == 1


@pytest.mark.parametrize("source,quote,event", [
    ("Day1：不回星河酒店取行李。", "回星河酒店取行李", "LUGGAGE_PICKUP"),
    ("Day1：如果下雨，回星河酒店取行李。", "回星河酒店取行李", "LUGGAGE_PICKUP"),
    ("Day1：可回星河酒店取行李。", "回星河酒店取行李", "LUGGAGE_PICKUP"),
    ("Day1：只有下雨才回星河酒店取行李。", "回星河酒店取行李", "LUGGAGE_PICKUP"),
    ("Day1：有时间就回星河酒店取行李。", "回星河酒店取行李", "LUGGAGE_PICKUP"),
    ("Day1：星河酒店。", "星河酒店", "LUGGAGE_PICKUP"),
    ("Day1：从星河酒店退房。", "从星河酒店退房", "LUGGAGE_PICKUP"),
    ("Day1：回星河酒店取行李。", "回星河酒店取行李", "OVERNIGHT"),
    ("Day1：星河酒店。Day2：回星河酒店取行李。", "星河酒店", "LUGGAGE_PICKUP"),
])
def test_quote_fallback_does_not_infer_an_action_from_negation_conditions_or_other_occurrences(source, quote, event):
    value = SemanticDraft.model_validate({"activities": [activity("星河酒店", source_quote=quote,
        category="住宿", lodging_event=event)]})
    result = proposal_from_draft(source, value)
    assert result.mentions[0].lodging_event is None
    assert result.mentions[0].lodging_role_uncertain


def test_recovery_selection_cannot_improve_by_dropping_a_failed_source_item():
    original = draft()
    original.activities.insert(1, original.activities[0].model_copy(update={"source_quote": "不存在的引用"}))
    before = proposal_from_draft(SOURCE, original, allow_partial=True)
    repaired = draft()
    repaired.activities[-1].lodging_evidence = EVIDENCE
    after = proposal_from_draft(SOURCE, repaired)
    assert after.unprocessed_count < before.unprocessed_count
    assert not improves_only_lodging_evidence(before, after)


def test_recovery_selection_cannot_trade_an_existing_day_for_completed_lodging_evidence():
    original = draft()
    before = proposal_from_draft(SOURCE, original)
    repaired = draft()
    repaired.activities[-1].lodging_evidence = EVIDENCE
    after = proposal_from_draft(SOURCE, repaired)
    after.mentions[1].day_index = 1
    assert not improves_only_lodging_evidence(before, after)
