"""Private source meaning/lineage with real command mutations, zero services."""
import json

import pytest
from pydantic import ValidationError

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import (
    ActivityCardView, ActivityDeleteCommand, ActivityMoveCommand, ActivityRole, ActivityTextEditCommand,
    MapReadinessView, ProposedMention, RedoCommand, StaySuggestionView, TripDayView,
    UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.source_order import (
    PrivateSourceOrderState, RequiredPrecedenceDraft, SourceOrderGroupDraft, VisitLocation,
    advance_source_order_state, bind_source_order_groups, load_source_order_state,
    route_order_constraints, seed_source_order_state,
)


SOURCE = "Day1：甲园、乙园、丙园、丁园。必须先甲园再乙园。"


def mentions(source=SOURCE, names=("甲园", "乙园", "丙园", "丁园"), *, offset=0, day=1):
    result = []
    for i, name in enumerate(names):
        start = source.index(name, offset)
        result.append(ProposedMention(mention_id=f"m-{day}-{i}", raw_text=name,
            span_start=start, span_end=start + len(name), role=ActivityRole.PLANNED,
            day_index=day, sequence_index=i, atomic_place_name=name))
    return result


def draft(items, *, kind="REQUIRED_PRECEDENCE", scope=SOURCE, evidence="必须先甲园再乙园。"):
    # Bound evidence must contain these exact visits, not a later repeated name.
    hard = (RequiredPrecedenceDraft(before_mention_id=items[0].mention_id,
            after_mention_id=items[1].mention_id, evidence=evidence),) if kind == "REQUIRED_PRECEDENCE" else ()
    return SourceOrderGroupDraft(kind=kind, member_mention_ids=tuple(m.mention_id for m in items),
                                scope_quote=scope, required_precedence=hard)


def scenario(required=True):
    source = "Day1：必须先甲园再乙园，然后丙园、丁园。" if required else "Day1：甲园、乙园、丙园、丁园。"
    items = mentions(source)
    assessment = bind_source_order_groups(source, items, [draft(items,
        kind="REQUIRED_PRECEDENCE" if required else "INITIAL_ORDER", scope=source,
        evidence="必须先甲园再乙园")])
    mapping = {m.mention_id: f"source-order-test-token-{i}" for i, m in enumerate(items)}
    result = UserFacingTripResult(status="READY", assumptions=[],
        days=[TripDayView(label="Day 1", activities=[ActivityCardView(activity_token=mapping[m.mention_id],
            name=m.atomic_place_name, category="景点", city="北京", area_or_address="固定地址",
            status="READY", time_hint=None, available_actions=["MOVE", "VIEW_DETAILS", "REPLACE", "DELETE"])
            for m in items])],
        map=MapReadinessView(status="AVAILABLE", message="固定地图"),
        stay=StaySuggestionView(status="UNAVAILABLE", message="固定住宿"), available_actions=["EDIT_CARDS"])
    state = seed_source_order_state(assessment, mapping, locations(result))
    return source, assessment, state, result


def locations(result):
    return tuple(VisitLocation(card.activity_token, day, pos)
                 for day, item in enumerate(result.days, 1) for pos, card in enumerate(item.activities))


def stable_order(state, result):
    return tuple(state.bindings[item.activity_token].visit_id for item in locations(result))


def test_typed_initial_order_is_not_all_locked_and_missing_assessment_is_unknown():
    source, assessment, state, result = scenario(required=False)
    constraints = route_order_constraints(state)
    assert len(constraints.verified_visit_ids) == 4 and constraints.hard_precedence == ()
    assert assessment.unknown_mention_ids == ()
    absent = bind_source_order_groups(source, mentions(source), [])
    assert len(absent.unknown_mention_ids) == 4 and absent.groups == ()
    unknown = seed_source_order_state(absent, {f"m-1-{i}": f"source-order-test-token-{i}" for i in range(4)}, locations(result))
    assert route_order_constraints(unknown).verified_visit_ids == frozenset()
    explicitly_unknown = bind_source_order_groups(source, mentions(source), [draft(mentions(source), kind="UNKNOWN", scope=None)])
    assert explicitly_unknown.groups == () and len(explicitly_unknown.unknown_mention_ids) == 4


def test_exact_evidence_of_another_occurrence_cannot_lock_the_current_visits():
    items = mentions()  # The route's A/B, not the later repeated A/B in the rule.
    assessment = bind_source_order_groups(SOURCE, items, [draft(items)])
    assert assessment.groups == () and len(assessment.unknown_mention_ids) == 4
    assert assessment.issues == ("ORDER_PRECEDENCE_UNBOUND",)


@pytest.mark.parametrize("problem", ["wrong_scope", "cross_day", "reference", "cancelled", "overlap", "forged_span", "cycle"])
def test_unbound_or_conflicting_assessments_do_not_promote_unknown_to_free(problem):
    source = "Day1：必须先甲园再乙园，然后丙园、丁园。"
    items = mentions(source)
    value = draft(items, scope=source, evidence="必须先甲园再乙园")
    drafts = [value]
    if problem == "wrong_scope":
        value = value.model_copy(update={"scope_quote": "没有这段原文"})
    elif problem == "cross_day":
        items[1] = items[1].model_copy(update={"day_index": 2})
    elif problem in {"reference", "cancelled"}:
        items[1] = items[1].model_copy(update={"role": ActivityRole.REFERENCE if problem == "reference" else ActivityRole.EXCLUDED})
    elif problem == "overlap":
        drafts.append(value.model_copy(update={"kind": "INITIAL_ORDER", "required_precedence": ()}))
    elif problem == "forged_span":
        items[1] = items[1].model_copy(update={"raw_text": "假的乙园"})
    elif problem == "cycle":
        value = value.model_copy(update={"required_precedence": (*value.required_precedence,
            RequiredPrecedenceDraft(before_mention_id="m-1-1", after_mention_id="m-1-0", evidence="必须先甲园再乙园"))})
    drafts[0] = value
    assessment = bind_source_order_groups(source, items, drafts)
    assert assessment.groups == () and assessment.issues


def test_revisit_identity_is_per_source_visit_not_per_name_and_scopes_do_not_cross_days():
    source = "Day1：甲园、乙园。\nDay2：甲园、丁园。"
    day1 = mentions(source, ("甲园", "乙园"))
    day2 = mentions(source, ("甲园", "丁园"), offset=source.index("Day2"), day=2)
    assessed = bind_source_order_groups(source, day1 + day2, [
        draft(day1, kind="INITIAL_ORDER", scope="Day1：甲园、乙园。"),
        draft(day2, kind="INITIAL_ORDER", scope="Day2：甲园、丁园。"),
    ])
    mapping = {m.mention_id: f"t-{m.mention_id}" for m in day1 + day2}
    locs = [VisitLocation(mapping[m.mention_id], m.day_index, m.sequence_index) for m in day1 + day2]
    state = seed_source_order_state(assessed, mapping, locs)
    assert len({origin.visit_id for origin in state.bindings.values()}) == 4
    assert state.bindings["t-m-1-0"].visit_id != state.bindings["t-m-2-0"].visit_id
    assert len(state.groups) == 2


def test_manual_move_overrides_violated_edge_while_automatic_move_cannot():
    _, _, state, result = scenario()
    original_json = state.model_dump_json()
    a, b, _, _ = stable_order(state, result)
    token = result.days[0].activities[0].activity_token
    mutation = apply_public_command(result, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
        activity_token=token, target_day_index=1, target_position=1))
    with pytest.raises(ValueError, match="Automatic edit"):
        advance_source_order_state(state, locations(result), locations(mutation.result), mutation.token_map)
    next_state = advance_source_order_state(state, locations(result), locations(mutation.result), mutation.token_map,
                                          user_moved_token=token)
    assert next_state.groups[0].hard_precedence == ((b, a),)
    assert next_state.groups[0].origin == "USER_OVERRIDE"
    assert stable_order(next_state, mutation.result) == (b, a, *stable_order(state, result)[2:])
    assert state.model_dump_json() == original_json
    restored = load_source_order_state(json.loads(next_state.model_dump_json()), locations(mutation.result))
    assert restored == next_state


