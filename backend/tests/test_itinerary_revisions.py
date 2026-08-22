from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.audit.repositories import InMemoryAuditRepository
from app.audit.service import AuditApplicationService
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.errors import IdempotencyKeyReusedError, LockedCommitmentError, RevisionConflictError
from app.itineraries.hash_service import (
    compute_content_hash,
    compute_report_input_hash,
    sha256_canonical,
    with_content_hash,
)
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


DATE_RANGE = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))


def _stop(
    stop_id: str,
    place_id: str,
    day_index: int,
    order_index: int,
    *,
    locked: bool = False,
    fixed: bool = False,
) -> ItineraryStop:
    return ItineraryStop(
        stop_id=stop_id,
        place_id=place_id,
        day_index=day_index,
        order_index=order_index,
        start_time="09:00" if order_index == 0 else "13:00",
        end_time="11:00" if order_index == 0 else "15:00",
        visit_duration_minutes=120,
        raw_name=f"地点{place_id}",
        locked=locked,
        fixed_commitment=fixed,
    )


def _revision(*, locked: bool = False, current_report: str | None = "legacy-report-1"):
    content = ItineraryRevisionContent(
        itinerary_id="itin-1",
        workspace_id="workspace-1",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=DATE_RANGE,
        days=[
            ItineraryDay(
                day_index=0,
                date=date(2026, 9, 1),
                stops=[_stop("s1", "p1", 0, 0, locked=locked), _stop("s2", "p2", 0, 1)],
            ),
            ItineraryDay(day_index=1, date=date(2026, 9, 2), stops=[_stop("s3", "p3", 1, 0)]),
        ],
        locked_commitments=["s1"] if locked else [],
        change_summary={"kind": "initial"},
        created_by="user-1",
        created_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )
    revision = with_content_hash(content)
    workspace = TripWorkspace(
        workspace_id="workspace-1",
        room_id="room-1",
        city="北京",
        trip_date_range=DATE_RANGE,
        current_itinerary_revision=1,
        current_report_id=current_report,
        status=WorkspaceStatus.NEEDS_CONFIRMATION,
        created_by="user-1",
    )
    return workspace, revision


def _command(
    operation: EditOperation,
    payload: dict,
    *,
    base_revision: int = 1,
    command_id: str = "command-1",
) -> ItineraryEditCommand:
    return ItineraryEditCommand(
        command_id=command_id,
        workspace_id="workspace-1",
        base_revision=base_revision,
        actor_user_id="user-1",
        operation=operation,
        payload=payload,
    )


async def _repo_service(*, locked: bool = False):
    workspace, revision = _revision(locked=locked)
    repository = InMemoryItineraryRepository()
    await repository.create_workspace(workspace, revision)
    return repository, RevisionCommandService(repository)


class TestCanonicalHash:
    def test_json_key_order_does_not_change_hash(self):
        left = {"城市": "北京", "nested": {"b": 2, "a": 1}}
        right = {"nested": {"a": 1, "b": 2}, "城市": "北京"}
        assert sha256_canonical(left) == sha256_canonical(right)

    def test_display_only_fields_do_not_change_content_hash(self):
        _, revision = _revision()
        changed_stop = revision.days[0].stops[0].model_copy(update={"raw_name": "展示别名", "notes": "UI note"})
        changed_day = revision.days[0].model_copy(update={"stops": [changed_stop, revision.days[0].stops[1]]})
        changed = revision.model_copy(update={"days": [changed_day, revision.days[1]]})
        assert compute_content_hash(changed) == revision.content_hash

    def test_semantic_change_changes_content_hash(self):
        _, revision = _revision()
        changed_stop = revision.days[0].stops[0].model_copy(update={"place_id": "another-place"})
        changed_day = revision.days[0].model_copy(update={"stops": [changed_stop, revision.days[0].stops[1]]})
        changed = revision.model_copy(update={"days": [changed_day, revision.days[1]]})
        assert compute_content_hash(changed) != revision.content_hash

    def test_report_hash_sorts_revision_sets(self):
        common = {
            "workspace_id": "w1",
            "task_id": "t1",
            "task_revision": 2,
            "itinerary_id": "i1",
            "itinerary_revision": 3,
            "content_hash": "a" * 64,
            "evidence_snapshot_id": "snapshot-1",
            "audit_rule_set_version": "rules-1",
        }
        first = compute_report_input_hash(
            **common,
            member_constraint_revisions=[("m2", 3), ("m1", 2)],
            place_resolution_versions=[("p2", 4), ("p1", 1)],
        )
        second = compute_report_input_hash(
            **common,
            member_constraint_revisions={"m1": 2, "m2": 3},
            place_resolution_versions={"p1": 1, "p2": 4},
        )
        assert first == second


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "payload", "expected_days"),
    [
        (
            EditOperation.ADD_STOP,
            {"stop": _stop("s4", "p4", 1, 1).model_dump(mode="json")},
            [1],
        ),
        (EditOperation.REORDER_STOP, {"stop_id": "s2", "target_day_index": 0, "target_order_index": 0}, [0]),
        (EditOperation.MOVE_TO_DAY, {"stop_id": "s2", "target_day_index": 1, "target_order_index": 1}, [0, 1]),
        (EditOperation.ADJUST_TIME, {"stop_id": "s2", "start_time": "14:00", "end_time": "16:00"}, [0]),
        (EditOperation.REPLACE_STOP, {"stop_id": "s2", "new_place_id": "p-new"}, [0]),
        (EditOperation.REMOVE_STOP, {"stop_id": "s2"}, [0]),
        (EditOperation.LOCK_STOP, {"stop_id": "s2"}, [0]),
    ],
)
async def test_each_edit_creates_append_only_revision(operation, payload, expected_days):
    repository, service = await _repo_service()
    result = await service.apply(
        _command(operation, payload),
        if_match_revision=1,
        idempotency_key=f"key-{operation.value}",
    )
    assert result.accepted is True
    assert result.new_revision == 2
    assert result.changed_days == expected_days
    assert await repository.get_revision("workspace-1", 1) is not None
    assert await repository.get_revision("workspace-1", 2) is not None
    workspace = await repository.get_workspace("workspace-1")
    assert workspace.current_itinerary_revision == 2
    assert workspace.current_report_id is None


