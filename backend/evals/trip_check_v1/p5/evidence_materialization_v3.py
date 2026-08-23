"""Label-free, receipt-bound Provider materialization for P5 v3."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.audit.models import EvidenceFact, EvidenceFreshness, EvidenceSnapshot, ProviderFailure
from app.importing.confidence import normalize_place_name
from app.importing.parser import ItineraryTextParser
from app.importing.service import parse_time_range
from app.repairs.candidates import FrozenRepairCandidate, freeze_candidate_set
from app.trip_check.provider_integrity import ProviderCallReceipt
from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.evidence_materialization_v2 import (
    _artifact,
    _fact,
    _input_projection,
    _mapping,
    _observed_at,
    _required_text,
    _route_freshness,
    _sha256,
)


EVIDENCE_MATERIALIZATION_SCHEMA_V3 = "trip-check-p5-evidence-materialization-v3"
EVIDENCE_POLICY_VERSION_V3 = "trip-check-p5-controlled-evidence-v3"
PROVIDER_V3 = "trip-check-p5-controlled-provider-v3"


def _place(place_id: str, name: str, city: str, lng: float, lat: float) -> dict[str, Any]:
    return {
        "place_id": place_id,
        "name": name,
        "city": city,
        "district": "controlled-fixture",
        "address": "controlled-fixture",
        "category": "attraction",
        "coords": {"lng": lng, "lat": lat},
    }


_PLACES = (
    _place("bj-forbidden-city", "故宫博物院", "北京", 116.397, 39.918),
    _place("bj-temple-of-heaven", "天坛公园", "北京", 116.417, 39.883),
    _place("bj-summer-palace", "颐和园", "北京", 116.273, 39.999),
    _place("bj-jingshan", "景山公园", "北京", 116.396, 39.925),
    _place("bj-national-museum", "中国国家博物馆", "北京", 116.407, 39.905),
    _place("bj-capital-museum", "首都博物馆", "北京", 116.350, 39.907),
    _place("bj-tiananmen-square", "天安门广场", "北京", 116.397, 39.904),
    _place("bj-badaling", "八达岭长城", "北京", 116.016, 40.356),
    _place("bj-nanluoguxiang", "南锣鼓巷", "北京", 116.403, 39.938),
    _place("bj-sanlitun-taikooli", "三里屯太古里", "北京", 116.455, 39.933),
    _place("sh-bund", "外滩", "上海", 121.490, 31.241),
    _place("sh-yuyuan", "豫园", "上海", 121.493, 31.227),
    _place("sh-oriental-pearl", "东方明珠广播电视塔", "上海", 121.500, 31.240),
    _place("sh-oriental-pearl-park", "东方明珠公园", "上海", 121.501, 31.239),
    _place("sh-tianzifang", "田子坊", "上海", 121.475, 31.211),
    _place("sh-disney", "上海迪士尼乐园", "上海", 121.657, 31.144),
    _place("hz-west-lake", "西湖风景名胜区", "杭州", 120.148, 30.244),
    _place("hz-west-lake-cultural-square", "西湖文化广场", "杭州", 120.165, 30.281),
    _place("hz-lingyin", "灵隐寺", "杭州", 120.102, 30.240),
    _place("hz-leifeng", "雷峰塔", "杭州", 120.149, 30.231),
    _place("hz-xixi", "西溪湿地国家公园", "杭州", 120.063, 30.268),
    _place("hz-hefang", "河坊街·清河坊", "杭州", 120.174, 30.238),
    _place("hz-longjing", "龙井村", "杭州", 120.104, 30.219),
)
_PLACE_BY_ID = {item["place_id"]: item for item in _PLACES}
_ALIASES = {
    normalize_place_name(alias): _PLACE_BY_ID[place_id]
    for alias, place_id in {
        "故宫博物院": "bj-forbidden-city",
        "故宫": "bj-forbidden-city",
        "天坛公园": "bj-temple-of-heaven",
        "颐和园": "bj-summer-palace",
        "景山公园": "bj-jingshan",
        "中国国家博物馆": "bj-national-museum",
        "首都博物馆": "bj-capital-museum",
        "天安门广场": "bj-tiananmen-square",
        "长城（八达岭）": "bj-badaling",
        "八达岭长城": "bj-badaling",
        "南锣鼓巷": "bj-nanluoguxiang",
        "三里屯太古里": "bj-sanlitun-taikooli",
        "外滩": "sh-bund",
        "豫园": "sh-yuyuan",
        "东方明珠广播电视塔": "sh-oriental-pearl",
        "田子坊": "sh-tianzifang",
        "上海迪士尼乐园": "sh-disney",
        "西湖风景名胜区": "hz-west-lake",
        "灵隐寺": "hz-lingyin",
        "雷峰塔": "hz-leifeng",
        "西溪湿地国家公园": "hz-xixi",
        "河坊街·清河坊": "hz-hefang",
        "龙井村（茶园）": "hz-longjing",
        "龙井村": "hz-longjing",
    }.items()
}
_AMBIGUOUS = {
    normalize_place_name("博物馆"): ("bj-national-museum", "bj-capital-museum"),
    normalize_place_name("东方明珠"): ("sh-oriental-pearl", "sh-oriental-pearl-park"),
    normalize_place_name("西湖"): ("hz-west-lake", "hz-west-lake-cultural-square"),
}


def _receipt_semantic_payload(receipt: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "provider": receipt.get("provider"),
        "operation": receipt.get("operation"),
        "execution_mode": receipt.get("execution_mode"),
        "status": receipt.get("status"),
        "request_hash": receipt.get("request_hash"),
        "response_hash": receipt.get("response_hash"),
        "observed_at": receipt.get("observed_at"),
        "source_url": receipt.get("source_url"),
        "affected_fields": receipt.get("affected_fields", []),
        "failure_category": receipt.get("failure_category"),
    }


def _provider_receipt(
    *,
    provider: str,
    operation: str,
    status: str,
    request: Mapping[str, Any],
    response: Mapping[str, Any],
    observed_at: datetime,
    affected_fields: Sequence[str] = (),
    failure_category: str | None = None,
) -> ProviderCallReceipt:
    payload = {
        "provider": provider,
        "operation": operation,
        "execution_mode": "fixture",
        "status": status,
        "request_hash": digest(request),
        "response_hash": digest(response),
        "observed_at": observed_at,
        "source_url": f"fixture://trip-check-p5-v3/{operation}",
        "affected_fields": list(affected_fields),
        "failure_category": failure_category,
    }
    json_payload = ProviderCallReceipt(**payload, receipt_id="pending").model_dump(mode="json")
    return ProviderCallReceipt(**payload, receipt_id=digest(_receipt_semantic_payload(json_payload)))


def _resolution_candidates(normalized_name: str, city: str) -> tuple[str, list[dict[str, Any]]]:
    ambiguous_ids = _AMBIGUOUS.get(normalized_name)
    if ambiguous_ids is not None:
        candidates = [dict(_PLACE_BY_ID[item]) for item in ambiguous_ids]
        if all(item["city"] == city for item in candidates):
            return "NEEDS_CONFIRMATION", candidates
    exact = _ALIASES.get(normalized_name)
    if exact is None:
        return "NO_CANDIDATE", []
    if exact["city"] != city:
        return "HARD_REJECTED", [dict(exact)]
    return "AUTO_RESOLVED", [dict(exact)]


def _resolution_plan(
    *, text: str, city: str, case_id: str, observed_at: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[ProviderCallReceipt]]:
    parsed = ItineraryTextParser().parse(text, import_id=f"materialize-v3-{case_id}")
    stops: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    receipts: list[ProviderCallReceipt] = []
    for ordinal, raw_stop in enumerate(parsed.raw_stops):
        normalized_name = normalize_place_name(raw_stop.raw_name)
        outcome, candidates = _resolution_candidates(normalized_name, city)
        response = {"outcome": outcome, "candidates": candidates}
        search_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="place.search",
            status="SUCCEEDED",
            request={"query": raw_stop.raw_name, "city": city},
            response=response,
            observed_at=observed_at,
            affected_fields=(f"entity_resolutions.{ordinal}",),
        )
        receipts.append(search_receipt)
        selected = candidates[0]["place_id"] if outcome == "AUTO_RESOLVED" else None
        resolutions.append(
            {
                "ordinal": ordinal,
                "day_index": raw_stop.day_index,
                "raw_name": raw_stop.raw_name,
                "normalized_name": normalized_name,
                "outcome": outcome,
                "selected_place_id": selected,
                "search_receipt_id": search_receipt.receipt_id,
                "candidates": candidates,
            }
        )
        if selected is None:
            continue
        candidate = candidates[0]
        start_time, end_time, _duration = parse_time_range(raw_stop.raw_time)
        stops.append(
            {
                "stop_id": f"stop-{ordinal + 1}",
                "place_id": selected,
                "display_name": candidate["name"],
                "city": city,
                "day_index": raw_stop.day_index,
                "order_index": sum(item["day_index"] == raw_stop.day_index for item in stops),
                "start_time": start_time,
                "end_time": end_time,
                "coords": candidate["coords"],
            }
        )
    if not parsed.raw_stops:
        raise ValueError("controlled v3 materialization parsed no stops")
    return stops, resolutions, receipts


def build_evidence_materialization_v3(case_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build one v3 artifact from label-free product and runner inputs."""

    case_id = _required_text(case_payload.get("case_id"), field="case_id")
    city = _required_text(case_payload.get("city"), field="city")
    if city not in {"北京", "上海", "杭州"}:
        raise ValueError("city is outside the controlled P5 scope")
    trip_days = case_payload.get("trip_days")
    group_size = case_payload.get("group_size")
    if not isinstance(trip_days, int) or isinstance(trip_days, bool) or not 2 <= trip_days <= 5:
        raise ValueError("trip_days must be between 2 and 5")
    if not isinstance(group_size, int) or isinstance(group_size, bool) or not 2 <= group_size <= 5:
        raise ValueError("group_size must be between 2 and 5")
    normalized_input_sha256 = _sha256(
        case_payload.get("normalized_input_sha256"), field="normalized_input_sha256"
    )
    runner_control = _mapping(case_payload.get("runner_control"), field="runner_control")
    seed = runner_control.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("runner_control.seed must be an integer")
    product_input, raw_text = _input_projection(case_payload)
    if normalized_input_sha256 != digest(product_input):
        raise ValueError("normalized_input_sha256 does not bind the projected product input")
    observed_at = _observed_at(seed)
    stops, resolutions, receipts = _resolution_plan(
        text=raw_text, city=city, case_id=case_id, observed_at=observed_at
    )
    source_payload = _artifact(
        "trip-check-p5-source-payload-v3",
        f"source-{case_id}",
        case_id=case_id,
        city=city,
        trip_days=trip_days,
        group_size=group_size,
        input_kind=case_payload.get("input_kind"),
        normalized_input_sha256=normalized_input_sha256,
        projected_input_sha256=digest(product_input),
        product_input=product_input,
        stops=stops,
        entity_resolutions=resolutions,
    )
    snapshot_id = f"snapshot-{digest({'case_id': case_id, 'input': normalized_input_sha256, 'v': 3})[:24]}"
    facts: list[EvidenceFact] = []
    identity_receipts: dict[str, ProviderCallReceipt] = {}
    for stop in stops:
        identity_response = {
            "place_id": stop["place_id"],
            "name": stop["display_name"],
            "city": city,
            "coords": stop["coords"],
        }
        identity_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="place.resolve",
            status="SUCCEEDED",
            request={"query": stop["display_name"], "city": city},
            response=identity_response,
            observed_at=observed_at,
            affected_fields=(f"places.{stop['place_id']}.identity",),
        )
        if stop["place_id"] in identity_receipts:
            continue
        receipts.append(identity_receipt)
        identity_receipts[stop["place_id"]] = identity_receipt
        facts.append(
            _fact(
                snapshot_id=snapshot_id,
                subject_type="PLACE",
                subject_id=stop["place_id"],
                fact_type="POI_IDENTITY",
                value=identity_response,
                receipt=identity_receipt,
                freshness=EvidenceFreshness.FRESH,
                confidence=1,
            )
        )
        opening_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="place.opening_hours",
            status="SUCCEEDED",
            request={"place_id": stop["place_id"]},
            response={"opening_hours": "07:00-22:00"},
            observed_at=observed_at,
            affected_fields=(f"places.{stop['place_id']}.opening_hours",),
        )
        receipts.append(opening_receipt)
        facts.append(
            _fact(
                snapshot_id=snapshot_id,
                subject_type="PLACE",
                subject_id=stop["place_id"],
                fact_type="OPENING_HOURS",
                value="07:00-22:00",
                receipt=opening_receipt,
                freshness=EvidenceFreshness.FRESH,
                confidence=1,
            )
        )

    route_freshness = _route_freshness(runner_control)
    for left, right in zip(stops, stops[1:]):
        if left["day_index"] != right["day_index"]:
            continue
        edge_id = f"{left['stop_id']}->{right['stop_id']}"
        for conflict_index, duration in enumerate(
            (20, 55) if route_freshness == EvidenceFreshness.CONFLICTING else (20,)
        ):
            unavailable = route_freshness == EvidenceFreshness.UNAVAILABLE
            response = (
                {"reason_code": "PROVIDER_ROUTE_UNAVAILABLE"}
                if unavailable
                else {"mode": "driving", "duration_minutes": duration, "distance_km": 3.0}
            )
            route_receipt = _provider_receipt(
                provider=PROVIDER_V3 if conflict_index == 0 else f"{PROVIDER_V3}-alternate",
                operation="route.audit",
                status="UNAVAILABLE" if unavailable else "SUCCEEDED",
                request={"edge_id": edge_id, "mode": "driving", "source": conflict_index},
                response=response,
                observed_at=observed_at,
                affected_fields=(f"route_edges.{edge_id}",),
                failure_category="PROVIDER_ROUTE_UNAVAILABLE" if unavailable else None,
            )
            receipts.append(route_receipt)
            facts.append(
                _fact(
                    snapshot_id=snapshot_id,
                    subject_type="ROUTE_EDGE",
                    subject_id=edge_id,
                    fact_type="ROUTE_TIME",
                    value=response,
                    receipt=route_receipt,
                    freshness=route_freshness,
                    confidence=0 if unavailable else 1,
                )
            )

    candidates: list[FrozenRepairCandidate] = []
    for stop in {item["place_id"]: item for item in stops}.values():
        candidate_route_receipt = _provider_receipt(
            provider=PROVIDER_V3,
            operation="route.candidate",
            status="SUCCEEDED",
            request={"anchor": "trip-center", "candidate_place_id": stop["place_id"]},
            response={
                "anchor": "trip-center",
                "candidate_place_id": stop["place_id"],
                "mode": "driving",
                "duration_minutes": 15,
            },
            observed_at=observed_at,
            affected_fields=(f"candidate_routes.{stop['place_id']}",),
        )
        receipts.append(candidate_route_receipt)
        candidates.append(
            FrozenRepairCandidate(
                canonical_place_id=stop["place_id"],
                display_name=stop["display_name"],
                place_receipt_id=identity_receipts[stop["place_id"]].receipt_id,
                route_receipt_ids=(candidate_route_receipt.receipt_id,),
            )
        )
    candidate_sets = []
    if candidates:
        frozen = freeze_candidate_set(f"candidate-set-{case_id}", candidates)
        candidate_sets.append(
            _artifact(
                "trip-check-p5-candidate-set-v3",
                frozen.candidate_set_id,
                candidate_set=frozen.model_dump(mode="json"),
            )
        )
    provider_failures = []
    if route_freshness == EvidenceFreshness.UNAVAILABLE:
        provider_failures.append(
            ProviderFailure(
                provider=PROVIDER_V3,
                error_category="PROVIDER_ROUTE_UNAVAILABLE",
                retryable=False,
                detail="controlled fault keeps affected route fields UNKNOWN",
            )
        )
    snapshot = EvidenceSnapshot(
        snapshot_id=snapshot_id,
        workspace_id=f"eval-workspace-{case_id}",
        itinerary_revision=1,
        provider_set=sorted({receipt.provider for receipt in receipts}),
        policy_version=EVIDENCE_POLICY_VERSION_V3,
        facts=facts,
        provider_failures=provider_failures,
        created_at=observed_at,
    )
    provider_snapshot_id = _required_text(
        runner_control.get("provider_snapshot_id"), field="runner_control.provider_snapshot_id"
    )
    fault_profile_id = _required_text(
        runner_control.get("fault_profile_id"), field="runner_control.fault_profile_id"
    )
    materialization = {
        "schema_version": EVIDENCE_MATERIALIZATION_SCHEMA_V3,
        "case_id": case_id,
        "source_payload": source_payload,
        "provider_snapshot": _artifact(
            "trip-check-p5-provider-snapshot-v3",
            provider_snapshot_id,
            execution_mode="fixture",
            fault_profile_id=fault_profile_id,
            evidence_freshness=route_freshness.value,
            receipt_ids=[receipt.receipt_id for receipt in receipts],
        ),
        "evidence_snapshot": _artifact(
            "trip-check-p5-evidence-snapshot-v3",
            snapshot.snapshot_id,
            snapshot=snapshot.model_dump(mode="json"),
        ),
        "candidate_sets": candidate_sets,
        "receipts": [receipt.model_dump(mode="json") for receipt in receipts],
    }
    materialization["evidence_materialization_hash"] = digest(materialization)
    return materialization


__all__ = [
    "EVIDENCE_MATERIALIZATION_SCHEMA_V3",
    "EVIDENCE_POLICY_VERSION_V3",
    "PROVIDER_V3",
    "build_evidence_materialization_v3",
]
