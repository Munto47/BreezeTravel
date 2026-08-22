from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from app.audit.engine import AuditEngine
from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import (
    AuditFinding,
    AuditReport,
    AuditRunInput,
    AuditStatus,
    EvidenceFreshness,
    EvidenceSnapshot,
    ProviderFailure,
)
from app.audit.repositories import AuditRepository
from app.audit.system_constraints import with_system_constraints
from app.itineraries.command_service import apply_operation
from app.itineraries.errors import ItineraryDomainError, ResourceNotFound
from app.itineraries.hash_service import sha256_canonical, with_content_hash
from app.itineraries.map_projection import build_map_projection
from app.itineraries.models import (
    EditOperation,
    ItineraryEditCommand,
    ItineraryRevision,
    ItineraryRevisionContent,
    RevisionSource,
)
from app.itineraries.repositories import ItineraryRepository
from app.itineraries.route_refresh import AmapRouteEvidenceProvider, RouteEvidenceProvider
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository
from app.repairs.errors import RepairNoFeasibleOptionError, RepairStaleError
from app.repairs.models import RepairOperation, RepairOperationType, RepairOption
from app.repairs.objective import (
    edit_cost,
    introduces_new_high_violation,
    new_unknown_count,
    repair_option_sort_key,
    route_cost_delta_minutes,
    unresolved_risk_cost,
)
from app.repairs.repositories import RepairRepository
from app.schemas.task_spec import DateRange, TripTaskSpec


_MAX_OPTIONS = 2
_MAX_OPERATIONS_PER_OPTION = 8


@dataclass(frozen=True)
class _RepairRouteEdge:
    evidence_id: str
    from_stop_id: str
    to_stop_id: str
    from_place_id: str
    to_place_id: str
    mode: str


def _normalized_mode(value: str) -> str:
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "car": "driving",
        "drive": "driving",
        "walk": "walking",
        "bus": "transit",
        "public_transport": "transit",
    }.get(normalized, normalized)


def _route_edges(revision: ItineraryRevision) -> dict[str, _RepairRouteEdge]:
    return {
        f"{left.stop_id}->{right.stop_id}": _RepairRouteEdge(
            evidence_id=f"{left.stop_id}->{right.stop_id}",
            from_stop_id=left.stop_id,
            to_stop_id=right.stop_id,
            from_place_id=left.place_id,
            to_place_id=right.place_id,
            mode=left.transport_to_next.mode if left.transport_to_next else "driving",
        )
        for day in revision.days
        for left, right in zip(day.stops, day.stops[1:])
    }


def _has_usable_route_fact(
    snapshot: EvidenceSnapshot,
    edge: _RepairRouteEdge,
    *,
    now: datetime,
) -> bool:
    """Match RouteGapRule's fail-closed evidence boundary before reusing a fact."""

    facts = [
        fact
        for fact in snapshot.facts
        if fact.subject_type == "ROUTE_EDGE"
        and fact.subject_id == edge.evidence_id
        and fact.fact_type == "ROUTE_TIME"
    ]
    if not facts or any(fact.freshness_status == EvidenceFreshness.CONFLICTING for fact in facts):
        return False
    fresh = [
        fact
        for fact in facts
        if fact.freshness_status == EvidenceFreshness.FRESH
        and (fact.valid_from is None or fact.valid_from <= now)
        and (fact.valid_until is None or fact.valid_until >= now)
    ]
    if not fresh:
        return False
    values: set[tuple[str, int]] = set()
    for fact in fresh:
        if not isinstance(fact.value, dict):
            return False
        duration = fact.value.get("duration_minutes")
        mode = fact.value.get("mode")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration < 0
            or not isinstance(mode, str)
            or not mode.strip()
        ):
            return False
        values.add((_normalized_mode(mode), int(duration)))
    return len(values) == 1 and next(iter(values))[0] == _normalized_mode(edge.mode)


class RepairRouteEvidenceRefresher(Protocol):
    async def collect(
        self,
        *,
        base: ItineraryRevision,
        preview: ItineraryRevision,
        source_snapshot: EvidenceSnapshot,
        now: datetime,
    ) -> tuple[list[EvidenceObservation], list[ProviderFailure]]: ...


