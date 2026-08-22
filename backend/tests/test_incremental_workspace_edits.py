from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from time import perf_counter

from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
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


def _seed() -> InMemoryItineraryRepository:
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    first = ItineraryStop(
        stop_id="a", place_id="pa", raw_name="A", day_index=0, order_index=0,
        start_time="09:00", end_time="10:00",
        transport_to_next=RevisionTransport(duration_minutes=10, distance_meters=2000),
    )
    second = ItineraryStop(
        stop_id="b", place_id="pb", raw_name="B", day_index=0, order_index=1,
        start_time="10:20", end_time="11:20",
        transport_to_next=RevisionTransport(duration_minutes=20, distance_meters=4000),
    )
    third = ItineraryStop(
        stop_id="c", place_id="pc", raw_name="C", day_index=0, order_index=2,
        start_time="12:00", end_time="13:00",
    )
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-incremental",
        workspace_id="workspace-incremental",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[first, second, third]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="user-incremental",
    ))
    repository = InMemoryItineraryRepository()
    asyncio.run(repository.create_workspace(TripWorkspace(
        workspace_id="workspace-incremental",
        room_id="room-incremental",
        city="北京",
        trip_date_range=date_range,
        current_itinerary_revision=1,
        created_by="user-incremental",
    ), revision))
    return repository


