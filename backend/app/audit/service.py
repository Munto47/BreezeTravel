from __future__ import annotations

from datetime import datetime, timezone

from app.audit.engine import AuditEngine
from app.audit.evidence_service import EvidenceObservation, EvidenceService
from app.audit.models import AuditReport, AuditRunInput, EvidenceSnapshot, ProviderFailure
from app.audit.repositories import AuditRepository
from app.audit.system_constraints import with_system_constraints
from app.itineraries.errors import ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripWorkspace
from app.itineraries.repositories import ItineraryRepository
from app.members.repositories import MemberConstraintRepository
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository
from app.schemas.task_spec import DateRange, TripTaskSpec


class AuditApplicationService:
    def __init__(
        self,
        *,
        itinerary_repository: ItineraryRepository,
        audit_repository: AuditRepository,
        evidence_service: EvidenceService | None = None,
        engine: AuditEngine | None = None,
        member_constraint_repository: MemberConstraintRepository | None = None,
    ):
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.evidence_service = evidence_service or EvidenceService()
        self.engine = engine or AuditEngine()
        self.member_constraint_repository = member_constraint_repository

    async def run_current_audit(
        self,
        workspace_id: str,
        *,
        task_id: str | None = None,
        provider_failures: list[ProviderFailure] | None = None,
        extra_observations: list[EvidenceObservation] | None = None,
        evidence_observations: list[EvidenceObservation] | None = None,
        now: datetime | None = None,
    ) -> AuditReport:
        _, snapshot, report, _ = await self._prepare_current_audit(
            workspace_id,
            task_id=task_id,
            provider_failures=provider_failures,
            extra_observations=extra_observations,
            evidence_observations=evidence_observations,
            now=now,
        )
        await self.audit_repository.save_snapshot(snapshot)
        return await self.audit_repository.save_report(report)

    async def prepare_current_evidence(
        self,
        workspace_id: str,
        *,
        task_id: str | None = None,
        provider_failures: list[ProviderFailure] | None = None,
        extra_observations: list[EvidenceObservation] | None = None,
        evidence_observations: list[EvidenceObservation] | None = None,
        snapshot_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[TripWorkspace, EvidenceSnapshot, dict]:
        """Prepare one replayable Evidence stage without creating an Audit report."""

        del task_id  # Reserved by the public audit contract; evidence itself is task-agnostic.
        now = now or datetime.now(timezone.utc)
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.current_itinerary_revision is None:
            raise ResourceNotFound("workspace does not have an itinerary revision")
        revision = await self.itinerary_repository.get_revision(
            workspace_id,
            workspace.current_itinerary_revision,
        )
        if revision is None:
            raise ResourceNotFound("current itinerary revision does not exist")

        place_ids = sorted({stop.place_id for day in revision.days for stop in day.stops})
        place_records = await self.audit_repository.load_place_records(
            workspace_id,
            place_ids,
            target_itinerary_revision=revision.revision,
        )
        observations = (
            evidence_observations
            if evidence_observations is not None
            else self.evidence_service.observations_from_revision(
                revision,
                place_records,
                now=now,
                target_itinerary_revision=revision.revision,
            )
        )
        observations = list(observations)
        observations.extend(extra_observations or [])
        latest_snapshot = await self.audit_repository.get_latest_snapshot(workspace_id)
        supersedes_snapshot_id = None
        if latest_snapshot is not None:
            supersedes_snapshot_id = (
                latest_snapshot.supersedes_snapshot_id
                if latest_snapshot.snapshot_id == snapshot_id
                else latest_snapshot.snapshot_id
            )
        snapshot = self.evidence_service.create_snapshot(
            workspace_id=workspace_id,
            itinerary_revision=revision.revision,
            observations=observations,
            provider_failures=provider_failures,
            supersedes_snapshot_id=supersedes_snapshot_id,
            snapshot_id=snapshot_id,
            now=now,
        )
        return workspace, snapshot, self._workspace_basis(workspace)

    async def prepare_report_from_snapshot(
        self,
        workspace_id: str,
        snapshot: EvidenceSnapshot,
        *,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[AuditReport, dict]:
        """Build an Audit report from the exact persisted Evidence snapshot."""

        now = now or datetime.now(timezone.utc)
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.current_itinerary_revision != snapshot.itinerary_revision:
            from app.audit.errors import AuditInputStaleError

            raise AuditInputStaleError(
                "evidence snapshot no longer binds the current itinerary revision",
                context={
                    "snapshot_revision": snapshot.itinerary_revision,
                    "current_revision": workspace.current_itinerary_revision,
                },
            )
        revision = await self.itinerary_repository.get_revision(workspace_id, snapshot.itinerary_revision)
        if revision is None:
            raise ResourceNotFound("current itinerary revision does not exist")
        task_spec = await self.audit_repository.load_task_spec(workspace_id, task_id)
        if task_spec is None:
            task_spec = self._default_task_spec(workspace)
        task_spec = with_system_constraints(task_spec)
        if self.member_constraint_repository is not None and workspace.current_member_constraint_revision is not None:
            effective_constraints = await self.member_constraint_repository.list_effective_constraints(
                workspace_id,
                workspace.current_member_constraint_revision,
            )
            member_revisions = {item.constraint_id: item.revision for item in effective_constraints}
        else:
            effective_constraints = []
            member_revisions = (
                {"workspace": workspace.current_member_constraint_revision}
                if workspace.current_member_constraint_revision is not None
                else {}
            )
        latest_report = await self.audit_repository.get_latest_report(workspace_id)
        if (
            latest_report is not None
            and latest_report.evidence_snapshot_id == snapshot.snapshot_id
            and latest_report.itinerary_revision == snapshot.itinerary_revision
        ):
            return latest_report, self._workspace_basis(workspace)
        report = self.engine.run(
            run_input=AuditRunInput(
                workspace_id=workspace_id,
                itinerary_revision=revision.revision,
                task_id=task_spec.task_id,
                task_revision=task_spec.task_revision,
                member_constraint_revision_set=member_revisions,
                member_constraints=effective_constraints,
                place_resolution_versions={stop.place_id: 1 for day in revision.days for stop in day.stops},
            ),
            revision=revision,
            task_spec=task_spec,
            evidence_snapshot=snapshot,
            supersedes_report_id=latest_report.report_id if latest_report else None,
            now=now,
        )
        return report, self._workspace_basis(workspace)

    async def run_current_audit_idempotent(
        self,
        workspace_id: str,
        *,
        operation: CreationOperation,
        target_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_body: dict,
        command_repository: CreationCommandRepository,
        task_id: str | None = None,
        provider_failures: list[ProviderFailure] | None = None,
        extra_observations: list[EvidenceObservation] | None = None,
        evidence_observations: list[EvidenceObservation] | None = None,
        now: datetime | None = None,
    ) -> tuple[AuditReport, bool]:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        basis = self._workspace_basis(workspace)
        request_hash = sha256_canonical(
            {
                "schema_version": 1,
                "operation": operation.value,
                "workspace_id": workspace_id,
                "target_id": target_id,
                "actor_user_id": actor_user_id,
                "body": request_body,
            }
        )
        claim = await command_repository.claim(
            workspace_id=workspace_id,
            operation=operation,
            target_id=target_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            basis=basis,
        )
        if claim.replay is not None:
            return AuditReport.model_validate(claim.replay.body), True
        try:
            _, snapshot, report, prepared_basis = await self._prepare_current_audit(
                workspace_id,
                task_id=task_id,
                provider_failures=provider_failures,
                extra_observations=extra_observations,
                evidence_observations=evidence_observations,
                now=now,
            )
            if prepared_basis != claim.basis:
                from app.audit.errors import AuditInputStaleError

                raise AuditInputStaleError(
                    "workspace inputs changed before audit preparation",
                    context={"expected_basis": claim.basis, "actual_basis": prepared_basis},
                )

            async def finalize(conn, stored_basis):
                stored = await self.audit_repository.save_audit_bundle(
                    snapshot,
                    report,
                    basis=stored_basis,
                    conn=conn,
                )
                return CreationCommandResponse(
                    status_code=200,
                    body=stored.model_dump(mode="json"),
                    headers={},
                )

            response = await command_repository.finalize(claim, finalize)
            return AuditReport.model_validate(response.body), response.idempotent_replay
        except Exception:
            await command_repository.abandon(claim)
            raise

    async def _prepare_current_audit(
        self,
        workspace_id: str,
        *,
        task_id: str | None = None,
        provider_failures: list[ProviderFailure] | None = None,
        extra_observations: list[EvidenceObservation] | None = None,
        evidence_observations: list[EvidenceObservation] | None = None,
        now: datetime | None = None,
    ) -> tuple[TripWorkspace, EvidenceSnapshot, AuditReport, dict]:
        now = now or datetime.now(timezone.utc)
        workspace, snapshot, evidence_basis = await self.prepare_current_evidence(
            workspace_id,
            task_id=task_id,
            provider_failures=provider_failures,
            extra_observations=extra_observations,
            evidence_observations=evidence_observations,
            now=now,
        )
        report, report_basis = await self.prepare_report_from_snapshot(
            workspace_id,
            snapshot,
            task_id=task_id,
            now=now,
        )
        if report_basis != evidence_basis:
            from app.audit.errors import AuditInputStaleError

            raise AuditInputStaleError(
                "workspace inputs changed between evidence and audit preparation",
                context={"evidence_basis": evidence_basis, "report_basis": report_basis},
            )
        return workspace, snapshot, report, report_basis

    @staticmethod
    def _workspace_basis(workspace: TripWorkspace) -> dict:
        return {
            "current_itinerary_revision": workspace.current_itinerary_revision,
            "current_task_spec_revision": workspace.current_task_spec_revision,
            "current_member_constraint_revision": workspace.current_member_constraint_revision,
            "current_report_id": workspace.current_report_id,
        }

    @staticmethod
    def _default_task_spec(workspace: TripWorkspace) -> TripTaskSpec:
        days = (workspace.trip_date_range.end - workspace.trip_date_range.start).days + 1
        return TripTaskSpec(
            task_id=f"workspace:{workspace.workspace_id}:default",
            room_id=workspace.room_id,
            task_revision=workspace.current_task_spec_revision or 1,
            city=workspace.city,
            date_range=DateRange(start=workspace.trip_date_range.start, days=days),
        )
