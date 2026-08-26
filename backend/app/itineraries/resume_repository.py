from __future__ import annotations

import json
from typing import Any, Protocol

from app.audit.models import (
    AuditReport,
    AuditSeverity,
    AuditStatus,
    EvidenceSnapshot,
    ProviderFailure,
)
from app.audit.repositories import _fact_from_row, _finding_from_row
from app.db.connection import get_pool
from app.importing.models import ImportStatus, ItineraryImport
from app.importing.repositories import _import_from_row
from app.itineraries.models import ItineraryRevision, TripWorkspace
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.repositories import _revision_from_row, _workspace_from_row
from app.itineraries.resume_models import TipsState, WorkspaceResume, WorkspaceWriteETags
from app.itineraries.tips_models import FinalTipsArtifact
from app.itineraries.tips_repositories import PostgresFinalTipsRepository
from app.repairs.models import RepairOperation, RepairOption
from app.repairs.objective import repair_option_sort_key
from app.trip_check.models import (
    AdviceBundle,
    TripBriefRevision,
    TripCheckRun,
    TripCheckRunStatus,
    TripCheckStage,
)
from app.trip_check.runs import _run_from_row


class WorkspaceResumeNotFound(Exception):
    """Deliberately hides whether a workspace is absent or merely unauthorized."""


class WorkspaceStateInconsistent(Exception):
    code = "WORKSPACE_STATE_INCONSISTENT"


class WorkspaceResumeRepository(Protocol):
    async def get_resume(self, workspace_id: str, user_id: str) -> WorkspaceResume: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _inconsistent(message: str) -> WorkspaceStateInconsistent:
    return WorkspaceStateInconsistent(message)


class PostgresWorkspaceResumeRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def _after_workspace_read(self, conn: Any, workspace: TripWorkspace) -> None:
        """Test seam for proving that all subsequent reads share this snapshot."""

    async def get_resume(self, workspace_id: str, user_id: str) -> WorkspaceResume:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction(isolation="repeatable_read", readonly=True):
                row = await conn.fetchrow(
                    """
                    SELECT workspace.*
                    FROM trip_workspaces AS workspace
                    JOIN room_members AS member
                      ON member.room_id = workspace.room_id
                     AND member.user_id = $2
                    WHERE workspace.workspace_id = $1
                    """,
                    workspace_id,
                    user_id,
                )
                if row is None:
                    raise WorkspaceResumeNotFound
                workspace = _workspace_from_row(row)
                await self._after_workspace_read(conn, workspace)
                try:
                    return await self._load_resume(conn, workspace)
                except WorkspaceStateInconsistent:
                    raise
                except (KeyError, TypeError, ValueError) as exc:
                    raise _inconsistent("workspace state cannot be reconstructed") from exc

    async def _load_resume(self, conn: Any, workspace: TripWorkspace) -> WorkspaceResume:
        workspace_id = workspace.workspace_id
        revision = await self._load_revision(conn, workspace)
        itinerary_import = await self._load_import(conn, workspace)
        brief = await self._load_brief(conn, workspace)
        trip_check_run, advice = await self._load_trip_check_run(conn, workspace)
        report, evidence = await self._load_report_and_evidence(conn, workspace, revision)
        proposed_repairs = await self._load_proposed_repairs(conn, workspace_id, report)
        applied_repair = await self._load_applied_repair(
            conn,
            workspace_id,
            report,
            revision,
        )
        tips = await self._load_tips(conn, workspace_id, report, revision)
        tips_state = self._tips_state(report, tips)

        return WorkspaceResume(
            workspace=workspace,
            current_revision=revision,
            current_import=itinerary_import,
            current_brief=brief,
            current_trip_check_run=trip_check_run,
            current_advice=advice,
            current_report=report,
            current_evidence=evidence,
            proposed_repairs=proposed_repairs,
            applied_repair=applied_repair,
            current_tips=tips,
            tips_state=tips_state,
            write_etags=WorkspaceWriteETags(
                itinerary=f'"{revision.revision}"' if revision is not None else None,
                import_=f'"{itinerary_import.state_version}"' if itinerary_import is not None else None,
            ),
        )

    async def _load_brief(
        self,
        conn: Any,
        workspace: TripWorkspace,
    ) -> TripBriefRevision | None:
        brief_id = workspace.current_brief_id
        brief_revision = workspace.current_trip_brief_revision
        if brief_id is None and brief_revision is None:
            return None
        if brief_id is None or brief_revision is None:
            raise _inconsistent("workspace brief pointer is incomplete")
        row = await conn.fetchrow(
            """
            SELECT content_json
            FROM trip_brief_revisions
            WHERE workspace_id = $1 AND brief_id = $2 AND revision = $3
            """,
            workspace.workspace_id,
            brief_id,
            brief_revision,
        )
        if row is None:
            raise _inconsistent("workspace points to a missing TripBrief revision")
        brief = TripBriefRevision.model_validate(_json_value(row["content_json"]))
        if brief.workspace_id != workspace.workspace_id or brief.brief_id != brief_id:
            raise _inconsistent("current TripBrief does not belong to the workspace")
        return brief

    async def _load_trip_check_run(
        self,
        conn: Any,
        workspace: TripWorkspace,
    ) -> tuple[TripCheckRun | None, AdviceBundle | None]:
        run_id = workspace.current_trip_check_run_id
        if run_id is None:
            return None, None
        row = await conn.fetchrow(
            "SELECT * FROM trip_check_runs WHERE workspace_id = $1 AND run_id = $2",
            workspace.workspace_id,
            run_id,
        )
        if row is None:
            raise _inconsistent("workspace points to a missing TripCheck run")
        run = _run_from_row(row)
        if run.advice_bundle_id is None:
            return run, None
        advice_json = await conn.fetchval(
            """
            SELECT bundle_json
            FROM advice_bundles
            WHERE workspace_id = $1 AND run_id = $2 AND advice_bundle_id = $3
            """,
            workspace.workspace_id,
            run.run_id,
            run.advice_bundle_id,
        )
        if advice_json is None:
            raise _inconsistent("TripCheck run points to a missing Advice bundle")
        advice = AdviceBundle.model_validate(_json_value(advice_json))
        postchecked = run.stage == TripCheckStage.POSTCHECK and run.status == TripCheckRunStatus.SUCCEEDED
        if postchecked:
            lineage = await conn.fetchrow(
                """
                SELECT source_report_id, postcheck_report_id, postcheck_snapshot_id
                FROM trip_check_postcheck_lineage
                WHERE run_id = $1 AND advice_bundle_id = $2
                """,
                run.run_id,
                advice.advice_bundle_id,
            )
            if (
                lineage is None
                or lineage["source_report_id"] != advice.report_id
                or lineage["postcheck_report_id"] != run.report_id
                or lineage["postcheck_snapshot_id"] != run.evidence_snapshot_id
            ):
                raise _inconsistent("TripCheck postcheck lineage does not match the current run")
        elif advice.report_id != run.report_id or advice.evidence_snapshot_id != run.evidence_snapshot_id:
            raise _inconsistent("TripCheck Advice lineage does not match the run")
        return run, advice

    async def _load_revision(
        self,
        conn: Any,
        workspace: TripWorkspace,
    ) -> ItineraryRevision | None:
        current_revision = workspace.current_itinerary_revision
        if current_revision is None:
            if workspace.current_report_id is not None:
                raise _inconsistent("workspace report has no current itinerary revision")
            return None
        row = await conn.fetchrow(
            """
            SELECT *
            FROM itinerary_revisions
            WHERE workspace_id = $1 AND revision = $2
            """,
            workspace.workspace_id,
            current_revision,
        )
        if row is None:
            raise _inconsistent("workspace points to a missing itinerary revision")
        return _revision_from_row(row)

    async def _load_import(
        self,
        conn: Any,
        workspace: TripWorkspace,
    ) -> ItineraryImport | None:
        import_id = workspace.current_import_id
        if import_id is None:
            return None
        row = await conn.fetchrow(
            """
            SELECT *
            FROM itinerary_imports
            WHERE workspace_id = $1 AND import_id = $2
            """,
            workspace.workspace_id,
            import_id,
        )
        if row is None:
            raise _inconsistent("workspace points to a missing itinerary import")
        resolution_rows = await conn.fetch(
            """
            SELECT resolution.*
            FROM itinerary_stop_resolutions AS resolution
            JOIN itinerary_imports AS imported
              ON imported.import_id = resolution.import_id
             AND imported.workspace_id = $1
            WHERE imported.workspace_id = $1 AND imported.import_id = $2
            ORDER BY resolution.day_index NULLS LAST, resolution.raw_stop_id
            """,
            workspace.workspace_id,
            import_id,
        )
        result = _import_from_row(row, list(resolution_rows))
        if result.status == ImportStatus.APPLIED:
            if result.applied_revision is None:
                raise _inconsistent("applied import is missing its applied revision")
            if (
                workspace.current_itinerary_revision is None
                or result.applied_revision > workspace.current_itinerary_revision
            ):
                raise _inconsistent("applied import revision is ahead of the workspace")
        elif result.applied_revision is not None:
            raise _inconsistent("non-applied import has an applied revision")
        return result

    async def _load_report_and_evidence(
        self,
        conn: Any,
        workspace: TripWorkspace,
        revision: ItineraryRevision | None,
    ) -> tuple[AuditReport | None, EvidenceSnapshot | None]:
        report_id = workspace.current_report_id
        if report_id is None:
            return None, None
        if revision is None:
            raise _inconsistent("workspace report has no current itinerary revision")

        row = await conn.fetchrow(
            """
            SELECT *
            FROM audit_reports
            WHERE workspace_id = $1 AND report_id = $2
            """,
            workspace.workspace_id,
            report_id,
        )
        if row is None:
            raise _inconsistent("workspace points to a missing audit report")
        finding_rows = await conn.fetch(
            """
            SELECT finding.*
            FROM audit_findings AS finding
            JOIN audit_reports AS report
              ON report.report_id = finding.report_id
             AND report.workspace_id = $1
            WHERE report.workspace_id = $1 AND report.report_id = $2
            ORDER BY finding.finding_id
            """,
            workspace.workspace_id,
            report_id,
        )
        report = AuditReport(
            report_id=row["report_id"],
            workspace_id=row["workspace_id"],
            itinerary_id=row["itinerary_id"],
            itinerary_revision=row["itinerary_revision"],
            task_id=row["task_id"],
            task_revision=row["task_revision"],
            member_constraint_revision_set=_json_value(row["member_constraint_revision_set"]),
            evidence_snapshot_id=row["evidence_snapshot_id"],
            audit_rule_set_version=row["audit_rule_set_version"],
            report_input_hash=row["report_input_hash"].strip(),
            overall_status=row["overall_status"],
            findings=[_finding_from_row(item) for item in finding_rows],
            created_at=row["created_at"],
            supersedes_report_id=row["supersedes_report_id"],
        )
        if (
            report.itinerary_revision != revision.revision
            or report.itinerary_id != revision.itinerary_id
        ):
            raise _inconsistent("current audit report does not match the current revision")

        snapshot_row = await conn.fetchrow(
            """
            SELECT *
            FROM evidence_snapshots
            WHERE workspace_id = $1
              AND snapshot_id = $2
              AND itinerary_revision = $3
            """,
            workspace.workspace_id,
            report.evidence_snapshot_id,
            revision.revision,
        )
        if snapshot_row is None:
            raise _inconsistent("current audit report points to missing or stale evidence")
        fact_rows = await conn.fetch(
            """
            SELECT fact.*
            FROM evidence_facts AS fact
            JOIN evidence_snapshots AS snapshot
              ON snapshot.snapshot_id = fact.snapshot_id
             AND snapshot.workspace_id = $1
            WHERE snapshot.workspace_id = $1 AND snapshot.snapshot_id = $2
            ORDER BY fact.fact_id
            """,
            workspace.workspace_id,
            report.evidence_snapshot_id,
        )
        evidence = EvidenceSnapshot(
            snapshot_id=snapshot_row["snapshot_id"],
            workspace_id=snapshot_row["workspace_id"],
            itinerary_revision=snapshot_row["itinerary_revision"],
            provider_set=list(snapshot_row["provider_set"] or []),
            policy_version=snapshot_row["policy_version"],
            facts=[_fact_from_row(item) for item in fact_rows],
            provider_failures=[
                ProviderFailure.model_validate(item)
                for item in (_json_value(snapshot_row["provider_failures_json"]) or [])
            ],
            created_at=snapshot_row["created_at"],
            supersedes_snapshot_id=snapshot_row["supersedes_snapshot_id"],
        )
        fact_ids = {fact.fact_id for fact in evidence.facts}
        if any(not set(finding.evidence_fact_ids).issubset(fact_ids) for finding in report.findings):
            raise _inconsistent("audit findings reference evidence outside the current snapshot")
        return report, evidence

    async def _load_proposed_repairs(
        self,
        conn: Any,
        workspace_id: str,
        report: AuditReport | None,
    ) -> list[RepairOption]:
        if report is None:
            return []
        rows = await conn.fetch(
            """
            SELECT repair.*, postcheck.workspace_id AS postcheck_workspace_id,
                   postcheck.itinerary_revision AS postcheck_revision
            FROM repair_options AS repair
            JOIN audit_reports AS source
              ON source.report_id = repair.source_report_id
             AND source.workspace_id = $1
            LEFT JOIN audit_reports AS postcheck
              ON postcheck.report_id = repair.postcheck_report_id
             AND postcheck.workspace_id = $1
            WHERE source.workspace_id = $1
              AND repair.source_report_id = $2
              AND repair.status = 'PROPOSED'
            ORDER BY repair.repair_id
            """,
            workspace_id,
            report.report_id,
        )
        options = [await self._repair_from_row(conn, workspace_id, row) for row in rows]
        finding_ids = {finding.finding_id for finding in report.findings}
        for option, row in zip(options, rows):
            if (
                option.base_itinerary_revision != report.itinerary_revision
                or not set(option.targeted_finding_ids).issubset(finding_ids)
                or option.result_preview.workspace_id != workspace_id
                or option.result_preview.revision != option.base_itinerary_revision + 1
                or row["postcheck_workspace_id"] != workspace_id
                or row["postcheck_revision"] != option.result_preview.revision
            ):
                raise _inconsistent("proposed repair lineage does not match the current report")
        return sorted(options, key=repair_option_sort_key)

    async def _load_applied_repair(
        self,
        conn: Any,
        workspace_id: str,
        report: AuditReport | None,
        revision: ItineraryRevision | None,
    ) -> RepairOption | None:
        if report is None or revision is None:
            return None
        rows = await conn.fetch(
            """
            SELECT repair.*, source.workspace_id AS source_workspace_id,
                   postcheck.workspace_id AS postcheck_workspace_id,
                   postcheck.itinerary_revision AS postcheck_revision
            FROM repair_options AS repair
            JOIN audit_reports AS source
              ON source.report_id = repair.source_report_id
             AND source.workspace_id = $1
            JOIN audit_reports AS postcheck
              ON postcheck.report_id = repair.postcheck_report_id
             AND postcheck.workspace_id = $1
            WHERE source.workspace_id = $1
              AND postcheck.workspace_id = $1
              AND repair.postcheck_report_id = $2
              AND repair.status = 'APPLIED'
            ORDER BY repair.repair_id
            """,
            workspace_id,
            report.report_id,
        )
        if len(rows) > 1:
            raise _inconsistent("multiple applied repairs point to the current report")
        if not rows:
            return None
        row = rows[0]
        option = await self._repair_from_row(conn, workspace_id, row)
        if (
            row["source_workspace_id"] != workspace_id
            or row["postcheck_workspace_id"] != workspace_id
            or row["postcheck_revision"] != revision.revision
            or option.result_preview.workspace_id != workspace_id
            or option.result_preview.revision != revision.revision
            or option.result_preview.content_hash != revision.content_hash
            or option.base_itinerary_revision + 1 != revision.revision
        ):
            raise _inconsistent("applied repair lineage does not match the current revision")
        return option

    async def _repair_from_row(self, conn: Any, workspace_id: str, row: Any) -> RepairOption:
        operation_rows = await conn.fetch(
            """
            SELECT operation.*
            FROM repair_operations AS operation
            JOIN repair_options AS repair
              ON repair.repair_id = operation.repair_id
            JOIN audit_reports AS source
              ON source.report_id = repair.source_report_id
             AND source.workspace_id = $1
            WHERE source.workspace_id = $1 AND operation.repair_id = $2
            ORDER BY operation.operation_index
            """,
            workspace_id,
            row["repair_id"],
        )
        operations: list[RepairOperation] = []
        for operation_row in operation_rows:
            payload = _json_value(operation_row["payload_json"]) or {}
            operations.append(
                RepairOperation(
                    operation=operation_row["operation"],
                    payload=payload.get("payload", {}),
                    rationale=payload.get("rationale", ""),
                )
            )
        try:
            return RepairOption(
                repair_id=row["repair_id"],
                source_report_id=row["source_report_id"],
                base_itinerary_revision=row["base_itinerary_revision"],
                operations=operations,
                targeted_finding_ids=list(row["targeted_finding_ids"] or []),
                edit_cost=row["edit_cost"],
                risk_cost=row["risk_cost"],
                route_cost_delta=row["route_cost_delta"],
                new_unknown_count=row["new_unknown_count"],
                tradeoffs=list(_json_value(row["tradeoffs_json"]) or []),
                affected_member_ids=list(row["affected_member_ids"] or []),
                result_preview=_json_value(row["result_preview_json"]),
                postcheck_report_id=row["postcheck_report_id"],
                status=row["status"],
                decided_by=row["decided_by"],
                decision_reason=row["decision_reason"],
                decided_at=row["decided_at"],
                created_at=row["created_at"],
            )
        except ValueError as exc:
            raise _inconsistent("repair option is structurally inconsistent") from exc

    async def _load_tips(
        self,
        conn: Any,
        workspace_id: str,
        report: AuditReport | None,
        revision: ItineraryRevision | None,
    ) -> FinalTipsArtifact | None:
        if report is None or revision is None:
            return None
        row = await conn.fetchrow(
            """
            SELECT *
            FROM final_tips_artifacts
            WHERE workspace_id = $1
              AND report_id = $2
              AND itinerary_revision = $3
            """,
            workspace_id,
            report.report_id,
            revision.revision,
        )
        if row is None:
            return None
        tips = PostgresFinalTipsRepository._from_row(row)
        expected_basis_hash = sha256_canonical({
            "revision_content_hash": revision.content_hash,
            "report_input_hash": report.report_input_hash,
        })
        if (
            tips.basis_content_hash != expected_basis_hash
            or tips.itinerary_revision != revision.revision
        ):
            raise _inconsistent("tips artifact does not match the current revision")
        blocking = self._has_blocking_findings(report)
        if blocking:
            raise _inconsistent("tips artifact exists for an ineligible audit report")
        return tips

    @classmethod
    def _tips_state(
        cls,
        report: AuditReport | None,
        tips: FinalTipsArtifact | None,
    ) -> TipsState:
        if report is None:
            return TipsState.NOT_APPLICABLE
        if cls._has_blocking_findings(report):
            return TipsState.INELIGIBLE
        if tips is None:
            return TipsState.NOT_GENERATED
        return TipsState.READY

    @staticmethod
    def _has_blocking_findings(report: AuditReport) -> bool:
        return any(
            finding.status == AuditStatus.VIOLATED
            and finding.severity in {AuditSeverity.BLOCKER, AuditSeverity.HIGH}
            for finding in report.findings
        )