@pytest.mark.asyncio
async def test_reorder_changes_order_without_losing_stop():
    repository, service = await _repo_service()
    await service.apply(
        _command(EditOperation.REORDER_STOP, {"stop_id": "s2", "target_order_index": 0}),
        if_match_revision=1,
        idempotency_key="reorder-1",
    )
    revision = await repository.get_revision("workspace-1", 2)
    assert [stop.stop_id for stop in revision.days[0].stops] == ["s2", "s1"]
    assert [stop.order_index for stop in revision.days[0].stops] == [0, 1]


@pytest.mark.asyncio
async def test_same_idempotency_request_is_replayed_once():
    repository, service = await _repo_service()
    command = _command(EditOperation.REMOVE_STOP, {"stop_id": "s2"})
    first = await service.apply(command, if_match_revision=1, idempotency_key="same-key")
    second = await service.apply(command, if_match_revision=1, idempotency_key="same-key")
    assert first.new_revision == second.new_revision == 2
    assert second.idempotent_replay is True
    assert len(await repository.list_revisions("workspace-1")) == 2


@pytest.mark.asyncio
async def test_same_idempotency_key_with_different_body_is_rejected():
    _, service = await _repo_service()
    await service.apply(
        _command(EditOperation.REMOVE_STOP, {"stop_id": "s2"}),
        if_match_revision=1,
        idempotency_key="reused-key",
    )
    with pytest.raises(IdempotencyKeyReusedError):
        await service.apply(
            _command(EditOperation.LOCK_STOP, {"stop_id": "s2"}, command_id="command-2"),
            if_match_revision=1,
            idempotency_key="reused-key",
        )


@pytest.mark.asyncio
async def test_stale_base_revision_is_rejected_without_overwrite():
    repository, service = await _repo_service()
    await service.apply(
        _command(EditOperation.REMOVE_STOP, {"stop_id": "s2"}),
        if_match_revision=1,
        idempotency_key="first-key",
    )
    with pytest.raises(RevisionConflictError) as captured:
        await service.apply(
            _command(EditOperation.LOCK_STOP, {"stop_id": "s3"}, command_id="command-2"),
            if_match_revision=1,
            idempotency_key="stale-key",
        )
    assert captured.value.context == {"expected_revision": 1, "actual_revision": 2}
    assert len(await repository.list_revisions("workspace-1")) == 2


@pytest.mark.asyncio
async def test_locked_stop_cannot_be_moved_or_removed():
    repository, service = await _repo_service(locked=True)
    with pytest.raises(LockedCommitmentError):
        await service.apply(
            _command(EditOperation.REMOVE_STOP, {"stop_id": "s1"}),
            if_match_revision=1,
            idempotency_key="locked-remove",
        )
    assert len(await repository.list_revisions("workspace-1")) == 1


@pytest.mark.asyncio
async def test_confirm_creates_revision_without_faking_semantic_change():
    repository, _ = await _repo_service()
    audits = InMemoryAuditRepository(repository.workspaces)
    await AuditApplicationService(
        itinerary_repository=repository,
        audit_repository=audits,
    ).run_current_audit("workspace-1", now=datetime(2026, 8, 20, tzinfo=timezone.utc))
    service = RevisionCommandService(repository, audit_repository=audits)
    result = await service.apply(
        _command(EditOperation.CONFIRM, {}),
        if_match_revision=1,
        idempotency_key="confirm-1",
    )
    original = await repository.get_revision("workspace-1", 1)
    confirmed = await repository.get_revision("workspace-1", result.new_revision)
    workspace = await repository.get_workspace("workspace-1")
    assert confirmed.content_hash == original.content_hash
    assert workspace.status == WorkspaceStatus.CONFIRMED


@pytest.mark.asyncio
async def test_undo_creates_new_revision_from_older_content():
    repository, service = await _repo_service()
    await service.apply(
        _command(EditOperation.REMOVE_STOP, {"stop_id": "s2"}),
        if_match_revision=1,
        idempotency_key="remove-before-undo",
    )
    result = await service.apply(
        _command(EditOperation.UNDO, {"target_revision": 1}, base_revision=2, command_id="undo-command"),
        if_match_revision=2,
        idempotency_key="undo-1",
    )
    restored = await repository.get_revision("workspace-1", result.new_revision)
    assert result.new_revision == 3
    assert [stop.stop_id for stop in restored.days[0].stops] == ["s1", "s2"]
    assert restored.parent_revision == 2
