"""Capture a three-city live Suggestion Provider snapshot without fixture fallback.

The artifact proves only a local-authorized observation through the concrete
Suggestion candidate and walking-route adapters.  It deliberately does not
claim opening hours, reservations, accessibility, public E2E, human evidence,
or release readiness.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.constraints.amap_types import typecodes_for_category
from app.schemas.place import Coordinates, PlaceCategory
from app.suggestions.models import SuggestionIntent
from app.suggestions.providers import (
    AmapCandidateSource,
    AmapRouteSource,
    CandidateRouteSource,
    ProviderCandidate,
    ProviderCandidateQuery,
    ProviderCandidateSource,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "backend"
    / "evidence"
    / "real_provider_local_authorized"
    / "suggestion_snapshot_2026-08-21.json"
)
EVIDENCE_CLASS = "real_provider_local_authorized"
INTENTS = (
    SuggestionIntent.NEARBY,
    SuggestionIntent.POPULAR,
    SuggestionIntent.FUN,
    SuggestionIntent.FOOD,
)
ANCHORS = (
    {
        "city": "北京",
        "place_id": "B000A7BD6T",
        "name": "故宫博物院",
        "coords": Coordinates(lng=116.3913, lat=39.9163),
    },
    {
        "city": "上海",
        "place_id": "B00155H52F",
        "name": "外滩",
        "coords": Coordinates(lng=121.4896, lat=31.2393),
    },
    {
        "city": "杭州",
        "place_id": "B0FFHZ0001",
        "name": "西湖风景名胜区",
        "coords": Coordinates(lng=120.1551, lat=30.2523),
    },
)

_INTENT_CATEGORIES = {
    SuggestionIntent.NEARBY: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
    SuggestionIntent.POPULAR: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
    SuggestionIntent.FUN: (PlaceCategory.ATTRACTION,),
    SuggestionIntent.FOOD: (PlaceCategory.FOOD,),
}
_INTENT_KEYWORDS = {
    SuggestionIntent.NEARBY: ("附近",),
    SuggestionIntent.POPULAR: ("热门", "口碑"),
    SuggestionIntent.FUN: ("景点", "好玩"),
    SuggestionIntent.FOOD: ("美食", "餐厅"),
}
_INTENT_RADIUS_M = {
    SuggestionIntent.NEARBY: 5_000,
    SuggestionIntent.POPULAR: 15_000,
    SuggestionIntent.FUN: 12_000,
    SuggestionIntent.FOOD: 5_000,
}

CandidateSourceFactory = Callable[[], ProviderCandidateSource]
RouteSourceFactory = Callable[[], CandidateRouteSource]


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalise_city(value: str) -> str:
    return str(value).strip().removesuffix("市")


def _preflight(runtime: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if runtime.get("runtime_profile") != "local_real":
        errors.append("runtime_profile_not_local_real")
    if runtime.get("amap_mock") is not False:
        errors.append("amap_mock_not_false")
    if runtime.get("demo_mode") is not False:
        errors.append("demo_mode_not_false")
    if not runtime.get("amap_api_key_configured"):
        errors.append("amap_credentials_missing")
    return errors


def _query(anchor: dict[str, Any], intent: SuggestionIntent) -> ProviderCandidateQuery:
    categories = _INTENT_CATEGORIES[intent]
    return ProviderCandidateQuery(
        city=anchor["city"],
        intents=(intent,),
        typecodes=tuple(
            dict.fromkeys(
                code
                for category in categories
                for code in typecodes_for_category(category)
            ),
        ),
        radius_m=_INTENT_RADIUS_M[intent],
        anchor_name=anchor["name"],
        anchor_place_id=anchor["place_id"],
        anchor_coords=anchor["coords"],
        keywords=_INTENT_KEYWORDS[intent],
        transport_mode="walking",
    )


def _safe_failure(exc: Exception) -> dict[str, str]:
    # Exception strings from HTTP clients can contain the fully rendered URL,
    # including credentials. Persist a stable category only.
    return {
        "error_category": type(exc).__name__,
        "reason_code": "LIVE_PROVIDER_CAPTURE_FAILED",
    }


def _select_candidates(
    *,
    anchor: dict[str, Any],
    batches: list[tuple[SuggestionIntent, list[ProviderCandidate]]],
    maximum: int = 6,
) -> tuple[list[tuple[ProviderCandidate, list[SuggestionIntent]]], dict[str, int]]:
    excluded = {
        "wrong_city": 0,
        "wrong_category": 0,
        "anchor_duplicate": 0,
        "canonical_duplicate": 0,
    }
    accepted_by_intent: dict[SuggestionIntent, list[ProviderCandidate]] = {}
    seen_global: dict[str, tuple[ProviderCandidate, list[SuggestionIntent]]] = {}
    anchor_name = anchor["name"].casefold()
    for intent, candidates in batches:
        accepted_by_intent[intent] = []
        allowed = {category.value for category in _INTENT_CATEGORIES[intent]}
        seen_in_intent: set[str] = set()
        for candidate in candidates:
            place = candidate.canonical_place
            identity = place.place_id.casefold()
            name_identity = place.name.strip().casefold()
            if _normalise_city(place.city) != anchor["city"]:
                excluded["wrong_city"] += 1
                continue
            if place.category not in allowed:
                excluded["wrong_category"] += 1
                continue
            if identity == anchor["place_id"].casefold() or name_identity == anchor_name:
                excluded["anchor_duplicate"] += 1
                continue
            if identity in seen_in_intent:
                excluded["canonical_duplicate"] += 1
                continue
            seen_in_intent.add(identity)
            accepted_by_intent[intent].append(candidate)
            if identity in seen_global:
                matched = seen_global[identity][1]
                if intent not in matched:
                    matched.append(intent)
            else:
                seen_global[identity] = (candidate, [intent])

    # Round-robin intent buckets so FOOD and FUN are not hidden by provider
    # ordering from a broader NEARBY/POPULAR response.
    selected: list[tuple[ProviderCandidate, list[SuggestionIntent]]] = []
    selected_ids: set[str] = set()
    cursors = {intent: 0 for intent in INTENTS}
    while len(selected) < maximum:
        progressed = False
        for intent in INTENTS:
            bucket = accepted_by_intent[intent]
            while cursors[intent] < len(bucket):
                candidate = bucket[cursors[intent]]
                cursors[intent] += 1
                identity = candidate.canonical_place.place_id.casefold()
                if identity not in selected_ids:
                    selected_ids.add(identity)
                    selected.append(seen_global[identity])
                    progressed = True
                    break
            if len(selected) >= maximum:
                break
        if not progressed:
            break
    return selected, excluded


def _operational_evidence(candidate: ProviderCandidate) -> dict[str, Any]:
    """Keep opening-like facts separate from the entity materialization receipt."""
    payload = candidate.model_dump(mode="json")
    facts = payload.get("current_facts")
    if facts:
        return {
            "status": "PROVIDER_EXPLICIT",
            "facts": facts,
            "note": "Explicit Provider fields are frozen, but are not a public or human-validated claim.",
        }
    return {
        "status": "UNKNOWN",
        "reason_code": "NOT_PROVIDED_BY_SUGGESTION_CANDIDATE_CONTRACT",
        "note": "Entity identity/coordinate receipt is not opening or reservation evidence.",
    }


async def _capture_city(
    anchor: dict[str, Any],
    *,
    candidate_source: ProviderCandidateSource,
    route_source: CandidateRouteSource,
    request_pause_seconds: float,
) -> dict[str, Any]:
    batches: list[tuple[SuggestionIntent, list[ProviderCandidate]]] = []
    query_receipts: list[dict[str, Any]] = []
    for intent in INTENTS:
        query = _query(anchor, intent)
        batch = await candidate_source.search(query)
        candidates = list(batch.candidates)
        batches.append((intent, candidates))
        first_receipt = candidates[0].provider_receipt if candidates else None
        query_receipts.append({
            "intent": intent.value,
            "query_contract_sha256": _canonical_hash(query.model_dump(mode="json")),
            "provider_snapshot_id": batch.provider_snapshot_id,
            "retrieved_at": batch.retrieved_at.isoformat().replace("+00:00", "Z"),
            "result_count": len(candidates),
            "provider_request_hash": first_receipt.request_hash if first_receipt else None,
            "provider_response_hash": first_receipt.response_hash if first_receipt else None,
        })
        if request_pause_seconds:
            await asyncio.sleep(request_pause_seconds)

    selected, excluded = _select_candidates(anchor=anchor, batches=batches)
    captured_candidates: list[dict[str, Any]] = []
    route_query = ProviderCandidateQuery(
        city=anchor["city"],
        intents=INTENTS,
        typecodes=tuple(dict.fromkeys(
            code
            for category in (PlaceCategory.ATTRACTION, PlaceCategory.FOOD)
            for code in typecodes_for_category(category)
        )),
        radius_m=15_000,
        anchor_name=anchor["name"],
        anchor_place_id=anchor["place_id"],
        anchor_coords=anchor["coords"],
        keywords=tuple(word for intent in INTENTS for word in _INTENT_KEYWORDS[intent]),
        transport_mode="walking",
    )
    for candidate, matched_intents in selected:
        route = await route_source.route_times(route_query, candidate)
        captured_candidates.append({
            "matched_intents": [intent.value for intent in matched_intents],
            "canonical_place": candidate.canonical_place.model_dump(mode="json"),
            "provider_receipt": candidate.provider_receipt.model_dump(mode="json"),
            "provider_signals": {
                "popularity": candidate.popularity,
                "diversity_tags": list(candidate.diversity_tags),
            },
            "operational_evidence": _operational_evidence(candidate),
            "route_times": route.model_dump(mode="json"),
        })
        if request_pause_seconds:
            await asyncio.sleep(request_pause_seconds)

    composite_snapshot = _canonical_hash([
        receipt["provider_snapshot_id"] for receipt in query_receipts
    ])
    return {
        "city": anchor["city"],
        "anchor": {
            "place_id": anchor["place_id"],
            "name": anchor["name"],
            "coords": anchor["coords"].model_dump(mode="json"),
            "authority": "fixed_canonical_anchor",
        },
        "provider_snapshot_id": f"amap-suggestion-composite-{composite_snapshot}",
        "query_receipts": query_receipts,
        "selection": {
            "raw_unique_candidate_count": len({
                candidate.canonical_place.place_id
                for _, candidates in batches
                for candidate in candidates
            }),
            "frozen_candidate_count": len(captured_candidates),
            "excluded_counts": excluded,
            "strategy": "intent_round_robin_then_provider_order",
        },
        "candidates": captured_candidates,
    }


def validate_artifact(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != "1.0":
        errors.append("schema_version_invalid")
    if report.get("evidence_class") != EVIDENCE_CLASS:
        errors.append("evidence_class_invalid")
    if report.get("runtime", {}).get("authentication_material_persisted") is not False:
        errors.append("authentication_material_boundary_missing")
    cities = report.get("cities") or []
    if [row.get("city") for row in cities] != [anchor["city"] for anchor in ANCHORS]:
        errors.append("fixed_three_city_order_invalid")
    for row in cities:
        city = row.get("city")
        anchor = row.get("anchor") or {}
        query_receipts = row.get("query_receipts") or []
        if [item.get("intent") for item in query_receipts] != [intent.value for intent in INTENTS]:
            errors.append(f"{city}:four_intent_queries_missing")
        candidates = row.get("candidates") or []
        if not 4 <= len(candidates) <= 6:
            errors.append(f"{city}:frozen_candidate_count_not_4_to_6")
        ids: set[str] = set()
        names: set[str] = set()
        for item in candidates:
            place = item.get("canonical_place") or {}
            receipt = item.get("provider_receipt") or {}
            route = item.get("route_times") or {}
            operational = item.get("operational_evidence") or {}
            place_id = str(place.get("place_id") or "")
            name = str(place.get("name") or "").strip().casefold()
            if place_id in ids or name in names:
                errors.append(f"{city}:duplicate_frozen_candidate")
            ids.add(place_id)
            names.add(name)
            if place_id == anchor.get("place_id") or name == str(anchor.get("name") or "").casefold():
                errors.append(f"{city}:{place_id}:anchor_duplicate")
            if _normalise_city(place.get("city", "")) != city:
                errors.append(f"{city}:{place_id}:wrong_city")
            if place.get("category") not in {PlaceCategory.ATTRACTION.value, PlaceCategory.FOOD.value}:
                errors.append(f"{city}:{place_id}:wrong_category")
            if (
                receipt.get("provider") != "amap"
                or receipt.get("execution_mode") != "live"
                or receipt.get("canonical_place_id") != place_id
                or receipt.get("name") != place.get("name")
                or receipt.get("city") != place.get("city")
                or receipt.get("request_hash") is None
                or len(receipt.get("request_hash", "")) != 64
                or receipt.get("response_hash") is None
                or len(receipt.get("response_hash", "")) != 64
            ):
                errors.append(f"{city}:{place_id}:provider_receipt_invalid")
            if operational.get("status") == "PROVIDER_EXPLICIT":
                facts = operational.get("facts") or []
                if not facts:
                    errors.append(f"{city}:{place_id}:operational_fact_missing")
                for fact in facts:
                    if (
                        fact.get("fact_type") not in {
                            "OPENING_HOURS",
                            "RESERVATION_POLICY",
                            "ACCESSIBILITY_POLICY",
                            "DIETARY_SUPPORT",
                        }
                        or fact.get("provider") != "amap_v5_place_around"
                        or fact.get("execution_mode") != "live"
                        or len(fact.get("request_hash", "")) != 64
                        or len(fact.get("response_hash", "")) != 64
                        or fact.get("request_hash") != receipt.get("request_hash")
                        or fact.get("response_hash") != receipt.get("response_hash")
                        or not fact.get("observed_at")
                    ):
                        errors.append(f"{city}:{place_id}:operational_fact_receipt_invalid")
            elif operational.get("status") != "UNKNOWN":
                errors.append(f"{city}:{place_id}:operational_evidence_boundary_invalid")
            route_receipts = route.get("route_receipts") or []
            if route.get("status") != "AVAILABLE" or len(route_receipts) != 1:
                errors.append(f"{city}:{place_id}:walking_route_unavailable")
                continue
            leg = route_receipts[0]
            if (
                leg.get("provider") != "amap"
                or leg.get("execution_mode") != "live"
                or leg.get("transport_mode") != "walking"
                or leg.get("origin_place_id") != anchor.get("place_id")
                or leg.get("destination_place_id") != place_id
                or len(leg.get("request_hash", "")) != 64
                or len(leg.get("response_hash", "")) != 64
            ):
                errors.append(f"{city}:{place_id}:route_receipt_invalid")
    integrity = report.get("integrity") or {}
    expected_hash = _canonical_hash({key: value for key, value in report.items() if key != "integrity"})
    if integrity.get("artifact_payload_sha256") != expected_hash:
        errors.append("artifact_payload_hash_mismatch")
    return errors


async def collect(
    *,
    candidate_source_factory: CandidateSourceFactory | None = None,
    route_source_factory: RouteSourceFactory | None = None,
    runtime: dict[str, Any] | None = None,
    request_pause_seconds: float = 0.2,
) -> dict[str, Any]:
    runtime = runtime or {
        "runtime_profile": settings.runtime_profile,
        "amap_mock": settings.amap_mock,
        "demo_mode": settings.demo_mode,
        "amap_api_key_configured": bool(settings.amap_api_key),
    }
    runtime_receipt = {
        **runtime,
        "amap_api_key_configured": bool(runtime.get("amap_api_key_configured")),
        "authentication_material_persisted": False,
        "raw_provider_payload_persisted": False,
        "request_mode": "sequential_low_rate",
        "request_pause_seconds": request_pause_seconds,
    }
    started_at = _now()
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "evidence_class": EVIDENCE_CLASS,
        "evidence_subtype": "suggestion_live_candidate_and_walking_route_snapshot",
        "claim_boundary": {
            "proves_local_authorized_live_entity": True,
            "proves_local_authorized_live_route": True,
            "proves_opening_hours": False,
            "proves_reservation": False,
            "proves_accessibility": False,
            "is_public_internet_e2e": False,
            "is_human_evidence": False,
            "is_release_approval": False,
        },
        "runtime": runtime_receipt,
        "run_started_at": started_at,
        "run_completed_at": None,
        "overall_status": "failed",
        "failure_receipt": None,
        "cities": [],
    }
    preflight_errors = _preflight(runtime_receipt)
    if preflight_errors:
        report["failure_receipt"] = {
            "stage": "preflight",
            "reason_codes": preflight_errors,
        }
    else:
        candidate_source = (candidate_source_factory or AmapCandidateSource)()
        route_source = (route_source_factory or AmapRouteSource)()
        try:
            for anchor in ANCHORS:
                report["cities"].append(await _capture_city(
                    anchor,
                    candidate_source=candidate_source,
                    route_source=route_source,
                    request_pause_seconds=request_pause_seconds,
                ))
            report["overall_status"] = "passed"
        except Exception as exc:  # One attempt leaves a durable failure receipt.
            report["failure_receipt"] = {
                "stage": "provider_capture",
                **_safe_failure(exc),
                "completed_city_count": len(report["cities"]),
            }
    report["run_completed_at"] = _now()
    report["integrity"] = {
        "artifact_payload_sha256": _canonical_hash(report),
        "hash_algorithm": "SHA-256 over canonical UTF-8 JSON excluding integrity",
        "passed": False,
        "validation_errors": [],
    }
    validation_errors = validate_artifact(report)
    if validation_errors:
        report["overall_status"] = "failed"
    report["integrity"]["passed"] = not validation_errors
    report["integrity"]["validation_errors"] = validation_errors
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--request-pause-seconds", type=float, default=0.2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output if args.output.is_absolute() else REPO_ROOT / args.output
    report = asyncio.run(collect(request_pause_seconds=max(0.0, args.request_pause_seconds)))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for city in report["cities"]:
        print(
            f"{city['city']}: candidates={city['selection']['frozen_candidate_count']} "
            f"queries={len(city['query_receipts'])}"
        )
    print(f"overall_status={report['overall_status']}")
    print(f"evidence_file={output}")
    if args.strict and report["overall_status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
