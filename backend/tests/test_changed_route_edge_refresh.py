from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from app.audit.models import EvidenceFreshness
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.constraints.geo_routes import RouteResult
from app.itineraries.hash_service import with_content_hash
from app.itineraries.incremental import IncrementalWorkspaceEditService
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    RevisionTransport,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.itineraries.route_refresh import ChangedRouteEdgeRefreshService
from app.operations.repositories import InMemoryCreationCommandRepository
from app.schemas.place import Coordinates


class ControlledRouteProvider:
    def __init__(self, *, fail: bool = False):
        self.calls: list[tuple[Coordinates, Coordinates, str, str]] = []
        self.fail = fail

    async def fetch(self, *, origin, destination, mode, city):
        self.calls.append((origin, destination, mode, city))
        if self.fail:
            raise TimeoutError("controlled provider failure")
        return RouteResult(
            status="ok", duration_minutes=37, distance_km=8.5,
            transfer_count=None, source="controlled_route", response_hash="a" * 64,
            observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )


def _repository(*, with_coords: bool = True) -> InMemoryItineraryRepository:
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    stops = [
        ItineraryStop(stop_id="a", place_id="pa", raw_name="A", day_index=0, order_index=0,
                      transport_to_next=RevisionTransport(duration_minutes=10, distance_meters=2000)),
        ItineraryStop(stop_id="b", place_id="pb", raw_name="B", day_index=0, order_index=1,
                      transport_to_next=RevisionTransport(duration_minutes=20, distance_meters=4000)),
        ItineraryStop(stop_id="c", place_id="pc", raw_name="C", day_index=0, order_index=2),
    ]
    projection = {
        stop.stop_id: {
            "place": {
                "place_id": stop.place_id,
                "coords": {"lng": 116.30 + index / 100, "lat": 39.90 + index / 100},
            },
            "coordinate_role": "TEST_CANONICAL",
            "provenance": "CONTROLLED_TEST",
        }
        for index, stop in enumerate(stops)
    } if with_coords else {}
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-route-refresh", workspace_id="workspace-route-refresh", revision=1,
        source_type=RevisionSource.MANUAL, city="北京", date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=stops),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        change_summary={"map_stop_projections": projection},
        created_by="route-user",
    ))
    repository = InMemoryItineraryRepository()
    asyncio.run(repository.create_workspace(TripWorkspace(
        workspace_id="workspace-route-refresh", room_id="room-route-refresh", city="北京",
        trip_date_range=date_range, current_itinerary_revision=1, created_by="route-user",
    ), revision))
    return repository


def _reorder(repository: InMemoryItineraryRepository, audits: InMemoryAuditRepository) -> None:
    result = asyncio.run(IncrementalWorkspaceEditService(repository, audit_repository=audits).apply(
        ItineraryEditCommand(
            command_id="reorder-for-route-refresh", workspace_id="workspace-route-refresh",
            base_revision=1, actor_user_id="route-user", operation=EditOperation.REORDER_STOP,
            payload={"stop_id": "b", "target_day_index": 0, "target_order_index": 3},
        ),
        if_match_revision=1, idempotency_key="reorder-for-route-refresh",
    ))
    assert result.new_revision == 2


def _service(repository, audits, provider):
    return ChangedRouteEdgeRefreshService(
        itinerary_repository=repository,
        audit_repository=audits,
        audit_service=AuditApplicationService(
            itinerary_repository=repository, audit_repository=audits,
        ),
        provider=provider,
    )


