"""Explicit order review versus omitted review, using existing live interfaces.

The Beijing reply below is the unchanged captured response to a generated narrow
input in beijing-relative-route-worker-live-v1. All other replies are controlled
fixtures, not additional model measurements. Identity responses are fixed.
"""
import copy
import json

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.semantic_recovery import merge_preserved_activities
from app.trip_understanding.source_order import (
    SourceOrderAssessment,
    VisitLocation,
    remap_source_order_assessment,
    retain_source_order_assessment,
    route_order_constraints,
    seed_source_order_state,
)
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_experience_strict_wire_contract import recorded_provider
from tests.test_semantic_day_sections import ScopedClient, provider as section_provider, source as section_source
from tests.test_source_order_runtime import NAMES, prepared


INCOMPLETE = "ORDER_ASSESSMENT_INCOMPLETE"
CAPTURED_SOURCE = "北京一日游：天安门广场、北海公园、故宫博物院、景山公园。"
CAPTURED_NAMES = ["天安门广场", "北海公园", "故宫博物院", "景山公园"]
CAPTURED_RAW = {
    "destination": "北京",
    "day_labels": [None],
    "activities": [
        {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": 1, "category": "景点"}
        for name in CAPTURED_NAMES
    ],
    "unprocessed_quotes": [],
    "choice_groups": [],
    "order_groups": [],
}


def proposal(text, raw):
    return _proposal_from_live_draft(text, SemanticDraft.model_validate(raw))


def assert_coverage(result, *, unassessed=(), unknown=()):
    assessment = result.order_assessment
    # The new compatibility field is asserted through the real model dump, so
    # the pre-change failure also shows the missing data instead of calling a
    # not-yet-existing speculative function.
    assert assessment.model_dump().get("unassessed_mention_ids") == tuple(unassessed)
    assert assessment.unknown_mention_ids == tuple(unknown)
    assert (INCOMPLETE in assessment.issues) == bool(unassessed)


def ids(result, *indices):
    return tuple(result.mentions[index].mention_id for index in indices)


def two_day_revisit():
    text = "北京。\nDay1：星河公园、月光公园。\nDay2：再次去星河公园、晨光公园。"
    raw = dict(destination="北京", day_labels=[None, None], unprocessed_quotes=[], activities=[
        dict(source_quote=name, place_name=name, role="PLANNED", day_index=day, occurrence=occurrence)
        for name, day, occurrence in [(NAMES[0], 1, 1), (NAMES[1], 1, 1), (NAMES[0], 2, 2), (NAMES[3], 2, 1)]
    ], order_groups=[
        dict(kind="INITIAL_ORDER", activity_indices=[0, 1], scope_quote="Day1：星河公园、月光公园。"),
        dict(kind="UNKNOWN", activity_indices=[2, 3], scope_quote="Day2：再次去星河公园、晨光公园。"),
    ])
    return text, raw


@pytest.mark.asyncio
async def test_captured_explicit_empty_review_preserves_four_visible_visits_and_never_grants_freedom():
    async with recorded_provider(copy.deepcopy(CAPTURED_RAW)) as (live, requests):
        output = await TripUnderstandingPipeline(live, FixedReplayPlaces()).run(CAPTURED_SOURCE)
    assert len(requests) == output.proposal.binding["external_calls"] == 1
    assert [m.atomic_place_name for m in output.proposal.mentions] == CAPTURED_NAMES
    assert [a.name for a in output.public_result.days[0].activities] == CAPTURED_NAMES
    assert output.public_result.coverage.unprocessed_count == 0  # No visit was lost.
    expected = tuple(m.mention_id for m in output.proposal.mentions)
    assert_coverage(output.proposal, unassessed=expected, unknown=expected)
    mapping = {a.compiled.mention.mention_id: a.compiled.public_activity_token for a in output.activities}
    state = seed_source_order_state(output.proposal.order_assessment, mapping, tuple(
        VisitLocation(card.activity_token, 1, index)
        for index, card in enumerate(output.public_result.days[0].activities)
    ))
    constraints = route_order_constraints(state)
    assert not constraints.verified_visit_ids and not constraints.hard_precedence


@pytest.mark.parametrize("missing", [True, False])
def test_missing_and_empty_new_assessment_are_incomplete_but_keep_all_safe_mentions(missing):
    text, raw = prepared()
    if missing:
        raw.pop("order_groups")
    else:
        raw["order_groups"] = []
    result = proposal(text, raw)
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert result.unprocessed_count == 0
    assert not result.order_assessment.groups
    assert_coverage(result, unassessed=ids(result, 0, 1, 2, 3), unknown=ids(result, 0, 1, 2, 3))


@pytest.mark.parametrize("kind", ["INITIAL_ORDER", "REQUIRED_PRECEDENCE", "UNKNOWN"])
def test_all_named_root_visits_explicitly_reviewed_as_each_supported_kind(kind):
    text, raw = prepared(kind)
    result = proposal(text, raw)
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert_coverage(result, unknown=ids(result, 0, 1, 2, 3) if kind == "UNKNOWN" else ())
    assert len(result.order_assessment.groups) == (0 if kind == "UNKNOWN" else 1)
    if kind == "REQUIRED_PRECEDENCE":
        assert result.order_assessment.groups[0].hard_precedence == ((result.mentions[0].mention_id, result.mentions[1].mention_id),)


@pytest.mark.parametrize("explicit_unknown", [False, True])
def test_local_unknown_is_reviewed_but_unmentioned_remainder_is_not(explicit_unknown):
    text, raw = prepared()
    raw["order_groups"][0]["activity_indices"] = [0, 1]
    if explicit_unknown:
        raw["order_groups"].append(dict(kind="UNKNOWN", activity_indices=[2, 3], scope_quote=text))
    result = proposal(text, raw)
    remainder = ids(result, 2, 3)
    assert_coverage(result, unassessed=() if explicit_unknown else remainder, unknown=remainder)
    assert result.order_assessment.groups[0].member_mention_ids == ids(result, 0, 1)


@pytest.mark.parametrize("case", ["zero", "optional_only", "anonymous_meal", "reference_only"])
def test_no_named_planned_root_does_not_require_an_order_group(case):
    if case == "zero":
        text, rows = "北京。Day1：休息。", []
    elif case == "anonymous_meal":
        text = "北京。Day1：午餐待定。"
        rows = [dict(source_quote="午餐待定", place_name=None, day_index=1, role="PLANNED", category="餐饮", meal_role="LUNCH")]
    else:
        text = "北京。Day1：如果有空再去星河公园。" if case == "optional_only" else "北京。Day1：星河公园仅作背景介绍。"
        rows = [dict(source_quote="星河公园", place_name="星河公园", day_index=1,
                     role="OPTIONAL" if case == "optional_only" else "REFERENCE")]
    result = proposal(text, dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=rows, order_groups=[]))
    assert len(result.mentions) == len(rows)
    assert not result.order_assessment.groups
    assert_coverage(result)
    if case == "anonymous_meal":
        assert result.mentions[0].meal_role == "LUNCH" and result.mentions[0].atomic_place_name is None


