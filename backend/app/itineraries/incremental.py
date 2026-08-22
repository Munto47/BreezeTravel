from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.audit.dependency_index import IncrementalDependencyIndex
from app.audit.evidence_service import EvidenceService
from app.audit.models import AuditDependency, AuditFinding, EvidenceSnapshot
from app.audit.repositories import AuditRepository
from app.audit.registry import AuditRuleContext, AuditRuleRegistry
from app.audit.system_constraints import with_system_constraints
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryPatchResult,
    ItineraryRevision,
)
from app.itineraries.repositories import ItineraryRepository
from app.members.models import MemberConstraint
from app.members.repositories import MemberConstraintRepository
from app.schemas.task_spec import DateRange, TripTaskSpec


_ORDER_OPERATIONS = {
    EditOperation.ADD_STOP,
    EditOperation.MOVE_STOP,
    EditOperation.MOVE_TO_DAY,
    EditOperation.REORDER_STOP,
    EditOperation.REPLACE_STOP,
    EditOperation.REMOVE_STOP,
    EditOperation.UNDO,
}


def dependencies_for_operation(operation: EditOperation) -> set[AuditDependency]:
    if operation in _ORDER_OPERATIONS:
        return {
            AuditDependency.DAY_ORDER,
            AuditDependency.ROUTE_EDGE,
            AuditDependency.HOTEL,
        }
    if operation is EditOperation.ADJUST_TIME:
        return {AuditDependency.DAY_ORDER, AuditDependency.TIME_WINDOW}
    if operation in {EditOperation.LOCK_STOP, EditOperation.UNLOCK_STOP}:
        return {AuditDependency.DAY_ORDER}
    return set(AuditDependency)


def _edges(days: list[ItineraryDay], changed_days: set[int]) -> dict[str, tuple[tuple[str, str], int | None]]:
    result: dict[str, tuple[tuple[str, str], int | None]] = {}
    for day in days:
        if day.day_index not in changed_days:
            continue
        for left, right in zip(day.stops, day.stops[1:]):
            key = f"day:{day.day_index}:edge:{left.stop_id}->{right.stop_id}"
            result[key] = (
                (left.place_id, right.place_id),
                left.transport_to_next.duration_minutes if left.transport_to_next is not None else None,
            )
    return result


def _last_end_time(day: ItineraryDay) -> str | None:
    return next((stop.end_time for stop in reversed(day.stops) if stop.end_time), None)


def build_route_delta(
    before: ItineraryRevision,
    after: ItineraryRevision,
    changed_days: list[int],
) -> dict[str, Any]:
    selected = set(changed_days)
    before_edges = _edges(before.days, selected)
    after_edges = _edges(after.days, selected)
    changed_edge_ids = sorted(
        edge_id
        for edge_id in set(before_edges) | set(after_edges)
        if (before_edges.get(edge_id) or (None,))[0] != (after_edges.get(edge_id) or (None,))[0]
    )
    evidence = []
    for edge_id in changed_edge_ids:
        before_edge = before_edges.get(edge_id)
        after_edge = after_edges.get(edge_id)
        previous = before_edge[1] if before_edge else None
        current = after_edge[1] if after_edge else None
        unavailable = (before_edge is not None and previous is None) or (after_edge is not None and current is None)
        evidence.append({
            "edge_id": edge_id,
            "previous_minutes": previous,
            "current_minutes": current,
            "freshness": "UNAVAILABLE" if unavailable else "FRESH",
            "source": "UNAVAILABLE" if unavailable else "REVISION_EDGE_CACHE",
            "reason_code": "ROUTE_EVIDENCE_UNAVAILABLE" if unavailable else None,
        })

    missing = [item["edge_id"] for item in evidence if item["freshness"] == "UNAVAILABLE"]
    known = [item for item in evidence if item["freshness"] == "FRESH"]
    if missing:
        status = "PARTIAL" if known else "UNAVAILABLE"
        previous_total = current_total = delta = None
    else:
        status = "AVAILABLE"
        previous_total = sum(item["previous_minutes"] or 0 for item in evidence)
        current_total = sum(item["current_minutes"] or 0 for item in evidence)
        delta = current_total - previous_total

    end_times = []
    for day_index in sorted(selected):
        end_times.append({
            "day_index": day_index,
            "previous_end_time": _last_end_time(before.days[day_index]),
            "current_end_time": _last_end_time(after.days[day_index]),
        })
    return {
        "status": status,
        "previous_minutes": previous_total,
        "current_minutes": current_total,
        "delta_minutes": delta,
        "changed_edges": evidence,
        "missing_edge_ids": missing,
        "day_end_times": end_times,
        "async_route_refresh_required": bool(missing),
    }