def test_refreshes_only_current_changed_edges_and_persists_new_immutable_bundle():
    repository = _repository()
    audits = InMemoryAuditRepository(repository.workspaces)
    # An existing audit makes the supersession boundary observable.  Its facts
    # must remain frozen while the current revision gets a distinct snapshot.
    original = asyncio.run(AuditApplicationService(
        itinerary_repository=repository, audit_repository=audits,
    ).run_current_audit("workspace-route-refresh"))
    original_snapshot = asyncio.run(audits.get_snapshot(original.evidence_snapshot_id))
    _reorder(repository, audits)
    provider = ControlledRouteProvider()

    result, replayed = asyncio.run(_service(repository, audits, provider).run_idempotent(
        workspace_id="workspace-route-refresh", itinerary_revision=2, actor_user_id="route-user",
        idempotency_key="refresh-current-new-edges", command_repository=InMemoryCreationCommandRepository(),
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    ))

    assert replayed is False
    assert len(provider.calls) == 2  # a→c and c→b; no provider call for removed a→b/b→c.
    assert result.route_delta["scope"] == "CURRENT_REVISION_CHANGED_EDGES_ONLY"
    assert [item["edge_id"] for item in result.route_delta["changed_edges"]] == [
        "day:0:edge:a->c", "day:0:edge:c->b",
    ]
    assert result.route_delta["status"] == "AVAILABLE"
    assert result.evidence_snapshot.itinerary_revision == 2
    assert result.evidence_snapshot.snapshot_id != original.evidence_snapshot_id
    assert result.evidence_snapshot.supersedes_snapshot_id == original.evidence_snapshot_id
    route_facts = [fact for fact in result.evidence_snapshot.facts if fact.subject_type == "ROUTE_EDGE"]
    assert {fact.subject_id for fact in route_facts} == {"a->c", "c->b"}
    assert all(fact.freshness_status == EvidenceFreshness.FRESH for fact in route_facts)
    # Read back the predecessor by ID: it was not modified in place.
    assert asyncio.run(audits.get_snapshot(original.evidence_snapshot_id)) == original_snapshot


def test_missing_canonical_coordinates_skips_provider_and_persists_unavailable_fact():
    repository = _repository(with_coords=False)
    audits = InMemoryAuditRepository(repository.workspaces)
    _reorder(repository, audits)
    provider = ControlledRouteProvider()

    result, _ = asyncio.run(_service(repository, audits, provider).run_idempotent(
        workspace_id="workspace-route-refresh", itinerary_revision=2, actor_user_id="route-user",
        idempotency_key="refresh-missing-coordinates", command_repository=InMemoryCreationCommandRepository(),
    ))

    assert provider.calls == []
    assert result.route_delta["status"] == "UNAVAILABLE"
    assert result.route_delta["missing_edge_ids"] == ["day:0:edge:a->c", "day:0:edge:c->b"]
    facts = [fact for fact in result.evidence_snapshot.facts if fact.subject_type == "ROUTE_EDGE"]
    assert all(fact.freshness_status == EvidenceFreshness.UNAVAILABLE for fact in facts)
    assert {fact.value["reason_code"] for fact in facts} == {"REVISION_STOP_COORDINATES_UNAVAILABLE"}


def test_provider_failure_is_explicit_unavailable_and_idempotent_replay_keeps_receipt():
    repository = _repository()
    audits = InMemoryAuditRepository(repository.workspaces)
    _reorder(repository, audits)
    provider = ControlledRouteProvider(fail=True)
    commands = InMemoryCreationCommandRepository()
    service = _service(repository, audits, provider)

    first, replayed = asyncio.run(service.run_idempotent(
        workspace_id="workspace-route-refresh", itinerary_revision=2, actor_user_id="route-user",
        idempotency_key="refresh-provider-failure", command_repository=commands,
    ))
    second, replayed_second = asyncio.run(service.run_idempotent(
        workspace_id="workspace-route-refresh", itinerary_revision=2, actor_user_id="route-user",
        idempotency_key="refresh-provider-failure", command_repository=commands,
    ))

    assert replayed is False and replayed_second is True
    assert len(provider.calls) == 2  # replay never invokes a provider again.
    assert first.route_delta["status"] == "UNAVAILABLE"
    assert all(item.error_category == "ROUTE_REFRESH_UNAVAILABLE" for item in first.provider_failures)
    assert second.evidence_snapshot.snapshot_id == first.evidence_snapshot.snapshot_id
    assert second.idempotent_replay is True
