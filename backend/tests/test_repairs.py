from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.evidence_service import EvidenceObservation
from app.audit.models import AuditSeverity, AuditStatus
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.hash_service import canonical_json, with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
    WorkspaceStatus,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.constraints.geo_routes import RouteResult
from app.repairs.errors import RepairNoFeasibleOptionError
from app.repairs.repositories import InMemoryRepairRepository
from app.repairs.search import BoundedRepairSearch, ProviderRepairRouteEvidenceRefresher


class ControlledRepairRouteProvider:
    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = []

    async def fetch(self, *, origin, destination, mode, city):
        self.calls.append((origin, destination, mode, city))
        if self.fail:
            raise TimeoutError("controlled route provider failure")
        return RouteResult(
            status="ok",
            duration_minutes=15,
            distance_km=2.5,
            transfer_count=None,
            source="controlled_route_fixture",
            response_hash="a" * 64,
            observed_at=None,
        )


def _stop(
    stop_id: str,
    place_id: str,
    day_index: int,
    order_index: int,
    start: str,
    end: str,
    *,
    locked: bool = False,
):
    hour, minute = (int(part) for part in start.split(":"))
    end_hour, end_minute = (int(part) for part in end.split(":"))
    return ItineraryStop(
        stop_id=stop_id,
        place_id=place_id,
        day_index=day_index,
        order_index=order_index,
        start_time=start,
        end_time=end,
        visit_duration_minutes=(end_hour * 60 + end_minute) - (hour * 60 + minute),
        raw_name=stop_id,
        locked=locked,
    )