class ProviderRepairRouteEvidenceRefresher:
    """Refresh preview edges that cannot safely reuse source route evidence."""

    def __init__(self, provider: RouteEvidenceProvider | None = None):
        self.provider = provider or AmapRouteEvidenceProvider()

    async def collect(
        self,
        *,
        base: ItineraryRevision,
        preview: ItineraryRevision,
        source_snapshot: EvidenceSnapshot,
        now: datetime,
    ) -> tuple[list[EvidenceObservation], list[ProviderFailure]]:
        before = _route_edges(base)
        current = _route_edges(preview)
        projection = build_map_projection(preview, lineage=[preview, base])
        projected = {item.stop_id: item for item in projection.stops}
        observations: list[EvidenceObservation] = []
        failures: list[ProviderFailure] = []

        for edge_id, edge in sorted(current.items()):
            old = before.get(edge_id)
            binding_changed = old is None or (
                old.from_place_id,
                old.to_place_id,
                _normalized_mode(old.mode),
            ) != (
                edge.from_place_id,
                edge.to_place_id,
                _normalized_mode(edge.mode),
            )
            if not binding_changed and _has_usable_route_fact(source_snapshot, edge, now=now):
                continue

            source = projected.get(edge.from_stop_id)
            destination = projected.get(edge.to_stop_id)
            if source is None or destination is None:
                observations.append(EvidenceObservation(
                    subject_type="ROUTE_EDGE",
                    subject_id=edge.evidence_id,
                    fact_type="ROUTE_TIME",
                    value={
                        "from_place_id": edge.from_place_id,
                        "to_place_id": edge.to_place_id,
                        "mode": edge.mode,
                        "reason_code": "REVISION_STOP_COORDINATES_UNAVAILABLE",
                    },
                    provider="canonical_coordinate_projection",
                    observed_at=now,
                    confidence=0,
                    freshness_status=EvidenceFreshness.UNAVAILABLE,
                ))
                continue

            try:
                route = await self.provider.fetch(
                    origin=source.coords,
                    destination=destination.coords,
                    mode=edge.mode,
                    city=preview.city,
                )
            except Exception as exc:  # Provider adapters are boundary code.
                provider = type(self.provider).__name__
                reason = type(exc).__name__
                failures.append(ProviderFailure(
                    provider=provider,
                    error_category="ROUTE_REFRESH_UNAVAILABLE",
                    retryable=False,
                    detail=reason,
                ))
                observations.append(EvidenceObservation(
                    subject_type="ROUTE_EDGE",
                    subject_id=edge.evidence_id,
                    fact_type="ROUTE_TIME",
                    value={
                        "from_place_id": edge.from_place_id,
                        "to_place_id": edge.to_place_id,
                        "mode": edge.mode,
                        "reason_code": reason,
                    },
                    provider=provider,
                    observed_at=now,
                    confidence=0,
                    freshness_status=EvidenceFreshness.UNAVAILABLE,
                ))
                continue

            observed_at = route.observed_at or now
            if route.status != "ok" or route.duration_minutes is None:
                reason = route.failure_reason or "ROUTE_EVIDENCE_UNAVAILABLE"
                failures.append(ProviderFailure(
                    provider=route.source,
                    error_category="ROUTE_REFRESH_UNAVAILABLE",
                    retryable=False,
                    detail=reason,
                ))
                observations.append(EvidenceObservation(
                    subject_type="ROUTE_EDGE",
                    subject_id=edge.evidence_id,
                    fact_type="ROUTE_TIME",
                    value={
                        "from_place_id": edge.from_place_id,
                        "to_place_id": edge.to_place_id,
                        "mode": edge.mode,
                        "reason_code": reason,
                    },
                    provider=route.source,
                    observed_at=observed_at,
                    confidence=0,
                    freshness_status=EvidenceFreshness.UNAVAILABLE,
                ))
                continue

            observations.append(EvidenceObservation(
                subject_type="ROUTE_EDGE",
                subject_id=edge.evidence_id,
                fact_type="ROUTE_TIME",
                value={
                    "from_place_id": edge.from_place_id,
                    "to_place_id": edge.to_place_id,
                    "mode": edge.mode,
                    "duration_minutes": route.duration_minutes,
                    "distance_km": route.distance_km,
                    "distance_meters": (
                        round(route.distance_km * 1000) if route.distance_km is not None else None
                    ),
                    "transfer_count": route.transfer_count,
                    "route_response_hash": route.response_hash,
                    "projection": {
                        "from_projection_revision": source.projection_revision,
                        "to_projection_revision": destination.projection_revision,
                        "from_coordinate_role": source.coordinate_role,
                        "to_coordinate_role": destination.coordinate_role,
                    },
                },
                provider=route.source,
                observed_at=observed_at,
                confidence=1,
            ))
        return observations, failures


