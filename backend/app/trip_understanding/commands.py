from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Callable

from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import (
    ActivityCardView,
    ActivityDeleteCommand,
    ActivityInsertCommand,
    ActivityMoveCommand,
    ActivityTextEditCommand,
    AssumptionSetCommand,
    MapReadinessView,
    PlaceReplaceCommand,
    TripDayView,
    TripUnderstandingCommand,
    UserFacingTripResult,
)


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


def _result_status(days: list[TripDayView]) -> str:
    cards = [card for day in days for card in day.activities]
    if len(cards) > 80:
        return "LIMITED"
    ready = sum(card.status == "READY" for card in cards)
    if cards and ready == len(cards):
        return "READY"
    if ready:
        return "PARTIAL_RESULT"
    return "BASIC_ONLY"


def apply_public_command(
    current: UserFacingTripResult,
    command: TripUnderstandingCommand,
    *,
    token_factory: Callable[[], str] = _default_token,
) -> PublicCommandMutation:
    result = current.model_copy(deep=True)
    changed: set[str] = set()
    inserted_card: ActivityCardView | None = None

    if isinstance(command, ActivityInsertCommand):
        _ensure_day(result.days, command.day_index)
        day = result.days[command.day_index - 1]
        inserted_card = ActivityCardView(
            activity_token=token_factory(),
            name=command.name,
            category=command.category,
            area_or_address=command.area_or_address,
            time_hint=command.time_hint,
            status="NEEDS_CONFIRMATION",
            available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
        )
        day.activities.insert(min(command.position, len(day.activities)), inserted_card)
        changed.add(day.label)
    elif isinstance(command, ActivityDeleteCommand):
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
            card.name = command.name
            card.area_or_address = "地点待确认"
            card.status = "NEEDS_CONFIRMATION"
        if command.time_hint is not None:
            card.time_hint = command.time_hint
        changed.add(result.days[day_index].label)
    elif isinstance(command, PlaceReplaceCommand):
        day_index, _position, card = _find_card(result.days, command.activity_token)
        card.name = command.replacement.name
        card.category = command.replacement.category
        card.area_or_address = command.replacement.area_or_address
        card.status = "NEEDS_CONFIRMATION"
        changed.add(result.days[day_index].label)
    elif isinstance(command, AssumptionSetCommand):
        assumption = next((item for item in result.assumptions if item.key == command.key), None)
        if assumption is None:
            raise CommandTargetChangedError("assumption is no longer present in the current result")
        assumption.value = command.value
        changed.update(day.label for day in result.days)

    token_map: dict[str, str] = {}
    inserted_token = inserted_card.activity_token if inserted_card else None
    for day in result.days:
        for card in day.activities:
            old_token = card.activity_token
            if inserted_card is card:
                continue
            new_token = token_factory()
            token_map[old_token] = new_token
            card.activity_token = new_token

    result.status = _result_status(result.days)
    result.map = MapReadinessView(
        status="NEEDS_UPDATE",
        message="卡片已调整，路线地图需要手动更新",
        available_actions=["RENDER_MAP"],
    )
    return PublicCommandMutation(
        result=result,
        changed_days=sorted(changed, key=lambda label: int(label.removeprefix("Day "))),
        token_map=token_map,
        inserted_token=inserted_token,
    )