def test_named_visits_reviewed_without_adding_anonymous_meal_to_the_group():
    text = "北京。\nDay1：星河公园。午餐待定。然后月光公园。"
    raw = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote="星河公园", place_name="星河公园", day_index=1, role="PLANNED"),
        dict(source_quote="午餐待定", place_name=None, day_index=1, role="PLANNED", category="餐饮", meal_role="LUNCH"),
        dict(source_quote="月光公园", place_name="月光公园", day_index=1, role="PLANNED"),
    ], order_groups=[dict(kind="UNKNOWN", activity_indices=[0, 2], scope_quote=text)])
    result = proposal(text, raw)
    assert len(result.mentions) == 3 and result.mentions[1].meal_role == "LUNCH"
    assert_coverage(result, unknown=ids(result, 0, 2))


@pytest.mark.parametrize("omit_second_day", [False, True])
def test_repeated_name_on_two_days_requires_each_actual_visit_to_be_reviewed(omit_second_day):
    text, raw = two_day_revisit()
    if omit_second_day:
        raw["order_groups"].pop()
    result = proposal(text, raw)
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [
        (NAMES[0], 1), (NAMES[1], 1), (NAMES[0], 2), (NAMES[3], 2),
    ]
    assert result.mentions[0].span_start != result.mentions[2].span_start
    assert_coverage(result, unassessed=ids(result, 2, 3) if omit_second_day else (), unknown=ids(result, 2, 3))


def test_two_visits_on_same_day_cannot_be_covered_by_repeating_the_first_row_index():
    text = "北京。\nDay1：早上去星河公园，晚上去星河公园。"
    raw = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote="星河公园", place_name="星河公园", day_index=1, role="PLANNED", occurrence=occurrence)
        for occurrence in (1, 2)
    ], order_groups=[dict(kind="UNKNOWN", activity_indices=[0, 0], scope_quote=text)])
    result = proposal(text, raw)
    assert len(result.mentions) == 2 and result.mentions[0].span_start < result.mentions[1].span_start
    assert_coverage(result, unassessed=ids(result, 0, 1), unknown=ids(result, 0, 1))


