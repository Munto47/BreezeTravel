from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.agents.nodes.tips_generator import generate_tips
from app.audit.models import AuditSeverity, AuditStatus
from app.audit.repositories import AuditRepository
from app.itineraries.adapters import revision_to_legacy
from app.itineraries.errors import ResourceNotFound, TipsInputConflictError, TipsNotEligibleError
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.repositories import ItineraryRepository
from app.itineraries.tips_models import FinalTipsArtifact
from app.itineraries.tips_repositories import FinalTipsRepository
from app.operations.models import CreationCommandResponse, CreationOperation
from app.operations.repositories import CreationCommandRepository
from app.schemas.itinerary import Itinerary


TipsGenerator = Callable[[Itinerary, str], Awaitable[Itinerary]]


class FinalTipsService:
    def __init__(
        self,
        *,
        itinerary_repository: ItineraryRepository,
        audit_repository: AuditRepository,
        tips_repository: FinalTipsRepository,
        generator: TipsGenerator | None = None,
    ):
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.tips_repository = tips_repository
        self.generator = generator or self._generate

    @staticmethod
    async def _generate(itinerary: Itinerary, preferences: str) -> Itinerary:
        return await generate_tips(itinerary, preferences=preferences)

    async def generate_for_report(
        self,
        report_id: str,
        *,
        preferences: str = "",
        now: datetime | None = None,
        persist: bool = True,
    ) -> FinalTipsArtifact:
        generation_input_hash = sha256_canonical(
            {
                "report_id": report_id,
                "preferences": preferences,
            }
        )
        existing = await self.tips_repository.get_by_report(report_id)
        if existing is not None:
            if existing.generation_input_hash != generation_input_hash:
                raise TipsInputConflictError("tips already exist for this report with different generation input")
            return existing

        report = await self.audit_repository.get_report(report_id)
        if report is None:
            raise ResourceNotFound("audit report does not exist")
        workspace = await self.itinerary_repository.get_workspace(report.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if (
            workspace.current_itinerary_revision != report.itinerary_revision
            or workspace.current_report_id != report.report_id
        ):
            raise TipsNotEligibleError(
                "tips require the current itinerary revision and current full audit report",
                context={
                    "report_revision": report.itinerary_revision,
                    "current_revision": workspace.current_itinerary_revision,
                    "current_report_id": workspace.current_report_id,
                },
            )
        revision = await self.itinerary_repository.get_revision(
            report.workspace_id,
            report.itinerary_revision,
        )
        if revision is None or revision.itinerary_id != report.itinerary_id:
            raise ResourceNotFound("audit revision does not exist")

        blocking = [
            finding
            for finding in report.findings
            if finding.status == AuditStatus.VIOLATED
            and finding.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
        ]
        if blocking:
            raise TipsNotEligibleError(
                "tips are deferred until BLOCKER/HIGH findings are repaired or confirmed",
                context={"blocking_finding_ids": [item.finding_id for item in blocking]},
            )

        snapshot = await self.audit_repository.get_snapshot(report.evidence_snapshot_id)
        if snapshot is None:
            raise ResourceNotFound("audit evidence snapshot does not exist")
        place_lookup: dict[str, dict] = {}
        for fact in snapshot.facts:
            if fact.subject_type == "PLACE" and fact.fact_type == "POI_IDENTITY" and isinstance(fact.value, dict):
                place_lookup.setdefault(fact.subject_id, dict(fact.value))
        itinerary = revision_to_legacy(
            revision,
            thread_id=workspace.room_id,
            place_lookup=place_lookup,
            preserve_unknown_times=True,
        )
        cautions = [
            finding.confirmation_action or finding.message
            for finding in report.findings
            if finding.status == AuditStatus.UNKNOWN
            or (
                finding.status == AuditStatus.VIOLATED and finding.severity in {AuditSeverity.MEDIUM, AuditSeverity.LOW}
            )
        ]
        audit_context = "\n".join(f"- {item}" for item in cautions[:12]) or "- 无待确认项"
        generator_preferences = (
            f"{preferences}\n"
            f"只为 canonical revision {revision.revision} / audit report {report.report_id} 生成提示。\n"
            "不得把 UNKNOWN 写成已确认，也不得与以下审计结论冲突：\n"
            f"{audit_context}"
        ).strip()
        tipped = await self.generator(itinerary, generator_preferences)
        tipped = tipped.model_copy(update={"version": revision.revision})
        basis_content_hash = sha256_canonical(
            {
                "revision_content_hash": revision.content_hash,
                "report_input_hash": report.report_input_hash,
            }
        )
        artifact_hash = sha256_canonical(
            {
                "report_id": report.report_id,
                "workspace_id": report.workspace_id,
                "itinerary_revision": revision.revision,
                "basis_content_hash": basis_content_hash,
                "itinerary": tipped.model_dump(mode="json"),
            }
        )
        artifact = FinalTipsArtifact(
            report_id=report.report_id,
            workspace_id=report.workspace_id,
            itinerary_revision=revision.revision,
            basis_content_hash=basis_content_hash,
            generation_input_hash=generation_input_hash,
            artifact_hash=artifact_hash,
            itinerary=tipped,
            created_at=now or datetime.now(timezone.utc),
        )
        return await self.tips_repository.save(artifact) if persist else artifact

    async def generate_for_report_idempotent(
        self,
        report_id: str,
        *,
        preferences: str,
        actor_user_id: str,
        idempotency_key: str,
        command_repository: CreationCommandRepository,
        now: datetime | None = None,
    ) -> tuple[FinalTipsArtifact, bool]:
        report = await self.audit_repository.get_report(report_id)
        if report is None:
            raise ResourceNotFound("audit report does not exist")
        workspace = await self.itinerary_repository.get_workspace(report.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        basis = {
            "current_itinerary_revision": workspace.current_itinerary_revision,
            "current_report_id": workspace.current_report_id,
        }
        request_hash = sha256_canonical(
            {
                "schema_version": 1,
                "operation": CreationOperation.GENERATE_TIPS.value,
                "workspace_id": report.workspace_id,
                "target_id": report_id,
                "actor_user_id": actor_user_id,
                "body": {"preferences": preferences},
            }
        )
        claim = await command_repository.claim(
            workspace_id=report.workspace_id,
            operation=CreationOperation.GENERATE_TIPS,
            target_id=report_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            basis=basis,
        )
        if claim.replay is not None:
            return FinalTipsArtifact.model_validate(claim.replay.body), True
        try:
            artifact = await self.generate_for_report(
                report_id,
                preferences=preferences,
                now=now,
                persist=False,
            )

            async def finalize(conn, stored_basis):
                stored = await self.tips_repository.save_with_basis(
                    artifact,
                    basis=stored_basis,
                    conn=conn,
                )
                return CreationCommandResponse(
                    status_code=200,
                    body=stored.model_dump(mode="json"),
                    headers={},
                )

            response = await command_repository.finalize(claim, finalize)
            return FinalTipsArtifact.model_validate(response.body), response.idempotent_replay
        except Exception:
            await command_repository.abandon(claim)
            raise
