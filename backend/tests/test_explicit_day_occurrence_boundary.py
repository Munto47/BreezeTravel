"""Explicit day boundaries; saved live answers, synthetic identity responses only."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft, SourceAnchorValidationError, _align_named_day_occurrences,
    _proposal_from_live_draft,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_live_name_field_contract import Client
from tests.test_semantic_day_sections import RecordingPlaces
from tests.test_semantic_supplement_budget import provider


def activity(quote, day, *, name=None, role="PLANNED", **extra):
    return dict(source_quote=quote, place_name=name, day_index=day, role=role, **extra)


def draft(*activities):
    return SemanticDraft.model_validate(dict(destination="成都", activities=activities))


@pytest.mark.asyncio
async def test_saved_shenzhen_answers_keep_two_days_and_correct_anonymous_occurrences():
    # Real raw strings are shared with the request-serialization regression.
    # Neither answer is repaired in this fixture; known omissions must remain.
    fixture = json.loads((Path(__file__).parent / "fixtures/live_shenzhen_name_repair.json").read_text(encoding="utf-8"))
    source = fixture["source"]
    client = Client(*fixture["responses"])
    places = RecordingPlaces()
    result = await TripUnderstandingPipeline(provider(client), places).run(source)
    assert result.proposal.day_count == len(result.public_result.days) == 2
    assert [[card.name for card in day.activities] for day in result.public_result.days] == [["莲花山公园"], []]
    assert places.calls == [("深圳", "莲花山公园")]
    meals = [m for m in result.proposal.mentions if m.meal_role == "LUNCH"]
    assert [(m.day_index, m.span_start) for m in meals] == [
        (1, source.index("午餐")), (2, source.rindex("午餐")),
    ]
    assert all(source[m.span_start:m.span_end] == m.raw_text for m in result.proposal.mentions)
    assert [len(day.meal_slots) for day in result.public_result.days] == [1, 1]
    assert any(d.field == "activities[5].day_index" and d.category == "SOURCE_DAY_QUOTE_MISMATCH"
               for d in result.proposal.diagnostics)
    assert any(d.category == "KNOWN_PLACE_UNCLASSIFIED" and source[d.span_start:d.span_end] == "深圳博物馆历史民俗馆"
               for d in result.proposal.diagnostics)
    assert any(d.category == "SOURCE_VISITS_UNPROCESSED" for d in result.proposal.diagnostics)
    assert result.public_result.coverage.complete is False
    assert result.public_result.coverage.unprocessed_count > 0
    assert len(client.calls) == result.inference_binding["external_calls"] == 2


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL"])
def test_extra_day_is_a_source_error_without_erasing_valid_days(role):
    source = "成都两日游。\nDay1\n望江楼公园。\nDay2\n金沙遗址博物馆。"
    original = draft(activity("望江楼公园", 1, name="望江楼公园"),
                     activity("金沙遗址博物馆", 2, name="金沙遗址博物馆"),
                     activity("望江楼公园", 3, name="望江楼公园", role=role))
    with pytest.raises(SourceAnchorValidationError, match="SOURCE_DAY_QUOTE_MISMATCH"):
        _proposal_from_live_draft(source, original)
    partial = _proposal_from_live_draft(source, original, allow_partial=True)
    assert partial.day_count == 2
    assert [(m.atomic_place_name, m.day_index) for m in partial.mentions] == [
        ("望江楼公园", 1), ("金沙遗址博物馆", 2),
    ]
    assert partial.unprocessed_count == 1
    assert partial.diagnostics[0].field == "activities[2].day_index"
    assert original.activities[2].day_index == 3  # No guessed correction.


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"])
def test_independent_repeated_lunch_binds_only_unique_quote_inside_given_day(line_ending):
    source = line_ending.join(["成都两日游。", "Day1", "望江楼公园后午餐待选。", "Day2", "金沙遗址博物馆后午餐待选。"])
    original = draft(activity("午餐待选", 1, category="餐饮", meal_role="LUNCH"),
                     activity("午餐待选", 2, category="餐饮", meal_role="LUNCH"))
    proposal = _proposal_from_live_draft(source, original)
    assert [(m.day_index, m.span_start, m.raw_text) for m in proposal.mentions] == [
        (1, source.index("午餐待选"), "午餐待选"),
        (2, source.rindex("午餐待选"), "午餐待选"),
    ]
    assert proposal.unprocessed_count == 0
    assert original.activities[1].occurrence == 1  # A copy is rebound.


@pytest.mark.asyncio
async def test_independent_two_day_meals_keep_both_real_neighbor_anchors():
    source = ("成都两日游。\nDay1\n上午望江楼公园，中午午餐待选，下午人民公园。"
              "\nDay2\n上午金沙遗址博物馆，中午午餐待选，下午浣花溪公园。")
    first = draft(activity("望江楼公园", 1, name="望江楼公园"),
                  activity("午餐待选", 1, category="餐饮", meal_role="LUNCH"),
                  activity("人民公园", 1, name="人民公园"),
                  activity("金沙遗址博物馆", 2, name="金沙遗址博物馆"),
                  activity("午餐待选", 2, category="餐饮", meal_role="LUNCH"),
                  activity("浣花溪公园", 2, name="浣花溪公园"))
    client = Client(first.model_dump_json())
    result = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(source)
    assert [[card.name for card in day.activities] for day in result.public_result.days] == [
        ["望江楼公园", "人民公园"], ["金沙遗址博物馆", "浣花溪公园"],
    ]
    for day in result.public_result.days:
        assert len(day.meal_slots) == 1
        slot = day.meal_slots[0]
        assert slot.meal_role == "LUNCH"
        assert slot.after_activity_token == day.activities[0].activity_token
        assert slot.before_activity_token == day.activities[1].activity_token
    meals = [m for m in result.proposal.mentions if m.meal_role]
    assert [m.span_start for m in meals] == [source.index("午餐待选"), source.rindex("午餐待选")]
    assert len(client.calls) == 1
    assert result.public_result.coverage.complete is True


@pytest.mark.parametrize("target_body", ["中午休息。", "午餐待选，若改时段则另一次午餐待选。"])
def test_missing_or_ambiguous_anonymous_day_quote_is_pending_not_borrowed(target_body):
    source = "Day1\n午餐待选。\nDay2\n" + target_body
    original = draft(activity("午餐待选", 1, category="餐饮", meal_role="LUNCH"),
                     activity("午餐待选", 2, category="餐饮", meal_role="LUNCH"))
    with pytest.raises(SourceAnchorValidationError, match="SOURCE_DAY_QUOTE_MISMATCH"):
        _proposal_from_live_draft(source, original)
    partial = _proposal_from_live_draft(source, original, allow_partial=True)
    assert [(m.day_index, m.span_start) for m in partial.mentions] == [(1, source.index("午餐待选"))]
    assert partial.unprocessed_count == 1
    assert partial.diagnostics[0].field == "activities[1].occurrence"


@pytest.mark.parametrize("source", [
    "Day1\n星河公园。\nDay2\n月光桥。\n更正：星河公园改到第三天。",
    "Day1\n星河公园。\nDay2\n月光桥。\n两天顺序对调。",
    "Day1\n星河公园。\nDay3\n月光桥。",
    "Day1\n星河公园。\nDay2\n月光桥。\nDay1\n星河公园。",
    "Day2\n月光桥。\nDay1\n星河公园。",
    "Day1.5\n星河公园。\nDay2\n月光桥。",
    "三日游。\nDay1\n星河公园。\nDay2\n月光桥。",
])
def test_unclear_or_changed_day_structure_does_not_authorize_rebinding(source):
    original = draft(activity("星河公园", 3, name="星河公园"))
    checked, issues, _hints = _align_named_day_occurrences(source, original)
    assert checked.model_dump() == original.model_dump()
    assert issues == []  # This narrow guard does not interpret unresolved schedules.


def test_same_day_ambiguous_revisits_are_not_selected_or_collapsed():
    source = "Day1\n星河公园。\nDay2\n上午星河公园，晚上再回星河公园。"
    original = draft(activity("星河公园", 1, name="星河公园"),
                     activity("星河公园", 2, name="星河公园"))
    partial = _proposal_from_live_draft(source, original, allow_partial=True)
    assert [(m.day_index, m.span_start) for m in partial.mentions] == [(1, source.index("星河公园"))]
    assert partial.unprocessed_count == 1


def test_explicit_same_day_repeat_occurrences_keep_both_in_model_order():
    source = "Day1\n星河公园。\nDay2\n上午星河公园，晚上再回星河公园。"
    original = draft(activity("星河公园", 2, name="星河公园", occurrence=3),
                     activity("星河公园", 2, name="星河公园", occurrence=2))
    result = _proposal_from_live_draft(source, original)
    assert [m.span_start for m in result.mentions] == [source.rindex("星河公园"), source.index("星河公园", source.index("Day2"))]
    assert result.unprocessed_count == 0