def _with_route_refresh(
    *,
    evidence_service: EvidenceService,
    derived: EvidenceSnapshot,
    observations: list[EvidenceObservation],
    failures: list[ProviderFailure],
    now: datetime,
) -> EvidenceSnapshot:
    if not observations and not failures:
        return derived
    refreshed_ids = {item.subject_id for item in observations}
    captured = evidence_service.create_snapshot(
        workspace_id=derived.workspace_id,
        itinerary_revision=derived.itinerary_revision,
        observations=observations,
        provider_failures=failures,
        supersedes_snapshot_id=derived.supersedes_snapshot_id,
        now=now,
    )
    kept = [
        fact
        for fact in derived.facts
        if not (
            fact.subject_type == "ROUTE_EDGE"
            and fact.fact_type == "ROUTE_TIME"
            and fact.subject_id in refreshed_ids
        )
    ]
    refreshed = [
        fact.model_copy(update={"snapshot_id": derived.snapshot_id})
        for fact in captured.facts
    ]
    return derived.model_copy(update={
        "facts": [*kept, *refreshed],
        "provider_set": sorted({*derived.provider_set, *captured.provider_set}),
        "provider_failures": [*derived.provider_failures, *failures],
    })


def _include_route_gap_in_time_operations(
    base: ItineraryRevision,
    operations: list[RepairOperation],
    route_observations: list[EvidenceObservation],
    *,
    source_snapshot: EvidenceSnapshot,
    now: datetime,
) -> list[RepairOperation]:
    """Refine overlap repairs with the freshly observed adjacent-route duration."""

    durations = {
        item.subject_id: int(item.value["duration_minutes"])
        for item in route_observations
        if item.subject_type == "ROUTE_EDGE"
        and item.fact_type == "ROUTE_TIME"
        and item.freshness_status != EvidenceFreshness.UNAVAILABLE
        and isinstance(item.value, dict)
        and isinstance(item.value.get("duration_minutes"), (int, float))
        and not isinstance(item.value.get("duration_minutes"), bool)
        and item.value["duration_minutes"] >= 0
    }
    for fact in source_snapshot.facts:
        if (
            fact.subject_type != "ROUTE_EDGE"
            or fact.fact_type != "ROUTE_TIME"
            or fact.freshness_status != EvidenceFreshness.FRESH
            or (fact.valid_from is not None and fact.valid_from > now)
            or (fact.valid_until is not None and fact.valid_until < now)
            or not isinstance(fact.value, dict)
        ):
            continue
        duration = fact.value.get("duration_minutes")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool) and duration >= 0:
            durations.setdefault(fact.subject_id, int(duration))
    if not durations:
        return operations

    by_stop = {
        item.payload.get("stop_id"): index
        for index, item in enumerate(operations)
        if item.operation == RepairOperationType.ADJUST_TIME
    }
    refined = list(operations)
    for day in base.days:
        for left, right in zip(day.stops, day.stops[1:]):
            left_end = _minutes(left.end_time)
            right_start = _minutes(right.start_time)
            duration = durations.get(f"{left.stop_id}->{right.stop_id}")
            if left_end is None or right_start is None or duration is None or right_start >= left_end:
                continue
            right_index = by_stop.get(right.stop_id)
            if right_index is not None:
                operation = refined[right_index]
                visit_duration = operation.payload.get("visit_duration_minutes")
                if isinstance(visit_duration, int):
                    start = _clock(left_end + duration)
                    end = _clock(left_end + duration + visit_duration)
                    if start is not None and end is not None:
                        refined[right_index] = operation.model_copy(update={
                            "payload": {**operation.payload, "start_time": start, "end_time": end},
                        })
                continue
            left_index = by_stop.get(left.stop_id)
            left_start = _minutes(left.start_time)
            if left_index is None or left_start is None:
                continue
            end_minutes = right_start - duration
            end = _clock(end_minutes)
            if end is None or end_minutes <= left_start:
                continue
            operation = refined[left_index]
            refined[left_index] = operation.model_copy(update={
                "payload": {
                    **operation.payload,
                    "end_time": end,
                    "visit_duration_minutes": end_minutes - left_start,
                },
            })
    return refined


