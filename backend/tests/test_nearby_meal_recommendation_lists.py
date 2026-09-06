"""Nearby restaurant suggestions do not become mandatory food stops."""

from __future__ import annotations

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline


FOOD_NAMES = ["青溪餐厅", "望星面馆"]


def _activity(name, *, quote=None, occurrence=1, category="餐饮", **values):
    return {"source_quote": quote or name, "place_name": name, "role": "PLANNED",
            "day_index": 1, "occurrence": occurrence, "category": category, **values}


async def _run(clause, rows):
    source = "北京一日游。\nDay1\n" + clause
    draft = SemanticDraft.model_validate({"destination": "北京", "activities": rows})

    class Inference:
        async def propose(self, text):
            return proposal_from_draft(text, draft)

    class Resolver:
        def __init__(self):
            self.calls = []

        async def resolve(self, **values):
            self.calls.append(values["atomic_place_name"])
            return None

    resolver = Resolver()
    output = await TripUnderstandingPipeline(Inference(), resolver).run(source)
    return source, output, resolver.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("meal", ["午饭", "午餐", "晚餐"])
@pytest.mark.parametrize("prefix", ["", "13:00 "])
@pytest.mark.parametrize("bundled", [False, True])
async def test_nearby_recommendation_list_is_source_bound_reference_with_zero_queries(meal, prefix, bundled):
    names = "、".join(FOOD_NAMES)
    rows = [_activity(names)] if bundled else [_activity(name) for name in FOOD_NAMES]
    source, output, queries = await _run(f"{prefix}{meal}：附近{names}，平价本地口味。", rows)
    assert queries == []
    assert [mention.atomic_place_name for mention in output.proposal.mentions] == FOOD_NAMES
    assert all(mention.role.value == "REFERENCE" for mention in output.proposal.mentions)
    assert all(not activity.compiled.eligible_for_place_search for activity in output.activities)
    assert all(source[mention.span_start:mention.span_end] == mention.raw_text
               for mention in output.proposal.mentions)
    assert all(day.activities == [] and day.alternatives == [] for day in output.public_result.days)


@pytest.mark.asyncio
async def test_an_existing_unnamed_meal_survives_without_turning_suggestions_into_visits():
    clause = "13:00 午饭：附近青溪餐厅、望星面馆，平价本地口味。"
    rows = [_activity(None, quote="午饭"), *[_activity(name) for name in FOOD_NAMES]]
    _, output, queries = await _run(clause, rows)
    assert queries == []
    assert [(mention.atomic_place_name, mention.role.value) for mention in output.proposal.mentions] == [
        (None, "PLANNED"), ("青溪餐厅", "REFERENCE"), ("望星面馆", "REFERENCE"),
    ]
    assert len(output.public_result.days[0].activities) == 1
    assert output.public_result.days[0].activities[0].name == "地点待确认"
    assert output.public_result.days[0].activities[0].category == "餐饮"
    assert output.public_result.days[0].alternatives == []


@pytest.mark.asyncio
@pytest.mark.parametrize("clause,names", [
    ("午饭：已订位青溪餐厅（望星路店）。", ["青溪餐厅（望星路店）"]),
    ("午餐：去附近青溪餐厅（东街店）用餐。", ["青溪餐厅（东街店）"]),
    ("午饭：附近青溪餐厅、望星面馆，依次各吃一份。", FOOD_NAMES),
    ("晚餐：附近青溪餐厅、望星面馆，美食串店计划，两家都去。", FOOD_NAMES),
    ("午餐：青溪餐厅、望星面馆，分别吃点心和面条。", FOOD_NAMES),
    ("美食串店计划：午餐：附近青溪餐厅、望星面馆。", FOOD_NAMES),
    ("已预订两家，午餐：附近青溪餐厅、望星面馆。", FOOD_NAMES),
])
async def test_explicit_booking_branches_and_food_crawls_keep_the_planned_stops(clause, names):
    _, output, queries = await _run(clause, [_activity(name) for name in names])
    assert queries == names
    assert [mention.atomic_place_name for mention in output.proposal.mentions] == names
    assert all(mention.role.value == "PLANNED" for mention in output.proposal.mentions)
    assert [card.name for card in output.public_result.days[0].activities] == names


@pytest.mark.asyncio
@pytest.mark.parametrize("note", ["无需订位", "不用订座", "尚未预订", "没有选定", "两家都不去"])
async def test_absent_booking_or_selection_does_not_turn_suggestions_into_visits(note):
    _, output, queries = await _run(f"午饭：附近青溪餐厅、望星面馆，{note}。", [_activity(name) for name in FOOD_NAMES])
    assert queries == []
    assert all(mention.role.value == "REFERENCE" for mention in output.proposal.mentions)


@pytest.mark.asyncio
async def test_a_later_explicit_visit_to_a_recommended_restaurant_is_not_deleted_by_name():
    clause = "午饭：附近青溪餐厅、望星面馆，平价本地口味。\n晚餐：已订位青溪餐厅。"
    rows = [_activity(name) for name in FOOD_NAMES] + [_activity("青溪餐厅", occurrence=2)]
    source, output, queries = await _run(clause, rows)
    assert queries == ["青溪餐厅"]
    assert [mention.role.value for mention in output.proposal.mentions] == ["REFERENCE", "REFERENCE", "PLANNED"]
    assert output.proposal.mentions[-1].span_start == source.rindex("青溪餐厅")
    assert [card.name for card in output.public_result.days[0].activities] == ["青溪餐厅"]
    assert output.public_result.days[0].alternatives == []


@pytest.mark.asyncio
async def test_nearby_attraction_visits_after_lunch_are_not_restaurant_recommendations():
    names = ["青溪公园", "望星博物馆"]
    _, output, queries = await _run("午饭后去附近青溪公园、望星博物馆。", [
        _activity(name, category="景点") for name in names
    ])
    assert queries == names
    assert all(mention.role.value == "PLANNED" for mention in output.proposal.mentions)
    assert [card.name for card in output.public_result.days[0].activities] == names
