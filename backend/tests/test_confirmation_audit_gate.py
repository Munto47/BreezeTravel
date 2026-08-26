from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.errors import CurrentAuditRequiredError
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
    WorkspaceStatus,
)
from app.itineraries.repositories import InMemoryItineraryRepository


NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)
DATE_RANGE = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))


async def _seed():
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="confirm-itinerary",
        workspace_id="confirm-workspace",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=DATE_RANGE,
        days=[
            ItineraryDay(day_index=0, date=DATE_RANGE.start, stops=[ItineraryStop(
                stop_id="confirm-stop", place_id="confirm-place", day_index=0, order_index=0,
                start_time="09:00", end_time="11:00",
            )]),
            ItineraryDay(day_index=1, date=DATE_RANGE.end, stops=[]),
        ],
        created_by="confirm-user",
        created_at=NOW,
    ))
    workspace = TripWorkspace(
        workspace_id="confirm-workspace",
        room_id="confirm-room",
        city="北京",
        trip_date_range=DATE_RANGE,
        current_itinerary_revision=1,
        status=WorkspaceStatus.DRAFT,
        created_by="confirm-user",
    )
    itineraries = InMemoryItineraryRepository()
    await itineraries.create_workspace(workspace, revision)
    audits = InMemoryAuditRepository(itineraries.workspaces)
    return itineraries, audits


def _confirm(*, base_revision: int = 1, command_id: str = "confirm-command"):
    return ItineraryEditCommand(
        command_id=command_id,
        workspace_id="confirm-workspace",
        base_revision=base_revision,
        actor_user_id="confirm-user",
        operation=EditOperation.CONFIRM,
    )


@pytest.mark.asyncio
async def test_confirmation_fails_closed_without_a_persisted_full_audit():
    itineraries, audits = await _seed()
    service = RevisionCommandService(itineraries, audit_repository=audits)

    with pytest.raises(CurrentAuditRequiredError) as captured:
        await service.apply(_confirm(), if_match_revision=1, idempotency_key="missing-audit")

    assert captured.value.context["reason"] == "CURRENT_REPORT_MISSING"
    assert (await itineraries.get_workspace("confirm-workspace")).current_itinerary_revision == 1
    assert len(await itineraries.list_revisions("confirm-workspace")) == 1


@pytest.mark.asyncio
async def test_confirmation_accepts_only_hash_valid_current_full_audit_and_replays():
    itineraries, audits = await _seed()
    report = await AuditApplicationService(
        itinerary_repository=itineraries,
        audit_repository=audits,
    ).run_current_audit("confirm-workspace", now=NOW)
    service = RevisionCommandService(itineraries, audit_repository=audits)

    result = await service.apply(_confirm(), if_match_revision=1, idempotency_key="valid-audit")
    replay = await service.apply(_confirm(), if_match_revision=1, idempotency_key="valid-audit")
    workspace = await itineraries.get_workspace("confirm-workspace")

    assert report.itinerary_revision == 1
    assert result.new_revision == 2
    assert replay.idempotent_replay is True
    assert workspace.status == WorkspaceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_confirmation_rejects_tampered_report_hash_without_writing_revision():
    itineraries, audits = await _seed()
    report = await AuditApplicationService(
        itinerary_repository=itineraries,
        audit_repository=audits,
    ).run_current_audit("confirm-workspace", now=NOW)
    audits.reports[report.report_id] = report.model_copy(update={"report_input_hash": "f" * 64})
    service = RevisionCommandService(itineraries, audit_repository=audits)

    with pytest.raises(CurrentAuditRequiredError) as captured:
        await service.apply(_confirm(), if_match_revision=1, idempotency_key="tampered-hash")

    assert captured.value.context["reason"] == "REPORT_INPUT_HASH_MISMATCH"
    assert len(await itineraries.list_revisions("confirm-workspace")) == 1


@pytest.mark.asyncio
async def test_confirmation_rejects_report_bound_to_an_old_revision():
    itineraries, audits = await _seed()
    report = await AuditApplicationService(
        itinerary_repository=itineraries,
        audit_repository=audits,
    ).run_current_audit("confirm-workspace", now=NOW)
    edits = RevisionCommandService(itineraries)
    await edits.apply(
        ItineraryEditCommand(
            command_id="change-before-confirm",
            workspace_id="confirm-workspace",
            base_revision=1,
            actor_user_id="confirm-user",
            operation=EditOperation.LOCK_STOP,
            payload={"stop_id": "confirm-stop"},
        ),
        if_match_revision=1,
        idempotency_key="change-before-confirm",
    )
    workspace = await itineraries.get_workspace("confirm-workspace")
    # Simulate a corrupt/stale pointer.  The confirmation check must not infer
    # freshness merely because a report id exists.
    itineraries.workspaces[workspace.workspace_id] = workspace.model_copy(update={"current_report_id": report.report_id})
    service = RevisionCommandService(itineraries, audit_repository=audits)

    with pytest.raises(CurrentAuditRequiredError) as captured:
        await service.apply(_confirm(base_revision=2), if_match_revision=2, idempotency_key="stale-report")

    assert captured.value.context["reason"] == "REPORT_REVISION_STALE"
    assert (await itineraries.get_workspace("confirm-workspace")).current_itinerary_revision == 2
