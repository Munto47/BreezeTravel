from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.audit.engine import AuditEngine
from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import (
    AuditRunInput,
    AuditSeverity,
    AuditStatus,
    EvidenceFreshness,
    ProviderFailure,
)
from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.constraints.verifier import ItineraryVerifier
from app.itineraries.adapters import legacy_to_revision
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    ResolutionStatus,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.schemas.itinerary import DayPlan, Itinerary, TimeSlot, TransportLeg, WeatherInfo
from app.schemas.task_spec import DateRange, HardConstraint, TripTaskSpec


NOW = datetime(2026, 8, 20, 8, tzinfo=timezone.utc)
DATE_RANGE = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))


def _revision(*, ambiguous: bool = False):
    stops_day_0 = [
        ItineraryStop(
            stop_id="s1",
            place_id="p1",
            day_index=0,
            order_index=0,
            start_time="09:00",
            end_time="11:00",
            raw_name="景点一",
            category="attraction",
            resolution_status=ResolutionStatus.AMBIGUOUS if ambiguous else ResolutionStatus.USER_CONFIRMED,
        ),
        ItineraryStop(
            stop_id="s2",
            place_id="food1",
            day_index=0,
            order_index=1,
            start_time="12:00",
            end_time="13:00",
            raw_name="餐厅一",
            category="food",
        ),
    ]
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-audit",
        workspace_id="workspace-audit",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=DATE_RANGE,
        days=[
            ItineraryDay(day_index=0, date=DATE_RANGE.start, stops=stops_day_0),
            ItineraryDay(day_index=1, date=DATE_RANGE.end, stops=[]),
        ],
        created_by="user-audit",
        created_at=NOW,
    ))


def _task(*constraints: HardConstraint) -> TripTaskSpec:
    return TripTaskSpec(
        task_id="task-audit",
        room_id="room-audit",
        task_revision=1,
        city="北京",
        date_range=DateRange(start=DATE_RANGE.start, days=2),
        hard_constraints=list(constraints),
    )


def _run(revision, task, snapshot):
    return AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id=task.task_id,
            task_revision=task.task_revision,
            place_resolution_versions={stop.place_id: 1 for day in revision.days for stop in day.stops},
        ),
        revision=revision,
        task_spec=task,
        evidence_snapshot=snapshot,
        now=NOW,
    )


class TestEvidenceService:
    def test_fresh_stale_unavailable_and_conflicting_are_distinct(self):
        service = EvidenceService()
        observations = [
            EvidenceObservation(
                subject_type="PLACE", subject_id="fresh", fact_type="OPENING_HOURS",
                value="09:00-18:00", provider="amap", observed_at=NOW,
            ),
            EvidenceObservation(
                subject_type="PLACE", subject_id="stale", fact_type="OPENING_HOURS",
                value="09:00-18:00", provider="amap", observed_at=NOW - timedelta(days=5),
            ),
            EvidenceObservation(
                subject_type="PLACE", subject_id="missing", fact_type="OPENING_HOURS",
                provider="amap", confidence=0, freshness_status=EvidenceFreshness.UNAVAILABLE,
            ),
            EvidenceObservation(
                subject_type="PLACE", subject_id="conflict", fact_type="OPENING_HOURS",
                value="09:00-18:00", provider="official", observed_at=NOW,
            ),
            EvidenceObservation(
                subject_type="PLACE", subject_id="conflict", fact_type="OPENING_HOURS",
                value="closed", provider="amap", observed_at=NOW,
            ),
        ]
        snapshot = service.create_snapshot(
            workspace_id="w", itinerary_revision=1, observations=observations, now=NOW,
        )
        status_by_subject = {}
        for fact in snapshot.facts:
            status_by_subject.setdefault(fact.subject_id, set()).add(fact.freshness_status)
        assert status_by_subject["fresh"] == {EvidenceFreshness.FRESH}
        assert status_by_subject["stale"] == {EvidenceFreshness.STALE}
        assert status_by_subject["missing"] == {EvidenceFreshness.UNAVAILABLE}
        assert status_by_subject["conflict"] == {EvidenceFreshness.CONFLICTING}