def test_undo_redo_copy_target_private_snapshot_and_never_reopen_source():
    source, _, original_state, original = scenario()
    token = original.days[0].activities[0].activity_token
    moved = apply_public_command(original, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
        activity_token=token, target_day_index=2, target_position=0))
    moved_state = advance_source_order_state(original_state, locations(original), locations(moved.result), moved.token_map,
                                             user_moved_token=token)
    # Source may now be deleted. Persisted state is structural only, and the
    # transition helpers below have no source text/reader argument at all.
    payload = moved_state.model_dump_json()
    assert source not in payload and "甲园" not in payload and "scope_" not in payload and "evidence" not in payload
    undo = apply_public_command(moved.result, UndoCommand(command_type="UNDO"), undo_result=original)
    undo_state = advance_source_order_state(original_state, locations(original), locations(undo.result), undo.token_map)
    assert stable_order(undo_state, undo.result) == stable_order(original_state, original)
    assert undo_state.groups == original_state.groups
    redo = apply_public_command(undo.result, RedoCommand(command_type="REDO"), redo_result=moved.result)
    redo_state = advance_source_order_state(moved_state, locations(moved.result), locations(redo.result), redo.token_map)
    assert stable_order(redo_state, redo.result) == stable_order(moved_state, moved.result)
    assert redo_state.groups == moved_state.groups


