from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import ProviderFailure
from app.audit.recheck import (
    LiveAmapPoiEvidenceRefresher,
    PreTripRecheckService,
    RecheckWindowState,
    RecheckEvidenceCollection,
    diff_evidence_snapshots,
)
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.schemas.place import Coordinates, Place, PlaceCategory, PlaceSource, RetrievalExecutionMode


class PartiallyFailingRefresher:
    async def collect(self, *, workspace_id, revision, place_records):
        observations = EvidenceService().observations_from_revision(
            revision,
            place_records,
            now=revision.created_at,
        )
        return RecheckEvidenceCollection(
            observations=observations,
            provider_failures=[ProviderFailure(
                provider="amap_poi",
                error_category="timeout",
                retryable=True,
                detail="opening-hours refresh timed out",
            )],
        )


def _snapshot_with_provider_facts(*providers: str):
    return EvidenceService().create_snapshot(
        workspace_id="provider-diff-workspace",
        itinerary_revision=1,
        observations=[
            EvidenceObservation(
                subject_type="PLACE",
                subject_id="provider-diff-place",
                fact_type="OPENING_HOURS",
                value="09:00-18:00",
                provider=provider,
                observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            )
            for provider in providers
        ],
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )


def _recheck_revision_and_records():
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="provider-recheck-itinerary",
        workspace_id="provider-recheck-workspace",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[
                ItineraryStop(
                    stop_id="provider-stop-one", place_id="provider-place-one", day_index=0, order_index=0,
                    start_time="09:00", end_time="11:00", raw_name="第一地点",
                ),
                ItineraryStop(
                    stop_id="provider-stop-two", place_id="provider-place-two", day_index=0, order_index=1,
                    start_time="12:00", end_time="14:00", raw_name="第二地点",
                ),
            ]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="recheck-user",
    ))
    records = {
        "provider-place-one": {
            "place_id": "provider-place-one", "name": "第一地点", "city": "北京",
            "opening_hours": "08:00-18:00", "provider": "stored_amap",
        },
        "provider-place-two": {
            "place_id": "provider-place-two", "name": "第二地点", "city": "北京",
            "opening_hours": "09:00-17:00", "provider": "stored_amap",
        },
    }
    return revision, records


def _live_place(place_id: str, name: str, opening_hours: str = "10:00-20:00") -> Place:
    return Place(
        place_id=place_id, name=name, category=PlaceCategory.ATTRACTION,
        address="北京市海淀区", coords=Coordinates(lng=116.31, lat=39.99), city="北京",
        source=PlaceSource.AMAP_POI, execution_mode=RetrievalExecutionMode.LIVE,
        retrieval_provider="amap", opening_hours=opening_hours, amap_price=88,
    )


@pytest.mark.asyncio
async def test_enabled_live_provider_recheck_replaces_stored_facts_and_persists_live_receipt():
    revision, records = _recheck_revision_and_records()
    observed_at = datetime(2026, 8, 20, 9, 0, tzinfo=timezone.utc)

    async def simulated_live_search(*, query, city):
        assert city == "北京"
        assert query in {"第一地点", "第二地点"}
        place_id = "provider-place-one" if query == "第一地点" else "provider-place-two"
        return [_live_place(place_id, query)], [{
            "provider": "amap", "execution_mode": "live", "retrieved_at": observed_at,
            "response_hash": "a" * 64, "result_count": 1, "status": "ok",
        }]

    result = await LiveAmapPoiEvidenceRefresher(
        enabled=True, search_with_audit=simulated_live_search,
    ).collect(workspace_id="provider-recheck-workspace", revision=revision, place_records=records)

    assert not result.provider_failures
    assert len(result.provider_receipts) == 2
    assert all(item.provider_call_attempted and item.execution_mode == "live" for item in result.provider_receipts)
    refreshed = [item for item in result.observations if item.provider == "amap_live_recheck"]
    assert {item.subject_id for item in refreshed if item.fact_type == "POI_IDENTITY"} == {
        "provider-place-one", "provider-place-two",
    }
    assert all(item.observed_at == observed_at for item in refreshed)
    receipts = [item for item in result.observations if item.fact_type == "POI_REFRESH_RECEIPT"]
    assert all(item.value["provider_call_attempted"] is True for item in receipts)
    snapshot = EvidenceService().create_snapshot(
        workspace_id="provider-recheck-workspace", itinerary_revision=1, observations=result.observations,
        now=observed_at,
    )
    assert "amap" in snapshot.provider_set
    assert all(item.response_hash for item in snapshot.facts if item.fact_type == "POI_REFRESH_RECEIPT")


@pytest.mark.asyncio
async def test_live_provider_partial_failure_keeps_stored_evidence_and_records_only_failed_place():
    revision, records = _recheck_revision_and_records()

    async def partial_live_search(*, query, city):
        if query == "第一地点":
            return [_live_place("provider-place-one", query)], [{
                "provider": "amap", "execution_mode": "live", "retrieved_at": datetime.now(timezone.utc),
                "response_hash": "b" * 64, "result_count": 1, "status": "ok",
            }]
        raise TimeoutError("provider timed out")

    result = await LiveAmapPoiEvidenceRefresher(
        enabled=True, search_with_audit=partial_live_search,
    ).collect(workspace_id="provider-recheck-workspace", revision=revision, place_records=records)

    assert len(result.provider_failures) == 1
    assert result.provider_failures[0].detail.startswith("provider-place-two:")
    failed_opening = next(
        item for item in result.observations
        if item.subject_id == "provider-place-two" and item.fact_type == "OPENING_HOURS"
    )
    assert failed_opening.provider == "stored_amap"
    failed_receipt = next(item for item in result.provider_receipts if item.subject_id == "provider-place-two")
    assert failed_receipt.provider_call_attempted is True
    assert failed_receipt.status == "error"


