from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from app.trip_understanding.errors import RouteProviderUnavailableError
from app.trip_understanding.map_render import InternalRouteModeFact, MapStop, RouteGeometryPoint
from app.trip_understanding.pipeline import canonical_sha256


AMAP_WALKING_ENDPOINT = "https://restapi.amap.com/v3/direction/walking"
AMAP_TRANSIT_ENDPOINT = "https://restapi.amap.com/v3/direction/transit/integrated"
_ENDPOINTS = {
    "walking": AMAP_WALKING_ENDPOINT,
    "transit": AMAP_TRANSIT_ENDPOINT,
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _coordinates(stop: MapStop) -> str | None:
    if stop.longitude is None or stop.latitude is None:
        return None
    return f"{stop.longitude:.6f},{stop.latitude:.6f}"


def _provider_request_id_hash(response: httpx.Response) -> str:
    for header in ("x-request-id", "x-acs-request-id", "x-trace-id"):
        value = response.headers.get(header)
        if value:
            return _sha256_text(value)
    return "NOT_EXPOSED_BY_PROVIDER"


def _parse_route(
    payload: dict[str, Any],
    mode: Literal["walking", "transit"],
) -> tuple[int, int, int, list[RouteGeometryPoint]] | None:
    route = payload.get("route")
    if not isinstance(route, dict):
        return None
    if mode == "walking":
        paths = route.get("paths")
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
            return None
        first = paths[0]
        transfer_count = 0
        geometry = _walking_geometry(first)
    else:
        transits = route.get("transits")
        if (
            not isinstance(transits, list)
            or not transits
            or not isinstance(transits[0], dict)
        ):
            return None
        first = transits[0]
        ride_legs = 0
        segments = first.get("segments")
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                bus = segment.get("bus")
                buslines = bus.get("buslines") if isinstance(bus, dict) else None
                railway = segment.get("railway")
                railway_name = railway.get("name") if isinstance(railway, dict) else None
                if (isinstance(buslines, list) and buslines) or railway_name:
                    ride_legs += 1
        transfer_count = max(0, ride_legs - 1)
        geometry = _transit_geometry(first)
    try:
        duration_seconds = float(first.get("duration") or 0)
        distance_meters = float(first.get("distance") or route.get("distance") or 0)
    except (TypeError, ValueError):
        return None
    if duration_seconds <= 0 or distance_meters <= 0:
        return None
    return (
        max(1, math.ceil(duration_seconds / 60)),
        max(1, round(distance_meters)),
        transfer_count,
        geometry,
    )


def _polyline_pieces(value: object) -> list[list[RouteGeometryPoint] | None]:
    if not isinstance(value, str) or not value:
        return []
    result: list[list[RouteGeometryPoint] | None] = []
    current: list[RouteGeometryPoint] = []
    for raw_point in value.split(";"):
        parts = raw_point.split(",")
        try:
            if len(parts) != 2:
                raise ValueError("invalid route coordinate")
            point = RouteGeometryPoint(
                longitude=float(parts[0]),
                latitude=float(parts[1]),
            )
        except (TypeError, ValueError):
            # Invalid coordinates are a missing piece, not permission to join
            # the valid coordinates on either side into a fictitious segment.
            if current:
                result.append(current)
                current = []
            result.append(None)
            continue
        if not current or current[-1] != point:
            current.append(point)
    if current:
        result.append(current)
    return result


def _append_geometry(target: list[RouteGeometryPoint], points: list[RouteGeometryPoint]) -> None:
    for point in points:
        if not target or target[-1] != point:
            target.append(point)


def _walking_geometry(path: dict[str, Any]) -> list[RouteGeometryPoint]:
    return _join_geometry_pieces(_walking_pieces(path))[0]


def _has_travel(value: dict[str, Any]) -> bool:
    for key in ("distance", "duration"):
        try:
            if float(value.get(key) or 0) > 0:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _walking_pieces(path: dict[str, Any]) -> list[list[RouteGeometryPoint] | None]:
    pieces: list[list[RouteGeometryPoint] | None] = []
    steps = path.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                parsed = _polyline_pieces(step.get("polyline"))
                pieces.extend(parsed)
                if sum(len(part) for part in parsed if part) < 2 and (_has_travel(step) or step.get("instruction")):
                    pieces.append(None)
            else:
                pieces.append(None)
    if not pieces and _has_travel(path):
        pieces.append(None)
    return pieces


def _join_geometry_pieces(pieces: list[list[RouteGeometryPoint] | None]) -> tuple[list[RouteGeometryPoint], list[int] | None, bool]:
    from app.trip_understanding.route_connection import coordinate_key
    result: list[RouteGeometryPoint] = []
    breaks: list[int] = []
    unknown = not pieces or any(piece is None for piece in pieces)
    missing_since_piece = False
    for points in pieces:
        if points is None:
            missing_since_piece = True
            continue
        if not points:
            continue
        split = bool(result) and (missing_since_piece or coordinate_key((result[-1].longitude, result[-1].latitude)) != coordinate_key((points[0].longitude, points[0].latitude)))
        if split:
            breaks.append(len(result))
            # Preserve both endpoints when a missing step begins and ends at
            # the same coordinate; deduplication must not erase its boundary.
            result.append(points[0])
            _append_geometry(result, points[1:])
        else:
            _append_geometry(result, points)
        missing_since_piece = False
    return result, breaks if len(result) >= 2 else None, not unknown


def _transit_geometry(transit: dict[str, Any]) -> list[RouteGeometryPoint]:
    return _join_geometry_pieces(_transit_pieces(transit))[0]


def _transit_pieces(transit: dict[str, Any]) -> list[list[RouteGeometryPoint] | None]:
    pieces: list[list[RouteGeometryPoint] | None] = []
    segments = transit.get("segments")
    if not isinstance(segments, list):
        return [None]
    for segment in segments:
        if not isinstance(segment, dict):
            pieces.append(None)
            continue
        before_segment = len(pieces)
        walking = segment.get("walking")
        if isinstance(walking, dict):
            pieces.extend(_walking_pieces(walking))
        bus = segment.get("bus")
        buslines = bus.get("buslines") if isinstance(bus, dict) else None
        if isinstance(buslines, list):
            for busline in buslines:
                if isinstance(busline, dict):
                    parsed = _polyline_pieces(busline.get("polyline"))
                    pieces.extend(parsed)
                    if sum(len(part) for part in parsed if part) < 2 and (_has_travel(busline) or busline.get("name") or busline.get("id")):
                        pieces.append(None)
        railway = segment.get("railway")
        if isinstance(railway, dict):
            # Station endpoints are not a railway polyline. Retain the points,
            # but never invent a straight rail segment between them.
            for key in ("departure_stop", "arrival_stop"):
                stop = railway.get(key)
                if isinstance(stop, dict):
                    pieces.extend(_polyline_pieces(stop.get("location")))
            if railway:
                pieces.append(None)
        if _has_travel(segment) and len(pieces) == before_segment:
            pieces.append(None)
    return pieces


def _geometry_structure(payload: dict[str, Any], mode: str) -> tuple[list[int] | None, bool]:
    route = payload.get("route", {})
    candidates = route.get("paths" if mode == "walking" else "transits", [])
    if not candidates or not isinstance(candidates[0], dict):
        return None, False
    pieces = _walking_pieces(candidates[0]) if mode == "walking" else _transit_pieces(candidates[0])
    _, breaks, complete = _join_geometry_pieces(pieces)
    return breaks, complete


class AmapRouteProvider:
    """Bounded live walking/transit adapter for the asynchronous G01 map job."""

    def __init__(
        self,
        *,
        api_key: str,
        deadline_seconds: float = 6.0,
        max_concurrency: int = 4,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Amap API key is required")
        if deadline_seconds <= 0:
            raise ValueError("Amap route deadline must be positive")
        if max_concurrency < 1 or max_concurrency > 8:
            raise ValueError("Amap route concurrency must be between 1 and 8")
        self.api_key = api_key
        self.deadline_seconds = deadline_seconds
        self.client = client
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def route(
        self,
        origin: MapStop,
        destination: MapStop,
        mode: Literal["walking", "transit"],
        *,
        observed_at: datetime,
    ) -> InternalRouteModeFact:
        origin_coordinates = _coordinates(origin)
        destination_coordinates = _coordinates(destination)
        if origin_coordinates is None or destination_coordinates is None:
            raise RouteProviderUnavailableError(
                "ROUTE_ENDPOINT_COORDINATES_UNAVAILABLE",
                provider_binding={
                    "provider": "AMAP_ROUTE_V3",
                    "mode": mode,
                    "external_calls": 0,
                    "raw_provider_response_retained": False,
                },
                external_call_count=0,
            )
        if mode == "transit" and (not origin.city or not destination.city):
            raise RouteProviderUnavailableError(
                "ROUTE_ENDPOINT_CITY_UNAVAILABLE",
                provider_binding={
                    "provider": "AMAP_ROUTE_V3",
                    "mode": mode,
                    "external_calls": 0,
                    "raw_provider_response_retained": False,
                },
                external_call_count=0,
            )

        endpoint = _ENDPOINTS[mode]
        params: dict[str, object] = {
            "key": self.api_key,
            "origin": origin_coordinates,
            "destination": destination_coordinates,
            "output": "json",
        }
        if mode == "walking":
            if origin.canonical_place_id:
                params["origin_id"] = origin.canonical_place_id
            if destination.canonical_place_id:
                params["destination_id"] = destination.canonical_place_id
        else:
            params.update(
                {
                    "city": origin.city,
                    "cityd": destination.city,
                    "strategy": "0",
                    "nightflag": "0",
                }
            )
        safe_params = {key: value for key, value in params.items() if key != "key"}
        request_hash = canonical_sha256(
            {"method": "GET", "endpoint": endpoint, "params": safe_params}
        )
        started = time.perf_counter()
        try:
            async with self.semaphore:
                if self.client is not None:
                    response = await self.client.get(
                        endpoint,
                        params=params,
                        timeout=self.deadline_seconds,
                    )
                else:
                    async with httpx.AsyncClient(timeout=self.deadline_seconds) as client:
                        response = await client.get(endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise RouteProviderUnavailableError(
                "DEADLINE_EXCEEDED",
                provider_binding={
                    "provider": "AMAP_ROUTE_V3",
                    "mode": mode,
                    "endpoint_sha256": _sha256_text(endpoint),
                    "request_sha256": request_hash,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "raw_provider_response_retained": False,
                },
                external_call_count=1,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RouteProviderUnavailableError(
                "PROVIDER_UNAVAILABLE",
                provider_binding={
                    "provider": "AMAP_ROUTE_V3",
                    "mode": mode,
                    "endpoint_sha256": _sha256_text(endpoint),
                    "request_sha256": request_hash,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "raw_provider_response_retained": False,
                },
                external_call_count=1,
            ) from exc

        if not isinstance(payload, dict):
            raise RouteProviderUnavailableError(
                "INVALID_PROVIDER_RESPONSE",
                provider_binding={
                    "provider": "AMAP_ROUTE_V3",
                    "mode": mode,
                    "endpoint_sha256": _sha256_text(endpoint),
                    "request_sha256": request_hash,
                    "raw_provider_response_retained": False,
                },
                external_call_count=1,
            )
        response_hash = canonical_sha256(payload)
        response_observed_at = datetime.now(UTC)
        binding: dict[str, object] = {
            "provider": "AMAP_ROUTE_V3",
            "execution_mode": "LIVE",
            "mode": mode,
            "endpoint_sha256": _sha256_text(endpoint),
            "request_sha256": request_hash,
            "response_sha256": response_hash,
            "provider_request_id_sha256": _provider_request_id_hash(response),
            "http_status": response.status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "observed_at": response_observed_at.isoformat().replace("+00:00", "Z"),
            "external_calls": 1,
            "raw_provider_response_retained": False,
        }
        if payload.get("status") != "1":
            raise RouteProviderUnavailableError(
                "PROVIDER_STATUS_ERROR",
                provider_binding={
                    **binding,
                    "infocode": str(payload.get("infocode") or "NOT_EXPOSED_BY_PROVIDER"),
                },
                external_call_count=1,
            )
        parsed = _parse_route(payload, mode)
        if parsed is None:
            raise RouteProviderUnavailableError(
                "NO_ROUTE",
                provider_binding=binding,
                external_call_count=1,
            )
        duration_minutes, distance_meters, transfer_count, geometry = parsed
        from app.trip_understanding.route_connection import connection_evidence
        breaks, complete = _geometry_structure(payload, mode)
        binding["route_connection"] = connection_evidence(origin, destination, geometry,
            geometry_break_indices=breaks, geometry_complete=complete)
        return InternalRouteModeFact(
            mode=mode,
            status="AVAILABLE",
            duration_minutes=duration_minutes,
            distance_meters=distance_meters,
            transfer_count=transfer_count,
            geometry=geometry,
            response_hash=response_hash,
            request_hash=request_hash,
            provider_binding=binding,
            external_call_count=1,
            observed_at=response_observed_at,
            expires_at=response_observed_at + timedelta(hours=24),
        )
