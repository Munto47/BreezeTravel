"""Selected collaboration places -> relative days -> bounded real driving legs.

Geographic grouping proposes an order; it is not a shortest-driving-time claim.
Only the final directed adjacent legs are queried. There is no clock scheduler,
weather request, model request, capacity removal or estimated travel duration.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from app.agents.nodes.optimizer import _haversine_km, _kmeans_cluster
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot, TransportLeg
from app.schemas.place import Place

logger = logging.getLogger(__name__)
DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"


def relative_days(places: list[Place], trip_days: int) -> list[list[Place]]:
    if not places or not 1 <= trip_days <= 14:
        raise ValueError("请选择地点和1至14天的相对行程")
    ids = [place.place_id for place in places]
    if len(set(ids)) != len(ids):
        raise ValueError("所选地点重复，请刷新房间后再排线")
    if any(not place.name.strip() or not place.city.strip() or
           not math.isfinite(place.coords.lng) or not math.isfinite(place.coords.lat) or
           not -180 <= place.coords.lng <= 180 or not -90 <= place.coords.lat <= 90
           for place in places):
        raise ValueError("有地点的位置尚未确认，请确认后再排线")
    # The existing geographic grouping has no duration or opening-hours input.
    # Include hotels as selected visits instead of silently consuming a hotel pool.
    clustered = _kmeans_cluster(places, trip_days)
    groups: dict[int, list[Place]] = {}
    for place in clustered:
        groups.setdefault(place.cluster_id or 0, []).append(place)
    # Stable Day labels follow the first selected member of each geographic group.
    groups_in_order = sorted(groups.values(), key=lambda group: min(ids.index(p.place_id) for p in group))
    result = []
    for group in groups_in_order:
        remaining = list(group)
        ordered = [remaining.pop(0)]
        while remaining:
            next_place = min(remaining, key=lambda p: _haversine_km(ordered[-1], p))
            remaining.remove(next_place)
            ordered.append(next_place)
        result.append(ordered)
    return result + [[] for _ in range(trip_days - len(result))]


async def plan_relative_route(
    places: list[Place], *, trip_days: int, thread_id: str, api_key: str,
    client: httpx.AsyncClient, deadline_seconds: float = 20.0,
) -> tuple[Itinerary, float | None]:
    ordered_days = relative_days(places, trip_days)
    deadline = time.monotonic() + deadline_seconds
    attempted = available = leg_count = 0
    total_distance = 0.0
    days = []
    for day_index, ordered in enumerate(ordered_days):
        slots = []
        for position, place in enumerate(ordered):
            transport = None
            has_next = position + 1 < len(ordered)
            if has_next:
                leg_count += 1
            remaining = deadline - time.monotonic()
            if has_next and api_key and remaining > 0:
                destination = ordered[position + 1]
                attempted += 1
                try:
                    # At most n-days directed requests, each once, within one deadline.
                    async with asyncio.timeout(min(6.0, remaining)):
                        response = await client.get(DRIVING_URL, params={
                            "key": api_key, "origin": f"{place.coords.lng},{place.coords.lat}",
                            "destination": f"{destination.coords.lng},{destination.coords.lat}", "output": "json",
                        }, timeout=min(6.0, remaining))
                    response.raise_for_status()
                    payload = response.json()
                    paths = payload.get("route", {}).get("paths", []) if isinstance(payload, dict) else []
                    if payload.get("status") == "1" and paths:
                        duration = float(paths[0]["duration"])
                        distance = float(paths[0]["distance"])
                        if math.isfinite(duration) and math.isfinite(distance) and duration > 0 and distance > 0:
                            transport = TransportLeg(mode="driving", status="AVAILABLE",
                                duration_mins=max(1, math.ceil(duration / 60)), distance_km=distance / 1000)
                            available += 1
                            total_distance += transport.distance_km
                except (httpx.HTTPError, TimeoutError, ValueError, TypeError, KeyError, AttributeError, IndexError):
                    # An unavailable edge never deletes a selected visit or becomes zero minutes.
                    pass
            slots.append(TimeSlot(place_id=place.place_id, place=place.model_dump(mode="json"), transport=transport))
        days.append(DayPlan(day_index=day_index, cluster_id=day_index, slots=slots))
    logger.info("collaboration_relative_route selected=%s driving_attempted=%s driving_available=%s",
                len(places), attempted, available)
    return Itinerary(itinerary_id=str(uuid4()), thread_id=thread_id, city=places[0].city,
                     days=days, generated_at=datetime.now(timezone.utc).isoformat()), (
                         round(total_distance, 2) if available == leg_count else None)
