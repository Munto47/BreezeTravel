"""Resolve provider and geographic evidence before candidate selection."""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from app.constraints.evidence import attach_constraint_evidence
from app.schemas.place import Coordinates, EvidenceStatus, GeoEvidence, Place
from app.schemas.recommendation_plan import RecommendationPlan


_CONFIRMATION_CHECKLISTS = {
    "accessible_room": "无台阶入口、电梯尺寸、无障碍浴室/客房配置与库存",
    "attraction_accessibility": "无台阶入口、无障碍卫生间、观光车与休息点",
    "hotel_transit_access": "到最近地铁口的实际步行路线、台阶与电梯",
}


def _distance_km(left: Place, right: Place) -> float:
    lat1, lon1 = radians(left.coords.lat), radians(left.coords.lng)
    lat2, lon2 = radians(right.coords.lat), radians(right.coords.lng)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(value))


def _distance_from_coords(left: Coordinates, right: Coordinates) -> float:
    lat1, lon1 = radians(left.lat), radians(left.lng)
    lat2, lon2 = radians(right.lat), radians(right.lng)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371.0088 * 2 * asin(sqrt(value))


def _anchor_candidate(anchor: str, places: list[Place]) -> Place | None:
    compact = "".join(anchor.lower().split())
    for place in places:
        names = [place.name, *place.canonical_entity_names]
        if any(compact in "".join(name.lower().split()) or "".join(name.lower().split()) in compact for name in names):
            return place
    return None


def _summary_status(place: Place) -> EvidenceStatus:
    statuses = {
        item.status for item in [*place.constraint_evidence, *place.geo_evidence]
    }
    if EvidenceStatus.REQUIRES_CONFIRMATION in statuses:
        return EvidenceStatus.REQUIRES_CONFIRMATION
    if EvidenceStatus.UNKNOWN in statuses:
        return EvidenceStatus.UNKNOWN
    return EvidenceStatus.VERIFIED


def _confirmation_actions(place: Place) -> list[str]:
    pending = [
        (
            f"{item.label}（{_CONFIRMATION_CHECKLISTS[item.constraint]}）"
            if item.constraint in _CONFIRMATION_CHECKLISTS
            else item.label
        )
        for item in place.constraint_evidence
        if item.status != EvidenceStatus.VERIFIED
    ]
    geo_pending = [
        item.anchor_place for item in place.geo_evidence
        if item.status != EvidenceStatus.VERIFIED
    ]
    actions: list[str] = []
    contact = f"致电 {place.phone}" if place.phone else "通过场所官方电话或预订页面联系"
    if pending:
        actions.append(f"{contact}确认：{'、'.join(pending)}")
    if geo_pending:
        actions.append(
            "打开地图路线功能核实与"
            + "、".join(dict.fromkeys(geo_pending))
            + "之间的实际通勤时间和路线"
        )
    return actions


def finalize_place_evidence(place: Place) -> Place:
    """Recompute summary/action fields after constraint or route evidence changes."""
    return place.model_copy(update={
        "selection_evidence_status": _summary_status(place),
        "confirmation_actions": _confirmation_actions(place),
    })


def resolve_candidate_evidence(
    places: list[Place],
    user_request: str,
    district_constraint: str | None = None,
    plan: RecommendationPlan | dict[str, Any] | None = None,
) -> list[Place]:
    """Attach evidence without using LLM descriptions or tags as proof."""

    resolved = attach_constraint_evidence(places, user_request, district_constraint)
    parsed = (
        plan if isinstance(plan, RecommendationPlan)
        else RecommendationPlan.model_validate(plan) if plan else None
    )
    if not parsed:
        return [
            finalize_place_evidence(place)
            for place in resolved
        ]

    output: list[Place] = []
    for place in resolved:
        geo_items: list[GeoEvidence] = []
        for slot in parsed.slots:
            if slot.slot_id not in place.recommendation_slot_ids or not slot.geo.anchor_place:
                continue
            anchor = _anchor_candidate(slot.geo.anchor_place, resolved)
            anchor_coords = slot.geo.anchor_coords or (anchor.coords if anchor else None)
            if anchor_coords is None:
                geo_items.append(GeoEvidence(
                    slot_id=slot.slot_id,
                    anchor_place=slot.geo.anchor_place,
                    status=EvidenceStatus.UNKNOWN,
                    transport_mode=slot.geo.transport_mode,
                    source="anchor_coordinates_unavailable",
                ))
                continue
            distance = _distance_from_coords(anchor_coords, place.coords)
            within_radius = slot.geo.max_radius_km is None or distance <= slot.geo.max_radius_km
            if slot.geo.max_radius_km is not None:
                geo_items.append(GeoEvidence(
                    slot_id=slot.slot_id,
                    anchor_place=slot.geo.anchor_place,
                    constraint_kind="proximity",
                    status=EvidenceStatus.VERIFIED,
                    satisfies_constraint=within_radius,
                    straight_line_distance_km=round(distance, 3),
                    transport_mode=slot.geo.transport_mode,
                    source="amap_anchor_coordinates_radius",
                    route_response_hash=slot.geo.anchor_response_hash,
                    observed_at=slot.geo.anchor_observed_at,
                ))
            if slot.geo.max_travel_minutes is not None or slot.geo.max_transfers is not None:
                geo_items.append(GeoEvidence(
                    slot_id=slot.slot_id,
                    anchor_place=slot.geo.anchor_place,
                    constraint_kind="route",
                    status=EvidenceStatus.UNKNOWN,
                    straight_line_distance_km=round(distance, 3),
                    transport_mode=slot.geo.transport_mode,
                    source="route_time_not_queried",
                ))
            elif slot.geo.max_radius_km is None:
                geo_items.append(GeoEvidence(
                    slot_id=slot.slot_id,
                    anchor_place=slot.geo.anchor_place,
                    constraint_kind="proximity",
                    status=EvidenceStatus.VERIFIED,
                    satisfies_constraint=True,
                    straight_line_distance_km=round(distance, 3),
                    transport_mode=slot.geo.transport_mode,
                    source="amap_coordinates_radius",
                ))
        place_with_geo = place.model_copy(update={"geo_evidence": geo_items})
        output.append(finalize_place_evidence(place_with_geo))
    return output
