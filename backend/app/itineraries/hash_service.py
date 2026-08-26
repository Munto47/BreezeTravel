from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from app.itineraries.models import ItineraryRevision, ItineraryRevisionContent


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    """Serialize semantic input deterministically across key order and platforms."""

    return json.dumps(_normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def revision_semantic_payload(revision: ItineraryRevisionContent | ItineraryRevision) -> dict[str, Any]:
    days = []
    for day in sorted(revision.days, key=lambda item: item.day_index):
        stops = []
        for stop in sorted(day.stops, key=lambda item: item.order_index):
            transport = stop.transport_to_next
            stops.append({
                "stop_id": stop.stop_id,
                "place_id": stop.place_id,
                "order_index": stop.order_index,
                "start_time": stop.start_time,
                "end_time": stop.end_time,
                "visit_duration_minutes": stop.visit_duration_minutes,
                "transport_mode": transport.mode if transport else None,
                "locked": stop.locked,
                "commitment_kind": stop.commitment_kind,
                "fixed_commitment": stop.fixed_commitment,
            })
        days.append({"day_index": day.day_index, "date": day.date, "stops": stops})
    return {
        "city": revision.city,
        "date_range": revision.date_range.model_dump(mode="json"),
        "days": days,
    }


def compute_content_hash(revision: ItineraryRevisionContent | ItineraryRevision) -> str:
    return sha256_canonical(revision_semantic_payload(revision))


def compute_report_input_hash(
    *,
    workspace_id: str,
    task_id: str,
    task_revision: int,
    itinerary_id: str,
    itinerary_revision: int,
    content_hash: str,
    member_constraint_revisions: Mapping[str, int] | Iterable[tuple[str, int]],
    place_resolution_versions: Mapping[str, int] | Iterable[tuple[str, int]],
    evidence_snapshot_id: str,
    audit_rule_set_version: str,
) -> str:
    member_items = member_constraint_revisions.items() if isinstance(member_constraint_revisions, Mapping) else member_constraint_revisions
    resolution_items = place_resolution_versions.items() if isinstance(place_resolution_versions, Mapping) else place_resolution_versions
    return sha256_canonical({
        "workspace_id": workspace_id,
        "task": {"task_id": task_id, "revision": task_revision},
        "itinerary": {
            "itinerary_id": itinerary_id,
            "revision": itinerary_revision,
            "content_hash": content_hash,
        },
        "member_constraint_revisions": sorted((str(key), int(value)) for key, value in member_items),
        "place_resolution_versions": sorted((str(key), int(value)) for key, value in resolution_items),
        "evidence_snapshot_id": evidence_snapshot_id,
        "audit_rule_set_version": audit_rule_set_version,
    })


def compute_command_request_hash(command_payload: Mapping[str, Any]) -> str:
    return sha256_canonical(command_payload)


def with_content_hash(content: ItineraryRevisionContent) -> ItineraryRevision:
    return ItineraryRevision(**content.model_dump(), content_hash=compute_content_hash(content))