def test_place_rename_changes_visit_origin_and_drops_only_its_reviewed_group():
    _, _, state, result = scenario()
    token = result.days[0].activities[0].activity_token
    mutation = apply_public_command(result, ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT", activity_token=token, name="另一个地方"))
    changed = advance_source_order_state(state, locations(result), locations(mutation.result), mutation.token_map,
                                        replaced_tokens=frozenset({token}))
    assert changed.bindings[mutation.token_map[token]].visit_id != state.bindings[token].visit_id
    assert changed.bindings[mutation.token_map[token]].source_kind == "UNKNOWN"
    assert changed.groups == ()  # No old place's order is attached to its replacement.
    for old, new in mutation.token_map.items():
        if old != token:
            assert changed.bindings[new].visit_id == state.bindings[old].visit_id


def test_token_only_rollover_for_hotel_and_g03_keeps_private_business_snapshot():
    _, _, state, result = scenario()
    before = locations(result)
    mapping = {v.activity_token: f"renewed-{i}" for i, v in enumerate(before)}
    after = [VisitLocation(mapping[v.activity_token], v.day_index, v.position) for v in before]
    refreshed = advance_source_order_state(state, before, after, mapping)
    assert refreshed.groups == state.groups
    assert [refreshed.bindings[v.activity_token].visit_id for v in after] == list(stable_order(state, result))
    # Memory hotel selection presently retains tokens: identity mapping also works.
    assert advance_source_order_state(state, before, before, {}) == state


def test_legacy_missing_state_remains_unknown_after_unrelated_edit_and_explicit_new_visit_is_distinct():
    _, _, _, result = scenario(required=False)
    old = load_source_order_state(None, locations(result))
    assert all(v.source_kind == "UNKNOWN" for v in old.bindings.values())
    assert route_order_constraints(old).verified_visit_ids == frozenset()
    before = locations(result)
    after = (*before, VisitLocation("user-new", 1, 4))
    next_state = advance_source_order_state(old, before, after, {}, user_added_tokens=frozenset({"user-new"}))
    assert next_state.bindings["user-new"].source_kind == "USER_ADDED"
    assert route_order_constraints(next_state).verified_visit_ids == {next_state.bindings["user-new"].visit_id}
    assert all(next_state.bindings[v.activity_token] == old.bindings[v.activity_token] for v in before)


def test_private_reader_rejects_source_fields_and_mismatched_or_ambiguous_tokens():
    _, _, state, result = scenario()
    with pytest.raises(ValidationError):
        PrivateSourceOrderState.model_validate({**state.model_dump(), "source_quote": "不该永久保存"})
    with pytest.raises(ValueError):
        load_source_order_state(state.model_dump(), locations(result)[:-1])
    before = locations(result)
    with pytest.raises(ValueError):
        advance_source_order_state(state, before, before, {before[0].activity_token: before[1].activity_token})
    with pytest.raises(ValueError):
        advance_source_order_state(state, before, list(reversed(before)), {}, user_moved_token="unknown-token")


def deletion_scenario(edges):
    names = ("起点广场", "甲园", "乙园", "丙园", "丁园", "终点公园")
    source = "Day1：" + "、".join(names) + "。必须满足所列地点间的先后要求。"
    items = mentions(source, names)
    group = SourceOrderGroupDraft(kind="REQUIRED_PRECEDENCE", member_mention_ids=tuple(m.mention_id for m in items),
        scope_quote=source, required_precedence=tuple(RequiredPrecedenceDraft(
            before_mention_id=items[a].mention_id, after_mention_id=items[b].mention_id, evidence=source)
            for a, b in edges))
    assessment = bind_source_order_groups(source, items, [group])
    assert len(assessment.groups) == 1 and not assessment.issues
    mapping = {m.mention_id: f"deletion-order-token-{i}" for i, m in enumerate(items)}
    _, _, _, result = scenario(required=False)
    result.days[0].activities = [ActivityCardView(activity_token=mapping[m.mention_id], name=m.atomic_place_name,
        category="景点", city="北京", area_or_address="固定地址", status="READY", time_hint=None,
        available_actions=["MOVE", "DELETE", "VIEW_DETAILS"]) for m in items]
    return seed_source_order_state(assessment, mapping, locations(result)), result


def delete_visit(state, result, name):
    token = next(c.activity_token for c in result.days[0].activities if c.name == name)
    mutation = apply_public_command(result, ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=token))
    return advance_source_order_state(state, locations(result), locations(mutation.result), mutation.token_map), mutation.result