@dataclass(frozen=True)
class PreparedRepairOption:
    option: RepairOption
    snapshot: EvidenceSnapshot
    postcheck: AuditReport


def _minutes(value: str | None) -> int | None:
    if not value or ":" not in value:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except ValueError:
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return hour * 60 + minute


def _clock(value: int) -> str | None:
    if not 0 <= value < 24 * 60:
        return None
    return f"{value // 60:02d}:{value % 60:02d}"


def _time_chain_operations(
    revision: ItineraryRevision,
    finding: AuditFinding,
) -> list[tuple[list[RepairOperation], list[str]]]:
    """Return two bounded, deterministic strategies for an overlapping day."""

    if finding.reason_code != "TIME_CHAIN_BROKEN" or not finding.affected_days:
        return []
    day_index = finding.affected_days[0]
    if day_index < 0 or day_index >= len(revision.days):
        return []
    day = revision.days[day_index]
    if len(day.stops) < 2:
        return []

    shift_later: list[RepairOperation] = []
    shorten_earlier: list[RepairOperation] = []
    shift_times = [(stop.start_time, stop.end_time) for stop in day.stops]
    shorten_times = list(shift_times)

    for index in range(1, len(day.stops)):
        previous_start = _minutes(shift_times[index - 1][0])
        previous_end = _minutes(shift_times[index - 1][1])
        current_start = _minutes(shift_times[index][0])
        current_end = _minutes(shift_times[index][1])
        if None in {previous_start, previous_end, current_start, current_end}:
            continue
        if current_start < previous_end:
            duration = max(current_end - current_start, day.stops[index].visit_duration_minutes or 0)
            new_start = _clock(previous_end)
            new_end = _clock(previous_end + duration)
            if (
                new_start is not None
                and new_end is not None
                and not (day.stops[index].locked or day.stops[index].fixed_commitment)
            ):
                shift_later.append(
                    RepairOperation(
                        operation=RepairOperationType.ADJUST_TIME,
                        payload={
                            "stop_id": day.stops[index].stop_id,
                            "start_time": new_start,
                            "end_time": new_end,
                            "visit_duration_minutes": duration,
                        },
                        rationale=f"将 {day.stops[index].raw_name or day.stops[index].stop_id} 顺延，保留原停留时长",
                    )
                )
                shift_times[index] = (new_start, new_end)

        previous_start = _minutes(shorten_times[index - 1][0])
        previous_end = _minutes(shorten_times[index - 1][1])
        current_start = _minutes(shorten_times[index][0])
        if None in {previous_start, previous_end, current_start}:
            continue
        if current_start < previous_end:
            duration = current_start - previous_start
            new_end = _clock(current_start)
            if (
                duration > 0
                and new_end is not None
                and not (day.stops[index - 1].locked or day.stops[index - 1].fixed_commitment)
            ):
                shorten_earlier.append(
                    RepairOperation(
                        operation=RepairOperationType.ADJUST_TIME,
                        payload={
                            "stop_id": day.stops[index - 1].stop_id,
                            "end_time": new_end,
                            "visit_duration_minutes": duration,
                        },
                        rationale=f"缩短 {day.stops[index - 1].raw_name or day.stops[index - 1].stop_id}，保留后续开始时间",
                    )
                )
                shorten_times[index - 1] = (shorten_times[index - 1][0], new_end)

    candidates: list[tuple[list[RepairOperation], list[str]]] = []
    if shift_later:
        candidates.append((shift_later, ["保留各地点停留时长，但当天结束时间会顺延"]))
    if shorten_earlier:
        candidates.append((shorten_earlier, ["保留后续开始时间，但前一地点停留时间会缩短"]))
    return candidates


