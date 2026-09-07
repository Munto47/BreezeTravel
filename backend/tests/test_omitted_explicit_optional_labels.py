"""Original synthetic cases for conservative omitted-option recovery."""

from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import (
    SemanticDraft,
    SourceAnchorValidationError,
    _retain_explicit_optional_labels,
    proposal_from_draft,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline


def _activity(name="星河公园", *, quote=None, day=1, role="PLANNED", **values):
    return {"source_quote": quote or name, "place_name": name,
            "role": role, "day_index": day, "category": "景点", **values}


def _draft(rows):
    return SemanticDraft.model_validate({"destination": "北京", "activities": rows})


@pytest.mark.parametrize("heading,day", [("Day1", 1), ("Day2", 2), ("第2天", 2)])
def test_omitted_literal_option_keeps_source_order_without_copying_time_or_city(heading, day):
    source = f"北京一日游。\n{heading}\n09:00 星河公园，停留30分钟。\nIris咖啡可以打卡。\n青溪博物馆。"
    draft = _draft([
        _activity(day=day, start_time="09:00", visit_duration_minutes=30,
                  timing_source="TEXT", time_evidence="09:00 星河公园，停留30分钟",
                  city="北京", city_evidence="北京一日游"),
        _activity("青溪博物馆", day=day),
    ])
    repaired = _retain_explicit_optional_labels(source, draft)
    assert [item.place_name for item in repaired.activities] == ["星河公园", "Iris咖啡", "青溪博物馆"]
    assert repaired.activities[0] == draft.activities[0]
    assert repaired.activities[2] == draft.activities[1]
    assert len(draft.activities) == 2
    added = repaired.activities[1]
    assert (added.source_quote, added.occurrence, added.day_index, added.role.value) == (
        "Iris咖啡", 1, day, "OPTIONAL",
    )
    assert added.city is None and added.city_evidence is None
    assert added.start_time is None and added.end_time is None
    assert added.visit_duration_minutes is None and added.time_evidence is None
    assert added.timing_source == "UNSPECIFIED" and not added.locked and not added.fixed_commitment


@pytest.mark.asyncio
async def test_recovered_option_is_visible_with_literal_evidence_and_zero_place_queries():
    source = "Day1\n星河公园。\nIris咖啡可以打卡。"
    draft = _draft([_activity(role="OPTIONAL")])

    class Inference:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    class NoSearch:
        async def resolve(self, **values):
            raise AssertionError("An optional activity must never query a place")

    output = await TripUnderstandingPipeline(Inference(), NoSearch()).run(source)
    assert [mention.atomic_place_name for mention in output.proposal.mentions] == ["星河公园", "Iris咖啡"]
    assert all(not activity.compiled.eligible_for_place_search for activity in output.activities)
    added = output.proposal.mentions[1]
    assert source[added.span_start:added.span_end] == added.raw_text == "Iris咖啡"
    assert added.role.value == "OPTIONAL" and added.day_index == 1
    assert added.city_hint is None and added.start_time is None
    assert output.public_result.days[0].activities == []
    assert [choice.name for choice in output.public_result.days[0].alternatives] == ["星河公园", "Iris咖啡"]


@pytest.mark.parametrize("tail", [
    "资料：云岭公园可以打卡。",
    "“云岭公园可以打卡。”",
    "> 可以去云岭公园。",
    "原文：\n可以去云岭公园。",
    "```text\n可以去云岭公园。\n```",
    "参考：\n云岭公园可以打卡。",
    "美式咖啡可以打卡。",
    "冷萃咖啡可以打卡。",
    "不要去云岭公园。",
    "云岭公园可以打卡，但这次不去。",
    "取消安排。\n云岭公园可以打卡。",
    "已选方案A。\n云岭公园可以打卡。",
    "最终决定只去星河公园。\n云岭公园可以打卡。",
    "更正：移到第三天。\n云岭公园可以打卡。",
])
def test_descriptions_drinks_negations_references_and_settled_plans_do_not_add_options(tail):
    source = "Day1\n星河公园。\n" + tail
    draft = _draft([_activity()])
    assert _retain_explicit_optional_labels(source, draft) == draft
    try:
        proposal = proposal_from_draft(source, draft)
    except SourceAnchorValidationError:
        pass  # An incomplete draft may request repair; it must not invent an option.
    else:
        assert [mention.atomic_place_name for mention in proposal.mentions] == ["星河公园"]


@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL", "REFERENCE", "EXCLUDED", "PASS_THROUGH"])
def test_an_existing_identical_name_in_any_role_is_not_duplicated(role):
    source = "Day1\n星河公园。\nIris咖啡可以打卡。"
    draft = _draft([_activity(), _activity("Iris咖啡", role=role)])
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("claim", ["Iris咖啡可以打卡", "Iris咖啡", "Iris"])
def test_an_existing_full_or_partial_source_claim_prevents_recovery(claim):
    source = "Day1\n星河公园。\nIris咖啡可以打卡。"
    draft = _draft([_activity(), _activity(None, quote=claim, role="REFERENCE")])
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("source,rows", [
    ("星河公园。Iris咖啡可以打卡。", [_activity()]),
    ("Day1\n星河公园。\nDay2\nIris咖啡可以打卡。", [_activity()]),
    ("Day1\nIris咖啡可以打卡。", []),
    ("Day1\n星河公园。明天可以去Iris咖啡。", [_activity()]),
    ("Day1\n星河公园。Iris咖啡可以打卡。\nDay2\nIris咖啡。", [_activity()]),
    ("Day1\n星河公园。Iris咖啡可以打卡。青溪博物馆。", [_activity("青溪博物馆"), _activity()]),
    ("Day1\n星河公园。Iris咖啡可以打卡。", [_activity(quote="未出现在原文的引用")]),
])
def test_unknown_cross_day_unclaimed_or_disordered_days_cannot_supply_an_option(source, rows):
    draft = _draft(rows)
    assert _retain_explicit_optional_labels(source, draft) == draft


@pytest.mark.parametrize("count", [159, 160])
def test_recovery_respects_the_activity_limit_without_mutating_the_original(count):
    names = [f"星河{i}公园" for i in range(count)]
    source = "Day1\n" + "。".join(names) + "。\nIris咖啡可以打卡。"
    draft = _draft([_activity(name) for name in names])
    if count == 160:
        with pytest.raises(SourceAnchorValidationError) as error:
            _retain_explicit_optional_labels(source, draft)
        assert error.value.issues == [{"field": "activities", "category": "TOO_MANY_ACTIVITIES"}]
    else:
        repaired = _retain_explicit_optional_labels(source, draft)
        assert len(repaired.activities) == 160
        assert repaired.activities[-1].place_name == "Iris咖啡"
        assert repaired.activities[-1].role.value == "OPTIONAL"
    assert len(draft.activities) == count