def named_precedence(state, result):
    names = {state.bindings[c.activity_token].visit_id: c.name for day in result.days for c in day.activities}
    return [(names[a], names[b]) for a, b in route_order_constraints(state).hard_precedence]


def test_delete_middle_visit_keeps_remaining_required_order_and_manual_override():
    state, result = deletion_scenario(((1, 2), (2, 3)))
    original_ids = {c.name: state.bindings[c.activity_token].visit_id for c in result.days[0].activities}
    changed, reduced = delete_visit(state, result, "乙园")
    assert named_precedence(changed, reduced) == [("甲园", "丙园")]
    assert {c.name: changed.bindings[c.activity_token].visit_id for c in reduced.days[0].activities} == {
        name: identity for name, identity in original_ids.items() if name != "乙园"}
    token = next(c.activity_token for c in reduced.days[0].activities if c.name == "甲园")
    moved = apply_public_command(reduced, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
        activity_token=token, target_day_index=1, target_position=2))
    with pytest.raises(ValueError, match="Automatic edit"):
        advance_source_order_state(changed, locations(reduced), locations(moved.result), moved.token_map)
    manual = advance_source_order_state(changed, locations(reduced), locations(moved.result), moved.token_map,
        user_moved_token=token)
    assert named_precedence(manual, moved.result) == [("丙园", "甲园")]
    assert manual.groups[0].origin == "USER_OVERRIDE"


@pytest.mark.parametrize("removed", [("乙园", "丙园"), ("丙园", "乙园")])
def test_multiple_deleted_visits_preserve_chain_in_either_deletion_order(removed):
    state, result = deletion_scenario(((1, 2), (2, 3), (3, 4)))
    for name in removed:
        state, result = delete_visit(state, result, name)
    assert named_precedence(state, result) == [("甲园", "丁园")]


def test_deleted_parallel_paths_keep_one_remaining_precedence_edge():
    state, result = deletion_scenario(((1, 2), (1, 3), (2, 4), (3, 4)))
    for name in ("乙园", "丙园"):
        state, result = delete_visit(state, result, name)
    assert named_precedence(state, result) == [("甲园", "丁园")]


def test_unrelated_deletion_does_not_add_shortcuts_through_surviving_visits():
    state, result = deletion_scenario(((1, 2), (2, 3)))
    changed, reduced = delete_visit(state, result, "丁园")
    assert named_precedence(changed, reduced) == [("甲园", "乙园"), ("乙园", "丙园")]
