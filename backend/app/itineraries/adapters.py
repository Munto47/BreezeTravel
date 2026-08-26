from __future__ import annotations

from datetime import datetime, time, timezone
from uuid import NAMESPACE_URL, uuid5

from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    ResolutionStatus,
    RevisionSource,
    RevisionTransport,
    TripDateRange,
)
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot, TransportLeg
from app.schemas.place import Coordinates


def _minutes_between(start: str, end: str) -> int | None:
    try:
        start_time = time.fromisoformat(start)
        end_time = time.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute
    if end_minutes < start_minutes:
        end_minutes += 24 * 60
    return end_minutes - start_minutes


def legacy_to_revision(
    itinerary: Itinerary,
    *,
    workspace_id: str,
    date_range: TripDateRange,
    created_by: str,
    source_type: RevisionSource = RevisionSource.PLANNER,
    parent_revision: int | None = None,
) -> ItineraryRevision:
    days: list[ItineraryDay] = []
    map_stop_projections: dict[str, dict] = {}
    for day in sorted(itinerary.days, key=lambda item: item.day_index):
        stops: list[ItineraryStop] = []
        for order_index, slot in enumerate(day.slots):
            transport = None
            if slot.transport is not None:
                transport = RevisionTransport(
                    mode=slot.transport.mode,
                    duration_minutes=slot.transport.duration_mins,
                    distance_meters=round(slot.transport.distance_km * 1000),
                )
            place = slot.place or {}
            stable_key = f"{itinerary.itinerary_id}:{itinerary.version}:{day.day_index}:{order_index}:{slot.place_id}"
            stop_id = str(uuid5(NAMESPACE_URL, stable_key))
            stops.append(ItineraryStop(
                stop_id=stop_id,
                place_id=slot.place_id,
                day_index=day.day_index,
                order_index=order_index,
                start_time=slot.start_time,
                end_time=slot.end_time,
                visit_duration_minutes=_minutes_between(slot.start_time, slot.end_time),
                transport_to_next=transport,
                raw_name=place.get("name"),
                resolution_status=ResolutionStatus.USER_CONFIRMED,
                fixed_commitment=bool(place.get("fixed_commitment", False)),
                locked=bool(place.get("locked", False)),
                category=str(place.get("category", "attraction")),
            ))
            # Preserve only coordinates explicitly present in the public
            # legacy itinerary payload.  Their role/provenance deliberately
            # says client supplied and unverified; this is map/query geometry,
            # never a Provider receipt or current-fact claim.
            try:
                coords = Coordinates.model_validate(place.get("coords"))
            except (TypeError, ValueError):
                coords = None
            if coords is not None:
                map_stop_projections[stop_id] = {
                    "place_id": slot.place_id,
                    "canonical_name": str(place.get("name") or slot.place_id),
                    "coords": coords.model_dump(mode="json"),
                    "coordinate_role": "CLIENT_SUPPLIED_ITINERARY_COORDINATE",
                    "provenance": "legacy_itinerary_v1:unverified_client_payload",
                    "receipt_hash": None,
                }
        days.append(ItineraryDay(day_index=day.day_index, date=day.date, stops=stops))

    try:
        created_at = datetime.fromisoformat(itinerary.generated_at.replace("Z", "+00:00"))
    except ValueError:
        created_at = datetime.now(timezone.utc)
    content = ItineraryRevisionContent(
        itinerary_id=itinerary.itinerary_id,
        workspace_id=workspace_id,
        revision=itinerary.version,
        parent_revision=parent_revision,
        source_type=source_type,
        city=itinerary.city,
        date_range=date_range,
        days=days,
        locked_commitments=[stop.stop_id for day in days for stop in day.stops if stop.locked or stop.fixed_commitment],
        change_summary={
            "adapter": "legacy_itinerary_v1",
            "map_stop_projections": map_stop_projections,
        },
        created_by=created_by,
        created_at=created_at,
    )
    return with_content_hash(content)


def revision_to_legacy(
    revision: ItineraryRevision,
    *,
    thread_id: str,
    place_lookup: dict[str, dict] | None = None,
    preserve_unknown_times: bool = False,
) -> Itinerary:
    place_lookup = place_lookup or {}
    days: list[DayPlan] = []
    for day in revision.days:
        slots: list[TimeSlot] = []
        for stop in day.stops:
            place = dict(place_lookup.get(stop.place_id, {}))
            place.setdefault("place_id", stop.place_id)
            place.setdefault("name", stop.raw_name or stop.place_id)
            place.setdefault("category", stop.category)
            if stop.commitment_kind is not None:
                place["commitment_kind"] = stop.commitment_kind.value
            transport = None
            if stop.transport_to_next is not None:
                transport = TransportLeg(
                    mode=stop.transport_to_next.mode,
                    duration_mins=stop.transport_to_next.duration_minutes or 0,
                    distance_km=(stop.transport_to_next.distance_meters or 0) / 1000,
                )
            slots.append(TimeSlot(
                place_id=stop.place_id,
                place=place,
                start_time=stop.start_time or ("" if preserve_unknown_times else "09:00"),
                end_time=(
                    stop.end_time
                    or ("" if preserve_unknown_times else stop.start_time or "09:00")
                ),
                transport=transport,
            ))
        days.append(DayPlan(day_index=day.day_index, date=day.date.isoformat() if day.date else None, cluster_id=day.day_index, slots=slots))
    return Itinerary(
        itinerary_id=revision.itinerary_id,
        thread_id=thread_id,
        city=revision.city,
        days=days,
        generated_at=revision.created_at.isoformat(),
        version=revision.revision,
    )
