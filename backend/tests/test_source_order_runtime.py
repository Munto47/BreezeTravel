"""Fixed model replies through the live provider/pipeline; no external services."""
import copy
import json

import pytest
from jsonschema import Draft202012Validator

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.models import SourceSemanticPlan
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.source_order import VisitLocation, route_order_constraints, seed_source_order_state
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_experience_strict_wire_contract import recorded_provider, request_schema
from tests.test_semantic_day_sections import ScopedClient, provider, source as section_source


NAMES = ["星河公园", "月光公园", "晚晴公园", "晨光公园"]


def prepared(kind="INITIAL_ORDER"):
    clause = ("必须先星河公园再月光公园，然后晚晴公园、晨光公园。" if kind == "REQUIRED_PRECEDENCE"
              else "星河公园、月光公园、晚晴公园、晨光公园。")
    scope = "Day1：" + clause
    text = "北京。\n" + scope
    group = dict(kind=kind, activity_indices=list(range(4)), scope_quote=scope, required_precedence=[])
    if kind == "REQUIRED_PRECEDENCE":
        group["required_precedence"] = [dict(before_index=0, after_index=1, evidence="必须先星河公园再月光公园")]
    raw = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote=name, place_name=name, day_index=1, role="PLANNED", category="景点") for name in NAMES
    ], order_groups=[group])
    return text, raw


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["INITIAL_ORDER", "REQUIRED_PRECEDENCE"])
async def test_real_wire_to_pipeline_seed_keeps_mainline_and_only_explicit_hard_edges(kind):
    text, raw = prepared(kind)
    async with recorded_provider(raw) as (live, requests):
        output = await TripUnderstandingPipeline(live, FixedReplayPlaces()).run(text)
    assert len(requests) == 1
    schema = request_schema(requests[0])
    Draft202012Validator(schema).validate(raw)
    assert "order_groups" in schema["properties"] and "order_groups" in schema["required"]
    assert "INITIAL_ORDER" in requests[0]["messages"][0]["content"]
    assessment = output.proposal.order_assessment
    assert len(assessment.groups) == 1 and not assessment.unknown_mention_ids
    assert [m.atomic_place_name for m in output.proposal.mentions] == NAMES
    assert [c.name for c in output.public_result.days[0].activities] == NAMES
    mapping = {a.compiled.mention.mention_id: a.compiled.public_activity_token for a in output.activities}
    locations = tuple(VisitLocation(card.activity_token, day, pos)
        for day, item in enumerate(output.public_result.days, 1) for pos, card in enumerate(item.activities))
    state = seed_source_order_state(assessment, mapping, locations)
    constraints = route_order_constraints(state)
    assert len(constraints.verified_visit_ids) == 4
    assert len(constraints.hard_precedence) == (1 if kind == "REQUIRED_PRECEDENCE" else 0)
    assert "scope_quote" not in state.model_dump_json() and "span" not in state.model_dump_json()
    public = output.public_result.model_dump_json()
    assert "order_assessment" not in public and "visit_id" not in public and "hard_precedence" not in public
    assert SourceSemanticPlan.model_validate(output.proposal.model_dump(mode="json")).order_assessment == assessment


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_explicit_unknown_assessment_never_means_free_and_old_plan_reads(missing):
    text, raw = prepared("UNKNOWN")
    if missing:
        raw.pop("order_groups")
    result = _proposal_from_live_draft(text, SemanticDraft.model_validate(raw))
    assert not result.order_assessment.groups and len(result.order_assessment.unknown_mention_ids) == 4
    assert result.unprocessed_count == 0  # Unknown optimization metadata is not a missing visit.
    old = result.model_dump(mode="json")
    old.pop("order_assessment")
    assert not SourceSemanticPlan.model_validate(old).order_assessment.groups


def test_combined_wire_row_expansion_maps_all_actual_members_without_index_guessing():
    text, raw = prepared()
    raw["activities"][:2] = [dict(source_quote="星河公园、月光公园", place_name="星河公园、月光公园",
        role="PLANNED", day_index=1, category="景点")]
    raw["order_groups"][0]["activity_indices"] = [0, 1, 2]
    result = _proposal_from_live_draft(text, SemanticDraft.model_validate(raw))
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert result.order_assessment.groups[0].member_mention_ids == tuple(m.mention_id for m in result.mentions)


@pytest.mark.asyncio
@pytest.mark.parametrize("includes_anonymous", [False, True])
async def test_named_order_group_and_separate_source_meal_slot_keep_both_boundaries(includes_anonymous):
    text, raw = prepared()
    text = "北京。\nDay1：星河公园、月光公园，中午找餐厅吃午餐，再去晚晴公园、晨光公园。"
    raw["activities"].insert(2, dict(source_quote="午餐", place_name=None, role="PLANNED",
        day_index=1, category="餐饮", meal_role="LUNCH"))
    raw["order_groups"][0].update(scope_quote=text,
        activity_indices=[0, 1, 2, 3, 4] if includes_anonymous else [0, 1, 3, 4])
    async with recorded_provider(raw) as (live, requests):
        output = await TripUnderstandingPipeline(live, FixedReplayPlaces()).run(text)
    assert len(requests) == 1
    day = output.public_result.days[0]
    assert [c.name for c in day.activities] == NAMES
    assert len(day.meal_slots) == 1 and day.meal_slots[0].meal_role == "LUNCH"
    slot = day.meal_slots[0]
    assert slot.after_activity_token == day.activities[1].activity_token
    assert slot.before_activity_token == day.activities[2].activity_token
    mapping = {a.compiled.mention.mention_id: a.compiled.public_activity_token for a in output.activities}
    state = seed_source_order_state(output.proposal.order_assessment, mapping,
        tuple(VisitLocation(c.activity_token, 1, i) for i, c in enumerate(day.activities)))
    assert len(route_order_constraints(state).verified_visit_ids) == (0 if includes_anonymous else 4)
    # Source meal anchors remain separate; the route context protects this gap.
    assert len(output.proposal.mentions) == 5


