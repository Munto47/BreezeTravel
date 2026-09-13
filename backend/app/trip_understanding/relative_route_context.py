"""Connect source visit identity to the single current public itinerary."""
from __future__ import annotations

from dataclasses import replace

from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.relative_route_options import RelativeRouteVisit
from app.trip_understanding.source_order import (
    VisitLocation, advance_source_order_state, load_source_order_state,
    route_order_constraints, seed_source_order_state,
)


def result_locations(result):
    return [VisitLocation(card.activity_token, day, position)
        for day, group in enumerate(result.days, 1) for position, card in enumerate(group.activities)]


def seed_order_for_output(output):
    return seed_source_order_state(output.proposal.order_assessment,
        {a.compiled.mention.mention_id: a.compiled.public_activity_token for a in output.activities},
        result_locations(output.public_result)).model_dump(mode="json")


def carry_source_order(proposal, before, after, token_map, *, command=None, confirmed_place=None, current_place_id=None):
    """Use the restored target's proposal/result for undo or redo, not the head."""
    try:
        previous = load_source_order_state(proposal.get("source_order"), result_locations(before))
        kind = getattr(command, "command_type", None)
        old_card = next((card for day in before.days for card in day.activities
            if card.activity_token == getattr(command, "activity_token", None)), None)
        replace_visit = old_card is not None and (kind == "PLACE_REPLACE"
            or kind == "ACTIVITY_TEXT_EDIT" and command.name is not None and command.name != old_card.name)
        if kind == "PLACE_CONFIRM" and old_card is not None and confirmed_place is not None:
            same_parent = current_place_id == confirmed_place.canonical_place_id if current_place_id else (
                old_card.name == confirmed_place.name and (old_card.city == confirmed_place.city
                    or old_card.city is None and old_card.status == "NEEDS_CONFIRMATION"))
            replace_visit = not same_parent
        replaced = frozenset({command.activity_token}) if replace_visit else frozenset()
        if kind == "ASSUMPTION_SET" and command.key == "destination":
            replaced = frozenset(previous.bindings)
        old_tokens = {card.activity_token for day in before.days for card in day.activities}
        new_tokens = {card.activity_token for day in after.days for card in day.activities}
        mapped = {token_map.get(token, token) for token in old_tokens}
        additions = frozenset(new_tokens - mapped) if kind in {"ACTIVITY_INSERT", "DINING_INSERT"} else frozenset()
        return advance_source_order_state(previous, result_locations(before), result_locations(after), token_map,
            user_moved_token=command.activity_token if kind == "ACTIVITY_MOVE" and not command.route_preview_token else None,
            replaced_tokens=replaced, user_added_tokens=additions).model_dump(mode="json")
    except ValueError as exc:
        raise CommandTargetChangedError("current visit order needs a new review") from exc


def route_context(result, plan, proposal):
    """Pending visits remain in sequence; selected hotel endpoints stay protected.

    The map can omit pending coordinates. It cannot silently omit those visits
    from the set the optimizer is allowed to change.
    """
    state = load_source_order_state(proposal.get("source_order"), result_locations(result))
    stops = {s.activity_token: s for s in plan.stops if s.activity_token and not s.is_stay_anchor}
    visits = []
    for day_index, day in enumerate(result.days, 1):
        for index, card in enumerate(day.activities):
            stop = stops.get(card.activity_token)
            if stop is None:
                stop = MapStop(activity_token=card.activity_token, day_index=day_index, day_label=day.label,
                    sequence_index=index, name=card.name, category=card.category, resolution_status="NEEDS_CONFIRMATION")
            else:
                stop = stop.model_copy(update={"sequence_index": index})
            binding = state.bindings[card.activity_token]
            visits.append(RelativeRouteVisit(binding.visit_id, stop, binding.source_kind, card.meal_role,
                any(card.activity_token in choice.activity_tokens for choice in day.choice_selections)))
    constraints = route_order_constraints(state)
    by_token = {v.stop.activity_token: v.visit_id for v in visits}
    gaps = []
    # Keeping both gap neighbours adjacent protects unselected source meals.
    for day in result.days:
        for slot in day.meal_slots:
            if slot.selection_status == "SELECTED":
                continue
            before, after = slot.after_activity_token, slot.before_activity_token
            if before in by_token and after in by_token:
                tokens = [card.activity_token for card in day.activities]
                a, b = tokens.index(before), tokens.index(after)
                if b == a + 1:
                    gaps.append((by_token[before], by_token[after]))
                else:
                    protected = set(tokens[min(a, b):max(a, b) + 1])
                    visits = [replace(v, meal_role=v.meal_role or "UNSPECIFIED")
                        if v.stop.activity_token in protected else v for v in visits]
            else:
                # A one-sided or unpositioned source meal is not a free slot.
                # Protect its anchor, or this day's visits when no anchor exists.
                protected = {before, after} - {None}
                if not protected:
                    protected = {c.activity_token for c in day.activities}
                visits = [replace(v, meal_role=v.meal_role or "UNSPECIFIED")
                    if v.stop.activity_token in protected else v for v in visits]
    return tuple(visits), replace(constraints, protected_adjacencies=tuple(gaps))