def _duplicate_place_operations(
    revision: ItineraryRevision,
    finding: AuditFinding,
) -> list[tuple[list[RepairOperation], list[str]]]:
    """Remove either end of a duplicated attraction occurrence pair.

    Ordering is derived from the canonical itinerary rather than from finding
    payload order so previews remain deterministic across repository backends.
    The normal command service still enforces the lock invariant, while this
    filter avoids offering a preview that is known to be invalid.
    """

    if finding.reason_code != "DUPLICATE_PLACE":
        return []
    place_id = finding.input_values.get("place_id")
    if not isinstance(place_id, str) or not place_id:
        return []

    occurrences = sorted(
        (
            (day.day_index, stop.order_index, stop)
            for day in revision.days
            for stop in day.stops
            if stop.place_id == place_id
        ),
        key=lambda item: (item[0], item[1], item[2].stop_id),
    )
    if len(occurrences) < 2:
        return []

    candidates: list[tuple[list[RepairOperation], list[str]]] = []
    for position, (day_index, _order_index, stop) in (
        ("较早", occurrences[0]),
        ("较晚", occurrences[-1]),
    ):
        if stop.locked or stop.fixed_commitment:
            continue
        display_name = stop.raw_name or stop.stop_id
        candidates.append(
            (
                [
                    RepairOperation(
                        operation=RepairOperationType.REMOVE_STOP,
                        payload={"stop_id": stop.stop_id},
                        rationale=f"删除第{day_index + 1}天{position}的重复安排 {display_name}",
                    )
                ],
                [
                    f"删除第{day_index + 1}天{position}安排 {display_name}（stop_id={stop.stop_id}），"
                    "保留同一景点的另一次安排"
                ],
            )
        )
    return candidates


def _repair_candidates(
    revision: ItineraryRevision,
    finding: AuditFinding,
) -> list[tuple[list[RepairOperation], list[str]]]:
    if finding.reason_code == "DUPLICATE_PLACE":
        return _duplicate_place_operations(revision, finding)
    return _time_chain_operations(revision, finding)


def _candidate_revision(
    base: ItineraryRevision,
    operations: list[RepairOperation],
    *,
    repair_id: str,
    now: datetime,
) -> ItineraryRevision:
    if not operations or len(operations) > _MAX_OPERATIONS_PER_OPTION:
        raise ValueError("repair operation count is outside the bounded search limit")
    working = base
    changed_days: set[int] = set()
    for index, operation in enumerate(operations):
        command = ItineraryEditCommand(
            command_id=f"repair-preview:{repair_id}:{index}",
            workspace_id=base.workspace_id,
            base_revision=base.revision,
            actor_user_id="repair-search",
            operation=EditOperation(operation.operation.value),
            payload=operation.payload,
        )
        days, changed = apply_operation(working, command)
        changed_days.update(changed)
        working = working.model_copy(update={"days": days})
    content = ItineraryRevisionContent(
        itinerary_id=base.itinerary_id,
        workspace_id=base.workspace_id,
        revision=base.revision + 1,
        parent_revision=base.revision,
        source_type=RevisionSource.REPAIR,
        city=base.city,
        date_range=base.date_range,
        days=working.days,
        locked_commitments=base.locked_commitments,
        change_summary={
            "kind": "repair_preview",
            "repair_id": repair_id,
            "base_content_hash": base.content_hash,
            "changed_days": sorted(changed_days),
            "operation_count": len(operations),
        },
        created_by="repair-search",
        created_at=now,
    )
    return with_content_hash(content)


def _target_resolved(source: AuditFinding, candidate_findings: list[AuditFinding]) -> bool:
    return not any(
        item.status == AuditStatus.VIOLATED
        and item.rule_id == source.rule_id
        and item.reason_code == source.reason_code
        and set(item.affected_days) == set(source.affected_days)
        for item in candidate_findings
    )


