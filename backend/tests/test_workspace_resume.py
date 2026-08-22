from __future__ import annotations

from datetime import date, datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_workspaces as workspace_api
from app.audit.models import AuditReport, AuditStatus, EvidenceSnapshot
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
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
from app.itineraries.resume_models import TipsState, WorkspaceResume, WorkspaceWriteETags
from app.itineraries.resume_repository import (
    WorkspaceResumeNotFound,
    WorkspaceStateInconsistent,
)
from app.repairs.models import RepairOperation, RepairOperationType, RepairOption, RepairStatus
from app.utils.auth import get_current_user


NOW = datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc)


def _revision(revision: int = 1, *, source_type: RevisionSource = RevisionSource.IMPORT) -> ItineraryRevision:
    date_range = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="resume-itinerary",
        workspace_id="resume-workspace",
        revision=revision,
        parent_revision=revision - 1 if revision > 1 else None,
        source_type=source_type,
        city="北京",
        date_range=date_range,
        days=[
            ItineraryDay(
                day_index=0,
                date=date_range.start,
                stops=[ItineraryStop(
                    stop_id="resume-stop",
                    place_id="resume-place",
                    day_index=0,
                    order_index=0,
                    start_time="09:00",
                    end_time="11:00",
                )],
            ),
            ItineraryDay(day_index=1, date=date_range.end, stops=[]),
        ],
        created_by="resume-user",
        created_at=NOW,
    ))


def _report(revision: ItineraryRevision, report_id: str) -> AuditReport:
    return AuditReport(
        report_id=report_id,
        workspace_id=revision.workspace_id,
        itinerary_id=revision.itinerary_id,
        itinerary_revision=revision.revision,
        task_id="resume-task",
        task_revision=1,
        evidence_snapshot_id=f"{report_id}-snapshot",
        audit_rule_set_version="rules-v1",
        report_input_hash=("a" if revision.revision == 1 else "b") * 64,
        overall_status=AuditStatus.SATISFIED,
        created_at=NOW,
    )


def _repair(
    *,
    source_report_id: str,
    postcheck_report_id: str,
    preview: ItineraryRevision,
    status: RepairStatus,
) -> RepairOption:
    return RepairOption(
        repair_id=f"repair-{status.value.lower()}",
        source_report_id=source_report_id,
        base_itinerary_revision=preview.revision - 1,
        operations=[RepairOperation(
            operation=RepairOperationType.ADJUST_TIME,
            payload={"stop_id": "resume-stop", "start_time": "09:30"},
            rationale="avoid overlap",
        )],
        targeted_finding_ids=["finding-1"],
        edit_cost=1,
        risk_cost=0,
        new_unknown_count=0,
        result_preview=preview,
        postcheck_report_id=postcheck_report_id,
        status=status,
        decided_by="resume-user" if status == RepairStatus.APPLIED else None,
        decided_at=NOW if status == RepairStatus.APPLIED else None,
        created_at=NOW,
    )


def _resume(*, applied: bool = False) -> WorkspaceResume:
    revision = _revision(2, source_type=RevisionSource.REPAIR) if applied else _revision()
    report = _report(revision, "postcheck-report" if applied else "current-report")
    itinerary_import = ItineraryImport(
        import_id="current-import",
        workspace_id=revision.workspace_id,
        source_type=ImportSourceType.AI_TEXT,
        raw_text="第1天：故宫",
        parse_version="parser-v1",
        status=ImportStatus.APPLIED if applied else ImportStatus.READY,
        state_version=3,
        applied_revision=1 if applied else None,
        created_by="resume-user",
        created_at=NOW,
        updated_at=NOW,
    )
    workspace = TripWorkspace(
        workspace_id=revision.workspace_id,
        room_id="resume-room",
        city="北京",
        trip_date_range=revision.date_range,
        current_itinerary_revision=revision.revision,
        current_report_id=report.report_id,
        current_import_id=itinerary_import.import_id,
        created_by="resume-user",
        created_at=NOW,
        updated_at=NOW,
    )
    applied_repair = (
        _repair(
            source_report_id="source-report",
            postcheck_report_id=report.report_id,
            preview=revision,
            status=RepairStatus.APPLIED,
        )
        if applied
        else None
    )
    return WorkspaceResume(
        workspace=workspace,
        current_revision=revision,
        current_import=itinerary_import,
        current_report=report,
        current_evidence=EvidenceSnapshot(
            snapshot_id=report.evidence_snapshot_id,
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            policy_version="policy-v1",
            created_at=NOW,
        ),
        applied_repair=applied_repair,
        tips_state=TipsState.NOT_GENERATED,
        write_etags=WorkspaceWriteETags(
            itinerary=f'"{revision.revision}"',
            import_=f'"{itinerary_import.state_version}"',
        ),
    )


