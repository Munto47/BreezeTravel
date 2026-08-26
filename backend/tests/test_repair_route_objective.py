from __future__ import annotations

from datetime import date, datetime, timezone

from app.audit.models import EvidenceFact, EvidenceFreshness, EvidenceSnapshot
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
)
from app.repairs.models import RepairOperation, RepairOperationType, RepairOption
from app.repairs.objective import repair_option_sort_key, route_cost_delta_minutes


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def _revision(*stop_ids: str, revision: int = 1):
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="route-objective-itinerary",
        workspace_id="route-objective-workspace",
        revision=revision,
        parent_revision=revision - 1 if revision > 1 else None,
        source_type=RevisionSource.REPAIR if revision > 1 else RevisionSource.IMPORT,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(
                day_index=0,
                date=date_range.start,
                stops=[
                    ItineraryStop(
                        stop_id=stop_id,
                        place_id=f"place-{stop_id}",
                        day_index=0,
                        order_index=index,
                        raw_name=stop_id,
                    )
                    for index, stop_id in enumerate(stop_ids)
                ],
            ),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="route-objective-test",
        created_at=NOW,
    ))


def _snapshot(revision: int, routes: dict[str, float | None]):
    snapshot_id = f"snapshot-{revision}-{len(routes)}-{sum(value or 0 for value in routes.values())}"
    facts = []
    for index, (edge_id, duration) in enumerate(routes.items()):
        facts.append(EvidenceFact(
            fact_id=f"fact-{revision}-{index}",
            snapshot_id=snapshot_id,
            subject_type="ROUTE_EDGE",
            subject_id=edge_id,
            fact_type="ROUTE_TIME",
            value=None if duration is None else {"duration_minutes": duration},
            provider="fixture",
            observed_at=NOW,
            response_hash=f"{index + 1:064x}",
            confidence=1 if duration is not None else 0,
            freshness_status=(
                EvidenceFreshness.FRESH if duration is not None else EvidenceFreshness.UNAVAILABLE
            ),
        ))
    return EvidenceSnapshot(
        snapshot_id=snapshot_id,
        workspace_id="route-objective-workspace",
        itinerary_revision=revision,
        provider_set=["fixture"],
        policy_version="test-v1",
        facts=facts,
        created_at=NOW,
    )


def test_route_cost_delta_known_positive_negative_and_zero():
    source = _revision("a", "b")
    candidate = _revision("a", "c", revision=2)
    source_snapshot = _snapshot(1, {"a->b": 30})

    assert route_cost_delta_minutes(
        source, candidate, source_snapshot, _snapshot(2, {"a->c": 45})
    ) == 15
    assert route_cost_delta_minutes(
        source, candidate, source_snapshot, _snapshot(2, {"a->c": 20})
    ) == -10
    assert route_cost_delta_minutes(
        source, source.model_copy(update={"revision": 2, "parent_revision": 1}),
        source_snapshot, _snapshot(2, {"a->b": 30}),
    ) == 0


def test_route_cost_delta_is_unknown_when_either_side_is_incomplete():
    source = _revision("a", "b")
    candidate = _revision("a", "c", revision=2)

    assert route_cost_delta_minutes(
        source, candidate, _snapshot(1, {"a->b": 30}), _snapshot(2, {"a->c": None})
    ) is None
    assert route_cost_delta_minutes(
        source, candidate, _snapshot(1, {}), _snapshot(2, {"a->c": 20})
    ) is None


def _option(
    repair_id: str,
    operation: RepairOperationType,
    *,
    risk_cost: float = 0,
    route_delta: float | None,
):
    preview = _revision("a", "b", revision=2)
    return RepairOption(
        repair_id=repair_id,
        source_report_id="report-1",
        base_itinerary_revision=1,
        operations=[RepairOperation(operation=operation, payload={"stop_id": "b"}, rationale="test")],
        targeted_finding_ids=["finding-1"],
        edit_cost=1 if operation == RepairOperationType.ADJUST_TIME else 8,
        risk_cost=risk_cost,
        route_cost_delta=route_delta,
        new_unknown_count=0,
        result_preview=preview,
        postcheck_report_id=f"postcheck-{repair_id}",
        created_at=NOW,
    )


def test_repair_sort_is_lexicographic_and_places_unknown_route_after_known_peer():
    known_positive = _option("known-positive", RepairOperationType.ADJUST_TIME, route_delta=10)
    known_negative = _option("known-negative", RepairOperationType.ADJUST_TIME, route_delta=-5)
    unknown = _option("unknown", RepairOperationType.ADJUST_TIME, route_delta=None)
    deletion = _option("deletion", RepairOperationType.REMOVE_STOP, route_delta=-30)
    residual_risk = _option(
        "residual-risk", RepairOperationType.ADJUST_TIME, risk_cost=1, route_delta=-100
    )

    ordered = sorted(
        [residual_risk, deletion, unknown, known_positive, known_negative],
        key=repair_option_sort_key,
    )

    assert [item.repair_id for item in ordered] == [
        "known-negative",
        "known-positive",
        "unknown",
        "deletion",
        "residual-risk",
    ]
