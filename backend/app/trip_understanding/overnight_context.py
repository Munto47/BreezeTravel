"""Pure overnight boundaries shared by stay scoring and map projection."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from app.trip_understanding.map_render import MapRenderPlan, MapStop


def normalized_city(value: str | None) -> str:
    return (value or "").strip().removesuffix("市")


def is_hotel(stop: MapStop) -> bool:
    return stop.is_stay_anchor or stop.category in {"住宿", "酒店", "hotel"}


def confirmed(stop: MapStop) -> bool:
    return (stop.resolution_status == "AUTO_MATCHED" and bool(stop.canonical_place_id)
            and stop.longitude is not None and stop.latitude is not None)


def stay_context_hash(plan: MapRenderPlan) -> str:
    # Selecting a recommended hotel changes no original activity. Edits do.
    values = [{"day": s.day_index, "name": s.name, "place": s.canonical_place_id,
               "city": s.city, "category": s.category, "status": s.resolution_status,
               "longitude": s.longitude, "latitude": s.latitude}
              for s in sorted(plan.stops, key=lambda s: (s.day_index, s.sequence_index))
              if not s.is_stay_anchor]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class OvernightSegment:
    key: str
    city: str | None
    overnight_days: list[int] = field(default_factory=list)
    # Direction uses the activity's calendar day, not the preceding night's day.
    anchors: list[tuple[int, str, MapStop]] = field(default_factory=list)
    preserved_hotels: list[str] = field(default_factory=list)
    uncertain: bool = False


def overnight_segments(plan: MapRenderPlan) -> list[OvernightSegment]:
    days: dict[int, list[MapStop]] = {}
    for stop in sorted(plan.stops, key=lambda s: (s.day_index, s.sequence_index)):
        if not stop.is_stay_anchor:
            days.setdefault(stop.day_index, []).append(stop)
    if len(days) < 2:
        return []
    segments: list[OvernightSegment] = []
    for night in range(min(days), max(days)):
        current, following = days.get(night, []), days.get(night + 1, [])
        hotels = [s for s in current if is_hotel(s)]
        before = [s for s in current if not is_hotel(s) and confirmed(s)]
        after = [s for s in following if not is_hotel(s) and confirmed(s)]
        last, first = (before[-1] if before else None), (after[0] if after else None)
        left, right = normalized_city(last.city if last else None), normalized_city(first.city if first else None)
        hotel_cities = {normalized_city(s.city) for s in hotels if s.city}
        city = next(iter(hotel_cities)) if len(hotel_cities) == 1 else left if left and left == right else None
        uncertain = not city or (not hotels and (not last or not first))
        preserved = list(dict.fromkeys(s.name for s in hotels))
        anchors = []
        if city and last and left == city:
            anchors.append((night, "LAST_TO_STAY", last))
        if city and first and right == city:
            anchors.append((night + 1, "STAY_TO_FIRST", first))
        if (segments and not uncertain and not segments[-1].uncertain and not preserved
                and not segments[-1].preserved_hotels and segments[-1].city == city
                and segments[-1].overnight_days[-1] == night - 1):
            segments[-1].overnight_days.append(night)
            segments[-1].anchors.extend(anchors)
        else:
            key = "stay_" + hashlib.sha256(f"{night}:{city or 'unknown'}".encode()).hexdigest()[:24]
            segments.append(OvernightSegment(key, city, [night], anchors, preserved, uncertain))
    return segments
