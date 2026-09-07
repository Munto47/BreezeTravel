"""User-confirmed recovery of a source hotel without inventing a visit day."""
from __future__ import annotations

import hashlib
import json

from app.trip_understanding.errors import CommandTargetChangedError
from app.trip_understanding.models import LodgingRecoveryIntent, UserFacingTripResult


def recovery_binding(pending_token: str, intent: LodgingRecoveryIntent) -> str:
    fingerprint = hashlib.sha256(json.dumps(intent.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
    return f"lodging-recovery:{pending_token}:{fingerprint}"


def validate_recovery_target(result: UserFacingTripResult, pending_token: str, intent: LodgingRecoveryIntent) -> list[int]:
    if not any(item.pending_token == pending_token for item in result.pending_lodgings):
        raise CommandTargetChangedError("pending hotel is no longer present")
    if intent.kind == "VISIT_ONLY":
        if intent.day_index > len(result.days):
            raise CommandTargetChangedError("visit day is no longer present")
        if intent.before_activity_token is not None and not any(card.activity_token == intent.before_activity_token
                for card in result.days[intent.day_index - 1].activities):
            raise CommandTargetChangedError("visit insertion boundary changed")
        return []
    nights = list(range(1, len(result.days))) if intent.kind == "WHOLE_TRIP" else list(intent.overnight_days)
    if not nights or any(night >= len(result.days) for night in nights):
        raise CommandTargetChangedError("overnight scope must name an existing night")
    return nights


def result_cards(result: UserFacingTripResult):
    """Confirmed constraints are editable place facts, never day visits."""
    return [card for day in result.days for card in day.activities] + list(result.lodging_constraints)


def confirmed_single_destination(result: UserFacingTripResult) -> str | None:
    """Follow the declared city only when every known visit corroborates it."""
    destination = next((item.value.removeprefix("暂按 ").strip().removesuffix("市")
        for item in result.assumptions if item.key == "destination"), None)
    cards = result_cards(result)
    if not destination or destination == "目的地待确认" or not cards:
        return None
    if any(card.status != "READY" or not card.city for card in cards):
        return None
    cities = {card.city.strip().removesuffix("市") for card in cards}
    return destination if cities == {destination} else None
