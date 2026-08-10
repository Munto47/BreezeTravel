from __future__ import annotations

from enum import Enum
from typing import Any, Iterable, Optional


def normalise(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    return "".join(str(value or "").lower().split())


def all_slots(itinerary) -> Iterable[tuple[int, Any]]:
    for day in itinerary.days:
        for slot in day.slots:
            yield day.day_index, slot


def place_text(slot) -> str:
    place = slot.place or {}
    values = [place.get("name"), place.get("address"), place.get("district"), place.get("category")]
    values.extend(place.get("tags") or [])
    return normalise(" ".join(str(item or "") for item in values))


def minutes(value: str) -> Optional[int]:
    if not value or value.startswith("次日"):
        return None
    try:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (TypeError, ValueError):
        return None


def find_constraints(spec, constraint_type: str):
    return [item for item in spec.hard_constraints if item.type == constraint_type]
