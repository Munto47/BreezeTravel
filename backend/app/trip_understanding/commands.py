from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable

from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import (
    MAX_TRIP_ACTIVITIES,
    ActivityCardView,
    ActivityDeleteCommand,
    ActivityInsertCommand,
    AlternativeInsertCommand,
    ChoiceSelectCommand,
    ChoiceClearCommand,
    ChoiceSelectionView,
    DiningInsertCommand,
    LodgingRecoverCommand,
    LodgingConstraintView,
    ActivityMoveCommand,
    ActivityTextEditCommand,
    ActivityTimeSetCommand,
    ActivityTimesShiftCommand,
    ActivityTimesApplyCommand,
    AssumptionSetCommand,
    MapReadinessView,
    PlaceReplaceCommand,
    PlaceConfirmCommand,
    UndoCommand,
    TripDayView,
    TripUnderstandingCommand,
    UserFacingTripResult,
)
from app.trip_understanding.timing import ActivityTiming, TIMING_FIELDS, clock_minutes, shift_clock, timing_values
from app.trip_understanding.pipeline import atomic_place_rejection_reason
from app.trip_understanding.lodging_recovery import result_cards, validate_recovery_target


@dataclass(frozen=True)
class PublicCommandMutation:
    result: UserFacingTripResult
    changed_days: list[str]
    token_map: dict[str, str]
    inserted_token: str | None = None


def _default_token() -> str:
    return secrets.token_urlsafe(24)


def _find_card(
    days: list[TripDayView],
    activity_token: str,
) -> tuple[int, int, ActivityCardView]:
    for day_index, day in enumerate(days):
        for position, card in enumerate(day.activities):
            if card.activity_token == activity_token:
                return day_index, position, card
    raise CommandTargetChangedError("activity is no longer present in the current result")


def _ensure_day(days: list[TripDayView], day_index: int) -> None:
    while len(days) < day_index:
        days.append(TripDayView(label=f"Day {len(days) + 1}", activities=[]))


def refresh_meal_slot_tokens(days: list[TripDayView], token_map: dict[str, str]) -> None:
    """Keep saved meal selection on its card; never rematch by meal position."""
    for day in days:
        cards = {card.activity_token: card for card in day.activities}
        for slot in day.meal_slots:
            for field in ("after_activity_token", "before_activity_token", "selected_activity_token"):
                previous = getattr(slot, field)
                refreshed = token_map.get(previous, previous)
                setattr(slot, field, refreshed if refreshed in cards else None)
            if slot.selection_status == "SELECTED":
                selected = cards.get(slot.selected_activity_token)
                if selected is None or selected.category != "餐饮" or selected.meal_role != slot.meal_role:
                    slot.selected_activity_token = None
                    slot.selection_status = "UNSELECTED"


def refresh_choice_selection_tokens(
    days: list[TripDayView], token_map: dict[str, str], token_factory: Callable[[], str] = _default_token,
) -> None:
    """Preserve a choice through token renewal, recording later manual changes."""
    for day in days:
        cards = {card.activity_token: index for index, card in enumerate(day.activities)}
        for alternative in day.alternatives:
            # Source alternatives are actionable only within this public
            # version. Group/branch identity and legacy missing tokens stay.
            if alternative.activity_token is not None:
                alternative.activity_token = token_factory()
            for field in ("after_activity_token", "before_activity_token"):
                previous = getattr(alternative, field)
                refreshed = token_map.get(previous, previous)
                setattr(alternative, field, refreshed if refreshed in cards else None)
            before = cards.get(alternative.before_activity_token)
            after = cards.get(alternative.after_activity_token)
            if before is not None and after is not None and after >= before:
                alternative.insertion_position = None
            elif before is not None:
                alternative.insertion_position = before
            elif after is not None:
                alternative.insertion_position = after + 1
            else:
                # A legacy snapshot has no position evidence. Empty current
                # days are unambiguous; otherwise the user must choose a slot.
                alternative.insertion_position = 0 if not cards else None
        # Two unselected groups sharing the same original gap cannot infer
        # their relative position from its two surrounding cards alone after
        # a mutation. Preserve card order and ask for an explicit position.
        shared = {}
        for alternative in day.alternatives:
            if alternative.choice_group_token:
                shared.setdefault((alternative.after_activity_token, alternative.before_activity_token), set()).add(alternative.choice_group_token)
        for alternative in day.alternatives:
            if alternative.choice_group_token and len(shared.get((alternative.after_activity_token, alternative.before_activity_token), ())) > 1:
                alternative.insertion_position = None
        for selection in day.choice_selections:
            refreshed = [token_map.get(token, token) for token in selection.activity_tokens]
            selection.activity_tokens = [token for token in refreshed if token in cards]
            if len(selection.activity_tokens) != len(refreshed):
                selection.status = "MODIFIED"


