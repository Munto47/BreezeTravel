from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.audit.repositories import AuditRepository
from app.itineraries.errors import CurrentAuditRequiredError, InvalidEditCommandError, LockedCommitmentError
from app.itineraries.hash_service import compute_command_request_hash, with_content_hash
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryPatchResult,
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    ResolutionStatus,
    RevisionSource,
    TripWorkspace,
    WorkspaceStatus,
)
from app.itineraries.repositories import ItineraryRepository


def _day_at(days: list[ItineraryDay], day_index: int) -> ItineraryDay:
    try:
        day = days[day_index]
    except (IndexError, TypeError):
        raise InvalidEditCommandError("target day does not exist", context={"day_index": day_index}) from None
    if day.day_index != day_index:
        raise InvalidEditCommandError("itinerary day indices are inconsistent")
    return day


def _find_stop(days: Iterable[ItineraryDay], stop_id: str) -> tuple[int, int, ItineraryStop]:
    for day in days:
        for index, stop in enumerate(day.stops):
            if stop.stop_id == stop_id:
                return day.day_index, index, stop
    raise InvalidEditCommandError("target stop does not exist", context={"stop_id": stop_id})


def _assert_not_locked(stop: ItineraryStop, operation: EditOperation) -> None:
    if stop.locked or stop.fixed_commitment:
        raise LockedCommitmentError(
            "operation would change a locked or fixed commitment",
            context={"stop_id": stop.stop_id, "operation": operation.value},
        )


def _normalize_day(day: ItineraryDay, stops: list[ItineraryStop]) -> ItineraryDay:
    normalized = [
        stop.model_copy(update={"day_index": day.day_index, "order_index": index})
        for index, stop in enumerate(stops)
    ]
    return day.model_copy(update={"stops": normalized})


def _replace_day(days: list[ItineraryDay], day_index: int, stops: list[ItineraryStop]) -> None:
    days[day_index] = _normalize_day(_day_at(days, day_index), stops)


def _route_edge_cache(days: Iterable[ItineraryDay]) -> dict[tuple[str, str, str, str], Any]:
    """Return only route evidence whose endpoints are still explicit.

    ``transport_to_next`` belongs to an edge, not to the left stop in
    isolation.  Indexing it by both endpoints prevents a reorder from silently
    attaching an old duration to a newly-created edge.
    """
    return {
        (left.stop_id, left.place_id, right.stop_id, right.place_id): left.transport_to_next
        for day in days
        for left, right in zip(day.stops, day.stops[1:])
        if left.transport_to_next is not None
    }


def _restore_unchanged_route_edges(
    days: list[ItineraryDay],
    *,
    changed_days: Iterable[int],
    cache: dict[tuple[str, str, str, str], Any],
) -> None:
    """Keep cached evidence for unchanged edges and invalidate new edges."""
    for day_index in changed_days:
        day = _day_at(days, day_index)
        updated: list[ItineraryStop] = []
        for index, stop in enumerate(day.stops):
            transport = None
            if index < len(day.stops) - 1:
                right = day.stops[index + 1]
                transport = cache.get((stop.stop_id, stop.place_id, right.stop_id, right.place_id))
            updated.append(stop.model_copy(update={"transport_to_next": transport}))
        _replace_day(days, day_index, updated)


def _payload_int(payload: dict[str, Any], key: str, *, default: int | None = None) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidEditCommandError(f"{key} must be an integer")
    return value


def _payload_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise InvalidEditCommandError(f"{key} is required")
    return value