async def _repair_context(
    *,
    lock_first: bool = False,
    lock_second: bool = False,
    route_provider: ControlledRepairRouteProvider | None = None,
    source_route_minutes: int | None = None,
    second_start: str = "11:00",
    second_end: str = "13:00",
):
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="repair-itinerary",
        workspace_id="repair-workspace",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(day_index=0, date=date_range.start, stops=[
                _stop("故宫", "p1", 0, 0, "09:00", "12:00", locked=lock_first),
                _stop("景山", "p2", 0, 1, second_start, second_end, locked=lock_second),
            ]),
            ItineraryDay(day_index=1, date=date_range.end, stops=[
                _stop("西湖", "p3", 1, 0, "10:00", "11:00"),
            ]),
        ],
        locked_commitments=[
            stop_id for stop_id, locked in (("故宫", lock_first), ("景山", lock_second)) if locked
        ],
        change_summary={
            "map_stop_projections": {
                stop_id: {
                    "place_id": place_id,
                    "coords": {"lng": lng, "lat": lat},
                    "coordinate_role": "CONTROLLED_CANONICAL_POI",
                    "provenance": "CONTROLLED_TEST_FIXTURE",
                }
                for stop_id, place_id, lng, lat in (
                    ("故宫", "p1", 116.397, 39.918),
                    ("景山", "p2", 116.396, 39.925),
                    ("西湖", "p3", 120.148, 30.244),
                )
            },
        },
        created_by="repair-user",
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    ))
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="repair-room",
        city="北京",
        trip_date_range=date_range,
        current_itinerary_revision=1,
        created_by="repair-user",
    )
    itinerary_repository = InMemoryItineraryRepository()
    await itinerary_repository.create_workspace(workspace, revision)
    audit_repository = InMemoryAuditRepository()
    audit_repository.current_revisions[workspace.workspace_id] = 1
    audit_repository.place_records[workspace.workspace_id] = {
        place_id: {
            "place_id": place_id,
            "name": name,
            "city": "北京",
            "category": "attraction",
            "opening_hours": "08:00-20:00",
            "retrieval_observed_at": datetime(2026, 8, 20, tzinfo=timezone.utc),
        }
        for place_id, name in (("p1", "故宫"), ("p2", "景山"), ("p3", "西湖"))
    }
    evidence_observations = []
    if source_route_minutes is not None:
        evidence_observations.append(EvidenceObservation(
            subject_type="ROUTE_EDGE",
            subject_id="故宫->景山",
            fact_type="ROUTE_TIME",
            value={"mode": "driving", "duration_minutes": source_route_minutes},
            provider="controlled_route_fixture",
            observed_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        ))
    source_report = await AuditApplicationService(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
    ).run_current_audit(
        workspace.workspace_id,
        evidence_observations=evidence_observations,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    itinerary_repository.workspaces[workspace.workspace_id] = workspace.model_copy(update={
        "current_report_id": source_report.report_id,
        "status": WorkspaceStatus.NEEDS_CONFIRMATION,
    })
    repair_repository = InMemoryRepairRepository(itinerary_repository, audit_repository)
    route_provider = route_provider or ControlledRepairRouteProvider()
    search = BoundedRepairSearch(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
        repair_repository=repair_repository,
        route_refresher=ProviderRepairRouteEvidenceRefresher(route_provider),
    )
    return itinerary_repository, audit_repository, repair_repository, search, revision, source_report


@pytest.mark.asyncio
async def test_repair_route_gap_offers_shift_or_shorten_and_runs_full_postcheck():
    _, audit_repository, _, search, _, source_report = await _repair_context(
        source_route_minutes=35,
        second_start="12:00",
        second_end="13:00",
    )

    source_finding = next(
        item for item in source_report.findings if item.reason_code == "ROUTE_GAP_INSUFFICIENT"
    )
    assert source_finding.input_values["available_minutes"] == 0
    assert source_finding.input_values["route_duration_minutes"] == 35

    options = await search.propose(
        source_report.report_id,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert len(options) == 2
    assert {item.operations[0].payload["stop_id"] for item in options} == {"故宫", "景山"}
    for option in options:
        postcheck = await audit_repository.get_report(option.postcheck_report_id)
        assert postcheck is not None
        assert not any(
            finding.status == AuditStatus.VIOLATED
            and finding.reason_code == "ROUTE_GAP_INSUFFICIENT"
            for finding in postcheck.findings
        )
        assert option.new_unknown_count == 0


@pytest.mark.asyncio
async def test_repair_ab_have_real_tradeoff_and_mandatory_full_postcheck():
    _, audit_repository, _, search, base, source_report = await _repair_context()
    source_finding = next(item for item in source_report.findings if item.reason_code == "TIME_CHAIN_BROKEN")
    assert source_finding.input_values["day_stops"] == [
        {
            "stop_id": "故宫",
            "place_id": "p1",
            "start_time": "09:00",
            "end_time": "12:00",
            "locked": False,
            "fixed_commitment": False,
        },
        {
            "stop_id": "景山",
            "place_id": "p2",
            "start_time": "11:00",
            "end_time": "13:00",
            "locked": False,
            "fixed_commitment": False,
        },
    ]
    options = await search.propose(source_report.report_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))

    assert len(options) == 2
    assert options[0].operations != options[1].operations
    assert options[0].tradeoffs != options[1].tradeoffs
    assert all(option.postcheck_report_id for option in options)
    assert all(option.new_unknown_count == 0 for option in options)
    # This fixture has no complete ROUTE_TIME evidence.  Absence must remain
    # explicit instead of being reported as a zero-minute route change.
    assert all(option.route_cost_delta is None for option in options)
    assert all(
        canonical_json(option.result_preview.days[1].model_dump(mode="json"))
        == canonical_json(base.days[1].model_dump(mode="json"))
        for option in options
    )
    for option in options:
        report = await audit_repository.get_report(option.postcheck_report_id)
        assert report is not None
        assert report.itinerary_revision == 2
        assert not any(
            finding.status == AuditStatus.VIOLATED and finding.reason_code == "TIME_CHAIN_BROKEN"
            for finding in report.findings
        )
        source_high = {
            (finding.rule_id, finding.reason_code, tuple(finding.affected_days))
            for finding in source_report.findings
            if finding.status == AuditStatus.VIOLATED
            and finding.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        }
        postcheck_high = {
            (finding.rule_id, finding.reason_code, tuple(finding.affected_days))
            for finding in report.findings
            if finding.status == AuditStatus.VIOLATED
            and finding.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        }
        assert postcheck_high <= source_high


@pytest.mark.asyncio
async def test_repair_refreshes_missing_route_before_full_postcheck():
    provider = ControlledRepairRouteProvider()
    _, audit_repository, _, search, _, source_report = await _repair_context(
        route_provider=provider,
    )

    options = await search.propose(
        source_report.report_id,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert len(options) == 2
    assert len(provider.calls) == 2
    for option in options:
        report = await audit_repository.get_report(option.postcheck_report_id)
        route_findings = [
            item for item in report.findings if item.rule_id == "audit.route_gap"
        ]
        assert route_findings
        assert all(item.status == AuditStatus.SATISFIED for item in route_findings)
        assert option.new_unknown_count == 0


@pytest.mark.asyncio
async def test_repair_reuses_fresh_unchanged_edge_without_provider_call():
    provider = ControlledRepairRouteProvider(fail=True)
    _, _, _, search, _, source_report = await _repair_context(
        route_provider=provider,
        source_route_minutes=15,
    )

    options = await search.propose(
        source_report.report_id,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert len(options) == 2
    assert provider.calls == []
    assert all(option.new_unknown_count == 0 for option in options)


@pytest.mark.asyncio
async def test_repair_provider_failure_keeps_route_unknown_and_rejects_candidate():
    provider = ControlledRepairRouteProvider(fail=True)
    _, _, _, search, _, source_report = await _repair_context(route_provider=provider)

    with pytest.raises(RepairNoFeasibleOptionError):
        await search.propose(
            source_report.report_id,
            now=datetime(2026, 8, 20, tzinfo=timezone.utc),
        )

    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_route_refresher_queries_changed_binding_only():
    provider = ControlledRepairRouteProvider()
    _, audit_repository, _, search, base, source_report = await _repair_context(
        route_provider=provider,
        source_route_minutes=15,
    )
    source_snapshot = await audit_repository.get_snapshot(source_report.evidence_snapshot_id)
    reversed_stops = [
        base.days[0].stops[1].model_copy(update={"order_index": 0}),
        base.days[0].stops[0].model_copy(update={"order_index": 1}),
    ]
    preview = base.model_copy(update={
        "revision": 2,
        "parent_revision": 1,
        "days": [
            base.days[0].model_copy(update={"stops": reversed_stops}),
            base.days[1],
        ],
    })

    observations, failures = await search.route_refresher.collect(
        base=base,
        preview=preview,
        source_snapshot=source_snapshot,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert failures == []
    assert len(provider.calls) == 1
    assert [item.subject_id for item in observations] == ["景山->故宫"]


@pytest.mark.asyncio
async def test_locked_stop_is_byte_equivalent_in_every_feasible_preview():
    _, _, _, search, base, source_report = await _repair_context(lock_first=True)
    options = await search.propose(source_report.report_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))

    assert len(options) == 1
    locked_before = base.days[0].stops[0]
    locked_after = options[0].result_preview.days[0].stops[0]
    assert canonical_json(locked_before.model_dump(mode="json")) == canonical_json(
        locked_after.model_dump(mode="json")
    )


@pytest.mark.asyncio
async def test_no_feasible_option_is_explicit_when_both_overlap_stops_are_locked():
    _, _, _, search, _, source_report = await _repair_context(lock_first=True, lock_second=True)

    with pytest.raises(RepairNoFeasibleOptionError) as captured:
        await search.propose(source_report.report_id, now=datetime(2026, 8, 20, tzinfo=timezone.utc))
    assert captured.value.code == "REPAIR_NO_FEASIBLE_OPTION"
    assert captured.value.context["unresolved_finding_ids"]


@pytest.mark.asyncio
async def test_apply_is_append_only_idempotent_and_advances_to_postcheck_report():
    itinerary_repository, audit_repository, repair_repository, search, base, source_report = await _repair_context()
    option = (await search.propose(source_report.report_id))[0]

    first = await repair_repository.apply_option(
        option.repair_id,
        actor_user_id="repair-user",
        if_match_revision=1,
        idempotency_key="repair-apply-1",
    )
    replay = await repair_repository.apply_option(
        option.repair_id,
        actor_user_id="repair-user",
        if_match_revision=1,
        idempotency_key="repair-apply-1",
    )

    assert first.new_revision == 2
    assert replay.idempotent_replay is True
    assert len(await itinerary_repository.list_revisions(base.workspace_id)) == 2
    assert (await itinerary_repository.get_revision(base.workspace_id, 1)).content_hash == base.content_hash
    workspace = await itinerary_repository.get_workspace(base.workspace_id)
    assert workspace.current_itinerary_revision == 2
    assert workspace.current_report_id == option.postcheck_report_id
    assert audit_repository.current_reports[base.workspace_id] == option.postcheck_report_id


@pytest.mark.asyncio
async def test_rejection_reason_is_recorded_without_creating_revision():
    itinerary_repository, _, repair_repository, search, base, source_report = await _repair_context()
    option = (await search.propose(source_report.report_id))[0]

    rejected = await repair_repository.reject_option(
        option.repair_id,
        actor_user_id="repair-user",
        reason="更愿意保留原时长，稍后手工调整",
    )

    assert rejected.status.value == "REJECTED"
    assert rejected.decision_reason == "更愿意保留原时长，稍后手工调整"
    assert rejected.decided_by == "repair-user"
    assert len(await itinerary_repository.list_revisions(base.workspace_id)) == 1
