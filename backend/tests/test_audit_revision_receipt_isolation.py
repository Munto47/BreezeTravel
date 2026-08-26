from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.engine import AuditEngine
from app.audit.evidence_service import EvidenceService
from app.audit.models import AuditRunInput, AuditStatus, EvidenceFreshness
from app.audit.repositories import InMemoryAuditRepository
from app.audit.repositories import PostgresAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.schemas.task_spec import DateRange, TripTaskSpec


NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
DATE_RANGE = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))


def _revision(number: int, parent: int | None, *, stop_id: str = "stop-poi") -> ItineraryRevision:
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="itinerary-receipt-isolation",
        workspace_id="workspace-receipt-isolation",
        revision=number,
        parent_revision=parent,
        source_type=RevisionSource.IMPORT if number == 1 else RevisionSource.MANUAL,
        city="北京",
        date_range=DATE_RANGE,
        days=[
            ItineraryDay(
                day_index=0,
                date=DATE_RANGE.start,
                stops=[ItineraryStop(
                    stop_id=stop_id,
                    place_id="poi-shared",
                    raw_name="同一个地点",
                    day_index=0,
                    order_index=0,
                    start_time="09:00",
                    end_time="10:00",
                )],
            ),
            ItineraryDay(day_index=1, date=DATE_RANGE.end, stops=[]),
        ],
        created_by="audit-user",
        created_at=NOW,
    ))


def _immutable_record(*, name: str, stop_id: str) -> dict:
    receipt = {
        "canonical_place_id": "poi-shared",
        "provider": "amap",
        "provider_place_id": "poi-shared",
        "name": name,
        "city": "北京",
        "district": "东城区",
        "address": "测试地址",
        "category": "attraction",
        "longitude": 116.4,
        "latitude": 39.9,
        "observed_at": NOW.isoformat(),
        "execution_mode": "fixture",
        "response_hash": sha256_canonical({"name": name}),
        "request_hash": sha256_canonical({"query": name}),
    }
    place_data = {
        "place_id": "poi-shared",
        "name": name,
        "city": "北京",
        "category": "attraction",
        "coords": {"lat": 39.9, "lng": 116.4},
        "provider": "amap",
        "retrieval_observed_at": NOW.isoformat(),
        "resolved_place_receipt": receipt,
    }
    return {
        "place_id": "poi-shared",
        "receipt_json": receipt,
        "place_data_json": place_data,
        "receipt_hash": sha256_canonical(receipt),
        "created_at": NOW,
        "stop_id": stop_id,
    }


def _repository(*, parent_of_three: int | None = 2) -> InMemoryAuditRepository:
    workspace = TripWorkspace(
        workspace_id="workspace-receipt-isolation",
        room_id="room-receipt-isolation",
        city="北京",
        trip_date_range=DATE_RANGE,
        current_itinerary_revision=3,
        created_by="audit-user",
    )
    return InMemoryAuditRepository(
        {workspace.workspace_id: workspace},
        place_records={workspace.workspace_id: {
            "poi-shared": {"place_id": "poi-shared", "name": "未来可变投影", "city": "杭州"},
        }},
        immutable_place_records={workspace.workspace_id: {
            1: {"stop-poi": _immutable_record(name="版本一", stop_id="stop-poi")},
            2: {"stop-poi": _immutable_record(name="版本二", stop_id="stop-poi")},
            3: {"stop-renamed": _immutable_record(name="版本三", stop_id="stop-renamed")},
        }},
        revision_parents={workspace.workspace_id: {1: None, 2: 1, 3: parent_of_three}},
        revision_stop_maps={workspace.workspace_id: {
            1: {"stop-poi": "poi-shared"},
            2: {"stop-poi": "poi-shared"},
            3: {"stop-renamed": "poi-shared"},
        }},
    )


async def _audit(
    repository: InMemoryAuditRepository,
    revision: ItineraryRevision,
) -> tuple[dict, object, object]:
    records = await repository.load_place_records(
        revision.workspace_id,
        ["poi-shared"],
        target_itinerary_revision=revision.revision,
    )
    evidence_service = EvidenceService()
    observations = evidence_service.observations_from_revision(
        revision,
        records,
        now=NOW,
        target_itinerary_revision=revision.revision,
    )
    snapshot = evidence_service.create_snapshot(
        workspace_id=revision.workspace_id,
        itinerary_revision=revision.revision,
        observations=observations,
        now=NOW,
    )
    task = TripTaskSpec(
        task_id="receipt-isolation-task",
        room_id="room-receipt-isolation",
        city="北京",
        date_range=DateRange(start=DATE_RANGE.start, days=2),
    )
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            task_id=task.task_id,
            task_revision=task.task_revision,
            place_resolution_versions={"poi-shared": 1},
        ),
        revision=revision,
        task_spec=task,
        evidence_snapshot=snapshot,
        now=NOW,
    )
    return records, snapshot, report


@pytest.mark.asyncio
async def test_each_audited_revision_reads_only_its_nearest_lineage_receipt():
    repository = _repository()

    revision_one = await _audit(repository, _revision(1, None))
    revision_two = await _audit(repository, _revision(2, 1))
    revision_three = await _audit(repository, _revision(3, 2, stop_id="stop-renamed"))

    for expected_revision, expected_name, result in (
        (1, "版本一", revision_one),
        (2, "版本二", revision_two),
        (3, "版本三", revision_three),
    ):
        records, snapshot, report = result
        assert records["poi-shared"]["name"] == expected_name
        assert records["poi-shared"]["receipt_itinerary_revision"] == expected_revision
        identity = next(fact for fact in snapshot.facts if fact.fact_type == "POI_IDENTITY")
        assert identity.value["name"] == expected_name
        city_finding = next(finding for finding in report.findings if finding.rule_id == "audit.place_city")
        assert city_finding.status == AuditStatus.SATISFIED

    # Omitting the new argument retains the old current-revision API behavior.
    current = await repository.load_place_records("workspace-receipt-isolation", ["poi-shared"])
    assert current["poi-shared"]["name"] == "版本三"