class FakeResumeRepository:
    def __init__(self, resume: WorkspaceResume):
        self.resume = resume
        self.mode = "ok"
        self.calls: list[tuple[str, str]] = []

    async def get_resume(self, workspace_id: str, user_id: str) -> WorkspaceResume:
        self.calls.append((workspace_id, user_id))
        if self.mode in {"missing", "denied"}:
            raise WorkspaceResumeNotFound
        if self.mode == "inconsistent":
            raise WorkspaceStateInconsistent("workspace pointers are inconsistent")
        return self.resume


def _client() -> tuple[TestClient, FakeResumeRepository]:
    repository = FakeResumeRepository(_resume())
    app = FastAPI()
    app.include_router(workspace_api.router, prefix="/api")
    app.dependency_overrides[workspace_api.get_workspace_resume_repository] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: "resume-user"
    return TestClient(app), repository


def test_workspace_id_alone_recovers_current_p1_p4_state():
    client, repository = _client()

    response = client.get("/api/trip-workspaces/resume-workspace/resume")

    assert response.status_code == 200
    assert repository.calls == [("resume-workspace", "resume-user")]
    body = response.json()
    assert body["workspace"]["workspace_id"] == "resume-workspace"
    assert body["current_revision"]["revision"] == 1
    assert body["current_import"]["import_id"] == "current-import"
    assert body["current_report"]["report_id"] == "current-report"
    assert body["current_evidence"]["snapshot_id"] == "current-report-snapshot"
    assert body["proposed_repairs"] == []
    assert body["applied_repair"] is None
    assert body["current_tips"] is None
    assert body["tips_state"] == "NOT_GENERATED"
    assert body["write_etags"] == {"itinerary": '"1"', "import": '"3"'}
    assert response.headers["cache-control"] == "private, no-cache"


def test_resume_strong_etag_covers_child_state_and_supports_conditional_get():
    client, repository = _client()
    first = client.get("/api/trip-workspaces/resume-workspace/resume")
    first_etag = first.headers["etag"]

    unchanged = client.get(
        "/api/trip-workspaces/resume-workspace/resume",
        headers={"If-None-Match": f'W/{first_etag}, "other"'},
    )
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == first_etag

    imported = repository.resume.current_import
    assert imported is not None
    repository.resume = repository.resume.model_copy(update={
        "current_import": imported.model_copy(update={"state_version": 4}),
        "write_etags": WorkspaceWriteETags(itinerary='"1"', import_='"4"'),
    })
    changed = client.get(
        "/api/trip-workspaces/resume-workspace/resume",
        headers={"If-None-Match": first_etag},
    )
    assert changed.status_code == 200
    assert changed.headers["etag"] != first_etag
    assert changed.json()["write_etags"]["import"] == '"4"'


def test_resume_exposes_applied_repair_through_postcheck_lineage():
    client, repository = _client()
    repository.resume = _resume(applied=True)

    body = client.get("/api/trip-workspaces/resume-workspace/resume").json()

    assert body["current_revision"]["revision"] == 2
    assert body["current_report"]["report_id"] == "postcheck-report"
    assert body["proposed_repairs"] == []
    assert body["applied_repair"]["source_report_id"] == "source-report"
    assert body["applied_repair"]["postcheck_report_id"] == "postcheck-report"
    assert body["applied_repair"]["result_preview"]["revision"] == 2


def test_missing_and_unauthorized_workspace_share_the_same_404_contract():
    client, repository = _client()
    responses = []
    for mode in ("missing", "denied"):
        repository.mode = mode
        responses.append(client.get("/api/trip-workspaces/resume-workspace/resume"))

    assert [response.status_code for response in responses] == [404, 404]
    assert responses[0].json() == responses[1].json() == {
        "detail": {"code": "RESOURCE_NOT_FOUND", "message": "workspace does not exist"}
    }


def test_inconsistent_workspace_uses_stable_409_code():
    client, repository = _client()
    repository.mode = "inconsistent"

    response = client.get("/api/trip-workspaces/resume-workspace/resume")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "WORKSPACE_STATE_INCONSISTENT"