@pytest.mark.parametrize("fault", ["missing_scope", "invented_scope", "wrong_index", "duplicate_index", "cross_day", "other_occurrence_scope"])
def test_unknown_is_reviewed_only_with_valid_unique_same_day_source_binding(fault):
    text, raw = prepared("UNKNOWN")
    group = raw["order_groups"][0]
    if fault == "missing_scope":
        group.pop("scope_quote")
    elif fault == "invented_scope":
        group["scope_quote"] = "原文没有的范围"
    elif fault == "wrong_index":
        group["activity_indices"] = [0, 1, 2, 7]
    elif fault == "duplicate_index":
        group["activity_indices"] = [0, 1, 2, 2]
    elif fault == "cross_day":
        text, raw = two_day_revisit()
        raw["order_groups"] = [dict(kind="UNKNOWN", activity_indices=[0, 1, 2, 3], scope_quote=text)]
    else:
        text, raw = two_day_revisit()
        raw["order_groups"] = [dict(kind="UNKNOWN", activity_indices=[2, 3], scope_quote="Day1：星河公园、月光公园。")]
    result = proposal(text, raw)
    assert len(result.mentions) == 4
    assert not result.order_assessment.groups
    assert_coverage(result, unassessed=ids(result, 0, 1, 2, 3), unknown=ids(result, 0, 1, 2, 3))


def test_overlapping_unknown_cannot_override_another_groups_positive_assessment():
    text, raw = prepared()
    raw["order_groups"].append(dict(kind="UNKNOWN", activity_indices=[0, 1, 2, 3], scope_quote=text))
    result = proposal(text, raw)
    assert not result.order_assessment.groups
    assert_coverage(result, unassessed=ids(result, 0, 1, 2, 3), unknown=ids(result, 0, 1, 2, 3))


def test_retention_and_day_remapping_preserve_reviewed_unknown_and_unreviewed_remainder():
    text, raw = prepared()
    raw["order_groups"] = [dict(kind="UNKNOWN", activity_indices=[0, 1], scope_quote=text)]
    result = proposal(text, raw)
    retained = retain_source_order_assessment(result.order_assessment, result.mentions)
    assert retained == result.order_assessment
    mapping = {m.mention_id: f"day-2-{m.mention_id}" for m in result.mentions}
    remapped = remap_source_order_assessment(retained, mapping, offset=20)
    assert remapped.unknown_mention_ids == tuple(mapping[mid] for mid in ids(result, 0, 1, 2, 3))
    assert remapped.model_dump().get("unassessed_mention_ids") == tuple(mapping[mid] for mid in ids(result, 2, 3))
    assert INCOMPLETE in remapped.issues and not remapped.groups


@pytest.mark.asyncio
@pytest.mark.parametrize("second_reviewed", [False, True])
async def test_real_day_provider_keeps_second_day_review_status_without_extra_model_calls(second_reviewed):
    class OrderScopedClient(ScopedClient):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            raw = json.loads(response.choices[0].message.content)
            rows = raw.get("activities", [])
            if rows:
                day = rows[0]["day_index"]
                raw["order_groups"] = [dict(
                    kind="INITIAL_ORDER" if day == 1 else "UNKNOWN", activity_indices=[0],
                    scope_quote=f"Day{day}：{rows[0]['source_quote']}。",
                )] if day == 1 or second_reviewed else []
                response.choices[0].message.content = json.dumps(raw, ensure_ascii=False)
            return response

    client = OrderScopedClient()
    result = await section_provider(client).propose(section_source())
    assert len(client.calls) == 3  # Existing structure + two day calls; no order-only call.
    assert len(result.mentions) == 2
    assert [(m.atomic_place_name, m.day_index) for m in result.mentions] == [("星河公园", 1), ("月光桥", 2)]
    assert_coverage(result, unassessed=() if second_reviewed else ids(result, 1), unknown=ids(result, 1))
    assert result.order_assessment.groups[0].member_mention_ids == ids(result, 0)


@pytest.mark.parametrize("original_has_unknown", [False, True])
def test_repair_reordered_rows_preserve_previous_explicit_review_without_inventing_one(original_has_unknown):
    text, raw = prepared("UNKNOWN")
    if not original_has_unknown:
        raw["order_groups"] = []
    original = SemanticDraft.model_validate(raw)
    changed = copy.deepcopy(raw)
    changed["activities"].reverse()
    changed["order_groups"] = []
    validated = _proposal_from_live_draft(text, original)
    repaired = merge_preserved_activities(text, original, validated, SemanticDraft.model_validate(changed))
    result = _proposal_from_live_draft(text, repaired)
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert_coverage(result, unassessed=() if original_has_unknown else ids(result, 0, 1, 2, 3),
                    unknown=ids(result, 0, 1, 2, 3))


def test_old_saved_assessment_defaults_remain_readable_without_claiming_freedom():
    old = SourceOrderAssessment.model_validate({"groups": [], "unknown_mention_ids": ["legacy-visit"], "issues": []})
    assert old.unknown_mention_ids == ("legacy-visit",) and not old.groups
    assert old.model_dump().get("unassessed_mention_ids") == ()