@pytest.mark.asyncio
async def test_receipt_from_non_ancestor_revision_is_never_visible():
    repository = _repository(parent_of_three=1)
    del repository.immutable_place_records["workspace-receipt-isolation"][3]

    records, snapshot, _ = await _audit(repository, _revision(3, 1, stop_id="stop-renamed"))

    assert records["poi-shared"]["name"] == "版本一"
    assert records["poi-shared"]["receipt_itinerary_revision"] == 1
    identity = next(fact for fact in snapshot.facts if fact.fact_type == "POI_IDENTITY")
    assert identity.value["name"] != "版本二"


@pytest.mark.asyncio
async def test_historical_revision_without_receipt_does_not_use_future_room_projection():
    repository = _repository()
    repository.immutable_place_records["workspace-receipt-isolation"][1] = {}

    records, snapshot, report = await _audit(repository, _revision(1, None))

    assert records == {}
    identity = next(fact for fact in snapshot.facts if fact.fact_type == "POI_IDENTITY")
    assert identity.freshness_status == EvidenceFreshness.UNAVAILABLE
    city_finding = next(finding for finding in report.findings if finding.rule_id == "audit.place_city")
    assert city_finding.status == AuditStatus.UNKNOWN
    assert city_finding.reason_code == "PLACE_CITY_UNKNOWN"


def test_observation_binding_rejects_records_for_another_revision():
    with pytest.raises(ValueError, match="target revision"):
        EvidenceService().observations_from_revision(
            _revision(1, None),
            {},
            now=NOW,
            target_itinerary_revision=2,
        )


@pytest.mark.asyncio
async def test_audit_application_passes_current_revision_as_explicit_receipt_bound():
    class TrackingAuditRepository(InMemoryAuditRepository):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.targets: list[int | None] = []

        async def load_place_records(
            self,
            workspace_id: str,
            place_ids: list[str],
            *,
            target_itinerary_revision: int | None = None,
        ) -> dict[str, dict]:
            self.targets.append(target_itinerary_revision)
            return await super().load_place_records(
                workspace_id,
                place_ids,
                target_itinerary_revision=target_itinerary_revision,
            )

    itinerary_repository = InMemoryItineraryRepository()
    revision = _revision(3, 2, stop_id="stop-renamed")
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="room-receipt-isolation",
        city="北京",
        trip_date_range=DATE_RANGE,
        current_itinerary_revision=3,
        created_by="audit-user",
    )
    itinerary_repository.workspaces[workspace.workspace_id] = workspace
    itinerary_repository.revisions[(workspace.workspace_id, 3)] = revision
    base = _repository()
    repository = TrackingAuditRepository(
        itinerary_repository.workspaces,
        place_records=base.place_records,
        immutable_place_records=base.immutable_place_records,
        revision_parents=base.revision_parents,
        revision_stop_maps=base.revision_stop_maps,
    )

    report = await AuditApplicationService(
        itinerary_repository=itinerary_repository,
        audit_repository=repository,
    ).run_current_audit(workspace.workspace_id, now=NOW)

    assert repository.targets == [3]
    snapshot = await repository.get_snapshot(report.evidence_snapshot_id)
    identity = next(fact for fact in snapshot.facts if fact.fact_type == "POI_IDENTITY")
    assert identity.value["name"] == "版本三"


@pytest.mark.asyncio
async def test_postgres_query_contract_uses_recursive_target_lineage_not_workspace_ceiling():
    receipt_row = _immutable_record(name="版本一", stop_id="stop-poi")
    receipt_row.update({
        "itinerary_revision": 1,
        "depth": 0,
    })

    class FakeConnection:
        def __init__(self):
            self.queries: list[tuple[str, tuple]] = []

        async def fetchrow(self, query, *args):
            self.queries.append((query, args))
            if "FROM trip_workspaces" in query:
                return {"current_itinerary_revision": 3}
            if "FROM itinerary_revisions" in query:
                return {"days_json": [
                    {"day_index": 0, "stops": [{"stop_id": "stop-poi", "place_id": "poi-shared"}]},
                    {"day_index": 1, "stops": []},
                ]}
            raise AssertionError(query)

        async def fetch(self, query, *args):
            self.queries.append((query, args))
            if "WITH RECURSIVE lineage" in query:
                return [receipt_row]
            raise AssertionError("room_places fallback must not run when an ancestor receipt exists")

    class Acquire:
        def __init__(self, connection):
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, *_):
            return False

    class FakePool:
        def __init__(self):
            self.connection = FakeConnection()

        def acquire(self):
            return Acquire(self.connection)

    pool = FakePool()
    records = await PostgresAuditRepository(pool).load_place_records(
        "workspace-receipt-isolation",
        ["poi-shared"],
        target_itinerary_revision=1,
    )

    assert records["poi-shared"]["name"] == "版本一"
    assert records["poi-shared"]["receipt_itinerary_revision"] == 1
    lineage_query, lineage_args = next(
        (query, args) for query, args in pool.connection.queries if "WITH RECURSIVE lineage" in query
    )
    assert "lineage.revision = ipr.itinerary_revision" in lineage_query
    assert lineage_args == ("workspace-receipt-isolation", ["poi-shared"], 1)
