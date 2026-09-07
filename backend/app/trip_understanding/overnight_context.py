"""Pure overnight boundaries shared by stay scoring and map projection."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json

from app.trip_understanding.map_render import MapRenderPlan, MapStop


def normalized_city(value: str | None) -> str:
    city = (value or "").strip().removesuffix("市")
    # This is the system's unknown-destination label, never a real city.
    return "" if city == "目的地待确认" else city


def is_hotel(stop: MapStop) -> bool:
    return stop.is_stay_anchor or stop.category in {"住宿", "酒店", "hotel"}


def is_overnight_hotel(stop: MapStop) -> bool:
    # Legacy unknown hotel cards retain their previous conservative meaning.
    # Explicit departure, checkout and luggage visits do not reserve this night.
    return (is_hotel(stop) and not stop.source_place_is_placeholder
            and not stop.lodging_role_uncertain and stop.lodging_event in {None, "OVERNIGHT"})


def unconfirmed_lodging_role(stop: MapStop) -> bool:
    return is_hotel(stop) and not stop.source_place_is_placeholder and stop.lodging_role_uncertain


def lodging_role_message(names: list[str]) -> str:
    return f"「{'、'.join(names)}」的入住、退房或取行李用途还需确认；暂不判断这晚住宿或推荐替换酒店"


def lodging_exclusion_message(names: list[str]) -> str:
    return f"原文要求另住，但「{'、'.join(names)}」的具体门店尚未核实；确认原店后再排除并推荐其他酒店"


def hotel_identity(place_id: str | None) -> str:
    # Legacy AMap bindings may carry their provider prefix. Never match names
    # or brands to exclude a different branch.
    return (place_id or "").removeprefix("amap:")


def excludes_hotel(segment, place_id: str | None) -> bool:
    return hotel_identity(place_id) in segment.excluded_place_ids


def is_boundary_visit(stop: MapStop) -> bool:
    return (not is_hotel(stop) or unconfirmed_lodging_role(stop)
            or stop.lodging_event in {"CHECK_OUT", "DEPARTURE", "LUGGAGE_PICKUP"})


def confirmed(stop: MapStop) -> bool:
    return (stop.resolution_status == "AUTO_MATCHED" and bool(stop.canonical_place_id)
            and stop.longitude is not None and stop.latitude is not None)


def stay_context_hash(plan: MapRenderPlan) -> str:
    # Selecting a recommended hotel changes no original activity. Edits do.
    values = [{"day": s.day_index, "name": s.name, "place": s.canonical_place_id,
               "city": s.city, "category": s.category, "lodging_event": s.lodging_event,
               "source_place_is_placeholder": s.source_place_is_placeholder,
               "lodging_role_uncertain": s.lodging_role_uncertain,
               "lodging_excluded_nights": sorted(set(s.lodging_excluded_nights)),
               "lodging_scope": s.lodging_scope, "status": s.resolution_status,
               "longitude": s.longitude, "latitude": s.latitude}
              for s in sorted(plan.stops, key=lambda s: (s.day_index, s.sequence_index))
              if not s.is_stay_anchor]
    constraints = [{"name": s.name, "place": s.canonical_place_id, "city": s.city,
        "status": s.resolution_status, "event": s.lodging_event, "scope": s.lodging_scope,
        "source_place_is_placeholder": s.source_place_is_placeholder,
        "lodging_role_uncertain": s.lodging_role_uncertain,
        "lodging_excluded_nights": sorted(set(s.lodging_excluded_nights)),
        "longitude": s.longitude, "latitude": s.latitude} for s in plan.lodging_constraints]
    return hashlib.sha256(json.dumps({"day_count": plan.day_count or max((s.day_index for s in plan.stops), default=0),
        "stops": values, "lodging_constraints": constraints}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


@dataclass
class OvernightSegment:
    key: str
    city: str | None
    overnight_days: list[int] = field(default_factory=list)
    # Direction uses the activity's calendar day, not the preceding night's day.
    anchors: list[tuple[int, str, MapStop]] = field(default_factory=list)
    preserved_hotels: list[str] = field(default_factory=list)
    uncertain: bool = False
    expected_boundary_count: int = 2
    missing_boundaries: list[dict] = field(default_factory=list)
    pending_lodging_roles: list[str] = field(default_factory=list)
    excluded_place_ids: list[str] = field(default_factory=list)
    unconfirmed_exclusions: list[str] = field(default_factory=list)


def overnight_segments(plan: MapRenderPlan) -> list[OvernightSegment]:
    days: dict[int, list[MapStop]] = {}
    for stop in sorted(plan.stops, key=lambda s: (s.day_index, s.sequence_index)):
        if not stop.is_stay_anchor:
            days.setdefault(stop.day_index, []).append(stop)
    day_count = plan.day_count or max(days, default=0)
    if day_count < 2:
        return []
    source_hotels = [s for s in [*plan.stops, *plan.lodging_constraints]
        if not s.is_stay_anchor and is_hotel(s) and not s.source_place_is_placeholder]
    segments: list[OvernightSegment] = []
    for night in range(1, day_count):
        current, following = days.get(night, []), days.get(night + 1, [])
        excluded = sorted({hotel_identity(s.canonical_place_id) for s in source_hotels
            if night in s.lodging_excluded_nights and confirmed(s)})
        unknown_exclusions = list(dict.fromkeys(s.name for s in source_hotels
            if night in s.lodging_excluded_nights and not confirmed(s)))
        hotels = [s for s in current if is_overnight_hotel(s) and hotel_identity(s.canonical_place_id) not in excluded]
        pending_roles = [s for s in current if unconfirmed_lodging_role(s)]
        # A source-backed departure/checkout at the next day's beginning refers
        # to the preceding night. Day 1 never invents an arrival-night stay.
        if following and unconfirmed_lodging_role(following[0]):
            pending_roles.append(following[0])
        if (following and is_hotel(following[0]) and not following[0].source_place_is_placeholder
                and not following[0].lodging_role_uncertain and following[0].lodging_event in {"CHECK_OUT", "DEPARTURE"}
                and hotel_identity(following[0].canonical_place_id) not in excluded):
            hotels.append(following[0])
        # Choose the real endpoints before checking identity. Never substitute an
        # earlier/later confirmed stop for an unresolved endpoint.
        before = [s for s in current if is_boundary_visit(s)]
        after = [s for s in following if is_boundary_visit(s)]
        last, first = (before[-1] if before else None), (after[0] if after else None)
        left, right = normalized_city(last.city if last else None), normalized_city(first.city if first else None)
        endpoint_cities = {value for value in (left, right) if value}
        for hotel in plan.lodging_constraints:
            hotel_city = normalized_city(hotel.city)
            if (is_overnight_hotel(hotel) and hotel.lodging_event == "OVERNIGHT" and hotel.lodging_scope == "WHOLE_TRIP"
                    and hotel_identity(hotel.canonical_place_id) not in excluded
                    and hotel_city and (not endpoint_cities or endpoint_cities == {hotel_city})):
                hotels.append(hotel)
        hotel_cities = {normalized_city(s.city) for s in hotels if s.city}
        city = next(iter(hotel_cities)) if len(hotel_cities) == 1 else left if left and left == right else None
        hotel_unconfirmed = any(not confirmed(hotel) for hotel in hotels)
        pending = list(dict.fromkeys(s.name for s in pending_roles))
        missing = []
        for day, direction, endpoint in ((night, "LAST_TO_STAY", last), (night + 1, "STAY_TO_FIRST", first)):
            reason = ("EMPTY_DAY" if endpoint is None else "UNCONFIRMED_PLACE" if not confirmed(endpoint)
                else "CITY_UNKNOWN" if not normalized_city(endpoint.city) else "OVERNIGHT_CITY_UNCONFIRMED" if not city
                else "DIFFERENT_CITY" if normalized_city(endpoint.city) != city
                else "HOTEL_UNCONFIRMED" if hotel_unconfirmed
                else "LODGING_ROLE_UNCONFIRMED" if pending
                else "LODGING_REPLACEMENT_UNCONFIRMED" if unknown_exclusions else None)
            if reason:
                missing.append({"day": day, "direction": direction, "name": endpoint.name if endpoint else None, "reason": reason})
        uncertain = bool(missing)
        preserved = list(dict.fromkeys(s.name for s in hotels))
        anchors = []
        if city and last and confirmed(last) and left == city:
            anchors.append((night, "LAST_TO_STAY", last))
        if city and first and confirmed(first) and right == city:
            anchors.append((night + 1, "STAY_TO_FIRST", first))
        if (segments and not uncertain and not segments[-1].uncertain and not preserved
                and not segments[-1].preserved_hotels and segments[-1].city == city
                and segments[-1].overnight_days[-1] == night - 1):
            segments[-1].overnight_days.append(night)
            segments[-1].anchors.extend(anchors)
            segments[-1].expected_boundary_count += 2
            segments[-1].excluded_place_ids = sorted(set(segments[-1].excluded_place_ids) | set(excluded))
        else:
            key = "stay_" + hashlib.sha256(f"{night}:{city or 'unknown'}".encode()).hexdigest()[:24]
            segments.append(OvernightSegment(key, city, [night], anchors, preserved, uncertain, 2, missing, pending,
                excluded, unknown_exclusions))
    return segments