def _result_status(days: list[TripDayView], constraints=()) -> str:
    cards = [card for day in days for card in day.activities] + list(constraints)
    if len(cards) > MAX_TRIP_ACTIVITIES:
        return "LIMITED"
    ready = sum(card.status == "READY" for card in cards)
    if cards and ready == len(cards):
        return "READY"
    if ready:
        return "PARTIAL_RESULT"
    return "BASIC_ONLY"


def refresh_result_coverage(result: UserFacingTripResult) -> None:
    """Recount current cards while preserving unresolved source semantics.

    Legacy results without a coverage assessment remain unassessed. Editing
    cards cannot certify that previously omitted source text was understood.
    """
    if result.coverage is None:
        return
    cards = [card for card in result_cards(result)
             if card.name != "地点待确认" and atomic_place_rejection_reason(card.name) is None]
    confirmed = sum(card.status == "READY" for card in cards)
    pending_source = bool(result.pending_lodgings or result.coverage.unclassified_mention_count or result.coverage.unprocessed_count)
    if pending_source and result.status != "LIMITED":
        result.status = "PARTIAL_RESULT"
    result.coverage = result.coverage.model_copy(update={
        "recognized_place_count": len(cards) + len(result.pending_lodgings), "confirmed_place_count": confirmed,
        "unresolved_place_count": len(cards) - confirmed + len(result.pending_lodgings),
        "complete": result.status == "READY" and not pending_source,
    })