def test_unknown_opening_never_becomes_satisfied_and_keeps_evidence_reference():
    revision = _revision()
    service = EvidenceService()
    observations = service.observations_from_revision(revision, {}, now=NOW)
    snapshot = service.create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=observations,
        now=NOW,
    )
    report = _run(revision, _task(), snapshot)
    opening_findings = [item for item in report.findings if item.rule_id == "constraint.opening_hours"]
    assert opening_findings
    assert all(item.status == AuditStatus.UNKNOWN for item in opening_findings)
    assert all(item.severity == AuditSeverity.HIGH for item in opening_findings)
    assert all(item.evidence_fact_ids for item in opening_findings)
    assert report.overall_status != AuditStatus.SATISFIED


def test_ambiguous_place_is_blocker_even_when_other_rules_can_run():
    revision = _revision(ambiguous=True)
    service = EvidenceService()
    snapshot = service.create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=service.observations_from_revision(revision, {}, now=NOW),
        now=NOW,
    )
    report = _run(revision, _task(), snapshot)
    blocker = next(item for item in report.findings if item.reason_code == "PLACE_NOT_RESOLVED")
    assert blocker.status == AuditStatus.VIOLATED
    assert blocker.severity == AuditSeverity.BLOCKER
    assert blocker.affected_stop_ids == ["s1"]


def test_missing_end_time_remains_unknown_instead_of_becoming_false_overlap():
    base = _revision()
    content = ItineraryRevisionContent.model_validate(base.model_dump(exclude={"content_hash"}))
    content = content.model_copy(update={
        "days": [
            base.days[0].model_copy(update={
                "stops": [
                    base.days[0].stops[0].model_copy(update={"end_time": None}),
                ],
            }),
            base.days[1],
        ],
    })
    revision = with_content_hash(content)
    snapshot = EvidenceService().create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=EvidenceService().observations_from_revision(revision, {}, now=NOW),
        now=NOW,
    )
    report = _run(revision, _task(), snapshot)
    day_zero_time = next(
        item for item in report.findings
        if item.rule_id == "constraint.time_chain" and item.affected_days == [0]
    )
    assert day_zero_time.status == AuditStatus.UNKNOWN
    assert day_zero_time.reason_code == "TIME_DATA_INVALID"
    assert day_zero_time.repairable is False
    assert day_zero_time.confirmation_action == "请补充该日程的开始和结束时间后重新审计"
    assert not any(
        item.reason_code == "TIME_CHAIN_BROKEN" and item.affected_days == [0]
        for item in report.findings
    )


@pytest.mark.asyncio
async def test_partial_provider_failure_still_persists_degraded_report():
    revision = _revision()
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="room-audit",
        city="北京",
        trip_date_range=DATE_RANGE,
        current_itinerary_revision=1,
        created_by="user-audit",
    )
    itinerary_repository = InMemoryItineraryRepository()
    await itinerary_repository.create_workspace(workspace, revision)
    audit_repository = InMemoryAuditRepository()
    audit_repository.current_revisions[workspace.workspace_id] = 1
    audit_repository.task_specs[workspace.workspace_id] = _task()

    report = await AuditApplicationService(
        itinerary_repository=itinerary_repository,
        audit_repository=audit_repository,
    ).run_current_audit(
        workspace.workspace_id,
        provider_failures=[ProviderFailure(provider="amap", error_category="timeout", retryable=True)],
        now=NOW,
    )
    snapshot = await audit_repository.get_snapshot(report.evidence_snapshot_id)
    assert report.findings
    assert snapshot.provider_failures[0].error_category == "timeout"
    assert any(item.status == AuditStatus.UNKNOWN for item in report.findings)


