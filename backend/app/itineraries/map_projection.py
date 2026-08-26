"""Canonical, coordinate-only map projection for a persisted itinerary revision.

An itinerary stop intentionally does not carry a latitude/longitude.  This
read model may use only an explicit coordinate projection recorded by the
authoritative revision/template path.  In particular it must never geocode a
name, reuse a coordinate after a replacement, or manufacture a city centre.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.itineraries.models import ItineraryRevision, ItineraryStop
from app.schemas.place import Coordinates


class MapStopProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    stop_id: str
    place_id: str
    name: str
    day_index: int = Field(ge=0)
    order_index: int = Field(ge=0)
    coords: Coordinates
    coordinate_role: str
    provenance: str
    receipt_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    projection_revision: int = Field(gt=0)


class MapCoordinateLink(BaseModel):
    model_config = ConfigDict(frozen=True)

    day_index: int = Field(ge=0)
    from_stop_id: str
    to_stop_id: str
    # This deliberately is not a route-provider result.  It is the only
    # honest visual linkage available when we have coordinates but no route
    # geometry/evidence for this exact revision edge.
    kind: str = "CANONICAL_COORDINATE_LINK"


class MapProjection(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str
    revision: int = Field(gt=0)
    city: str
    stops: list[MapStopProjection] = Field(default_factory=list)
    coordinate_links: list[MapCoordinateLink] = Field(default_factory=list)
    missing_stop_ids: list[str] = Field(default_factory=list)
    status: str
    unavailable_reason: str | None = None


def _all_stops(revision: ItineraryRevision) -> Iterable[ItineraryStop]:
    for day in revision.days:
        yield from day.stops


def _projection_entries(revision: ItineraryRevision) -> dict[str, dict[str, Any]]:
    """Return only known, serialized template/revision coordinate entries.

    ``template_anchor_places`` is written by TemplateApplicationService.  The
    second key permits a future canonical revision writer to persist explicit
    coordinates without teaching clients to infer anything.  Both formats are
    checked again against the *current* stop id and place id below.
    """
    summary = revision.change_summary
    raw = summary.get("map_stop_projections")
    if not isinstance(raw, dict):
        raw = summary.get("template_anchor_places")
    return raw if isinstance(raw, dict) else {}


def _entry_for_stop(
    entry: object,
    stop: ItineraryStop,
) -> tuple[Coordinates, str, str, str | None, str | None] | None:
    if not isinstance(entry, dict):
        return None
    # Template entries use ``place``.  A future revision projection uses the
    # compact ``place_id`` / ``coords`` shape.  Neither obtains an implicit
    # fallback from the stop name.
    place = entry.get("place")
    raw_place_id = entry.get("place_id")
    raw_coords = entry.get("coords")
    if isinstance(place, dict):
        raw_place_id = place.get("place_id")
        raw_coords = place.get("coords")
    if raw_place_id != stop.place_id or not isinstance(raw_coords, dict):
        return None
    try:
        coords = Coordinates.model_validate(raw_coords)
    except (TypeError, ValueError):
        return None
    role = entry.get("coordinate_role")
    provenance = entry.get("provenance")
    if not isinstance(role, str) or not role or not isinstance(provenance, str) or not provenance:
        return None
    canonical_name = entry.get("canonical_name")
    if canonical_name is None and isinstance(place, dict):
        canonical_name = place.get("name")
    if not isinstance(canonical_name, str) or not canonical_name:
        canonical_name = None
    receipt_hash = entry.get("receipt_hash")
    if not isinstance(receipt_hash, str) or re.fullmatch(r"[0-9a-f]{64}", receipt_hash) is None:
        receipt_hash = None
    return coords, role, provenance, canonical_name, receipt_hash


def build_map_projection(
    revision: ItineraryRevision,
    *,
    lineage: Iterable[ItineraryRevision],
) -> MapProjection:
    """Build one revision projection from its own and ancestor projections.

    Stop IDs are durable through moves/reorders.  The exact ``(stop_id,
    place_id)`` match prevents a REPLACE_STOP from inheriting the old place's
    coordinate.  A newer explicit projection wins over an ancestor one.
    """
    lineage = list(lineage)
    entries_by_stop: dict[str, tuple[str, Coordinates, str, str, str | None, str | None, int]] = {}
    for ancestor in lineage:
        for stop_id, entry in _projection_entries(ancestor).items():
            if not isinstance(stop_id, str) or stop_id in entries_by_stop:
                continue
            # Verify this entry against the source revision too.  This stops a
            # malformed summary from becoming a coordinate authority.
            source_stop = next((item for item in _all_stops(ancestor) if item.stop_id == stop_id), None)
            if source_stop is None:
                continue
            parsed = _entry_for_stop(entry, source_stop)
            if parsed is not None:
                coords, role, provenance, canonical_name, receipt_hash = parsed
                entries_by_stop[stop_id] = (
                    source_stop.place_id,
                    coords,
                    role,
                    provenance,
                    canonical_name,
                    receipt_hash,
                    ancestor.revision,
                )

    projected: list[MapStopProjection] = []
    missing: list[str] = []
    projected_ids: set[str] = set()
    for day in revision.days:
        for stop in day.stops:
            candidate = entries_by_stop.get(stop.stop_id)
            # Revalidate the place id on the target revision.  This is the
            # critical replacement invalidation boundary.
            if candidate is None:
                missing.append(stop.stop_id)
                continue
            source_place_id, coords, role, provenance, canonical_name, receipt_hash, source_revision = candidate
            if source_place_id != stop.place_id:
                missing.append(stop.stop_id)
                continue
            projected.append(MapStopProjection(
                stop_id=stop.stop_id, place_id=stop.place_id,
                name=canonical_name or stop.raw_name or stop.place_id,
                day_index=day.day_index, order_index=stop.order_index,
                coords=coords, coordinate_role=role, provenance=provenance,
                receipt_hash=receipt_hash,
                projection_revision=source_revision,
            ))
            projected_ids.add(stop.stop_id)

    links = [
        MapCoordinateLink(day_index=day.day_index, from_stop_id=left.stop_id, to_stop_id=right.stop_id)
        for day in revision.days
        for left, right in zip(day.stops, day.stops[1:])
        if left.stop_id in projected_ids and right.stop_id in projected_ids
    ]
    if not projected:
        status, reason = "UNAVAILABLE", "REVISION_STOP_COORDINATES_UNAVAILABLE"
    elif missing:
        status, reason = "PARTIAL", "SOME_REVISION_STOP_COORDINATES_UNAVAILABLE"
    else:
        status, reason = "AVAILABLE", None
    return MapProjection(
        workspace_id=revision.workspace_id, revision=revision.revision, city=revision.city,
        stops=projected, coordinate_links=links, missing_stop_ids=missing,
        status=status, unavailable_reason=reason,
    )
