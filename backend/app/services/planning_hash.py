"""Canonical collaborative planning input hashing."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from app.schemas.place import Place
from app.schemas.task_spec import TripTaskSpec


def _place_payload(item: Place | dict[str, Any]) -> dict[str, Any]:
    raw = item.model_dump(mode="json") if isinstance(item, Place) else dict(item)
    return {
        "place_id": raw.get("place_id") or raw.get("placeId"),
        "name": raw.get("name"),
        "category": raw.get("category"),
        "price": raw.get("amap_price", raw.get("amapPrice")),
        "opening_hours": raw.get("opening_hours", raw.get("openingHours")),
        "estimated_duration": raw.get("estimated_duration", raw.get("estimatedDuration")),
        "voted_by": sorted(raw.get("voted_by", raw.get("votedBy", [])) or []),
        "pinned": bool(raw.get("is_pinned", raw.get("isPinned", False))),
        "excluded": bool(raw.get("excluded", False)),
    }


def compute_planning_input_hash(
    task_spec: TripTaskSpec,
    places: Iterable[Place | dict[str, Any]],
    itinerary_version: int,
) -> str:
    payload = {
        "task_spec": task_spec.model_dump(mode="json", exclude={"assumptions"}),
        "places": sorted((_place_payload(item) for item in places), key=lambda item: str(item["place_id"])),
        "itinerary_version": itinerary_version,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_verification_stale(current_hash: str, report_hash: str | None) -> bool:
    return not report_hash or current_hash != report_hash