def _command(operation: EditOperation, payload: dict, *, command_id: str) -> ItineraryEditCommand:
    return ItineraryEditCommand(
        command_id=command_id,
        workspace_id="workspace-incremental",
        base_revision=1,
        actor_user_id="user-incremental",
        operation=operation,
        payload=payload,
        client_timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_reorder_invalidates_only_new_route_edges_and_returns_incremental_preview():
    repository = _seed()
    result = asyncio.run(IncrementalWorkspaceEditService(repository).apply(
        _command(
            EditOperation.REORDER_STOP,
            {"stop_id": "b", "target_day_index": 0, "target_order_index": 3},
            command_id="reorder-b",
        ),
        if_match_revision=1,
        idempotency_key="reorder-b-key",
    ))

    revision = asyncio.run(repository.get_revision("workspace-incremental", 2))
    assert revision is not None
    assert [stop.stop_id for stop in revision.days[0].stops] == ["a", "c", "b"]
    assert all(stop.transport_to_next is None for stop in revision.days[0].stops)
    assert result.route_delta is not None
    assert result.route_delta["status"] == "PARTIAL"
    assert result.route_delta["missing_edge_ids"] == [
        "day:0:edge:a->c",
        "day:0:edge:c->b",
    ]
    assert result.route_delta["async_route_refresh_required"] is True
    assert result.audit_mode == "INCREMENTAL_REVISION_ONLY"
    assert result.llm_calls == 0
    assert "constraint.time_chain" in result.affected_rule_ids
    assert all(
        not finding["affected_days"] or 0 in finding["affected_days"]
        for finding in result.incremental_findings
    )


def test_lock_reuses_unchanged_route_edges_without_route_refresh():
    repository = _seed()
    result = asyncio.run(IncrementalWorkspaceEditService(repository).apply(
        _command(EditOperation.LOCK_STOP, {"stop_id": "b"}, command_id="lock-b"),
        if_match_revision=1,
        idempotency_key="lock-b-key",
    ))

    revision = asyncio.run(repository.get_revision("workspace-incremental", 2))
    assert revision is not None
    assert revision.days[0].stops[0].transport_to_next is not None
    assert revision.days[0].stops[0].transport_to_next.duration_minutes == 10
    assert revision.days[0].stops[1].transport_to_next is not None
    assert revision.days[0].stops[1].transport_to_next.duration_minutes == 20
    assert result.changed_route_edges == []
    assert result.route_delta == {
        "status": "AVAILABLE",
        "previous_minutes": 0,
        "current_minutes": 0,
        "delta_minutes": 0,
        "changed_edges": [],
        "missing_edge_ids": [],
        "day_end_times": [{"day_index": 0, "previous_end_time": "13:00", "current_end_time": "13:00"}],
        "async_route_refresh_required": False,
    }


def test_incremental_idempotent_replay_keeps_the_same_preview():
    repository = _seed()
    service = IncrementalWorkspaceEditService(repository)
    command = _command(EditOperation.REMOVE_STOP, {"stop_id": "c"}, command_id="remove-c")
    first = asyncio.run(service.apply(
        command, if_match_revision=1, idempotency_key="remove-c-key",
    ))
    replay = asyncio.run(service.apply(
        command, if_match_revision=1, idempotency_key="remove-c-key",
    ))

    assert replay.idempotent_replay is True
    assert replay.route_delta == first.route_delta
    assert replay.incremental_findings == first.incremental_findings
    assert replay.affected_rule_ids == first.affected_rule_ids


def test_replace_does_not_reuse_route_evidence_for_the_old_place():
    repository = _seed()
    result = asyncio.run(IncrementalWorkspaceEditService(repository).apply(
        _command(
            EditOperation.REPLACE_STOP,
            {"stop_id": "b", "new_place_id": "pb-new", "raw_name": "B new"},
            command_id="replace-b",
        ),
        if_match_revision=1,
        idempotency_key="replace-b-key",
    ))

    revision = asyncio.run(repository.get_revision("workspace-incremental", 2))
    assert revision is not None
    assert revision.days[0].stops[0].transport_to_next is None
    assert revision.days[0].stops[1].transport_to_next is None
    assert result.changed_route_edges == ["day:0:edge:a->b", "day:0:edge:b->c"]
    assert result.route_delta is not None
    assert result.route_delta["status"] == "UNAVAILABLE"
    assert result.route_delta["missing_edge_ids"] == ["day:0:edge:a->b", "day:0:edge:b->c"]


def _finding_signature(finding: dict) -> dict:
    """Compare rule conclusions while excluding per-run UUID evidence handles."""
    return {
        key: value
        for key, value in finding.items()
        if key not in {"finding_id", "evidence_fact_ids"}
    }


def test_incremental_preview_matches_full_audit_for_affected_rules_with_known_inputs():
    repository = _seed()
    audits = InMemoryAuditRepository(repository.workspaces)
    audits.place_records["workspace-incremental"] = {
        place_id: {
            "place_id": place_id,
            "name": place_id,
            "city": "北京",
            "category": "attraction",
            "opening_hours": "08:00-18:00",
            "provider": "controlled-fixture",
            "retrieval_observed_at": "2026-09-01T00:00:00+00:00",
        }
        for place_id in ("pa", "pb", "pc")
    }
    service = IncrementalWorkspaceEditService(repository, audit_repository=audits)
    result = asyncio.run(service.apply(
        _command(EditOperation.LOCK_STOP, {"stop_id": "b"}, command_id="parity-lock-b"),
        if_match_revision=1,
        idempotency_key="parity-lock-b-key",
    ))
    revision = asyncio.run(repository.get_revision("workspace-incremental", 2))
    assert revision is not None
    full = asyncio.run(AuditApplicationService(
        itinerary_repository=repository,
        audit_repository=audits,
    ).run_current_audit("workspace-incremental", now=revision.created_at))
    changed_days = {0}
    full_affected = [
        finding.model_dump(mode="json")
        for finding in full.findings
        if finding.rule_id in set(result.affected_rule_ids)
        and (not finding.affected_days or changed_days.intersection(finding.affected_days))
    ]
    preview = [_finding_signature(item) for item in result.incremental_findings]

    assert preview == [_finding_signature(item) for item in full_affected]


def test_fifty_sequential_edits_preserve_every_revision_and_controlled_p95():
    repository = _seed()
    service = IncrementalWorkspaceEditService(repository)

    async def apply_all():
        latencies: list[float] = []
        revision = 1
        for index in range(50):
            operation = EditOperation.LOCK_STOP if index % 2 == 0 else EditOperation.UNLOCK_STOP
            started = perf_counter()
            result = await service.apply(
                ItineraryEditCommand(
                    command_id=f"sequential-{index}",
                    workspace_id="workspace-incremental",
                    base_revision=revision,
                    actor_user_id="user-incremental",
                    operation=operation,
                    payload={"stop_id": "a"},
                    client_timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),
                ),
                if_match_revision=revision,
                idempotency_key=f"sequential-key-{index}",
            )
            latencies.append(perf_counter() - started)
            assert result.new_revision == revision + 1
            revision += 1
        return revision, latencies

    final_revision, latencies = asyncio.run(apply_all())
    stored = asyncio.run(repository.list_revisions("workspace-incremental"))
    p95 = sorted(latencies)[int(len(latencies) * 0.95) - 1]

    assert final_revision == 51
    assert [item.revision for item in stored] == list(range(1, 52))
    # This is deliberately in-memory and provider/LLM-free: it is a bounded
    # regression sentinel for the cache-only incremental path, not a network SLA.
    print(f"controlled incremental cache-only p95={p95 * 1000:.1f}ms")
    assert p95 < 0.5, f"controlled incremental cache-only p95={p95 * 1000:.1f}ms"