def apply_operation(
    base: ItineraryRevision,
    command: ItineraryEditCommand,
    *,
    undo_target: ItineraryRevision | None = None,
) -> tuple[list[ItineraryDay], list[int]]:
    days = [day.model_copy(deep=True) for day in base.days]
    route_cache = _route_edge_cache(base.days)
    payload = command.payload
    operation = command.operation
    changed_days: set[int] = set()

    if operation == EditOperation.ADD_STOP:
        try:
            stop = ItineraryStop.model_validate(payload["stop"])
        except (KeyError, ValueError) as exc:
            raise InvalidEditCommandError("ADD_STOP requires a valid stop payload") from exc
        if any(candidate.stop_id == stop.stop_id for day in days for candidate in day.stops):
            raise InvalidEditCommandError("stop_id already exists", context={"stop_id": stop.stop_id})
        day_index = stop.day_index
        day = _day_at(days, day_index)
        target_index = min(stop.order_index, len(day.stops))
        updated = list(day.stops)
        updated.insert(target_index, stop)
        _replace_day(days, day_index, updated)
        changed_days.add(day_index)

    elif operation == EditOperation.REORDER_STOP and "swap_day_indices" in payload:
        raw_indices = payload["swap_day_indices"]
        if (
            not isinstance(raw_indices, list)
            or len(raw_indices) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in raw_indices)
        ):
            raise InvalidEditCommandError("swap_day_indices must contain two day indices")
        left_index, right_index = raw_indices
        if left_index == right_index:
            raise InvalidEditCommandError("swap day indices must differ")
        left = _day_at(days, left_index)
        right = _day_at(days, right_index)
        for stop in [*left.stops, *right.stops]:
            _assert_not_locked(stop, operation)
        left_stops = [stop.model_copy(update={"transport_to_next": None}) for stop in left.stops]
        right_stops = [stop.model_copy(update={"transport_to_next": None}) for stop in right.stops]
        _replace_day(days, left_index, right_stops)
        _replace_day(days, right_index, left_stops)
        changed_days.update({left_index, right_index})

    elif operation in {EditOperation.MOVE_STOP, EditOperation.MOVE_TO_DAY, EditOperation.REORDER_STOP}:
        stop_id = _payload_str(payload, "stop_id")
        source_day_index, source_index, stop = _find_stop(days, stop_id)
        _assert_not_locked(stop, operation)
        target_day_index = _payload_int(payload, "target_day_index", default=source_day_index)
        target_day = _day_at(days, target_day_index)
        target_index = _payload_int(payload, "target_order_index", default=len(target_day.stops))
        if target_index < 0:
            raise InvalidEditCommandError("target_order_index must be non-negative")
        source_stops = list(days[source_day_index].stops)
        moved = source_stops.pop(source_index).model_copy(update={"transport_to_next": None})
        _replace_day(days, source_day_index, source_stops)
        target_stops = list(days[target_day_index].stops)
        if target_day_index == source_day_index and target_index > source_index:
            target_index -= 1
        target_index = min(target_index, len(target_stops))
        target_stops.insert(target_index, moved)
        _replace_day(days, target_day_index, target_stops)
        changed_days.update({source_day_index, target_day_index})

    elif operation == EditOperation.ADJUST_TIME:
        stop_id = _payload_str(payload, "stop_id")
        day_index, stop_index, stop = _find_stop(days, stop_id)
        _assert_not_locked(stop, operation)
        update = {
            key: payload[key]
            for key in ("start_time", "end_time", "visit_duration_minutes")
            if key in payload
        }
        if not update:
            raise InvalidEditCommandError("ADJUST_TIME requires at least one time field")
        updated_stops = list(days[day_index].stops)
        updated_stops[stop_index] = stop.model_copy(update=update)
        _replace_day(days, day_index, updated_stops)
        changed_days.add(day_index)

    elif operation == EditOperation.REPLACE_STOP:
        stop_id = _payload_str(payload, "stop_id")
        new_place_id = _payload_str(payload, "new_place_id")
        day_index, stop_index, stop = _find_stop(days, stop_id)
        _assert_not_locked(stop, operation)
        update: dict[str, Any] = {
            "place_id": new_place_id,
            "raw_name": payload.get("raw_name"),
            "source_raw_stop_id": payload.get("source_raw_stop_id"),
            "resolution_status": payload.get("resolution_status", ResolutionStatus.USER_CONFIRMED),
            "category": payload.get("category", stop.category),
            "transport_to_next": None,
        }
        updated_stops = list(days[day_index].stops)
        updated_stops[stop_index] = stop.model_copy(update=update)
        _replace_day(days, day_index, updated_stops)
        changed_days.add(day_index)

    elif operation == EditOperation.REMOVE_STOP:
        stop_id = _payload_str(payload, "stop_id")
        day_index, stop_index, stop = _find_stop(days, stop_id)
        _assert_not_locked(stop, operation)
        updated_stops = list(days[day_index].stops)
        updated_stops.pop(stop_index)
        _replace_day(days, day_index, updated_stops)
        changed_days.add(day_index)

    elif operation == EditOperation.LOCK_STOP:
        stop_id = _payload_str(payload, "stop_id")
        day_index, stop_index, stop = _find_stop(days, stop_id)
        updated_stops = list(days[day_index].stops)
        updated_stops[stop_index] = stop.model_copy(update={"locked": True})
        _replace_day(days, day_index, updated_stops)
        changed_days.add(day_index)

    elif operation == EditOperation.UNLOCK_STOP:
        stop_id = _payload_str(payload, "stop_id")
        day_index, stop_index, stop = _find_stop(days, stop_id)
        if stop.fixed_commitment:
            raise LockedCommitmentError("fixed commitments cannot be unlocked", context={"stop_id": stop_id})
        updated_stops = list(days[day_index].stops)
        updated_stops[stop_index] = stop.model_copy(update={"locked": False})
        _replace_day(days, day_index, updated_stops)
        changed_days.add(day_index)

    elif operation == EditOperation.UNDO:
        if undo_target is None:
            raise InvalidEditCommandError("UNDO requires a valid target revision")
        days = [day.model_copy(deep=True) for day in undo_target.days]
        changed_days.update(day.day_index for day in days if day.model_dump() != base.days[day.day_index].model_dump())

    elif operation == EditOperation.CONFIRM:
        changed_days = set()

    elif operation == EditOperation.APPLY_REPAIR:
        raise InvalidEditCommandError("APPLY_REPAIR must use RepairService with a postcheck report")

    else:
        raise InvalidEditCommandError("unsupported edit operation", context={"operation": operation.value})

    _restore_unchanged_route_edges(days, changed_days=changed_days, cache=route_cache)
    return days, sorted(changed_days)