def _legacy_itinerary() -> Itinerary:
    place_a = {"place_id": "p1", "name": "景点", "category": "attraction", "opening_hours": "08:00-18:00"}
    place_food = {"place_id": "p2", "name": "午餐", "category": "food", "opening_hours": "10:00-20:00"}
    return Itinerary(
        itinerary_id="legacy-parity",
        thread_id="room-audit",
        city="北京",
        days=[
            DayPlan(
                day_index=0,
                date="2026-09-01",
                cluster_id=0,
                slots=[
                    TimeSlot(
                        place_id="p1", place=place_a, start_time="09:00", end_time="11:00",
                        transport=TransportLeg(mode="driving", duration_mins=20, distance_km=8),
                    ),
                    TimeSlot(place_id="p2", place=place_food, start_time="12:00", end_time="13:00"),
                ],
                weather_summary=WeatherInfo(condition="晴", temp_high=28, temp_low=20, suggestion="适合出行"),
            ),
            DayPlan(day_index=1, date="2026-09-02", cluster_id=1, slots=[]),
        ],
        generated_at=NOW.isoformat(),
        version=1,
    )


def test_audit_adapter_preserves_legacy_rule_status_and_reason_parity():
    legacy = _legacy_itinerary()
    task = _task(HardConstraint(id="travel", type="max_daily_travel_minutes", value=60))
    legacy_report = ItineraryVerifier().verify(task, legacy, places=[])
    revision = legacy_to_revision(
        legacy,
        workspace_id="workspace-audit",
        date_range=DATE_RANGE,
        created_by="user-audit",
    )
    observations: list[EvidenceObservation] = []
    for day in legacy.days:
        for index, slot in enumerate(day.slots):
            observations.extend([
                EvidenceObservation(
                    subject_type="PLACE", subject_id=slot.place_id, fact_type="POI_IDENTITY",
                    value=slot.place, provider="amap", observed_at=NOW,
                ),
                EvidenceObservation(
                    subject_type="PLACE", subject_id=slot.place_id, fact_type="OPENING_HOURS",
                    value=slot.place.get("opening_hours"), provider="amap", observed_at=NOW,
                ),
            ])
            if slot.transport and index < len(day.slots) - 1:
                left = revision.days[day.day_index].stops[index]
                right = revision.days[day.day_index].stops[index + 1]
                observations.append(EvidenceObservation(
                    subject_type="ROUTE_EDGE",
                    subject_id=f"{left.stop_id}->{right.stop_id}",
                    fact_type="ROUTE_TIME",
                    value={
                        "mode": slot.transport.mode,
                        "duration_minutes": slot.transport.duration_mins,
                        "distance_km": slot.transport.distance_km,
                    },
                    provider="amap_route",
                    observed_at=NOW,
                ))
        if day.weather_summary:
            observations.append(EvidenceObservation(
                subject_type="DAY",
                subject_id=str(day.day_index),
                fact_type="WEATHER",
                value=day.weather_summary.model_dump(),
                provider="weather",
                observed_at=NOW,
            ))
    snapshot = EvidenceService().create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=observations,
        now=NOW,
    )
    audit_report = _run(revision, task, snapshot)
    legacy_pairs = sorted((check.reason_code, check.status.value) for check in legacy_report.checks)
    audit_pairs = sorted(
        (finding.reason_code, finding.status.value)
        for finding in audit_report.findings
        if finding.rule_id.startswith("constraint.")
    )
    assert audit_pairs == legacy_pairs


def test_report_hash_changes_when_evidence_snapshot_changes():
    revision = _revision()
    service = EvidenceService()
    observations = service.observations_from_revision(revision, {}, now=NOW)
    first = service.create_snapshot(
        workspace_id=revision.workspace_id, itinerary_revision=1, observations=observations, now=NOW,
    )
    second = service.create_snapshot(
        workspace_id=revision.workspace_id, itinerary_revision=1, observations=observations, now=NOW,
    )
    assert _run(revision, _task(), first).report_input_hash != _run(revision, _task(), second).report_input_hash
