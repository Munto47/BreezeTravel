"""Literal gate actions remain instructions without guessing a gate's name."""
import json
from pathlib import Path

import pytest

from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client, provider
from tests.test_source_visit_supplement import apply, plan, row


@pytest.mark.asyncio
async def test_saved_shenzhen_actions_keep_gates_without_fixing_bad_self_parent_or_pickup():
    fixture = json.loads((Path(__file__).parent / "fixtures/live_shenzhen_gate_actions.json").read_text(encoding="utf-8"))
    client = Client(*fixture["responses"])
    result = await TripUnderstandingPipeline(provider(client), FixedReplayPlaces()).run(fixture["source"])
    assert len(client.calls) == 2
    assert [[card.name for card in day.activities] for day in result.public_result.days] == [
        ["莲花山公园", "深圳市当代艺术与城市规划馆"],
        ["深圳博物馆历史民俗馆", "莲花山公园"],
    ]
    assert result.resolution_receipt["attempted_count"] == 4
    parents = [mention for mention in result.proposal.mentions if not mention.parent_mention_id]
    assert [(m.day_index, m.sequence_index, m.atomic_place_name, m.meal_role) for m in parents] == [
        (1, 0, "莲花山公园", None), (1, 1, None, "LUNCH"),
        (1, 2, "深圳市当代艺术与城市规划馆", None),
        (2, 0, "深圳博物馆历史民俗馆", None), (2, 1, None, "LUNCH"),
        (2, 2, "莲花山公园", None),
    ]
    for day in result.public_result.days:
        assert len(day.meal_slots) == 1
        slot = day.meal_slots[0]
        assert slot.meal_role == "LUNCH"
        assert slot.after_activity_token == day.activities[0].activity_token
        assert slot.before_activity_token == day.activities[1].activity_token
    assert [detail.model_dump() for detail in result.public_result.days[0].activities[0].source_details] == [
        {"name": "入口：从南门进", "optional": False},
        {"name": "风筝广场", "optional": False},
        {"name": "邓小平雕像", "optional": False},
        {"name": "桃花林", "optional": True},
        {"name": "出口：从南门出", "optional": False},
    ]
    assert [d.name for d in result.public_result.days[0].activities[1].source_details] == ["仅看外观，不入内部"]
    assert all(not card.source_details for card in result.public_result.days[1].activities)
    assert [d.field for d in result.proposal.diagnostics if d.category == "SOURCE_VISIT_UNRESOLVED"] == [
        "source_visits[6]", "source_visits[7]",
    ]
    assert result.proposal.unprocessed_count == 2
    assert result.public_result.coverage.complete is False
    for mention in result.proposal.mentions:
        assert fixture["source"][mention.span_start:mention.span_end] == mention.raw_text


@pytest.mark.parametrize("prefix", ["从", "由", "经"])
@pytest.mark.parametrize("verb,kind", [("进入", "ENTRY"), ("出去", "EXIT")])
def test_complete_gate_action_preserves_literal_display_instead_of_guessing_name(prefix, verb, kind):
    quote = f"{prefix}东门{verb}"
    source = f"苏州。\nDay1：青岚园，{quote}。"
    before = plan(source, [("青岚园", 1, 1)])
    after = apply(source, before, [row(0, kind, quote, quote)])
    assert after.unprocessed_count == 0
    assert after.mentions[:-1] == before.mentions
    child = after.mentions[-1]
    assert child.atomic_place_name == child.raw_text == quote
    assert source[child.span_start:child.span_end] == quote


@pytest.mark.parametrize("quote,kind", [("从化门进", "ENTRY"), ("从化门出", "EXIT")])
def test_prefix_that_may_be_part_of_the_name_is_never_stripped(quote, kind):
    source = f"苏州。\nDay1：青岚园，{quote}。"
    before = plan(source, [("青岚园", 1, 1)])
    after = apply(source, before, [row(0, kind, quote, quote)])
    assert after.unprocessed_count == 0
    assert after.mentions[-1].atomic_place_name == quote
    assert after.mentions[-1].atomic_place_name != "化门"


@pytest.mark.parametrize("prefix", ["不", "不要", "不再", "并非"])
@pytest.mark.parametrize("quote,kind", [("从东门进", "ENTRY"), ("由西门出", "EXIT")])
def test_full_gate_quote_cannot_hide_same_clause_negation(prefix, quote, kind):
    source = f"苏州。\nDay1：青岚园，{prefix}{quote}。"
    before = plan(source, [("青岚园", 1, 1)])
    after = apply(source, before, [row(0, kind, quote, quote)])
    assert after.mentions == before.mentions and after.unprocessed_count == 1


@pytest.mark.parametrize("quote,kind", [("从不经东门进", "ENTRY"), ("不从西门出", "EXIT"),
    ("从东门出", "ENTRY"), ("由西门进", "EXIT")])
def test_embedded_negation_and_wrong_direction_stay_rejected(quote, kind):
    source = f"苏州。\nDay1：青岚园，{quote}。"
    before = plan(source, [("青岚园", 1, 1)])
    after = apply(source, before, [row(0, kind, quote, quote)])
    assert after.mentions == before.mentions and after.unprocessed_count == 1


@pytest.mark.parametrize("kind,quote", [("ENTRY", "从东门进"), ("EXIT", "经西门出")])
def test_complete_gate_action_keeps_parent_and_repeated_occurrence_boundaries(kind, quote):
    source = f"苏州。\nDay1：青岚园，{quote}。\nDay2：青岚园，{quote}。"
    before = plan(source, [("青岚园", 1, 1), ("青岚园", 2, 2)])
    wrong = apply(source, before, [row(1, kind, quote, quote, occurrence=1)])
    assert wrong.mentions == before.mentions and wrong.unprocessed_count == 1
    nonexistent = apply(source, before, [row(1, kind, quote, quote, occurrence=3)])
    assert nonexistent.mentions == before.mentions and nonexistent.unprocessed_count == 1
    correct = apply(source, before, [row(1, kind, quote, quote, occurrence=2)])
    assert correct.unprocessed_count == 0
    assert correct.mentions[-1].parent_mention_id == before.mentions[1].mention_id
    assert correct.mentions[-1].day_index == 2
    assert correct.mentions[-1].span_start == source.rindex(quote)


def test_complete_gate_action_cannot_attach_to_previous_different_parent():
    source = "苏州。\nDay1：青岚园。随后到听雨园，从东门进。"
    before = plan(source, [("青岚园", 1, 1), ("听雨园", 1, 1)])
    after = apply(source, before, [row(0, "ENTRY", "从东门进", "从东门进")])
    assert after.mentions == before.mentions and after.unprocessed_count == 1


def test_paired_markdown_gate_action_preserves_original_span_and_direction():
    source = "苏州。\nDay1：**青岚园**，从**东门**进。"
    before = plan(source, [("青岚园", 1, 1)])
    after = apply(source, before, [row(0, "ENTRY", "从东门进", "从东门进")])
    assert after.unprocessed_count == 0
    child = after.mentions[-1]
    assert child.atomic_place_name == "从东门进"
    assert child.raw_text == "从**东门**进"
    assert source[child.span_start:child.span_end] == child.raw_text
