from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.audit.models import AuditReport, AuditRunInput, AuditStatus, EvidenceSnapshot
from app.audit.registry import AuditRuleContext, AuditRuleRegistry
from app.audit.report_hash import compute_report_input_hash
from app.itineraries.models import ItineraryRevision
from app.schemas.task_spec import TripTaskSpec


class AuditEngine:
    def __init__(self, registry: AuditRuleRegistry | None = None):
        self.registry = registry or AuditRuleRegistry()

    def run(
        self,
        *,
        run_input: AuditRunInput,
        revision: ItineraryRevision,
        task_spec: TripTaskSpec,
        evidence_snapshot: EvidenceSnapshot,
        supersedes_report_id: str | None = None,
        now: datetime | None = None,
    ) -> AuditReport:
        now = now or datetime.now(timezone.utc)
        if revision.workspace_id != run_input.workspace_id:
            raise ValueError("revision belongs to another workspace")
        if revision.revision != run_input.itinerary_revision:
            raise ValueError("audit input revision does not match the itinerary")
        if evidence_snapshot.workspace_id != run_input.workspace_id:
            raise ValueError("evidence snapshot belongs to another workspace")
        if evidence_snapshot.itinerary_revision != revision.revision:
            raise ValueError("evidence snapshot was collected for another itinerary revision")
        if task_spec.task_id != run_input.task_id or task_spec.task_revision != run_input.task_revision:
            raise ValueError("task spec does not match audit run input")

        context = AuditRuleContext(
            task_spec=task_spec,
            revision=revision,
            evidence_snapshot=evidence_snapshot,
            member_constraints=tuple(run_input.member_constraints),
            now=now,
        )
        findings = [finding for rule in self.registry.rules for finding in rule.evaluate(context)]
        if any(finding.status == AuditStatus.VIOLATED for finding in findings):
            overall_status = AuditStatus.VIOLATED
        elif any(finding.status == AuditStatus.UNKNOWN for finding in findings):
            overall_status = AuditStatus.UNKNOWN
        else:
            overall_status = AuditStatus.SATISFIED

        report_input_hash = compute_report_input_hash(
            workspace_id=run_input.workspace_id,
            task_id=run_input.task_id,
            task_revision=run_input.task_revision,
            itinerary_id=revision.itinerary_id,
            itinerary_revision=revision.revision,
            content_hash=revision.content_hash,
            member_constraint_revisions=run_input.member_constraint_revision_set,
            place_resolution_versions=run_input.place_resolution_versions,
            evidence_snapshot_id=evidence_snapshot.snapshot_id,
            audit_rule_set_version=self.registry.rule_set_version,
        )
        return AuditReport(
            report_id=str(uuid4()),
            workspace_id=run_input.workspace_id,
            itinerary_id=revision.itinerary_id,
            itinerary_revision=revision.revision,
            task_id=run_input.task_id,
            task_revision=run_input.task_revision,
            member_constraint_revision_set=run_input.member_constraint_revision_set,
            evidence_snapshot_id=evidence_snapshot.snapshot_id,
            audit_rule_set_version=self.registry.rule_set_version,
            report_input_hash=report_input_hash,
            overall_status=overall_status,
            findings=findings,
            created_at=now,
            supersedes_report_id=supersedes_report_id,
        )
