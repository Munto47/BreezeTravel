from __future__ import annotations

import asyncio
import hashlib
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from app.trip_understanding.errors import RouteProviderUnavailableError
from app.trip_understanding.map_render import InternalRouteModeFact, MapStop
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
) -> tuple[int, int, int] | None:
    route = payload.get("route")
    if not isinstance(route, dict):
        return None
    if mode == "walking":
        paths = route.get("paths")
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
            return None
        first = paths[0]
        transfer_count = 0
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
    )


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
        duration_minutes, distance_meters, transfer_count = parsed
        return InternalRouteModeFact(
            mode=mode,
            status="AVAILABLE",
            duration_minutes=duration_minutes,
            distance_meters=distance_meters,
            transfer_count=transfer_count,
            response_hash=response_hash,
            request_hash=request_hash,
            provider_binding=binding,
            external_call_count=1,
            observed_at=response_observed_at,
            expires_at=response_observed_at + timedelta(hours=24),
        )