class BoundedRepairSearch:
    def __init__(
        self,
        *,
        itinerary_repository: ItineraryRepository,
        audit_repository: AuditRepository,
        repair_repository: RepairRepository,
        evidence_service: EvidenceService | None = None,
        engine: AuditEngine | None = None,
        route_refresher: RepairRouteEvidenceRefresher | None = None,
    ):
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.repair_repository = repair_repository
        self.evidence_service = evidence_service or EvidenceService()
        self.engine = engine or AuditEngine()
        self.route_refresher = route_refresher or ProviderRepairRouteEvidenceRefresher()

    async def propose(self, source_report_id: str, *, now: datetime | None = None) -> list[RepairOption]:
        prepared = await self._prepare(source_report_id, now=now)
        stored: list[RepairOption] = []
        for item in prepared:
            postcheck = await self.audit_repository.save_preview_bundle(item.snapshot, item.postcheck)
            option = item.option.model_copy(update={"postcheck_report_id": postcheck.report_id})
            stored.append(await self.repair_repository.save_option(option))
        return stored

    async def propose_idempotent(
        self,
        source_report_id: str,
        *,
        actor_user_id: str,
        idempotency_key: str,
        command_repository: CreationCommandRepository,
        now: datetime | None = None,
    ) -> tuple[list[RepairOption], bool]:
        source_report = await self.audit_repository.get_report(source_report_id)
        if source_report is None:
            raise ResourceNotFound("audit report does not exist")
        workspace = await self.itinerary_repository.get_workspace(source_report.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        basis = {
            "current_itinerary_revision": workspace.current_itinerary_revision,
            "current_report_id": workspace.current_report_id,
        }
        request_hash = sha256_canonical(
            {
                "schema_version": 1,
                "operation": CreationOperation.PROPOSE_REPAIRS.value,
                "workspace_id": source_report.workspace_id,
                "target_id": source_report_id,
                "actor_user_id": actor_user_id,
                "body": {},
            }
        )
        claim = await command_repository.claim(
            workspace_id=source_report.workspace_id,
            operation=CreationOperation.PROPOSE_REPAIRS,
            target_id=source_report_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            basis=basis,
        )
        if claim.replay is not None:
            return [RepairOption.model_validate(item) for item in claim.replay.body], True
        try:
            prepared = await self._prepare(source_report_id, now=now)

            async def finalize(conn, stored_basis):
                if conn is not None:
                    current = await conn.fetchrow(
                        """
                        SELECT current_itinerary_revision, current_report_id
                        FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE
                        """,
                        source_report.workspace_id,
                    )
                    actual_basis = {
                        "current_itinerary_revision": current["current_itinerary_revision"] if current else None,
                        "current_report_id": current["current_report_id"] if current else None,
                    }
                else:
                    current_workspace = await self.itinerary_repository.get_workspace(source_report.workspace_id)
                    actual_basis = {
                        "current_itinerary_revision": (
                            current_workspace.current_itinerary_revision if current_workspace else None
                        ),
                        "current_report_id": current_workspace.current_report_id if current_workspace else None,
                    }
                if actual_basis != stored_basis:
                    raise RepairStaleError(
                        "repair basis changed during proposal search",
                        context={"expected_basis": stored_basis, "actual_basis": actual_basis},
                    )
                stored: list[RepairOption] = []
                for item in prepared:
                    postcheck = await self.audit_repository.save_preview_bundle(
                        item.snapshot,
                        item.postcheck,
                        conn=conn,
                    )
                    option = item.option.model_copy(update={"postcheck_report_id": postcheck.report_id})
                    stored.append(await self.repair_repository.save_option(option, conn=conn))
                return CreationCommandResponse(
                    status_code=201,
                    body=[item.model_dump(mode="json") for item in stored],
                    headers={},
                )

            response = await command_repository.finalize(claim, finalize)
            return [RepairOption.model_validate(item) for item in response.body], response.idempotent_replay
        except Exception:
            await command_repository.abandon(claim)
            raise

    async def _prepare(
        self,
        source_report_id: str,
        *,
        now: datetime | None = None,
    ) -> list[PreparedRepairOption]:
        now = now or datetime.now(timezone.utc)
        source_report = await self.audit_repository.get_report(source_report_id)
        if source_report is None:
            raise ResourceNotFound("audit report does not exist")
        workspace = await self.itinerary_repository.get_workspace(source_report.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if (
            workspace.current_itinerary_revision != source_report.itinerary_revision
            or workspace.current_report_id != source_report.report_id
        ):
            raise RepairStaleError(
                "repair requires the current audit report",
                context={
                    "report_revision": source_report.itinerary_revision,
                    "current_revision": workspace.current_itinerary_revision,
                    "current_report_id": workspace.current_report_id,
                },
            )
        base = await self.itinerary_repository.get_revision(
            source_report.workspace_id,
            source_report.itinerary_revision,
        )
        snapshot = await self.audit_repository.get_snapshot(source_report.evidence_snapshot_id)
        if base is None or snapshot is None:
            raise ResourceNotFound("audit input revision or evidence snapshot does not exist")
        task_spec = await self.audit_repository.load_task_spec(source_report.workspace_id, source_report.task_id)
        if task_spec is None:
            task_spec = TripTaskSpec(
                task_id=source_report.task_id,
                room_id=workspace.room_id,
                task_revision=source_report.task_revision,
                city=workspace.city,
                date_range=DateRange(
                    start=workspace.trip_date_range.start,
                    days=(workspace.trip_date_range.end - workspace.trip_date_range.start).days + 1,
                ),
            )
        task_spec = with_system_constraints(task_spec)

        target = next(
            (
                item
                for item in source_report.findings
                if item.status == AuditStatus.VIOLATED
                and item.repairable
                and item.severity.value in {"BLOCKER", "HIGH"}
            ),
            None,
        )
        if target is None:
            raise RepairNoFeasibleOptionError(
                "the report has no supported repairable BLOCKER or HIGH finding",
                context={"source_report_id": source_report_id},
            )

        proposals: list[PreparedRepairOption] = []
        seen_payloads: set[str] = set()
        for operations, tradeoffs in _repair_candidates(base, target):
            operation_hash = sha256_canonical([item.model_dump(mode="json") for item in operations])
            if operation_hash in seen_payloads:
                continue
            seen_payloads.add(operation_hash)
            repair_id = str(uuid4())
            try:
                preview = _candidate_revision(base, operations, repair_id=repair_id, now=now)
            except (ItineraryDomainError, ValueError):
                continue
            route_observations, route_failures = await self.route_refresher.collect(
                base=base,
                preview=preview,
                source_snapshot=snapshot,
                now=now,
            )
            operations = _include_route_gap_in_time_operations(
                base,
                operations,
                route_observations,
                source_snapshot=snapshot,
                now=now,
            )
            try:
                preview = _candidate_revision(base, operations, repair_id=repair_id, now=now)
            except (ItineraryDomainError, ValueError):
                continue
            candidate_snapshot = self.evidence_service.derive_snapshot_for_revision(snapshot, preview, now=now)
            candidate_snapshot = _with_route_refresh(
                evidence_service=self.evidence_service,
                derived=candidate_snapshot,
                observations=route_observations,
                failures=route_failures,
                now=now,
            )
            candidate_report = self.engine.run(
                run_input=AuditRunInput(
                    workspace_id=source_report.workspace_id,
                    itinerary_revision=preview.revision,
                    task_id=source_report.task_id,
                    task_revision=source_report.task_revision,
                    member_constraint_revision_set=source_report.member_constraint_revision_set,
                    place_resolution_versions={stop.place_id: 1 for day in preview.days for stop in day.stops},
                ),
                revision=preview,
                task_spec=task_spec,
                evidence_snapshot=candidate_snapshot,
                supersedes_report_id=source_report.report_id,
                now=now,
            )
            if not _target_resolved(target, candidate_report.findings):
                continue
            if introduces_new_high_violation(source_report, candidate_report):
                continue
            unknowns = new_unknown_count(source_report, candidate_report)
            if unknowns:
                continue
            postcheck = candidate_report
            option = RepairOption(
                repair_id=repair_id,
                source_report_id=source_report.report_id,
                base_itinerary_revision=base.revision,
                operations=operations,
                targeted_finding_ids=[target.finding_id],
                edit_cost=edit_cost(operations),
                risk_cost=unresolved_risk_cost(postcheck),
                route_cost_delta=route_cost_delta_minutes(
                    base,
                    preview,
                    snapshot,
                    candidate_snapshot,
                ),
                new_unknown_count=unknowns,
                tradeoffs=tradeoffs,
                affected_member_ids=target.affected_member_ids,
                result_preview=preview,
                postcheck_report_id=postcheck.report_id,
                created_at=now,
            )
            proposals.append(
                PreparedRepairOption(
                    option=option,
                    snapshot=candidate_snapshot,
                    postcheck=postcheck,
                )
            )

        if not proposals:
            raise RepairNoFeasibleOptionError(
                "no candidate passed locked-item, high-risk and UNKNOWN postcheck gates",
                context={
                    "source_report_id": source_report_id,
                    "unresolved_finding_ids": [target.finding_id],
                },
            )
        return sorted(proposals, key=lambda item: repair_option_sort_key(item.option))[:_MAX_OPTIONS]
