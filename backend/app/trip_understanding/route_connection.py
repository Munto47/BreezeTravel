"""Scope of returned route numbers, independent from route availability.

POI route IDs may select access points. Keep that useful supplier segment, but
do not silently treat it as a connection between the requested coordinates.
The six-decimal grid is the precision actually sent by AmapRouteProvider, not
an invented walking-distance tolerance. It does not establish entrance names,
opening status, or walkability of any missing connection.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
from typing import Literal

ConnectionStatus = Literal["VERIFIED", "UNVERIFIED"]
_GRID = Decimal("0.000001")


def coordinate_key(value: object) -> tuple[str, str] | None:
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (tuple, list)):
        parts = value
    else:
        return None
    if len(parts) != 2:
        return None
    try:
        lon, lat = (Decimal(str(part).strip()) for part in parts)
        if not lon.is_finite() or not lat.is_finite() or not -180 <= lon <= 180 or not -90 <= lat <= 90:
            return None
        return tuple(format(part.quantize(_GRID, rounding=ROUND_HALF_EVEN), ".6f") for part in (lon, lat))
    except (ValueError, InvalidOperation):
        return None


def stop_coordinates(stop) -> tuple[str, str] | None:
    if getattr(stop, "longitude", None) is None or getattr(stop, "latitude", None) is None:
        return None
    # Match the actual wire formatting rather than identity/name equality.
    return coordinate_key(f"{stop.longitude:.6f},{stop.latitude:.6f}")


def connection_evidence(origin, destination, geometry, *, geometry_break_indices=(), geometry_complete=True) -> dict:
    """Construct evidence for known pieces; the default is one authored segment.

    Provider adapters supply original piece boundaries and completeness. A
    missing step can leave drawable pieces while making the whole path unknown.
    Old receipts lack this proof and cannot be used for path comparison.
    """
    start = coordinate_key((geometry[0].longitude, geometry[0].latitude)) if geometry else None
    end = coordinate_key((geometry[-1].longitude, geometry[-1].latitude)) if geometry else None
    requested_start, requested_end = stop_coordinates(origin), stop_coordinates(destination)
    boundaries = list(geometry_break_indices) if geometry_break_indices is not None else None
    status = "VERIFIED" if geometry_complete is True and boundaries == [] and len(geometry) >= 2 and requested_start and requested_end and start == requested_start and end == requested_end else "UNVERIFIED"
    return {"status": status, "coordinate_precision": 6, "geometry_point_count": len(geometry),
            "requested_origin": requested_start, "requested_destination": requested_end,
            "returned_origin": start, "returned_destination": end, "geometry_break_indices": boundaries,
            "geometry_complete": geometry_complete is True}


def geometry_break_indices(binding: object) -> list[int] | None:
    scope = binding.get("route_connection") if isinstance(binding, dict) else None
    if not isinstance(scope, dict):
        return None
    value, count = scope.get("geometry_break_indices"), scope.get("geometry_point_count")
    if (not isinstance(value, list) or type(count) is not int or count < 2
            or any(type(i) is not int or not 0 < i < count for i in value)
            or value != sorted(set(value))):
        return None
    return value


def connection_status(binding: object) -> ConnectionStatus:
    if not isinstance(binding, dict):
        return "UNVERIFIED"
    scope = binding.get("route_connection")
    if (not isinstance(scope, dict) or scope.get("status") != "VERIFIED" or scope.get("coordinate_precision") != 6
            or type(scope.get("geometry_point_count")) is not int or scope["geometry_point_count"] < 2
            or scope.get("geometry_complete") is not True or geometry_break_indices(binding) != []):
        return "UNVERIFIED"
    origin, destination = coordinate_key(scope.get("requested_origin")), coordinate_key(scope.get("requested_destination"))
    return "VERIFIED" if origin and destination and origin == coordinate_key(scope.get("returned_origin")) and destination == coordinate_key(scope.get("returned_destination")) else "UNVERIFIED"


def verified_connection(fact, origin=None, destination=None) -> bool:
    if getattr(fact, "status", None) != "AVAILABLE" or connection_status(getattr(fact, "provider_binding", None)) != "VERIFIED":
        return False
    scope = fact.provider_binding["route_connection"]
    if origin is not None and stop_coordinates(origin) != coordinate_key(scope["requested_origin"]):
        return False
    if destination is not None and stop_coordinates(destination) != coordinate_key(scope["requested_destination"]):
        return False
    return True


def route_segment(fact, origin=None, destination=None) -> dict | None:
    """A returned segment may be usable without covering the POI centers."""
    binding = getattr(fact, "provider_binding", None)
    scope = binding.get("route_connection") if isinstance(binding, dict) else None
    if (getattr(fact, "status", None) != "AVAILABLE" or not isinstance(scope, dict) or scope.get("coordinate_precision") != 6
            or type(scope.get("geometry_point_count")) is not int or scope["geometry_point_count"] < 2
            or scope.get("geometry_complete") is not True or geometry_break_indices(binding) != []):
        return None
    if not all(coordinate_key(scope.get(key)) for key in ("requested_origin", "requested_destination", "returned_origin", "returned_destination")):
        return None
    if origin is not None and stop_coordinates(origin) != coordinate_key(scope["requested_origin"]):
        return None
    if destination is not None and stop_coordinates(destination) != coordinate_key(scope["requested_destination"]):
        return None
    return scope


def compared_path_scope(before, after) -> Literal["REQUESTED_POINTS", "RETURNED_SEGMENTS"] | None:
    """Compare only connected paths with equal actual outer endpoints."""
    if not before or not after:
        return None
    scopes = [[route_segment(fact) for fact in chain] for chain in (before, after)]
    if any(item is None for chain in scopes for item in chain):
        return None
    for chain in scopes:
        if any(coordinate_key(left["returned_destination"]) != coordinate_key(right["returned_origin"]) for left, right in zip(chain, chain[1:])):
            return None
    left, right = scopes
    if (coordinate_key(left[0]["returned_origin"]) != coordinate_key(right[0]["returned_origin"])
            or coordinate_key(left[-1]["returned_destination"]) != coordinate_key(right[-1]["returned_destination"])):
        return None
    return "REQUESTED_POINTS" if all(verified_connection(fact) for fact in [*before, *after]) else "RETURNED_SEGMENTS"