@pytest.mark.asyncio
async def test_disabled_live_provider_recheck_uses_stored_fallback_without_claiming_network_call():
    revision, records = _recheck_revision_and_records()
    calls = 0

    async def must_not_call(*, query, city):
        nonlocal calls
        calls += 1
        raise AssertionError("disabled live recheck called provider")

    result = await LiveAmapPoiEvidenceRefresher(
        enabled=False, search_with_audit=must_not_call,
    ).collect(workspace_id="provider-recheck-workspace", revision=revision, place_records=records)

    assert calls == 0
    assert not result.provider_failures
    assert result.provider_receipts[0].provider_call_attempted is False
    assert result.provider_receipts[0].execution_mode == "stored_fallback"
    assert result.provider_receipts[0].detail == "live_recheck_disabled"
    assert any(item.provider == "stored_amap" for item in result.observations)
    fallback_receipt = next(item for item in result.observations if item.fact_type == "POI_REFRESH_RECEIPT")
    assert fallback_receipt.provider == "pre_trip_recheck"
    snapshot = EvidenceService().create_snapshot(
        workspace_id="provider-recheck-workspace", itinerary_revision=1, observations=result.observations,
    )
    assert "amap" not in snapshot.provider_set
    assert any(item.provider == "pre_trip_recheck" for item in snapshot.facts if item.fact_type == "POI_REFRESH_RECEIPT")


@pytest.mark.asyncio
async def test_pre_trip_recheck_keeps_partial_evidence_and_exposes_provider_degradation():
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="recheck-itinerary",
        workspace_id="recheck-workspace",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[ItineraryStop(
                stop_id="recheck-stop", place_id="recheck-place", day_index=0, order_index=0,
                start_time="09:00", end_time="11:00",
            )]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="recheck-user",
    ))
    workspace = TripWorkspace(
        workspace_id="recheck-workspace", room_id="recheck-room", city="北京",
        trip_date_range=date_range, current_itinerary_revision=1, created_by="recheck-user",
    )
    itineraries = InMemoryItineraryRepository()
    await itineraries.create_workspace(workspace, revision)
    audits = InMemoryAuditRepository(itineraries.workspaces)
    audits.place_records[workspace.workspace_id] = {
        "recheck-place": {"place_id": "recheck-place", "name": "复检景点", "city": "北京", "opening_hours": "08:00-18:00"}
    }
    baseline = await AuditApplicationService(
        itinerary_repository=itineraries, audit_repository=audits,
    ).run_current_audit(workspace.workspace_id)

    result, replayed = await PreTripRecheckService(
        itinerary_repository=itineraries,
        audit_repository=audits,
        evidence_refresher=PartiallyFailingRefresher(),
    ).run_idempotent(
        source_report_id=baseline.report_id,
        actor_user_id="recheck-user",
        idempotency_key="partial-provider-recheck",
        command_repository=InMemoryCreationCommandRepository(),
    )

    assert replayed is False
    assert result.degraded is True
    assert result.provider_failures[0].error_category == "timeout"
    assert result.provider_failure_changes[0].change_type == "ADDED"
    assert result.evidence_snapshot.facts  # failure does not discard successful evidence
    assert result.report.supersedes_report_id == baseline.report_id


def test_recheck_window_uses_explicit_china_midnight_trip_date_reference():
    trip_start = date(2026, 9, 1)
    service = PreTripRecheckService.__new__(PreTripRecheckService)

    early = service._window_for_trip_start(
        trip_start=trip_start,
        reference_at=datetime(2026, 8, 29, tzinfo=timezone.utc),
    )
    recommended = service._window_for_trip_start(
        trip_start=trip_start,
        reference_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
    )
    late = service._window_for_trip_start(
        trip_start=trip_start,
        reference_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )

    assert early[0] == RecheckWindowState.EARLY
    assert recommended[0] == RecheckWindowState.RECOMMENDED_24_48H
    assert late[0] == RecheckWindowState.LATE
    assert recommended[1].isoformat() == "2026-09-01T00:00:00+08:00"
    assert recommended[2] == 40.0
    assert "首日零点" in recommended[3]


def test_provider_change_requires_an_unambiguous_one_to_one_subject_fact_swap():
    old = _snapshot_with_provider_facts("stored_amap")
    replacement = _snapshot_with_provider_facts("amap_live_recheck")
    changed = diff_evidence_snapshots(old, replacement)

    assert len(changed) == 1
    assert changed[0].change_type.value == "PROVIDER_CHANGED"
    assert changed[0].before.provider == "stored_amap"
    assert changed[0].after.provider == "amap_live_recheck"

    # Two old candidates and one new candidate cannot be matched safely.  Do
    # not turn that into a plausible but false provider replacement relation.
    ambiguous_old = _snapshot_with_provider_facts("stored_amap", "official_site")
    ambiguous_new = _snapshot_with_provider_facts("amap_live_recheck")
    ambiguous_changes = diff_evidence_snapshots(ambiguous_old, ambiguous_new)

    assert {item.change_type.value for item in ambiguous_changes} == {"ADDED", "REMOVED"}
    assert all(item.change_type.value != "PROVIDER_CHANGED" for item in ambiguous_changes)