def _edge_signatures(days: Iterable[ItineraryDay], changed_days: Iterable[int]) -> dict[str, tuple[str, str]]:
    selected = set(changed_days)
    return {
        f"day:{day.day_index}:edge:{left.stop_id}->{right.stop_id}": (left.place_id, right.place_id)
        for day in days
        if day.day_index in selected
        for left, right in zip(day.stops, day.stops[1:])
    }


def _changed_route_edges(
    before: list[ItineraryDay],
    after: list[ItineraryDay],
    changed_days: list[int],
) -> list[str]:
    """Return the edge-level symmetric difference, not every edge in a day."""
    before_edges = _edge_signatures(before, changed_days)
    after_edges = _edge_signatures(after, changed_days)
    return sorted(
        edge_id
        for edge_id in set(before_edges) | set(after_edges)
        if before_edges.get(edge_id) != after_edges.get(edge_id)
    )


def build_revision_command_outcome(
    workspace: TripWorkspace,
    base: ItineraryRevision,
    command: ItineraryEditCommand,
    *,
    undo_target: ItineraryRevision | None = None,
) -> tuple[ItineraryRevision, ItineraryPatchResult, WorkspaceStatus]:
    """Build one immutable revision without performing persistence.

    Keeping this pure lets the ordinary itinerary repository and the
    suggestion-aware Undo repository use the exact same revision semantics
    while choosing their own atomic write boundary.
    """
    days, changed_days = apply_operation(base, command, undo_target=undo_target)
    locked_commitments = sorted({
        stop.stop_id
        for day in days
        for stop in day.stops
        if stop.locked or stop.fixed_commitment
    })
    source = RevisionSource.REPAIR if command.operation == EditOperation.APPLY_REPAIR else RevisionSource.MANUAL
    content = ItineraryRevisionContent(
        itinerary_id=base.itinerary_id,
        workspace_id=base.workspace_id,
        revision=base.revision + 1,
        parent_revision=base.revision,
        source_type=source,
        city=base.city,
        date_range=base.date_range,
        days=days,
        locked_commitments=locked_commitments,
        change_summary={
            "command_id": command.command_id,
            "operation": command.operation.value,
            "base_content_hash": base.content_hash,
            "changed_days": changed_days,
        },
        created_by=command.actor_user_id,
    )
    revision = with_content_hash(content)
    result = ItineraryPatchResult(
        accepted=True,
        command_id=command.command_id,
        new_revision=revision.revision,
        changed_days=changed_days,
        changed_route_edges=_changed_route_edges(base.days, days, changed_days),
        report_stale=True,
    )
    next_status = WorkspaceStatus.CONFIRMED if command.operation == EditOperation.CONFIRM else WorkspaceStatus.DRAFT
    return revision, result, next_status


class RevisionCommandService:
    def __init__(self, repository: ItineraryRepository, *, audit_repository: AuditRepository | None = None):
        self.repository = repository
        self.audit_repository = audit_repository

    async def apply(
        self,
        command: ItineraryEditCommand,
        *,
        if_match_revision: int,
        idempotency_key: str,
    ) -> ItineraryPatchResult:
        if if_match_revision != command.base_revision:
            raise InvalidEditCommandError("If-Match and command base_revision must match")
        if not idempotency_key or len(idempotency_key) > 200:
            raise InvalidEditCommandError("Idempotency-Key must contain 1 to 200 characters")

        undo_target = None
        if command.operation == EditOperation.UNDO:
            target_revision = _payload_int(command.payload, "target_revision")
            if target_revision >= command.base_revision:
                raise InvalidEditCommandError("UNDO target_revision must be older than base_revision")
            undo_target = await self.repository.get_revision(command.workspace_id, target_revision)
            if undo_target is None:
                raise InvalidEditCommandError("UNDO target revision does not exist")

        request_hash = compute_command_request_hash(command.model_dump(mode="json"))

        def build(
            workspace: TripWorkspace,
            base: ItineraryRevision,
        ) -> tuple[ItineraryRevision, ItineraryPatchResult, WorkspaceStatus]:
            return build_revision_command_outcome(
                workspace,
                base,
                command,
                undo_target=undo_target,
            )

        precondition = None
        if command.operation == EditOperation.CONFIRM:
            if self.audit_repository is None:
                raise CurrentAuditRequiredError(
                    "final confirmation requires an audit repository",
                    context={"reason": "AUDIT_GATE_NOT_CONFIGURED"},
                )

            async def precondition(conn, workspace, base):
                await self.audit_repository.assert_current_confirmation_audit(
                    workspace,
                    base,
                    conn=conn,
                )

        return await self.repository.execute_command(
            command,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            builder=build,
            precondition=precondition,
        )