@dataclass(frozen=True)
class IncrementalAuditPreview:
    findings: list[AuditFinding]
    affected_rule_ids: list[str]


class IncrementalAuditEngine:
    """Revision-only preview using the same registered rule implementations.

    It deliberately has no provider or model dependency.  Missing route,
    weather, or opening-hour evidence remains UNKNOWN/UNAVAILABLE.  A complete
    persisted report is still required before final confirmation.
    """

    def __init__(self, registry: AuditRuleRegistry | None = None):
        self.registry = registry or AuditRuleRegistry()
        self.index = IncrementalDependencyIndex(self.registry.rules)

    def evaluate(
        self,
        revision: ItineraryRevision,
        *,
        dependencies: set[AuditDependency],
        changed_days: list[int],
        task_spec: TripTaskSpec | None = None,
        evidence_snapshot: EvidenceSnapshot | None = None,
        member_constraints: list[MemberConstraint] | None = None,
        now: datetime | None = None,
    ) -> IncrementalAuditPreview:
        affected_ids = self.index.affected_rule_ids(dependencies)
        selected_rules = [rule for rule in self.registry.rules if rule.rule_id in affected_ids]
        now = now or datetime.now(timezone.utc)
        days = (revision.date_range.end - revision.date_range.start).days + 1
        task_spec = task_spec or with_system_constraints(TripTaskSpec(
            task_id=f"workspace:{revision.workspace_id}:incremental-preview",
            room_id=revision.workspace_id,
            city=revision.city,
            date_range=DateRange(start=revision.date_range.start, days=days),
        ))
        snapshot = evidence_snapshot or EvidenceSnapshot(
            snapshot_id=f"incremental-preview:{uuid4()}",
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            provider_set=[],
            policy_version="incremental-revision-only-v1",
            facts=[],
            provider_failures=[],
            created_at=now,
        )
        context = AuditRuleContext(
            task_spec=task_spec,
            revision=revision,
            evidence_snapshot=snapshot,
            now=now,
            member_constraints=tuple(member_constraints or ()),
        )
        changed = set(changed_days)
        raw_findings = [
            finding
            for rule in selected_rules
            for finding in rule.evaluate(context)
            if not finding.affected_days or changed.intersection(finding.affected_days)
        ]
        findings = [
            finding.model_copy(update={
                "finding_id": "incremental:" + sha256_canonical({
                    "revision": revision.revision,
                    "rule_id": finding.rule_id,
                    "reason_code": finding.reason_code,
                    "affected_days": finding.affected_days,
                    "affected_stop_ids": finding.affected_stop_ids,
                    "input_values": finding.input_values,
                })[:24],
            })
            for finding in raw_findings
        ]
        return IncrementalAuditPreview(
            findings=findings,
            affected_rule_ids=sorted(affected_ids),
        )


