"""Capture bounded three-city, three-hop live Amap suggestion chains.

The command performs one sequential pass only.  It never retries and never
falls back to fixtures.  A complete city contains three independently queried
anchor rounds, which is enough to grow one seed into a four-stop itinerary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.audit.repositories import InMemoryAuditRepository
from app.audit.suggestion_gate import SuggestionAuditGate
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.members.repositories import InMemoryMemberConstraintRepository
from app.route_priors.loader import RoutePriorLoader
from app.schemas.place import PlaceCategory
from app.suggestions.models import FreshnessStatus, SuggestionClassification, SuggestionIntent
from app.suggestions.providers import (
    AmapCandidateSource,
    AmapRouteSource,
    CandidateRouteSource,
    ProviderCandidate,
    ProviderCandidateBatch,
    ProviderCandidateQuery,
    ProviderCandidateSource,
    RouteTimes,
)
from app.suggestions.ranking import AnchorCandidateRanker, RankingContext
from scripts.capture_suggestion_provider_snapshot import (
    ANCHORS,
    EVIDENCE_CLASS,
    INTENTS,
    _canonical_hash,
    _normalise_city,
    _operational_evidence,
    _preflight,
    _query,
    _safe_failure,
    _select_candidates,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = (
    REPO_ROOT
    / "backend"
    / "evidence"
    / "real_provider_local_authorized"
    / "suggestion_chain_snapshot_2026-08-21-v2.json"
)
SCHEMA_VERSION = "1.1"
EVIDENCE_SUBTYPE = "suggestion_live_chained_candidate_and_walking_route_snapshot"
CHAIN_ROUNDS = 3
MAX_CANDIDATES_PER_ROUND = 6
MAX_PROVIDER_REQUESTS = len(ANCHORS) * CHAIN_ROUNDS * (
    len(INTENTS) + MAX_CANDIDATES_PER_ROUND
)

CandidateSourceFactory = Callable[[], ProviderCandidateSource]
RouteSourceFactory = Callable[[], CandidateRouteSource]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _combined_query(anchor: dict[str, Any]) -> ProviderCandidateQuery:
    # The public product requests all four visible intents in one round.  The
    # four individual Provider searches below are a deterministic recall plan;
    # this exact combined contract is what the frozen adapter later matches.
    from app.constraints.amap_types import typecodes_for_category

    return ProviderCandidateQuery(
        city=anchor["city"],
        intents=INTENTS,
        typecodes=tuple(
            dict.fromkeys(
                code
                for category in (PlaceCategory.ATTRACTION, PlaceCategory.FOOD)
                for code in typecodes_for_category(category)
            )
        ),
        radius_m=15_000,
        anchor_name=anchor["name"],
        anchor_place_id=anchor["place_id"],
        anchor_coords=anchor["coords"],
        keywords=("附近", "热门", "口碑", "景点", "好玩", "美食", "餐厅"),
        transport_mode="walking",
    )


class _BatchSource:
    def __init__(self, batch: ProviderCandidateBatch):
        self.batch = batch

    async def search(self, query: ProviderCandidateQuery) -> ProviderCandidateBatch:
        return self.batch


class _RouteMap:
    def __init__(self, routes: dict[str, RouteTimes]):
        self.routes = routes

    async def route_times(
        self, query: ProviderCandidateQuery, candidate: ProviderCandidate
    ) -> RouteTimes:
        return self.routes.get(
            candidate.canonical_place.place_id,
            RouteTimes(status="UNKNOWN", reason_code="CHAIN_ROUTE_NOT_CAPTURED"),
        )


def _next_anchor(candidate: ProviderCandidate) -> dict[str, Any]:
    place = candidate.canonical_place
    return {
        "city": place.city,
        "place_id": place.place_id,
        "name": place.name,
        "coords": place.coords,
    }


async def _capture_round(
    anchor: dict[str, Any],
    *,
    round_index: int,
    selected_place_ids: set[str],
    selected_place_names: set[str],
    candidate_source: ProviderCandidateSource,
    route_source: CandidateRouteSource,
    request_pause_seconds: float,
    route_prior_loader: RoutePriorLoader,
    audit_gate: SuggestionAuditGate,
    workspace: TripWorkspace,
    base: ItineraryRevision,
    insert_after_stop_id: str,
) -> tuple[dict[str, Any], ProviderCandidate | None, Any | None, int]:
    batches: list[tuple[SuggestionIntent, list[ProviderCandidate]]] = []
    query_receipts: list[dict[str, Any]] = []
    request_count = 0
    for intent in INTENTS:
        query = _query(anchor, intent)
        batch = await candidate_source.search(query)
        request_count += 1
        candidates = list(batch.candidates)
        batches.append((intent, candidates))
        first = candidates[0].provider_receipt if candidates else None
        query_receipts.append(
            {
                "intent": intent.value,
                "query_contract_sha256": _canonical_hash(query.model_dump(mode="json")),
                "provider_snapshot_id": batch.provider_snapshot_id,
                "retrieved_at": batch.retrieved_at.isoformat().replace("+00:00", "Z"),
                "result_count": len(candidates),
                "provider": "amap",
                "provider_request_hash": first.request_hash if first else None,
                "provider_response_hash": first.response_hash if first else None,
                "execution_mode": "live",
                "source_url": "https://restapi.amap.com/v5/place/around",
            }
        )
        if request_pause_seconds:
            await asyncio.sleep(request_pause_seconds)

    selected, excluded = _select_candidates(
        anchor=anchor,
        batches=batches,
        maximum=MAX_CANDIDATES_PER_ROUND,
    )
    selected = [
        item
        for item in selected
        if item[0].canonical_place.place_id not in selected_place_ids
        and item[0].canonical_place.name.strip().casefold() not in selected_place_names
    ]
    query = _combined_query(anchor)
    routes: dict[str, RouteTimes] = {}
    frozen_candidates: list[dict[str, Any]] = []
    for candidate, matched_intents in selected:
        route = await route_source.route_times(query, candidate)
        request_count += 1
        routes[candidate.canonical_place.place_id] = route
        frozen_candidates.append(
            {
                "matched_intents": [intent.value for intent in matched_intents],
                "canonical_place": candidate.canonical_place.model_dump(mode="json"),
                "provider_receipt": candidate.provider_receipt.model_dump(mode="json"),
                "provider_signals": {
                    "popularity": candidate.popularity,
                    "diversity_tags": list(candidate.diversity_tags),
                },
                "operational_evidence": _operational_evidence(candidate),
                "route_times": route.model_dump(mode="json"),
            }
        )
        if request_pause_seconds:
            await asyncio.sleep(request_pause_seconds)

    latest_observed = max(
        (candidate.provider_receipt.observed_at for candidate, _ in selected),
        default=datetime.now(timezone.utc),
    )
    batch_id_hash = _canonical_hash([item["provider_snapshot_id"] for item in query_receipts])
    batch = ProviderCandidateBatch(
        provider_snapshot_id=f"amap-suggestion-chain-round-{batch_id_hash}",
        candidates=tuple(candidate for candidate, _ in selected),
        retrieved_at=latest_observed,
    )
    ranker = AnchorCandidateRanker(
        _BatchSource(batch),
        _RouteMap(routes),
        route_prior_loader=route_prior_loader,
    )
    result = await ranker.rank(
        RankingContext(
            query=query,
            allowed_categories=frozenset({PlaceCategory.ATTRACTION, PlaceCategory.FOOD}),
            selected_place_ids=frozenset(selected_place_ids),
            selected_place_names=frozenset(selected_place_names),
            canonical_duplicate_names=frozenset(selected_place_names),
            as_of=latest_observed,
        )
    )
    audit_gate.clock = lambda: latest_observed
    ranker_acceptable = [
        item
        for item in result.candidates
        if item.hard_gate.passed
        and item.evidence_freshness.status is FreshnessStatus.FRESH
        and item.route_delta.status == "AVAILABLE"
        and item.classification
        in {
            SuggestionClassification.ON_ROUTE,
            SuggestionClassification.ACCEPTABLE_DETOUR,
        }
    ]
    gated = [
        await audit_gate.evaluate_candidate(
            workspace=workspace,
            base=base,
            candidate=item,
            day_index=0,
            insert_after_stop_id=insert_after_stop_id,
            insert_before_stop_id=None,
        )
        for item in ranker_acceptable
    ]
    accepted_draft = next(
        (
            item
            for item in gated
            if item.hard_gate.passed
            and item.evidence_freshness.status is FreshnessStatus.FRESH
            and item.route_delta.status == "AVAILABLE"
            and item.classification
            in {
                SuggestionClassification.ON_ROUTE,
                SuggestionClassification.ACCEPTABLE_DETOUR,
            }
        ),
        None,
    )
    accepted = next(
        (
            candidate
            for candidate, _ in selected
            if accepted_draft is not None
            and candidate.canonical_place.place_id == accepted_draft.canonical_place.place_id
        ),
        None,
    )
    reason_code = None if accepted else "NO_AUTHORITATIVE_GATE_ACCEPTABLE_CANDIDATE"
    round_payload = {
        "round_index": round_index,
        "status": "COMPLETE" if accepted else "PARTIAL",
        "reason_code": reason_code,
        "anchor": {
            "place_id": anchor["place_id"],
            "name": anchor["name"],
            "coords": anchor["coords"].model_dump(mode="json"),
            "authority": "fixed_canonical_anchor" if round_index == 1 else "prior_round_selected_canonical",
        },
        "provider_snapshot_id": batch.provider_snapshot_id,
        "query_receipts": query_receipts,
        "selection": {
            "raw_unique_candidate_count": len(
                {
                    candidate.canonical_place.place_id
                    for _, candidates in batches
                    for candidate in candidates
                }
            ),
            "frozen_candidate_count": len(frozen_candidates),
            "excluded_counts": excluded,
            "strategy": "existing_anchor_ranker_then_authoritative_audit_gate",
            "selected_candidate_place_id": (
                accepted.canonical_place.place_id if accepted is not None else None
            ),
            "selected_candidate_rank": accepted_draft.rank_position if accepted_draft else None,
            "selected_classification": (
                accepted_draft.classification.value if accepted_draft else None
            ),
            "selected_route_minutes": (
                accepted_draft.route_delta.delta_route_minutes if accepted_draft else None
            ),
            "ranking_context_hash": result.context_hash,
            "ranking_policy_version": result.policy_version,
            "authoritative_gate_status": (
                accepted_draft.audit_gate.status.value
                if accepted_draft is not None and accepted_draft.audit_gate is not None
                else "NO_SATISFIED_CANDIDATE"
            ),
            "rejected_gate_reason_codes": sorted({
                reason
                for item in gated
                for reason in item.hard_gate.reason_codes
            }),
            "capture_gate_scope": "product SuggestionAuditGate + suggestion-slot-v1",
        },
        "candidates": frozen_candidates,
    }
    return round_payload, accepted, accepted_draft, request_count


async def _capture_city(
    initial_anchor: dict[str, Any],
    *,
    candidate_source: ProviderCandidateSource,
    route_source: CandidateRouteSource,
    request_pause_seconds: float,
    route_prior_loader: RoutePriorLoader,
) -> tuple[dict[str, Any], int]:
    current_anchor = initial_anchor
    selected_ids = {initial_anchor["place_id"]}
    selected_names = {initial_anchor["name"].strip().casefold()}
    rounds: list[dict[str, Any]] = []
    request_count = 0
    start = date(2026, 10, 1)
    date_range = TripDateRange(start=start, end=start + timedelta(days=2))
    workspace = TripWorkspace(
        workspace_id=f"capture-chain-{initial_anchor['city']}",
        room_id=f"capture-chain-room-{initial_anchor['city']}",
        city=initial_anchor["city"],
        trip_date_range=date_range,
        current_itinerary_revision=1,
        created_by="capture-script",
    )
    seed_stop = ItineraryStop(
        stop_id=f"capture-seed-{initial_anchor['place_id']}",
        place_id=initial_anchor["place_id"],
        day_index=0,
        order_index=0,
        start_time="09:00",
        end_time="10:00",
        visit_duration_minutes=60,
        raw_name=initial_anchor["name"],
        category="attraction",
    )
    base = with_content_hash(ItineraryRevisionContent(
        itinerary_id=f"capture-itinerary-{initial_anchor['city']}",
        workspace_id=workspace.workspace_id,
        revision=1,
        source_type=RevisionSource.PLANNER,
        city=workspace.city,
        date_range=date_range,
        days=[
            ItineraryDay(day_index=index, date=start + timedelta(days=index), stops=[seed_stop] if index == 0 else [])
            for index in range(3)
        ],
        created_by="capture-script",
    ))
    workspaces = {workspace.workspace_id: workspace}
    audit_repository = InMemoryAuditRepository(workspaces)
    audit_gate = SuggestionAuditGate(
        audit_repository,
        InMemoryMemberConstraintRepository(workspaces),
        clock=lambda: datetime.now(timezone.utc),
    )
    anchor_stop_id = seed_stop.stop_id
    for round_index in range(1, CHAIN_ROUNDS + 1):
        # Gate time is reset per round to the latest captured receipt below;
        # this stays deterministic and prevents future/stale evidence drift.
        round_payload, accepted, accepted_draft, used = await _capture_round(
            current_anchor,
            round_index=round_index,
            selected_place_ids=selected_ids,
            selected_place_names=selected_names,
            candidate_source=candidate_source,
            route_source=route_source,
            request_pause_seconds=request_pause_seconds,
            route_prior_loader=route_prior_loader,
            audit_gate=audit_gate,
            workspace=workspace,
            base=base,
            insert_after_stop_id=anchor_stop_id,
        )
        rounds.append(round_payload)
        request_count += used
        if accepted is None or accepted_draft is None:
            break
        base, anchor_stop_id = audit_gate._preview_revision(
            base,
            accepted_draft,
            day_index=0,
            insert_after_stop_id=anchor_stop_id,
            insert_before_stop_id=None,
        )
        workspace = workspace.model_copy(update={"current_itinerary_revision": base.revision})
        workspaces[workspace.workspace_id] = workspace
        selected_ids.add(accepted.canonical_place.place_id)
        selected_names.add(accepted.canonical_place.name.strip().casefold())
        current_anchor = _next_anchor(accepted)
    status = "COMPLETE" if len(rounds) == CHAIN_ROUNDS and all(
        item["status"] == "COMPLETE" for item in rounds
    ) else "PARTIAL"
    return {
        "city": initial_anchor["city"],
        "chain_status": status,
        "required_rounds": CHAIN_ROUNDS,
        "completed_rounds": sum(item["status"] == "COMPLETE" for item in rounds),
        "initial_anchor": {
            "place_id": initial_anchor["place_id"],
            "name": initial_anchor["name"],
            "coords": initial_anchor["coords"].model_dump(mode="json"),
            "authority": "fixed_canonical_anchor",
        },
        "rounds": rounds,
        "selected_chain_place_ids": [
            initial_anchor["place_id"],
            *[
                item["selection"]["selected_candidate_place_id"]
                for item in rounds
                if item["selection"]["selected_candidate_place_id"]
            ],
        ],
    }, request_count


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def validate_artifact(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if report.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version_invalid")
    if report.get("evidence_class") != EVIDENCE_CLASS:
        errors.append("evidence_class_invalid")
    if report.get("evidence_subtype") != EVIDENCE_SUBTYPE:
        errors.append("evidence_subtype_invalid")
    # This artifact proves only a bounded Provider capture.  The G2 product
    # replay is a separate RunSpec artifact which byte-binds this snapshot.
    # Requiring the replay receipt inside the snapshot would be circular: the
    # product cannot load the snapshot until it is PASSED, while the snapshot
    # could not become PASSED until the product had loaded it.  Keep the claim
    # fail-closed here and let the external G2 gate own the replay claim.
    replay = report.get("authoritative_public_asgi_replay") or {}
    if replay != {
        "status": "NOT_RUN",
        "transport": "SEPARATE_RUNSPEC_PUBLIC_HTTP",
        "gate_decision": "UNAVAILABLE",
        "reason_code": "G2_REPLAY_RECORDED_OUTSIDE_PROVIDER_SNAPSHOT",
    }:
        errors.append("authoritative_replay_boundary_invalid")
    if (report.get("claim_boundary") or {}).get(
        "proves_three_city_four_stop_snapshot_replay"
    ) is not False:
        errors.append("authoritative_replay_claim_must_remain_false")
    runtime = report.get("runtime") or {}
    if (
        runtime.get("runtime_profile") != "local_real"
        or runtime.get("amap_mock") is not False
        or runtime.get("demo_mode") is not False
        or runtime.get("authentication_material_persisted") is not False
        or runtime.get("raw_provider_payload_persisted") is not False
        or runtime.get("fixture_fallback_allowed") is not False
        or runtime.get("amap_api_key_configured") is not True
        or runtime.get("request_mode") != "single_sequential_bounded_no_retry"
        or runtime.get("retry_count") != 0
    ):
        errors.append("runtime_boundary_invalid")
    requests = report.get("request_accounting") or {}
    if (
        not isinstance(requests.get("actual_requests"), int)
        or requests.get("actual_requests") > requests.get("max_requests", -1)
        or requests.get("max_requests") != MAX_PROVIDER_REQUESTS
    ):
        errors.append("request_budget_invalid")
    cities = report.get("cities") or []
    if [row.get("city") for row in cities] != [anchor["city"] for anchor in ANCHORS]:
        errors.append("fixed_three_city_order_invalid")
    for city_row in cities:
        city = city_row.get("city")
        rounds = city_row.get("rounds") or []
        if city_row.get("chain_status") == "COMPLETE" and len(rounds) != CHAIN_ROUNDS:
            errors.append(f"{city}:complete_chain_round_count_invalid")
        previous_selected = None
        for index, row in enumerate(rounds, 1):
            # A candidate may legitimately be returned again from a nearby
            # later anchor.  Only duplicates inside one frozen set, plus the
            # already accepted chain itself, are prohibited.
            seen_in_round: set[tuple[Any, str]] = set()
            anchor = row.get("anchor") or {}
            if row.get("round_index") != index:
                errors.append(f"{city}:round_index_invalid")
            if index > 1 and anchor.get("place_id") != previous_selected:
                errors.append(f"{city}:round_{index}:anchor_continuity_invalid")
            queries = row.get("query_receipts") or []
            if [item.get("intent") for item in queries] != [intent.value for intent in INTENTS]:
                errors.append(f"{city}:round_{index}:query_plan_invalid")
            for query in queries:
                if (
                    query.get("provider") != "amap"
                    or query.get("execution_mode") != "live"
                    or query.get("source_url") != "https://restapi.amap.com/v5/place/around"
                    or not _valid_hash(query.get("query_contract_sha256"))
                    or not _valid_hash(query.get("provider_request_hash"))
                    or not _valid_hash(query.get("provider_response_hash"))
                    or not query.get("retrieved_at")
                ):
                    errors.append(f"{city}:round_{index}:query_receipt_invalid")
            candidates = row.get("candidates") or []
            if row.get("status") == "COMPLETE" and not 4 <= len(candidates) <= 6:
                errors.append(f"{city}:round_{index}:candidate_count_invalid")
            candidate_by_id = {}
            for item in candidates:
                place = item.get("canonical_place") or {}
                receipt = item.get("provider_receipt") or {}
                route = item.get("route_times") or {}
                place_id = place.get("place_id")
                candidate_by_id[place_id] = item
                identity = (place_id, str(place.get("name") or "").strip().casefold())
                if place_id == anchor.get("place_id") or identity in seen_in_round:
                    errors.append(f"{city}:round_{index}:{place_id}:duplicate_or_anchor")
                seen_in_round.add(identity)
                if _normalise_city(place.get("city", "")) != city:
                    errors.append(f"{city}:round_{index}:{place_id}:wrong_city")
                if (
                    receipt.get("provider") != "amap"
                    or receipt.get("execution_mode") != "live"
                    or receipt.get("canonical_place_id") != place_id
                    or not _valid_hash(receipt.get("request_hash"))
                    or not _valid_hash(receipt.get("response_hash"))
                    or not receipt.get("observed_at")
                    or receipt.get("source_url") != "https://restapi.amap.com/v5/place/around"
                ):
                    errors.append(f"{city}:round_{index}:{place_id}:entity_receipt_invalid")
                operational = item.get("operational_evidence") or {}
                if operational.get("status") == "PROVIDER_EXPLICIT":
                    for fact in operational.get("facts") or []:
                        if (
                            fact.get("request_hash") != receipt.get("request_hash")
                            or fact.get("response_hash") != receipt.get("response_hash")
                            or fact.get("execution_mode") != "live"
                            or not fact.get("observed_at")
                            or fact.get("source_url") != "https://restapi.amap.com/v5/place/around"
                        ):
                            errors.append(f"{city}:round_{index}:{place_id}:current_fact_invalid")
                elif operational.get("status") != "UNKNOWN" or operational.get("facts"):
                    errors.append(f"{city}:round_{index}:{place_id}:current_fact_boundary_invalid")
                route_receipts = route.get("route_receipts") or []
                if route.get("status") != "AVAILABLE" or len(route_receipts) != 1:
                    errors.append(f"{city}:round_{index}:{place_id}:route_unavailable")
                else:
                    leg = route_receipts[0]
                    if (
                        leg.get("origin_place_id") != anchor.get("place_id")
                        or leg.get("destination_place_id") != place_id
                        or leg.get("provider") != "amap"
                        or leg.get("execution_mode") != "live"
                        or leg.get("transport_mode") != "walking"
                        or not _valid_hash(leg.get("request_hash"))
                        or not _valid_hash(leg.get("response_hash"))
                        or not leg.get("observed_at")
                        or leg.get("source_url") != "https://restapi.amap.com/v3/direction/walking"
                    ):
                        errors.append(f"{city}:round_{index}:{place_id}:route_receipt_invalid")
            selection = row.get("selection") or {}
            previous_selected = selection.get("selected_candidate_place_id")
            selected_item = candidate_by_id.get(previous_selected)
            if row.get("status") == "COMPLETE" and (
                selected_item is None
                or selection.get("selected_classification")
                not in {"ON_ROUTE", "ACCEPTABLE_DETOUR"}
                or selection.get("authoritative_gate_status") != "SATISFIED"
                or not isinstance(selection.get("selected_candidate_rank"), int)
                or not 1 <= selection.get("selected_candidate_rank") <= 6
                or not isinstance(selection.get("selected_route_minutes"), int)
                or selection.get("selected_route_minutes") > 30
            ):
                errors.append(f"{city}:round_{index}:selected_candidate_invalid")
        expected_chain = [
            (city_row.get("initial_anchor") or {}).get("place_id"),
            *[
                row.get("selection", {}).get("selected_candidate_place_id")
                for row in rounds
                if row.get("selection", {}).get("selected_candidate_place_id")
            ],
        ]
        if city_row.get("selected_chain_place_ids") != expected_chain:
            errors.append(f"{city}:selected_chain_invalid")
        elif len(expected_chain) != len(set(expected_chain)):
            errors.append(f"{city}:selected_chain_duplicate")
    capture_complete = bool(
        len(cities) == len(ANCHORS)
        and all(
            city_row.get("chain_status") == "COMPLETE"
            and city_row.get("completed_rounds") == CHAIN_ROUNDS
            for city_row in cities
        )
    )
    if report.get("capture_status") == "COMPLETE" and not capture_complete:
        errors.append("capture_status_complete_without_three_complete_chains")
    if report.get("overall_status") == "PASSED" and (
        report.get("capture_status") != "COMPLETE"
        or not capture_complete
        or report.get("failure_receipt") is not None
    ):
        errors.append("passed_without_complete_provider_capture")
    integrity = report.get("integrity") or {}
    expected_hash = _canonical_hash({key: value for key, value in report.items() if key != "integrity"})
    if integrity.get("artifact_payload_sha256") != expected_hash:
        errors.append("artifact_payload_hash_mismatch")
    return list(dict.fromkeys(errors))


def seal_report(report: dict[str, Any]) -> dict[str, Any]:
    """Recompute the excluded integrity envelope without Provider access."""
    report.pop("integrity", None)
    report["integrity"] = {
        "artifact_payload_sha256": _canonical_hash(report),
        "hash_algorithm": "SHA-256 over canonical UTF-8 JSON excluding integrity",
        "passed": False,
        "validation_errors": [],
    }
    errors = validate_artifact(report)
    if errors and report.get("overall_status") == "PASSED":
        report["overall_status"] = "FAILED"
        report["integrity"]["artifact_payload_sha256"] = _canonical_hash(
            {key: value for key, value in report.items() if key != "integrity"}
        )
        errors = validate_artifact(report)
    report["integrity"]["passed"] = not errors
    report["integrity"]["validation_errors"] = errors
    return report


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
        "fixture_fallback_allowed": False,
        "request_mode": "single_sequential_bounded_no_retry",
        "retry_count": 0,
        "request_pause_seconds": request_pause_seconds,
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evidence_class": EVIDENCE_CLASS,
        "evidence_subtype": EVIDENCE_SUBTYPE,
        "claim_boundary": {
            "proves_local_authorized_live_entity": True,
            "proves_local_authorized_live_route": True,
            "proves_three_city_four_stop_snapshot_replay": False,
            "proves_current_live_state_at_replay": False,
            "proves_opening_hours": False,
            "proves_reservation": False,
            "proves_accessibility": False,
            "is_public_internet_e2e": False,
            "is_human_evidence": False,
            "is_release_approval": False,
        },
        "runtime": runtime_receipt,
        "run_started_at": _now(),
        "run_completed_at": None,
        "overall_status": "FAILED",
        "failure_receipt": None,
        "capture_status": "FAILED",
        "authoritative_public_asgi_replay": {
            "status": "NOT_RUN",
            "transport": "SEPARATE_RUNSPEC_PUBLIC_HTTP",
            "gate_decision": "UNAVAILABLE",
            "reason_code": "G2_REPLAY_RECORDED_OUTSIDE_PROVIDER_SNAPSHOT",
        },
        "request_accounting": {
            "max_requests": MAX_PROVIDER_REQUESTS,
            "actual_requests": 0,
            "candidate_query_requests": 0,
            "walking_route_requests": 0,
        },
        "cities": [],
    }
    preflight_errors = _preflight(runtime_receipt)
    if preflight_errors:
        report["failure_receipt"] = {"stage": "preflight", "reason_codes": preflight_errors}
    else:
        candidate_source = (candidate_source_factory or AmapCandidateSource)()
        route_source = (route_source_factory or AmapRouteSource)()
        route_prior_loader = RoutePriorLoader()
        try:
            for anchor in ANCHORS:
                city, used = await _capture_city(
                    anchor,
                    candidate_source=candidate_source,
                    route_source=route_source,
                    request_pause_seconds=request_pause_seconds,
                    route_prior_loader=route_prior_loader,
                )
                report["cities"].append(city)
                report["request_accounting"]["actual_requests"] += used
            completed = sum(city["chain_status"] == "COMPLETE" for city in report["cities"])
            report["capture_status"] = "COMPLETE" if completed == len(ANCHORS) else "PARTIAL"
            report["overall_status"] = "PASSED" if completed == len(ANCHORS) else "PARTIAL"
            if completed == len(ANCHORS):
                report["failure_receipt"] = None
            else:
                report["failure_receipt"] = {
                    "stage": "chain_selection",
                    "reason_code": "THREE_CITY_CHAIN_INCOMPLETE",
                    "complete_city_count": completed,
                }
        except Exception as exc:
            report["failure_receipt"] = {
                "stage": "provider_capture",
                **_safe_failure(exc),
                "completed_city_count": len(report["cities"]),
            }
    report["request_accounting"]["candidate_query_requests"] = sum(
        len(round_row.get("query_receipts") or [])
        for city in report["cities"]
        for round_row in city.get("rounds") or []
    )
    report["request_accounting"]["walking_route_requests"] = sum(
        len(round_row.get("candidates") or [])
        for city in report["cities"]
        for round_row in city.get("rounds") or []
    )
    report["run_completed_at"] = _now()
    return seal_report(report)


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
            f"{city['city']}: status={city['chain_status']} "
            f"rounds={city['completed_rounds']}/{city['required_rounds']}"
        )
    print(
        f"provider_requests={report['request_accounting']['actual_requests']}/"
        f"{report['request_accounting']['max_requests']}"
    )
    print(f"overall_status={report['overall_status']}")
    print(f"evidence_file={output}")
    if args.strict and report["overall_status"] != "PASSED":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