def apply_public_command(
    current: UserFacingTripResult,
    command: TripUnderstandingCommand,
    *,
    token_factory: Callable[[], str] = _default_token,
    undo_result: UserFacingTripResult | None = None,
    confirmed_place=None,
    current_place_id: str | None = None,
    source_lunch_gaps: dict[str, str] | None = None,
    dining_plan=None,
) -> PublicCommandMutation:
    result = current.model_copy(deep=True)
    changed: set[str] = set()
    inserted_card: ActivityCardView | None = None
    filled_gap_token: str | None = None

    if isinstance(command, UndoCommand):
        if not current.can_undo or undo_result is None:
            raise CommandTargetChangedError("no edit is available to undo")
        result = undo_result.model_copy(deep=True)
        changed.update(day.label for day in current.days)
        changed.update(day.label for day in result.days)
    elif isinstance(command, ActivityTimeSetCommand):
        day_index, _, card = _find_card(result.days, command.activity_token)
        values = timing_values(card)
        values.update({name: getattr(command, name) for name in TIMING_FIELDS if name in command.model_fields_set})
        values["timing_source"] = "USER"
        try:
            validated = ActivityTiming.model_validate(values)
        except ValueError as exc:
            raise CommandTargetChangedError("activity timing is inconsistent") from exc
        for name, value in timing_values(validated).items():
            setattr(card, name, value)
        card.time_hint = card.start_time
        changed.add(result.days[day_index].label)
    elif isinstance(command, ActivityTimesShiftCommand):
        for token in dict.fromkeys(command.activity_tokens):
            day_index, _, card = _find_card(result.days, token)
            if card.locked or card.fixed_commitment or not card.start_time:
                raise CommandTargetChangedError("a fixed activity cannot be shifted")
            try:
                card.start_time = shift_clock(card.start_time, command.minutes)
                card.end_time = shift_clock(card.end_time, command.minutes)
            except ValueError as exc:
                raise CommandTargetChangedError("shift crosses a day boundary") from exc
            card.timing_source = "USER"
            card.time_hint = card.start_time
            changed.add(result.days[day_index].label)
    elif isinstance(command, ActivityTimesApplyCommand):
        if len({item.activity_token for item in command.changes}) != len(command.changes):
            raise CommandTargetChangedError("an activity cannot appear twice in one timing change")
        updates = []
        for item in command.changes:
            day_index, _, card = _find_card(result.days, item.activity_token)
            if card.locked or card.fixed_commitment or not card.start_time:
                raise CommandTargetChangedError("a fixed activity cannot be shifted")
            shift = clock_minutes(item.start_time) - clock_minutes(card.start_time)
            try:
                if shift <= 0 or item.end_time != shift_clock(card.end_time, shift):
                    raise ValueError("a schedule shift must preserve the visit window")
                if card.visit_duration_minutes is not None and clock_minutes(item.start_time) + card.visit_duration_minutes >= 1440:
                    raise ValueError("the proposed shift crosses a day boundary")
                timing = ActivityTiming.model_validate({**timing_values(card),
                    "start_time": item.start_time, "end_time": item.end_time, "timing_source": "USER"})
            except ValueError as exc:
                raise CommandTargetChangedError("the proposed schedule is not a same-day shift") from exc
            updates.append((day_index, card, timing))
        # Validate every member before applying the transaction's visible changes.
        for day_index, card, timing in updates:
            for name, value in timing_values(timing).items():
                setattr(card, name, value)
            card.time_hint = card.start_time
            changed.add(result.days[day_index].label)
    elif isinstance(command, PlaceConfirmCommand):
        if confirmed_place is None:
            raise CommandTargetChangedError("a verified place selection is required")
        constraint = next((card for card in result.lodging_constraints if card.activity_token == command.activity_token), None)
        if constraint:
            if confirmed_place.category != "住宿":
                raise CommandTargetChangedError("a lodging constraint requires a hotel")
            card = constraint
            changed.update(result.days[night - 1].label for night in constraint.overnight_days)
        else:
            day_index, _, card = _find_card(result.days, command.activity_token)
            changed.add(result.days[day_index].label)
        if card.source_details:
            same_parent = (current_place_id == confirmed_place.canonical_place_id if current_place_id
                else card.name == confirmed_place.name and card.city == confirmed_place.city)
            if not same_parent:
                card.source_details = []
        card.name = confirmed_place.name
        card.category = confirmed_place.category
        card.area_or_address = confirmed_place.area_or_address
        card.city = confirmed_place.city
        card.photo_url = None
        card.status = "READY"
        card.knowledge_suggestions = []
    elif isinstance(command, LodgingRecoverCommand):
        nights = validate_recovery_target(result, command.pending_token, command.intent)
        if confirmed_place is None or confirmed_place.category != "住宿":
            raise CommandTargetChangedError("a verified hotel is required")
        pending = next(item for item in result.pending_lodgings if item.pending_token == command.pending_token)
        values = dict(activity_token=token_factory(), name=confirmed_place.name, category="住宿",
            city=confirmed_place.city, area_or_address=confirmed_place.area_or_address, status="READY",
            lodging_role_uncertain=False, available_actions=["VIEW_DETAILS", "REPLACE", "DELETE"])
        if command.intent.kind == "VISIT_ONLY":
            inserted_card = ActivityCardView(**values, lodging_event="VISIT_ONLY")
            inserted_card.available_actions.append("MOVE")
            day = result.days[command.intent.day_index - 1]
            position = next((index for index, card in enumerate(day.activities)
                if card.activity_token == command.intent.before_activity_token), len(day.activities))
            day.activities.insert(position, inserted_card)
            changed.add(day.label)
        else:
            inserted_card = LodgingConstraintView(**values, lodging_event="OVERNIGHT",
                scope=command.intent.kind, overnight_days=nights,
                lodging_scope="WHOLE_TRIP" if command.intent.kind == "WHOLE_TRIP" else None)
            result.lodging_constraints.append(inserted_card)
            changed.update(result.days[night - 1].label for night in nights)
        result.pending_lodgings.remove(pending)
        if result.coverage:
            result.coverage.unprocessed_count = max(0, result.coverage.unprocessed_count - pending.unprocessed_count)
    elif isinstance(command, DiningInsertCommand):
        if confirmed_place is None or confirmed_place.category != "餐饮" or atomic_place_rejection_reason(confirmed_place.name):
            raise CommandTargetChangedError("a verified dining selection is required")
        day_index, position, anchor = _find_card(result.days, command.after_activity_token)
        if anchor.status != "READY" or (anchor.city and anchor.city != confirmed_place.city):
            raise CommandTargetChangedError("dining anchor needs confirmation")
        day = result.days[day_index]
        lunch_gap = None
        selected_slot = None
        if command.meal_role == "LUNCH":
            from app.trip_understanding.daily_dining import meal_context

            meal, _, _ = meal_context(day, [])
            if meal.get("existing_activity_token"):
                # A still-valid candidate from an old page cannot duplicate the source lunch.
                raise CommandTargetChangedError("this day already has a lunch place to retain or confirm")
            slots = [slot for slot in day.meal_slots if slot.meal_role == command.meal_role]
            if len(slots) == 1 and dining_plan is not None:
                context, _, _ = meal_context(day, dining_plan.stops, source_gaps=source_lunch_gaps)
                if (context.get("after_activity_token") == command.after_activity_token
                        and bool(context.get("insert_before")) == command.insert_before):
                    selected_slot = slots[0]
            gaps = [card for card in day.activities if card.activity_token in (source_lunch_gaps or {})]
            if len(gaps) > 1:
                raise CommandTargetChangedError("multiple source lunches need a specific choice first")
            if gaps:
                lunch_gap = gaps[0]
                if dining_plan is not None:
                    context, _, _ = meal_context(day, dining_plan.stops, source_gaps=source_lunch_gaps)
                    if (context.get("after_activity_token") != command.after_activity_token
                            or bool(context.get("insert_before")) != command.insert_before):
                        raise CommandTargetChangedError("lunch position changed; refresh dining suggestions")
        if any(card.name == confirmed_place.name and card.area_or_address == confirmed_place.area_or_address for card in day.activities):
            raise CommandTargetChangedError("dining place is already in this day")
        values = dict(activity_token=token_factory(), name=confirmed_place.name,
            category="餐饮", area_or_address=confirmed_place.area_or_address, city=confirmed_place.city,
            meal_role=command.meal_role, photo_url=None, knowledge_suggestions=[],
            status="READY", available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"])
        if lunch_gap:
            filled_gap_token = lunch_gap.activity_token
            inserted_card = lunch_gap.model_copy(update=values)
            if source_lunch_gaps[filled_gap_token] == "POSITIONAL":
                day.activities[day.activities.index(lunch_gap)] = inserted_card
            else:
                day.activities.remove(lunch_gap)
                position = day.activities.index(anchor)
                day.activities.insert(position + (0 if command.insert_before else 1), inserted_card)
        else:
            inserted_card = ActivityCardView(**values)
            day.activities.insert(position + (0 if command.insert_before else 1), inserted_card)
        if selected_slot is not None:
            selected_slot.selection_status = "SELECTED"
            selected_slot.selected_activity_token = inserted_card.activity_token
        changed.add(day.label)
    elif isinstance(command, ChoiceClearCommand):
        if command.day_index > len(result.days):
            raise CommandTargetChangedError("choice day is unavailable")
        day = result.days[command.day_index - 1]
        selection = next((item for item in day.choice_selections if item.choice_group_token == command.choice_group_token), None)
        members = [item for item in day.alternatives if item.choice_group_token == command.choice_group_token
                   and selection is not None and item.branch_token == selection.branch_token]
        cards = {card.activity_token for card in day.activities}
        if (selection is None or not members or not set(selection.activity_tokens) <= cards
                or (selection.status == "SELECTED" and (command.preserve_activities or len(selection.activity_tokens) != len(members)))
                or (selection.status == "MODIFIED" and not command.preserve_activities)):
            raise CommandTargetChangedError("the chosen branch has been manually changed")
        chosen = set(selection.activity_tokens)
        if not command.preserve_activities:
            day.activities = [card for card in day.activities if card.activity_token not in chosen]
        day.choice_selections.remove(selection)
        changed.add(day.label)
    elif isinstance(command, ChoiceSelectCommand):
        if command.day_index > len(result.days):
            raise CommandTargetChangedError("choice day is unavailable")
        day = result.days[command.day_index - 1]
        group = [item for item in day.alternatives if item.choice_group_token == command.choice_group_token]
        branches = {item.branch_token for item in group}
        if (len(branches) != 2 or None in branches or command.branch_token not in branches
                or not all(item.choice_group_selectable for item in group)
                or any(item.choice_group_token == command.choice_group_token for item in day.choice_selections)):
            raise CommandTargetChangedError("choice is no longer available in this version")
        position = command.position
        if position is None:
            source_positions = {item.insertion_position for item in group}
            if len(source_positions) != 1 or None in source_positions:
                raise CommandTargetChangedError("choice source position needs an explicit selection")
            position = next(iter(source_positions))
        if not 0 <= position <= len(day.activities):
            raise CommandTargetChangedError("choice position no longer exists")
        members = [item for item in group if item.branch_token == command.branch_token]
        if len(result_cards(result)) + len(members) > MAX_TRIP_ACTIVITIES:
            raise CommandTargetChangedError("the selected branch exceeds trip capacity")
        added = []
        for member in members:
            if atomic_place_rejection_reason(member.name) is not None:
                raise CommandTargetChangedError("choice member requires clarification")
            card = ActivityCardView(activity_token=token_factory(), name=member.name, city=member.city,
                category=member.category, meal_role=member.meal_role, area_or_address="地点待确认",
                source_details=[detail.model_copy(deep=True) for detail in member.source_details],
                **timing_values(member), status="NEEDS_CONFIRMATION",
                available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"])
            day.activities.insert(position + len(added), card)
            added.append(card.activity_token)
        day.choice_selections.append(ChoiceSelectionView(choice_group_token=command.choice_group_token,
            branch_token=command.branch_token, activity_tokens=added))
        changed.add(day.label)
    elif isinstance(command, AlternativeInsertCommand):
        if command.day_index > len(result.days):
            raise CommandTargetChangedError("alternative day is unavailable")
        day = result.days[command.day_index - 1]
        matches = [(index, item) for index, source_day in enumerate(result.days, start=1)
                   for item in source_day.alternatives if item.activity_token == command.alternative_token]
        if len(matches) != 1 or matches[0][0] != command.day_index:
            raise CommandTargetChangedError("alternative is no longer unique in this day's current result")
        member = matches[0][1]
        if command.position > len(day.activities) or len(result_cards(result)) >= MAX_TRIP_ACTIVITIES:
            raise CommandTargetChangedError("alternative position or trip capacity is unavailable")
        if atomic_place_rejection_reason(member.name) is not None:
            raise CommandTargetChangedError("alternative requires clarification")
        inserted_card = ActivityCardView(
            activity_token=token_factory(), name=member.name, city=member.city,
            category=member.category, meal_role=member.meal_role,
            source_details=[detail.model_copy(deep=True) for detail in member.source_details],
            area_or_address="地点待确认", **timing_values(member), status="NEEDS_CONFIRMATION",
            available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
        )
        day.activities.insert(command.position, inserted_card)
        changed.add(day.label)
    elif isinstance(command, ActivityInsertCommand):
        _ensure_day(result.days, command.day_index)
        day = result.days[command.day_index - 1]
        inserted_card = ActivityCardView(
            activity_token=token_factory(),
            name=command.name,
            city=command.city,
            category=command.category,
            area_or_address=command.area_or_address,
            time_hint=command.time_hint,
            **timing_values(command),
            status="NEEDS_CONFIRMATION",
            available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
        )
        day.activities.insert(min(command.position, len(day.activities)), inserted_card)
        changed.add(day.label)
    elif isinstance(command, ActivityDeleteCommand):
        constraint = next((card for card in result.lodging_constraints if card.activity_token == command.activity_token), None)
        if constraint:
            result.lodging_constraints.remove(constraint)
            changed.update(result.days[night - 1].label for night in constraint.overnight_days)
        else:
            day_index, position, _card = _find_card(result.days, command.activity_token)
            changed.add(result.days[day_index].label)
            result.days[day_index].activities.pop(position)
    elif isinstance(command, ActivityMoveCommand):
        source_day, position, card = _find_card(result.days, command.activity_token)
        source_label = result.days[source_day].label
        result.days[source_day].activities.pop(position)
        _ensure_day(result.days, command.target_day_index)
        target = result.days[command.target_day_index - 1]
        target.activities.insert(min(command.target_position, len(target.activities)), card)
        changed.update((source_label, target.label))
    elif isinstance(command, ActivityTextEditCommand):
        day_index, _position, card = _find_card(result.days, command.activity_token)
        if command.name is not None:
            if command.name != card.name:
                card.source_details = []
            card.name = command.name
            card.area_or_address = "地点待确认"
            card.photo_url = None
            card.status = "NEEDS_CONFIRMATION"
            card.knowledge_suggestions = []
        if command.time_hint is not None:
            card.time_hint = command.time_hint
            card.start_time = None
            card.end_time = None
            card.visit_duration_minutes = None
            card.timing_source = "USER"
        changed.add(result.days[day_index].label)
    elif isinstance(command, PlaceReplaceCommand):
        day_index, _position, card = _find_card(result.days, command.activity_token)
        card.source_details = []
        card.name = command.replacement.name
        card.category = command.replacement.category
        card.area_or_address = command.replacement.area_or_address
        card.photo_url = None
        card.status = "NEEDS_CONFIRMATION"
        card.knowledge_suggestions = []
        changed.add(result.days[day_index].label)
    elif isinstance(command, AssumptionSetCommand):
        assumption = next((item for item in result.assumptions if item.key == command.key), None)
        if assumption is None:
            raise CommandTargetChangedError("assumption is no longer present in the current result")
        assumption.value = command.value
        if command.key == "destination":
            for card in result_cards(result):
                card.status = "NEEDS_CONFIRMATION"
                card.area_or_address = "地点待确认"
                card.photo_url = None
                card.city = None
                card.knowledge_suggestions = []
        changed.update(day.label for day in result.days)

    changed_choice_token = None
    if isinstance(command, PlaceReplaceCommand):
        changed_choice_token = command.activity_token
    elif isinstance(command, ActivityTextEditCommand) and command.name is not None:
        old = next((card for card in result_cards(current) if card.activity_token == command.activity_token), None)
        if old is not None and old.name != command.name:
            changed_choice_token = command.activity_token
    elif isinstance(command, PlaceConfirmCommand) and confirmed_place:
        old = next((card for card in result_cards(current) if card.activity_token == command.activity_token), None)
        same_parent = (current_place_id == confirmed_place.canonical_place_id if current_place_id
            else old is not None and old.name == confirmed_place.name and old.city == confirmed_place.city)
        if not same_parent:
            changed_choice_token = command.activity_token
    if changed_choice_token:
        for day in result.days:
            for selection in day.choice_selections:
                if changed_choice_token in selection.activity_tokens:
                    selection.status = "MODIFIED"

    token_map: dict[str, str] = {}
    inserted_token = inserted_card.activity_token if inserted_card else None
    if filled_gap_token:
        token_map[filled_gap_token] = inserted_token
    for card in result_cards(result):
        old_token = card.activity_token
        if inserted_card is card:
            continue
        new_token = token_factory()
        token_map[old_token] = new_token
        card.activity_token = new_token
    for pending in result.pending_lodgings:
        old_token = pending.pending_token
        pending.pending_token = token_factory()
        token_map[old_token] = pending.pending_token

    refresh_meal_slot_tokens(result.days, token_map)
    refresh_choice_selection_tokens(result.days, token_map, token_factory)

    result.status = _result_status(result.days, result.lodging_constraints)
    refresh_result_coverage(result)
    result.can_undo = not isinstance(command, UndoCommand)
    result.map = MapReadinessView(
        status="NEEDS_UPDATE",
        message="卡片已调整，路线地图需要手动更新",
        available_actions=["RENDER_MAP"],
    )
    return PublicCommandMutation(
        result=result,
        changed_days=list(dict.fromkeys(day.label for day in [*current.days, *result.days] if day.label in changed)),
        token_map=token_map,
        inserted_token=inserted_token,
    )