@pytest.mark.parametrize("problem", ["wrong_scope", "wrong_index", "cross_day", "conflicting_kind", "ambiguous_edge"])
def test_wire_binding_failures_leave_same_mainline_but_optimization_unknown(problem):
    text, raw = prepared()
    group = raw["order_groups"][0]
    if problem == "wrong_scope":
        group["scope_quote"] = "Day1：没有这段文字。"
    elif problem == "wrong_index":
        group["activity_indices"][-1] = 6
    elif problem == "cross_day":
        text = "北京。\nDay1：星河公园、月光公园。\nDay2：晚晴公园、晨光公园。"
        group["scope_quote"] = text
        raw["day_labels"] = [None, None]
        for row in raw["activities"][2:]:
            row["day_index"] = 2
    elif problem == "conflicting_kind":
        group["required_precedence"] = [dict(before_index=0, after_index=1, evidence="星河公园、月光公园")]
    else:
        raw["activities"][:2] = [dict(source_quote="星河公园、月光公园", place_name="星河公园、月光公园",
            role="PLANNED", day_index=1, category="景点")]
        group.update(kind="REQUIRED_PRECEDENCE", activity_indices=[0, 1, 2],
            required_precedence=[dict(before_index=0, after_index=1, evidence="星河公园、月光公园、晚晴公园")])
    result = _proposal_from_live_draft(text, SemanticDraft.model_validate(raw))
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert not result.order_assessment.groups and len(result.order_assessment.unknown_mention_ids) == 4


def test_same_place_on_two_days_is_two_independent_bound_visits():
    text = "北京。\nDay1：星河公园、月光公园。\nDay2：再次去星河公园、晨光公园。"
    rows = [dict(source_quote=name, place_name=name, role="PLANNED", day_index=day, occurrence=occ)
        for name, day, occ in [(NAMES[0], 1, 1), (NAMES[1], 1, 1), (NAMES[0], 2, 2), (NAMES[3], 2, 1)]]
    raw = dict(destination="北京", activities=rows, order_groups=[
        dict(kind="INITIAL_ORDER", activity_indices=[0, 1], scope_quote="Day1：星河公园、月光公园。"),
        dict(kind="INITIAL_ORDER", activity_indices=[2, 3], scope_quote="Day2：再次去星河公园、晨光公园。")])
    result = _proposal_from_live_draft(text, SemanticDraft.model_validate(raw))
    assert len(result.order_assessment.groups) == 2
    a, b = result.order_assessment.groups
    assert set(a.member_mention_ids).isdisjoint(b.member_mention_ids)
    assert a.scope_end < b.scope_start


@pytest.mark.asyncio
async def test_repair_retains_original_assessment_and_rebinds_reordered_wire_rows():
    text, raw = prepared("REQUIRED_PRECEDENCE")
    broken = copy.deepcopy(raw)
    broken["activities"].append(dict(source_quote="星河公园", place_name="不在原文的名字", day_index=1, role="PLANNED"))
    repaired = copy.deepcopy(raw)
    repaired["activities"].reverse()
    repaired.pop("order_groups")  # Repair cannot erase the earlier validated rule.
    async with recorded_provider(broken, repaired) as (live, requests):
        result = await live.propose(text)
    assert len(requests) == 2 and requests[0]["response_format"] == requests[1]["response_format"]
    assert [m.atomic_place_name for m in result.mentions] == NAMES
    assert len(result.order_assessment.groups) == 1
    assert result.order_assessment.groups[0].hard_precedence == ((result.mentions[0].mention_id, result.mentions[1].mention_id),)


@pytest.mark.asyncio
@pytest.mark.parametrize("second_assessed", [True, False])
async def test_day_sections_keep_all_assessments_and_rebind_full_source_coordinates(second_assessed):
    class OrderScopedClient(ScopedClient):
        async def create(self, **kwargs):
            response = await super().create(**kwargs)
            raw = json.loads(response.choices[0].message.content)
            rows = raw.get("activities", [])
            if rows and (second_assessed or rows[0]["day_index"] == 1):
                day = rows[0]["day_index"]
                raw["order_groups"] = [dict(kind="INITIAL_ORDER", activity_indices=[0],
                    scope_quote=f"Day{day}：{rows[0]['source_quote']}。")]
                response.choices[0].message.content = json.dumps(raw)
            return response

    text = section_source()
    client = OrderScopedClient()
    result = await provider(client).propose(text)
    assert len(client.calls) == 3  # Existing structure + day calls, no new assessment call.
    assert len(result.order_assessment.groups) == (2 if second_assessed else 1)
    assert len(result.order_assessment.unknown_mention_ids) == (0 if second_assessed else 1)
    for group in result.order_assessment.groups:
        assert all(mid.startswith("day-") for mid in group.member_mention_ids)
        assert text[group.scope_start:group.scope_end].startswith("Day")
        assert all(group.scope_start <= m.span_start < m.span_end <= group.scope_end
                   for m in result.mentions if m.mention_id in group.member_mention_ids)
