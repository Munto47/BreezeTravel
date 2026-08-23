"""Deterministic, eval-only Provider/Evidence/CandidateSet materialization for P5 v2.

The materializer intentionally projects only product input and runner-control
fields.  Oracle/expected payloads are neither accepted as inputs to helpers nor
copied into the materialized artifact.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

from app.audit.models import EvidenceFact, EvidenceFreshness, EvidenceSnapshot, ProviderFailure
from app.repairs.candidates import FrozenRepairCandidate, FrozenRepairCandidateSet, freeze_candidate_set
from app.trip_check.provider_integrity import ProviderCallReceipt
from evals.trip_check_v1.p5.data_contract import digest


MATERIALIZATION_SCHEMA_VERSION = "trip-check-p5-evidence-materialization-v2"
EVIDENCE_POLICY_VERSION = "trip-check-p5-controlled-evidence-v2"
_TOP_LEVEL_KEYS = {
    "schema_version",
    "case_id",
    "source_payload",
    "provider_snapshot",
    "evidence_snapshot",
    "candidate_sets",
    "receipts",
    "evidence_materialization_hash",
}
_FORBIDDEN_KEYS = {"oracle", "oracle_sha256", "expected"}
_CITY_PLACES: dict[str, tuple[tuple[str, tuple[str, ...], float, float], ...]] = {
    "北京": (
        ("bj-forbidden-city", ("故宫博物院", "故宫"), 116.397, 39.918),
        ("bj-temple-of-heaven", ("天坛公园",), 116.417, 39.883),
        ("bj-summer-palace", ("颐和园",), 116.273, 39.999),
        ("bj-jingshan", ("景山公园",), 116.396, 39.925),
        ("bj-national-museum", ("中国国家博物馆",), 116.407, 39.905),
    ),
    "上海": (
        ("sh-bund", ("外滩",), 121.490, 31.241),
        ("sh-yuyuan", ("豫园",), 121.493, 31.227),
        ("sh-oriental-pearl", ("东方明珠广播电视塔", "东方明珠"), 121.500, 31.240),
        ("sh-tianzifang", ("田子坊",), 121.475, 31.211),
        ("sh-disney", ("上海迪士尼乐园",), 121.657, 31.144),
    ),
    "杭州": (
        ("hz-west-lake", ("西湖风景名胜区", "西湖"), 120.148, 30.244),
        ("hz-lingyin", ("灵隐寺",), 120.102, 30.240),
        ("hz-leifeng", ("雷峰塔",), 120.149, 30.231),
        ("hz-xixi", ("西溪湿地国家公园",), 120.063, 30.268),
        ("hz-hefang", ("河坊街·清河坊",), 120.174, 30.238),
    ),
}


def _mapping(value: Any, *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    return value


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _sha256(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field} must be a lowercase sha256")
    return value


def _artifact(schema_version: str, artifact_id: str, **content: Any) -> dict[str, Any]:
    payload = {"schema_version": schema_version, "artifact_id": artifact_id, **content}
    payload["content_sha256"] = digest(payload)
    return payload


def _validate_artifact_hash(payload: Mapping[str, Any], *, field: str) -> None:
    actual = _sha256(payload.get("content_sha256"), field=f"{field}.content_sha256")
    content = {key: value for key, value in payload.items() if key != "content_sha256"}
    if actual != digest(content):
        raise ValueError(f"{field} content hash mismatch")


def _observed_at(seed: int) -> datetime:
    return datetime(2026, 8, 23, tzinfo=timezone.utc) + timedelta(seconds=seed % 86_400)


def _input_projection(case_payload: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    product_input = _mapping(case_payload.get("product_input"), field="product_input")
    input_kind = _required_text(case_payload.get("input_kind"), field="input_kind")
    text_key = "raw_text" if input_kind == "TEXT" else "ocr_text"
    text = _required_text(product_input.get(text_key), field=f"product_input.{text_key}")
    projection: dict[str, Any] = {
        "source_type": _required_text(product_input.get("source_type"), field="product_input.source_type"),
        text_key: text,
    }
    if input_kind == "SYNTHETIC_SCREENSHOT":
        render_spec = _mapping(product_input.get("render_spec"), field="product_input.render_spec")
        projection["render_spec"] = {
            key: render_spec.get(key)
            for key in (
                "schema_version",
                "format",
                "theme",
                "layout",
                "width",
                "height",
                "seed",
                "text_sha256",
            )
        }
    return projection, text


def _extract_stops(text: str, city: str) -> list[dict[str, Any]]:
    matches: list[tuple[int, str, str, float, float]] = []
    occupied: list[tuple[int, int]] = []
    aliases = sorted(
        (
            (alias, place_id, lng, lat)
            for place_id, place_aliases, lng, lat in _CITY_PLACES[city]
            for alias in place_aliases
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for alias, place_id, lng, lat in aliases:
        start = 0
        while (position := text.find(alias, start)) >= 0:
            span = (position, position + len(alias))
            if not any(span[0] < existing[1] and existing[0] < span[1] for existing in occupied):
                matches.append((position, place_id, alias, lng, lat))
                occupied.append(span)
            start = position + len(alias)
    matches.sort(key=lambda item: item[0])
    stops: list[dict[str, Any]] = []
    for index, (position, place_id, display_name, lng, lat) in enumerate(matches):
        prefix = text[:position]
        day_markers = list(re.finditer(r"第([1-5])天", prefix))
        day_index = int(day_markers[-1].group(1)) - 1 if day_markers else 0
        time_matches = list(re.finditer(r"([0-2]\d:[0-5]\d)-([0-2]\d:[0-5]\d)", prefix))
        start_time, end_time = time_matches[-1].groups() if time_matches else (None, None)
        stops.append(
            {
                "stop_id": f"stop-{index + 1}",
                "place_id": place_id,
                "display_name": display_name,
                "city": city,
                "day_index": day_index,
                "order_index": sum(item["day_index"] == day_index for item in stops),
                "start_time": start_time,
                "end_time": end_time,
                "coords": {"lng": lng, "lat": lat},
            }
        )
    if not stops:
        raise ValueError("controlled materialization found no supported place in product input")
    return stops


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
        "source_url": f"fixture://trip-check-p5-v2/{operation}",
        "affected_fields": list(affected_fields),
        "failure_category": failure_category,
    }
    json_payload = ProviderCallReceipt(**payload, receipt_id="pending").model_dump(mode="json")
    return ProviderCallReceipt(**payload, receipt_id=digest(_receipt_semantic_payload(json_payload)))


def _route_freshness(runner_control: Mapping[str, Any]) -> EvidenceFreshness:
    explicit = runner_control.get("evidence_freshness")
    if explicit is not None:
        try:
            return EvidenceFreshness(str(explicit))
        except ValueError as exc:
            raise ValueError("runner_control.evidence_freshness is unsupported") from exc
    fault_profile = str(runner_control.get("fault_profile_id") or "none").casefold()
    if "conflict" in fault_profile:
        return EvidenceFreshness.CONFLICTING
    if "route_unavailable" in fault_profile or "provider_unavailable" in fault_profile:
        return EvidenceFreshness.UNAVAILABLE
    return EvidenceFreshness.FRESH


def _fact(
    *,
    snapshot_id: str,
    subject_type: str,
    subject_id: str,
    fact_type: str,
    value: Any,
    receipt: ProviderCallReceipt,
    freshness: EvidenceFreshness,
    confidence: float,
) -> EvidenceFact:
    return EvidenceFact(
        fact_id=digest(
            {
                "snapshot_id": snapshot_id,
                "subject_type": subject_type,
                "subject_id": subject_id,
                "fact_type": fact_type,
                "receipt_id": receipt.receipt_id,
            }
        ),
        snapshot_id=snapshot_id,
        subject_type=subject_type,
        subject_id=subject_id,
        fact_type=fact_type,
        value=value,
        provider=receipt.provider,
        source_url=receipt.source_url,
        observed_at=receipt.observed_at,
        response_hash=receipt.response_hash or digest({"receipt_id": receipt.receipt_id}),
        confidence=confidence,
        freshness_status=freshness,
    )


def build_evidence_materialization(case_payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build one deterministic P5 v2 artifact without consulting labels."""

    case_id = _required_text(case_payload.get("case_id"), field="case_id")
    city = _required_text(case_payload.get("city"), field="city")
    if city not in _CITY_PLACES:
        raise ValueError("city is outside the controlled P5 scope")
    input_kind = _required_text(case_payload.get("input_kind"), field="input_kind")
    if input_kind not in {"TEXT", "SYNTHETIC_SCREENSHOT"}:
        raise ValueError("input_kind is outside the controlled P5 scope")
    trip_days = case_payload.get("trip_days")
    group_size = case_payload.get("group_size")
    if not isinstance(trip_days, int) or isinstance(trip_days, bool) or not 2 <= trip_days <= 5:
        raise ValueError("trip_days must be between 2 and 5")
    if not isinstance(group_size, int) or isinstance(group_size, bool) or not 2 <= group_size <= 5:
        raise ValueError("group_size must be between 2 and 5")
    normalized_input_sha256 = _sha256(
        case_payload.get("normalized_input_sha256"),
        field="normalized_input_sha256",
    )
    runner_control = _mapping(case_payload.get("runner_control"), field="runner_control")
    seed = runner_control.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError("runner_control.seed must be an integer")
    product_input, raw_text = _input_projection(case_payload)
    if normalized_input_sha256 != digest(product_input):
        raise ValueError("normalized_input_sha256 does not bind the projected product input")
    stops = _extract_stops(raw_text, city)
    observed_at = _observed_at(seed)
    provider = "trip-check-p5-controlled-provider-v2"
    provider_snapshot_id = _required_text(
        runner_control.get("provider_snapshot_id"),
        field="runner_control.provider_snapshot_id",
    )
    fault_profile_id = _required_text(
        runner_control.get("fault_profile_id"),
        field="runner_control.fault_profile_id",
    )
    source_payload = _artifact(
        "trip-check-p5-source-payload-v2",
        f"source-{case_id}",
        case_id=case_id,
        city=city,
        trip_days=trip_days,
        group_size=group_size,
        input_kind=input_kind,
        normalized_input_sha256=normalized_input_sha256,
        projected_input_sha256=digest(product_input),
        product_input=product_input,
        stops=stops,
    )
    snapshot_id = f"snapshot-{digest({'case_id': case_id, 'input': normalized_input_sha256})[:24]}"
    receipts: list[ProviderCallReceipt] = []
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
            provider=provider,
            operation="place.resolve",
            status="SUCCEEDED",
            request={"query": stop["display_name"], "city": city},
            response=identity_response,
            observed_at=observed_at,
            affected_fields=(f"places.{stop['place_id']}.identity",),
        )
        if stop["place_id"] not in identity_receipts:
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
            opening_response = {"opening_hours": "07:00-22:00"}
            opening_receipt = _provider_receipt(
                provider=provider,
                operation="place.opening_hours",
                status="SUCCEEDED",
                request={"place_id": stop["place_id"]},
                response=opening_response,
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
        route_values = (20, 55) if route_freshness == EvidenceFreshness.CONFLICTING else (20,)
        for conflict_index, duration in enumerate(route_values):
            unavailable = route_freshness == EvidenceFreshness.UNAVAILABLE
            response = (
                {"reason_code": "PROVIDER_ROUTE_UNAVAILABLE"}
                if unavailable
                else {"mode": "driving", "duration_minutes": duration, "distance_km": 3.0}
            )
            route_receipt = _provider_receipt(
                provider=provider if conflict_index == 0 else f"{provider}-alternate",
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

    candidate_sets: list[dict[str, Any]] = []
    if fault_profile_id != "empty_candidate_set":
        candidates: list[FrozenRepairCandidate] = []
        for stop in {item["place_id"]: item for item in stops}.values():
            candidate_response = {
                "anchor": "trip-center",
                "candidate_place_id": stop["place_id"],
                "mode": "driving",
                "duration_minutes": 15,
            }
            candidate_route_receipt = _provider_receipt(
                provider=provider,
                operation="route.candidate",
                status="SUCCEEDED",
                request={"anchor": "trip-center", "candidate_place_id": stop["place_id"]},
                response=candidate_response,
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
        frozen = freeze_candidate_set(f"candidate-set-{case_id}", candidates)
        candidate_sets.append(
            _artifact(
                "trip-check-p5-candidate-set-v2",
                frozen.candidate_set_id,
                candidate_set=frozen.model_dump(mode="json"),
            )
        )

    provider_failures = []
    if route_freshness == EvidenceFreshness.UNAVAILABLE:
        provider_failures.append(
            ProviderFailure(
                provider=provider,
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
        policy_version=EVIDENCE_POLICY_VERSION,
        facts=facts,
        provider_failures=provider_failures,
        created_at=observed_at,
    )
    receipt_payloads = [receipt.model_dump(mode="json") for receipt in receipts]
    provider_snapshot = _artifact(
        "trip-check-p5-provider-snapshot-v2",
        provider_snapshot_id,
        execution_mode="fixture",
        fault_profile_id=fault_profile_id,
        evidence_freshness=route_freshness.value,
        receipt_ids=[receipt.receipt_id for receipt in receipts],
    )
    evidence_snapshot = _artifact(
        "trip-check-p5-evidence-snapshot-v2",
        snapshot.snapshot_id,
        snapshot=snapshot.model_dump(mode="json"),
    )
    materialization = {
        "schema_version": MATERIALIZATION_SCHEMA_VERSION,
        "case_id": case_id,
        "source_payload": source_payload,
        "provider_snapshot": provider_snapshot,
        "evidence_snapshot": evidence_snapshot,
        "candidate_sets": candidate_sets,
        "receipts": receipt_payloads,
    }
    materialization["evidence_materialization_hash"] = digest(materialization)
    return validate_evidence_materialization(materialization)


def _walk_forbidden(value: Any, *, path: str = "artifact") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"{path} contains forbidden label field: {key}")
            _walk_forbidden(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_forbidden(item, path=f"{path}[{index}]")


def validate_evidence_materialization(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Read back all models and fail closed on incomplete or tampered lineage."""

    if not isinstance(payload, Mapping):
        raise ValueError("materialization must be an object")
    keys = set(payload)
    if keys != _TOP_LEVEL_KEYS:
        raise ValueError(
            f"materialization fields mismatch: missing={_TOP_LEVEL_KEYS - keys} extra={keys - _TOP_LEVEL_KEYS}"
        )
    if payload.get("schema_version") != MATERIALIZATION_SCHEMA_VERSION:
        raise ValueError("materialization schema is unsupported")
    _walk_forbidden(payload)
    expected_materialization_hash = digest(
        {key: value for key, value in payload.items() if key != "evidence_materialization_hash"}
    )
    if (
        _sha256(
            payload.get("evidence_materialization_hash"),
            field="evidence_materialization_hash",
        )
        != expected_materialization_hash
    ):
        raise ValueError("evidence materialization hash mismatch")

    source_payload = _mapping(payload.get("source_payload"), field="source_payload")
    provider_snapshot = _mapping(payload.get("provider_snapshot"), field="provider_snapshot")
    evidence_artifact = _mapping(payload.get("evidence_snapshot"), field="evidence_snapshot")
    for field, artifact in (
        ("source_payload", source_payload),
        ("provider_snapshot", provider_snapshot),
        ("evidence_snapshot", evidence_artifact),
    ):
        _validate_artifact_hash(artifact, field=field)
    if payload.get("case_id") != source_payload.get("case_id"):
        raise ValueError("source payload belongs to another case")

    raw_receipts = payload.get("receipts")
    if not isinstance(raw_receipts, list):
        raise ValueError("receipts must be an array")
    receipts = [ProviderCallReceipt.model_validate(item) for item in raw_receipts]
    receipt_by_id: dict[str, ProviderCallReceipt] = {}
    for receipt in receipts:
        if receipt.receipt_id in receipt_by_id:
            raise ValueError("provider receipt ids must be unique")
        receipt_json = receipt.model_dump(mode="json")
        if receipt.receipt_id != digest(_receipt_semantic_payload(receipt_json)):
            raise ValueError("provider receipt semantic hash mismatch")
        receipt_by_id[receipt.receipt_id] = receipt
    if provider_snapshot.get("receipt_ids") != [receipt.receipt_id for receipt in receipts]:
        raise ValueError("provider snapshot receipt binding mismatch")

    snapshot = EvidenceSnapshot.model_validate(evidence_artifact.get("snapshot"))
    if snapshot.snapshot_id != evidence_artifact.get("artifact_id"):
        raise ValueError("evidence artifact id does not match EvidenceSnapshot")
    providers = {receipt.provider for receipt in receipts}
    if set(snapshot.provider_set) != providers:
        raise ValueError("EvidenceSnapshot provider_set does not match receipts")
    receipt_response_bindings = {(receipt.provider, receipt.response_hash) for receipt in receipts}
    for fact in snapshot.facts:
        if (fact.provider, fact.response_hash) not in receipt_response_bindings:
            raise ValueError("evidence fact lacks its exact provider response receipt")

    raw_candidate_sets = payload.get("candidate_sets")
    if not isinstance(raw_candidate_sets, list):
        raise ValueError("candidate_sets must be an array")
    candidate_set_ids: set[str] = set()
    for index, raw_candidate_artifact in enumerate(raw_candidate_sets):
        candidate_artifact = _mapping(raw_candidate_artifact, field=f"candidate_sets[{index}]")
        _validate_artifact_hash(candidate_artifact, field=f"candidate_sets[{index}]")
        candidate_set = FrozenRepairCandidateSet.model_validate(candidate_artifact.get("candidate_set"))
        if not candidate_set.candidates:
            raise ValueError("a materialized CandidateSet cannot be empty")
        if candidate_set.candidate_set_id != candidate_artifact.get("artifact_id"):
            raise ValueError("candidate artifact id does not match CandidateSet")
        if candidate_set.candidate_set_id in candidate_set_ids:
            raise ValueError("candidate set ids must be unique")
        candidate_set_ids.add(candidate_set.candidate_set_id)
        for candidate in candidate_set.candidates:
            place_receipt = receipt_by_id.get(candidate.place_receipt_id)
            if (
                place_receipt is None
                or place_receipt.operation != "place.resolve"
                or place_receipt.status != "SUCCEEDED"
                or place_receipt.response_hash is None
                or f"places.{candidate.canonical_place_id}.identity" not in place_receipt.affected_fields
            ):
                raise ValueError("candidate lacks a successful place receipt")
            if not candidate.route_receipt_ids:
                raise ValueError("candidate lacks a route receipt")
            for receipt_id in candidate.route_receipt_ids:
                route_receipt = receipt_by_id.get(receipt_id)
                if (
                    route_receipt is None
                    or route_receipt.operation != "route.candidate"
                    or route_receipt.status != "SUCCEEDED"
                    or route_receipt.response_hash is None
                    or f"candidate_routes.{candidate.canonical_place_id}" not in route_receipt.affected_fields
                ):
                    raise ValueError("candidate lacks a successful route receipt")

    canonical = {key: value for key, value in payload.items()}
    return canonical