class IncrementalWorkspaceEditService:
    def __init__(
        self,
        repository: ItineraryRepository,
        *,
        audit_engine: IncrementalAuditEngine | None = None,
        audit_repository: AuditRepository | None = None,
        member_constraint_repository: MemberConstraintRepository | None = None,
        evidence_service: EvidenceService | None = None,
    ):
        self.repository = repository
        self.command_service = RevisionCommandService(repository)
        self.audit_engine = audit_engine or IncrementalAuditEngine()
        self.audit_repository = audit_repository
        self.member_constraint_repository = member_constraint_repository
        self.evidence_service = evidence_service or EvidenceService()

    async def _load_current_audit_context(
        self,
        revision: ItineraryRevision,
        *,
        now: datetime,
    ) -> tuple[TripTaskSpec | None, EvidenceSnapshot | None, list[MemberConstraint]]:
        """Rebuild the same local task/evidence inputs used by a full audit.

        This is intentionally provider-free: it reads the current persisted
        room-place evidence and task/constraint revisions, then derives a
        transient snapshot for this new itinerary revision.  It does not claim
        that an incremental preview is a persisted full report.
        """
        if self.audit_repository is None:
            return None, None, []
        workspace = await self.repository.get_workspace(revision.workspace_id)
        if workspace is None:
            return None, None, []
        place_ids = sorted({stop.place_id for day in revision.days for stop in day.stops})
        place_records = await self.audit_repository.load_place_records(
            revision.workspace_id,
            place_ids,
            target_itinerary_revision=revision.revision,
        )
        latest_snapshot = await self.audit_repository.get_latest_snapshot(revision.workspace_id)
        snapshot = self.evidence_service.create_snapshot(
            workspace_id=revision.workspace_id,
            itinerary_revision=revision.revision,
            observations=self.evidence_service.observations_from_revision(
                revision,
                place_records,
                now=now,
                target_itinerary_revision=revision.revision,
            ),
            supersedes_snapshot_id=latest_snapshot.snapshot_id if latest_snapshot else None,
            now=now,
        )
        task_spec = await self.audit_repository.load_task_spec(revision.workspace_id)
        if task_spec is None:
            days = (revision.date_range.end - revision.date_range.start).days + 1
            task_spec = TripTaskSpec(
                task_id=f"workspace:{revision.workspace_id}:default",
                room_id=workspace.room_id,
                city=workspace.city,
                date_range=DateRange(start=revision.date_range.start, days=days),
                task_revision=workspace.current_task_spec_revision or 1,
            )
        constraints = []
        if self.member_constraint_repository is not None and workspace.current_member_constraint_revision is not None:
            constraints = await self.member_constraint_repository.list_effective_constraints(
                revision.workspace_id,
                workspace.current_member_constraint_revision,
            )
        return with_system_constraints(task_spec), snapshot, constraints

    async def apply(
        self,
        command: ItineraryEditCommand,
        *,
        if_match_revision: int,
        idempotency_key: str,
    ) -> ItineraryPatchResult:
        base = await self.repository.get_revision(command.workspace_id, command.base_revision)
        if base is None:
            # Preserve the RevisionCommandService's stable domain error path.
            return await self.command_service.apply(
                command,
                if_match_revision=if_match_revision,
                idempotency_key=idempotency_key,
            )
        result = await self.command_service.apply(
            command,
            if_match_revision=if_match_revision,
            idempotency_key=idempotency_key,
        )
        return await self.enrich_committed_result(command, result, base=base)

    async def enrich_committed_result(
        self,
        command: ItineraryEditCommand,
        result: ItineraryPatchResult,
        *,
        base: ItineraryRevision | None = None,
    ) -> ItineraryPatchResult:
        """Attach the provider-free incremental preview to an already committed edit."""
        if result.new_revision is None:
            return result
        if base is None:
            base = await self.repository.get_revision(command.workspace_id, command.base_revision)
        if base is None:
            return result
        revision = await self.repository.get_revision(command.workspace_id, result.new_revision)
        if revision is None:
            return result
        dependencies = dependencies_for_operation(command.operation)
        task_spec, snapshot, constraints = await self._load_current_audit_context(
            revision,
            now=revision.created_at,
        )
        preview = self.audit_engine.evaluate(
            revision,
            dependencies=dependencies,
            changed_days=result.changed_days,
            task_spec=task_spec,
            evidence_snapshot=snapshot,
            member_constraints=constraints,
            now=revision.created_at,
        )
        return result.model_copy(update={
            "route_delta": build_route_delta(base, revision, result.changed_days),
            "incremental_findings": [item.model_dump(mode="json") for item in preview.findings],
            "affected_rule_ids": preview.affected_rule_ids,
            "audit_mode": "INCREMENTAL_REVISION_ONLY",
            "llm_calls": 0,
        })
