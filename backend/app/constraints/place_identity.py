"""Provider-independent POI identity and deterministic entity deduplication."""

from __future__ import annotations

import math
import unicodedata
from typing import Any, Iterable

from app.schemas.place import Place, RetrievalExecutionMode


_GENERIC_SUFFIXES = ("风景名胜区", "旅游景区", "旅游区", "景区")
_MAX_SAME_POI_DISTANCE_METERS = 300.0


def normalize_place_name(name: str, city: str = "") -> str:
    """Normalize presentation variants without collapsing distinct branches."""
    value = unicodedata.normalize("NFKC", name or "").strip().lower()
    value = "".join(ch for ch in value if not unicodedata.category(ch).startswith(("P", "Z")))
    compact_city = unicodedata.normalize("NFKC", city or "").removesuffix("市").lower()
    if compact_city and value.startswith(compact_city) and len(value) > len(compact_city) + 1:
        value = value[len(compact_city):]
    for suffix in _GENERIC_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix) + 1:
            value = value[: -len(suffix)]
            break
    return value


def _value(place: Any, key: str, default: Any = None) -> Any:
    return place.get(key, default) if isinstance(place, dict) else getattr(place, key, default)


def _coords(place: Any) -> tuple[float, float] | None:
    coords = _value(place, "coords")
    if coords is None:
        return None
    lng = coords.get("lng") if isinstance(coords, dict) else getattr(coords, "lng", None)
    lat = coords.get("lat") if isinstance(coords, dict) else getattr(coords, "lat", None)
    if not isinstance(lng, (int, float)) or not isinstance(lat, (int, float)):
        return None
    return float(lng), float(lat)


def coordinate_distance_meters(left: Any, right: Any) -> float | None:
    a = _coords(left)
    b = _coords(right)
    if a is None or b is None:
        return None
    lng1, lat1 = map(math.radians, a)
    lng2, lat2 = map(math.radians, b)
    d_lng = lng2 - lng1
    d_lat = lat2 - lat1
    h = math.sin(d_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(d_lng / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(h)))


def same_canonical_place(left: Any, right: Any) -> bool:
    left_id = str(_value(left, "place_id", "") or "")
    right_id = str(_value(right, "place_id", "") or "")
    if left_id and left_id == right_id:
        return True

    left_category = str(getattr(_value(left, "category", ""), "value", _value(left, "category", "")))
    right_category = str(getattr(_value(right, "category", ""), "value", _value(right, "category", "")))
    if not left_category or left_category != right_category:
        return False

    left_name = normalize_place_name(str(_value(left, "name", "")), str(_value(left, "city", "")))
    right_name = normalize_place_name(str(_value(right, "name", "")), str(_value(right, "city", "")))
    if not left_name or left_name != right_name:
        return False
    distance = coordinate_distance_meters(left, right)
    return distance is not None and distance <= _MAX_SAME_POI_DISTANCE_METERS


def _quality(place: Any) -> tuple[int, int]:
    raw_mode = _value(place, "execution_mode")
    mode = str(getattr(raw_mode, "value", raw_mode) or "")
    mode_rank = {
        RetrievalExecutionMode.LIVE.value: 3,
        RetrievalExecutionMode.FALLBACK.value: 2,
        RetrievalExecutionMode.FIXTURE.value: 1,
    }.get(mode, 0)
    populated = sum(
        bool(_value(place, field))
        for field in ("address", "district", "amap_rating", "amap_price", "opening_hours", "phone")
    )
    return mode_rank, populated


def deduplicate_places(places: Iterable[Place]) -> list[Place]:
    """Stable entity dedupe; a live/richer record replaces a weaker duplicate."""
    merged: list[Place] = []
    for place in places:
        duplicate_index = next(
            (index for index, existing in enumerate(merged) if same_canonical_place(existing, place)),
            None,
        )
        if duplicate_index is None:
            merged.append(place)
        else:
            existing = merged[duplicate_index]
            winner = place if _quality(place) > _quality(existing) else existing
            slot_ids = list(dict.fromkeys([
                *(_value(existing, "recommendation_slot_ids", []) or []),
                *(_value(place, "recommendation_slot_ids", []) or []),
            ]))
            canonical_names = list(dict.fromkeys([
                *(_value(existing, "canonical_entity_names", []) or []),
                *(_value(place, "canonical_entity_names", []) or []),
            ]))
            merged[duplicate_index] = winner.model_copy(update={
                "recommendation_slot_ids": slot_ids,
                "canonical_entity_names": canonical_names,
            })
    return merged
