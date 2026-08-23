from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import ValidationError

from app.audit.errors import AuditInputStaleError
from app.audit.registry import AuditRuleRegistry
from app.audit.models import (
    AuditFinding,
    AuditReport,
    EvidenceFact,
    EvidenceSnapshot,
    ProviderFailure,
)
from app.db.connection import get_pool
from app.itineraries.errors import CurrentAuditRequiredError
from app.itineraries.hash_service import compute_report_input_hash, sha256_canonical
from app.itineraries.models import ItineraryRevision, TripWorkspace
from app.importing.models import ResolvedPlaceReceipt
from app.schemas.task_spec import TripTaskSpec


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _member_constraint_token(report: AuditReport) -> int:
    """Return the workspace-level append revision bound into an audit report."""

    return max(report.member_constraint_revision_set.values(), default=0)


def _assert_current_input_tokens(
    report: AuditReport,
    *,
    itinerary_revision: int | None,
    task_revision: int | None,
    member_constraint_revision: int | None,
) -> None:
    expected_task_revision = report.task_revision
    expected_member_revision = _member_constraint_token(report)
    actual_task_revision = task_revision or 1
    actual_member_revision = member_constraint_revision or 0
    if (
        itinerary_revision != report.itinerary_revision
        or actual_task_revision != expected_task_revision
        or actual_member_revision != expected_member_revision
    ):
        raise AuditInputStaleError(
            "workspace inputs changed during audit",
            context={
                "expected_itinerary_revision": report.itinerary_revision,
                "actual_itinerary_revision": itinerary_revision,
                "expected_task_revision": expected_task_revision,
                "actual_task_revision": actual_task_revision,
                "expected_member_constraint_revision": expected_member_revision,
                "actual_member_constraint_revision": actual_member_revision,
            },
        )


class AuditRepository(Protocol):
    async def save_snapshot(self, snapshot: EvidenceSnapshot) -> EvidenceSnapshot: ...

    async def get_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None: ...

    async def get_latest_snapshot(self, workspace_id: str) -> EvidenceSnapshot | None: ...

    async def save_report(self, report: AuditReport) -> AuditReport: ...

    async def save_audit_bundle(
        self,
        snapshot: EvidenceSnapshot,
        report: AuditReport,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> AuditReport: ...

    async def save_preview_report(self, report: AuditReport) -> AuditReport: ...

    async def save_preview_bundle(
        self,
        snapshot: EvidenceSnapshot,
        report: AuditReport,
        *,
        conn: Any | None = None,
    ) -> AuditReport: ...

    async def get_report(self, report_id: str) -> AuditReport | None: ...

    async def get_latest_report(self, workspace_id: str) -> AuditReport | None: ...

    async def assert_current_confirmation_audit(
        self,
        workspace: TripWorkspace,
        revision: ItineraryRevision,
        *,
        conn: Any | None = None,
    ) -> None: ...

    async def load_place_records(
        self,
        workspace_id: str,
        place_ids: list[str],
        *,
        target_itinerary_revision: int | None = None,
    ) -> dict[str, dict[str, Any]]: ...

    async def load_task_spec(self, workspace_id: str, task_id: str | None = None) -> TripTaskSpec | None: ...


def _fact_from_row(row: Any) -> EvidenceFact:
    return EvidenceFact(
        fact_id=row["fact_id"],
        snapshot_id=row["snapshot_id"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        fact_type=row["fact_type"],
        value=_json_value(row["value_json"]),
        provider=row["provider"],
        source_url=row["source_url"],
        observed_at=row["observed_at"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        response_hash=row["response_hash"].strip(),
        confidence=row["confidence"],
        freshness_status=row["freshness_status"],
    )


def _finding_from_row(row: Any) -> AuditFinding:
    return AuditFinding(
        finding_id=row["finding_id"],
        rule_id=row["rule_id"],
        rule_version=row["rule_version"],
        status=row["status"],
        severity=row["severity"],
        reason_code=row["reason_code"],
        message=row["message"],
        input_values=_json_value(row["input_values_json"]),
        affected_days=list(row["affected_days"] or []),
        affected_stop_ids=list(row["affected_stop_ids"] or []),
        affected_member_ids=list(row["affected_member_ids"] or []),
        evidence_fact_ids=list(row["evidence_fact_ids"] or []),
        repairable=row["repairable"],
        confirmation_action=row["confirmation_action"],
    )


class PostgresAuditRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def save_snapshot(self, snapshot: EvidenceSnapshot) -> EvidenceSnapshot:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await self.get_snapshot_with_conn(conn, snapshot.snapshot_id)
            if existing is not None:
                if sha256_canonical(existing.model_dump(mode="json")) != sha256_canonical(
                    snapshot.model_dump(mode="json")
                ):
                    raise AuditInputStaleError(
                        "evidence snapshot id already exists with different content",
                        context={"snapshot_id": snapshot.snapshot_id},
                    )
                return existing
            await self._insert_snapshot(conn, snapshot)
        return snapshot

    async def _insert_snapshot(self, conn: Any, snapshot: EvidenceSnapshot) -> None:
        await conn.execute(
            """
                INSERT INTO evidence_snapshots (
                    snapshot_id, workspace_id, itinerary_revision, provider_set,
                    policy_version, provider_failures_json, created_at, supersedes_snapshot_id
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8)
                """,
            snapshot.snapshot_id,
            snapshot.workspace_id,
            snapshot.itinerary_revision,
            snapshot.provider_set,
            snapshot.policy_version,
            json.dumps([failure.model_dump(mode="json") for failure in snapshot.provider_failures], ensure_ascii=False),
            snapshot.created_at,
            snapshot.supersedes_snapshot_id,
        )
        for fact in snapshot.facts:
            await conn.execute(
                """
                    INSERT INTO evidence_facts (
                        fact_id, snapshot_id, subject_type, subject_id, fact_type,
                        value_json, provider, source_url, observed_at, valid_from,
                        valid_until, response_hash, confidence, freshness_status
                    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10, $11, $12, $13, $14)
                    """,
                fact.fact_id,
                fact.snapshot_id,
                fact.subject_type,
                fact.subject_id,
                fact.fact_type,
                json.dumps(fact.value, ensure_ascii=False),
                fact.provider,
                fact.source_url,
                fact.observed_at,
                fact.valid_from,
                fact.valid_until,
                fact.response_hash,
                fact.confidence,
                fact.freshness_status.value,
            )

    async def get_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM evidence_snapshots WHERE snapshot_id = $1", snapshot_id)
            if row is None:
                return None
            fact_rows = await conn.fetch(
                "SELECT * FROM evidence_facts WHERE snapshot_id = $1 ORDER BY fact_id",
                snapshot_id,
            )
        return EvidenceSnapshot(
            snapshot_id=row["snapshot_id"],
            workspace_id=row["workspace_id"],
            itinerary_revision=row["itinerary_revision"],
            provider_set=list(row["provider_set"] or []),
            policy_version=row["policy_version"],
            provider_failures=[
                ProviderFailure.model_validate(item) for item in _json_value(row["provider_failures_json"])
            ],
            facts=[_fact_from_row(fact_row) for fact_row in fact_rows],
            created_at=row["created_at"],
            supersedes_snapshot_id=row["supersedes_snapshot_id"],
        )

    async def get_latest_snapshot(self, workspace_id: str) -> EvidenceSnapshot | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            snapshot_id = await conn.fetchval(
                "SELECT snapshot_id FROM evidence_snapshots WHERE workspace_id = $1 ORDER BY created_at DESC LIMIT 1",
                workspace_id,
            )
        return await self.get_snapshot(snapshot_id) if snapshot_id else None

    async def save_report(self, report: AuditReport) -> AuditReport:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            return await self._insert_report(conn, report)

    async def _insert_report(
        self,
        conn: Any,
        report: AuditReport,
        *,
        basis: dict[str, Any] | None = None,
    ) -> AuditReport:
        if True:  # keep all report writes in the caller's transaction
            workspace = await conn.fetchrow(
                "SELECT current_itinerary_revision, current_task_spec_revision, "
                "current_member_constraint_revision, current_report_id "
                "FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                report.workspace_id,
            )
            if basis is not None:
                actual_basis = {
                    "current_itinerary_revision": workspace["current_itinerary_revision"] if workspace else None,
                    "current_task_spec_revision": workspace["current_task_spec_revision"] if workspace else None,
                    "current_member_constraint_revision": (
                        workspace["current_member_constraint_revision"] if workspace else None
                    ),
                    "current_report_id": workspace["current_report_id"] if workspace else None,
                }
                if actual_basis != basis:
                    raise AuditInputStaleError(
                        "workspace inputs changed after idempotent audit claim",
                        context={"expected_basis": basis, "actual_basis": actual_basis},
                    )
            _assert_current_input_tokens(
                report,
                itinerary_revision=workspace["current_itinerary_revision"] if workspace else None,
                task_revision=workspace["current_task_spec_revision"] if workspace else None,
                member_constraint_revision=(workspace["current_member_constraint_revision"] if workspace else None),
            )
            existing_id = await conn.fetchval(
                "SELECT report_id FROM audit_reports WHERE workspace_id = $1 AND report_input_hash = $2",
                report.workspace_id,
                report.report_input_hash,
            )
            if existing_id:
                existing = await self._get_report_with_conn(conn, existing_id)
                if existing is None:
                    raise RuntimeError("audit report hash exists without readable report")
                return existing
            supersedes = (
                await conn.fetchval(
                    "SELECT report_id FROM audit_reports WHERE report_id = $1",
                    workspace["current_report_id"],
                )
                if workspace["current_report_id"]
                else None
            )
            stored = report.model_copy(update={"supersedes_report_id": supersedes})
            await conn.execute(
                """
                INSERT INTO audit_reports (
                    report_id, workspace_id, itinerary_id, itinerary_revision, task_id,
                    task_revision, member_constraint_revision_set, evidence_snapshot_id,
                    audit_rule_set_version, report_input_hash, overall_status, created_at,
                    supersedes_report_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13)
                """,
                stored.report_id,
                stored.workspace_id,
                stored.itinerary_id,
                stored.itinerary_revision,
                stored.task_id,
                stored.task_revision,
                json.dumps(stored.member_constraint_revision_set, ensure_ascii=False),
                stored.evidence_snapshot_id,
                stored.audit_rule_set_version,
                stored.report_input_hash,
                stored.overall_status.value,
                stored.created_at,
                stored.supersedes_report_id,
            )
            for finding in stored.findings:
                await conn.execute(
                    """
                    INSERT INTO audit_findings (
                        finding_id, report_id, rule_id, rule_version, status, severity,
                        reason_code, message, input_values_json, affected_days, affected_stop_ids,
                        affected_member_ids, evidence_fact_ids, repairable, confirmation_action
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14, $15)
                    """,
                    finding.finding_id,
                    stored.report_id,
                    finding.rule_id,
                    finding.rule_version,
                    finding.status.value,
                    finding.severity.value,
                    finding.reason_code,
                    finding.message,
                    json.dumps(finding.input_values, ensure_ascii=False),
                    finding.affected_days,
                    finding.affected_stop_ids,
                    finding.affected_member_ids,
                    finding.evidence_fact_ids,
                    finding.repairable,
                    finding.confirmation_action,
                )
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_report_id = $2, status = 'NEEDS_CONFIRMATION', updated_at = NOW()
                WHERE workspace_id = $1
                """,
                stored.workspace_id,
                stored.report_id,
            )
            return stored

    async def save_audit_bundle(
        self,
        snapshot: EvidenceSnapshot,
        report: AuditReport,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> AuditReport:
        if conn is not None:
            await self._insert_snapshot(conn, snapshot)
            return await self._insert_report(conn, report, basis=basis)
        pool = await self._get_pool()
        async with pool.acquire() as acquired, acquired.transaction():
            await self._insert_snapshot(acquired, snapshot)
            return await self._insert_report(acquired, report, basis=basis)

    async def save_preview_report(self, report: AuditReport) -> AuditReport:
        """Persist a full postcheck for an immutable repair preview without advancing workspace state."""

        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing_id = await conn.fetchval(
                "SELECT report_id FROM audit_reports WHERE workspace_id = $1 AND report_input_hash = $2",
                report.workspace_id,
                report.report_input_hash,
            )
            if existing_id:
                existing = await self._get_report_with_conn(conn, existing_id)
                if existing is None:
                    raise RuntimeError("audit report hash exists without readable report")
                return existing
            await conn.execute(
                """
                INSERT INTO audit_reports (
                    report_id, workspace_id, itinerary_id, itinerary_revision, task_id,
                    task_revision, member_constraint_revision_set, evidence_snapshot_id,
                    audit_rule_set_version, report_input_hash, overall_status, created_at,
                    supersedes_report_id
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13)
                """,
                report.report_id,
                report.workspace_id,
                report.itinerary_id,
                report.itinerary_revision,
                report.task_id,
                report.task_revision,
                json.dumps(report.member_constraint_revision_set, ensure_ascii=False),
                report.evidence_snapshot_id,
                report.audit_rule_set_version,
                report.report_input_hash,
                report.overall_status.value,
                report.created_at,
                report.supersedes_report_id,
            )
            for finding in report.findings:
                await conn.execute(
                    """
                    INSERT INTO audit_findings (
                        finding_id, report_id, rule_id, rule_version, status, severity,
                        reason_code, message, input_values_json, affected_days, affected_stop_ids,
                        affected_member_ids, evidence_fact_ids, repairable, confirmation_action
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14, $15)
                    """,
                    finding.finding_id,
                    report.report_id,
                    finding.rule_id,
                    finding.rule_version,
                    finding.status.value,
                    finding.severity.value,
                    finding.reason_code,
                    finding.message,
                    json.dumps(finding.input_values, ensure_ascii=False),
                    finding.affected_days,
                    finding.affected_stop_ids,
                    finding.affected_member_ids,
                    finding.evidence_fact_ids,
                    finding.repairable,
                    finding.confirmation_action,
                )
        return report

    async def save_preview_bundle(
        self,
        snapshot: EvidenceSnapshot,
        report: AuditReport,
        *,
        conn: Any | None = None,
    ) -> AuditReport:
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as acquired, acquired.transaction():
                return await self.save_preview_bundle(snapshot, report, conn=acquired)
        await self._insert_snapshot(conn, snapshot)
        existing_id = await conn.fetchval(
            "SELECT report_id FROM audit_reports WHERE workspace_id = $1 AND report_input_hash = $2",
            report.workspace_id,
            report.report_input_hash,
        )
        if existing_id:
            existing = await self._get_report_with_conn(conn, existing_id)
            if existing is None:
                raise RuntimeError("preview report hash exists without readable report")
            return existing
        await conn.execute(
            """
            INSERT INTO audit_reports (
                report_id, workspace_id, itinerary_id, itinerary_revision, task_id,
                task_revision, member_constraint_revision_set, evidence_snapshot_id,
                audit_rule_set_version, report_input_hash, overall_status, created_at,
                supersedes_report_id
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12, $13)
            """,
            report.report_id,
            report.workspace_id,
            report.itinerary_id,
            report.itinerary_revision,
            report.task_id,
            report.task_revision,
            json.dumps(report.member_constraint_revision_set, ensure_ascii=False),
            report.evidence_snapshot_id,
            report.audit_rule_set_version,
            report.report_input_hash,
            report.overall_status.value,
            report.created_at,
            report.supersedes_report_id,
        )
        for finding in report.findings:
            await conn.execute(
                """
                INSERT INTO audit_findings (
                    finding_id, report_id, rule_id, rule_version, status, severity,
                    reason_code, message, input_values_json, affected_days, affected_stop_ids,
                    affected_member_ids, evidence_fact_ids, repairable, confirmation_action
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11, $12, $13, $14, $15)
                """,
                finding.finding_id,
                report.report_id,
                finding.rule_id,
                finding.rule_version,
                finding.status.value,
                finding.severity.value,
                finding.reason_code,
                finding.message,
                json.dumps(finding.input_values, ensure_ascii=False),
                finding.affected_days,
                finding.affected_stop_ids,
                finding.affected_member_ids,
                finding.evidence_fact_ids,
                finding.repairable,
                finding.confirmation_action,
            )
        return report

    async def _get_report_with_conn(self, conn: Any, report_id: str) -> AuditReport | None:
        row = await conn.fetchrow("SELECT * FROM audit_reports WHERE report_id = $1", report_id)
        if row is None:
            return None
        finding_rows = await conn.fetch(
            "SELECT * FROM audit_findings WHERE report_id = $1 ORDER BY finding_id",
            report_id,
        )
        return AuditReport(
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
            findings=[_finding_from_row(finding_row) for finding_row in finding_rows],
            created_at=row["created_at"],
            supersedes_report_id=row["supersedes_report_id"],
        )

    async def get_report(self, report_id: str) -> AuditReport | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await self._get_report_with_conn(conn, report_id)

    async def get_latest_report(self, workspace_id: str) -> AuditReport | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            report_id = await conn.fetchval(
                "SELECT report_id FROM audit_reports WHERE workspace_id = $1 ORDER BY created_at DESC LIMIT 1",
                workspace_id,
            )
        return await self.get_report(report_id) if report_id else None

    async def assert_current_confirmation_audit(
        self,
        workspace: TripWorkspace,
        revision: ItineraryRevision,
        *,
        conn: Any | None = None,
    ) -> None:
        """Fail closed unless the locked workspace points at a hash-valid full report.

        ``execute_command`` supplies its already-locked transaction connection.
        Therefore a concurrent audit cannot replace ``current_report_id`` after
        this check but before the confirmation mutation is committed.
        """
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as acquired:
                await self.assert_current_confirmation_audit(workspace, revision, conn=acquired)
                return
        report_id = workspace.current_report_id
        if not report_id:
            raise CurrentAuditRequiredError(
                "final confirmation requires a current full audit report",
                context={"reason": "CURRENT_REPORT_MISSING", "current_revision": revision.revision},
            )
        report = await self._get_report_with_conn(conn, report_id)
        if report is None:
            raise CurrentAuditRequiredError(
                "current audit report is missing",
                context={"reason": "CURRENT_REPORT_NOT_FOUND", "report_id": report_id},
            )
        snapshot = await self.get_snapshot_with_conn(conn, report.evidence_snapshot_id)
        self._assert_confirmation_audit_valid(workspace, revision, report, snapshot)

    @staticmethod
    def _assert_confirmation_audit_valid(
        workspace: TripWorkspace,
        revision: ItineraryRevision,
        report: AuditReport,
        snapshot: EvidenceSnapshot | None,
    ) -> None:
        """Validate full report/snapshot binding and recompute its immutable input hash."""
        reason: str | None = None
        if report.workspace_id != workspace.workspace_id or report.itinerary_id != revision.itinerary_id:
            reason = "REPORT_SCOPE_MISMATCH"
        elif report.itinerary_revision != revision.revision:
            reason = "REPORT_REVISION_STALE"
        elif report.task_revision != (workspace.current_task_spec_revision or 1):
            reason = "REPORT_TASK_STALE"
        elif _member_constraint_token(report) != (workspace.current_member_constraint_revision or 0):
            reason = "REPORT_MEMBER_CONSTRAINT_STALE"
        elif snapshot is None:
            reason = "EVIDENCE_SNAPSHOT_MISSING"
        elif snapshot.workspace_id != workspace.workspace_id or snapshot.itinerary_revision != revision.revision:
            reason = "EVIDENCE_SNAPSHOT_STALE"
        elif report.audit_rule_set_version != AuditRuleRegistry().rule_set_version:
            reason = "AUDIT_RULE_SET_STALE"
        if reason is not None:
            raise CurrentAuditRequiredError(
                "final confirmation requires a current full audit report",
                context={
                    "reason": reason,
                    "report_id": report.report_id,
                    "current_revision": revision.revision,
                    "report_revision": report.itinerary_revision,
                },
            )
        expected_hash = compute_report_input_hash(
            workspace_id=workspace.workspace_id,
            task_id=report.task_id,
            task_revision=report.task_revision,
            itinerary_id=revision.itinerary_id,
            itinerary_revision=revision.revision,
            content_hash=revision.content_hash,
            member_constraint_revisions=report.member_constraint_revision_set,
            place_resolution_versions={stop.place_id: 1 for day in revision.days for stop in day.stops},
            evidence_snapshot_id=snapshot.snapshot_id,
            audit_rule_set_version=report.audit_rule_set_version,
        )
        if expected_hash != report.report_input_hash:
            raise CurrentAuditRequiredError(
                "current audit report input hash does not match the current itinerary",
                context={
                    "reason": "REPORT_INPUT_HASH_MISMATCH",
                    "report_id": report.report_id,
                    "current_revision": revision.revision,
                },
            )

    async def get_snapshot_with_conn(self, conn: Any, snapshot_id: str) -> EvidenceSnapshot | None:
        row = await conn.fetchrow("SELECT * FROM evidence_snapshots WHERE snapshot_id = $1", snapshot_id)
        if row is None:
            return None
        fact_rows = await conn.fetch(
            "SELECT * FROM evidence_facts WHERE snapshot_id = $1 ORDER BY fact_id",
            snapshot_id,
        )
        return EvidenceSnapshot(
            snapshot_id=row["snapshot_id"],
            workspace_id=row["workspace_id"],
            itinerary_revision=row["itinerary_revision"],
            provider_set=list(row["provider_set"] or []),
            policy_version=row["policy_version"],
            provider_failures=[
                ProviderFailure.model_validate(item) for item in _json_value(row["provider_failures_json"])
            ],
            facts=[_fact_from_row(fact_row) for fact_row in fact_rows],
            created_at=row["created_at"],
            supersedes_snapshot_id=row["supersedes_snapshot_id"],
        )

    @staticmethod
    def _target_stop_map(days_json: Any) -> dict[str, set[str]]:
        """Return place -> stop ids from the exact revision being audited."""

        result: dict[str, set[str]] = {}
        for day in _json_value(days_json) or []:
            for stop in day.get("stops", []):
                place_id = stop.get("place_id")
                stop_id = stop.get("stop_id")
                if place_id and stop_id:
                    result.setdefault(str(place_id), set()).add(str(stop_id))
        return result

    @staticmethod
    def _valid_immutable_receipt(row: Any) -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Fail closed when a pre-constraint/externally-corrupted row is encountered."""

        receipt = _json_value(row["receipt_json"])
        place_data = _json_value(row["place_data_json"])
        if not isinstance(receipt, dict) or not isinstance(place_data, dict):
            return None
        if receipt.get("canonical_place_id") != row["place_id"]:
            return None
        if place_data.get("resolved_place_receipt") != receipt:
            return None
        if sha256_canonical(receipt) != str(row["receipt_hash"]).strip():
            return None
        try:
            resolved = ResolvedPlaceReceipt.model_validate(receipt)
        except ValidationError:
            return None
        coords = place_data.get("coords")
        if not isinstance(coords, dict):
            return None
        if (
            place_data.get("place_id") != resolved.canonical_place_id
            or place_data.get("name") != resolved.name
            or place_data.get("city") != resolved.city
            or coords.get("lng") != resolved.longitude
            or coords.get("lat") != resolved.latitude
        ):
            return None
        return receipt, place_data

    async def load_place_records(
        self,
        workspace_id: str,
        place_ids: list[str],
        *,
        target_itinerary_revision: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not place_ids:
            return {}
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            workspace = await conn.fetchrow(
                "SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id = $1",
                workspace_id,
            )
            if workspace is None or workspace["current_itinerary_revision"] is None:
                return {}
            current_revision = int(workspace["current_itinerary_revision"])
            target_revision = (
                current_revision
                if target_itinerary_revision is None
                else target_itinerary_revision
            )
            target_row = await conn.fetchrow(
                """
                SELECT days_json
                FROM itinerary_revisions
                WHERE workspace_id = $1 AND revision = $2
                """,
                workspace_id,
                target_revision,
            )
            if target_row is None:
                return {}
            target_stop_map = self._target_stop_map(target_row["days_json"])
            target_place_ids = set(place_ids) & set(target_stop_map)
            if not target_place_ids:
                return {}
            receipt_rows = await conn.fetch(
                """
                WITH RECURSIVE lineage AS (
                    SELECT revision, parent_revision, 0 AS depth
                    FROM itinerary_revisions
                    WHERE workspace_id = $1 AND revision = $3
                    UNION ALL
                    SELECT parent.revision, parent.parent_revision, lineage.depth + 1
                    FROM itinerary_revisions parent
                    JOIN lineage ON parent.revision = lineage.parent_revision
                    WHERE parent.workspace_id = $1
                )
                SELECT ipr.place_id, ipr.stop_id, ipr.itinerary_revision,
                       ipr.place_data_json, ipr.receipt_json, ipr.receipt_hash,
                       ipr.created_at, lineage.depth
                FROM itinerary_place_receipts ipr
                JOIN lineage ON lineage.revision = ipr.itinerary_revision
                WHERE ipr.workspace_id = $1 AND ipr.place_id = ANY($2::text[])
                ORDER BY ipr.place_id, lineage.depth, ipr.stop_id
                """,
                workspace_id,
                sorted(target_place_ids),
                target_revision,
            )
        result: dict[str, dict[str, Any]] = {}
        # Rows are nearest-ancestor first.  Prefer a receipt attached to a stop
        # still present in the target map when several rows exist at the same
        # revision, but allow a same-place ancestor receipt to survive a stop
        # id change made by a later edit.
        valid_rows: dict[str, list[tuple[Any, dict[str, Any], dict[str, Any]]]] = {}
        for row in receipt_rows:
            valid = self._valid_immutable_receipt(row)
            if valid is None:
                continue
            receipt, place_data = valid
            valid_rows.setdefault(row["place_id"], []).append((row, receipt, place_data))
        for place_id, candidates in valid_rows.items():
            row, receipt, place_data = min(
                candidates,
                key=lambda item: (
                    item[0]["depth"],
                    0 if item[0]["stop_id"] in target_stop_map[place_id] else 1,
                    item[0]["stop_id"],
                ),
            )
            record = dict(place_data)
            record["resolved_place_receipt"] = receipt
            record["receipt_hash"] = row["receipt_hash"].strip()
            record["immutable_receipt"] = True
            record["receipt_itinerary_revision"] = row["itinerary_revision"]
            record["receipt_stop_id"] = row["stop_id"]
            record.setdefault("updated_at", row["created_at"])
            result[place_id] = record

        # room_places is a mutable current projection.  It is safe only for a
        # current-revision compatibility read and can never replace a valid
        # immutable receipt selected above.  Historical reads without a
        # lineage receipt deliberately become UNKNOWN.
        if target_revision == current_revision:
            missing_ids = sorted(target_place_ids - set(result))
            if missing_ids:
                async with pool.acquire() as conn:
                    room_rows = await conn.fetch(
                        """
                        SELECT rp.place_id, rp.place_data, rp.updated_at
                        FROM room_places rp
                        JOIN trip_workspaces tw ON tw.room_id = rp.room_id
                        WHERE tw.workspace_id = $1 AND rp.place_id = ANY($2::text[])
                        """,
                        workspace_id,
                        missing_ids,
                    )
                for row in room_rows:
                    record = dict(_json_value(row["place_data"]) or {})
                    record.setdefault("updated_at", row["updated_at"])
                    result[row["place_id"]] = record
        return result

    async def load_task_spec(self, workspace_id: str, task_id: str | None = None) -> TripTaskSpec | None:
        pool = await self._get_pool()
        query = """
            SELECT t.spec_json
            FROM trip_task_specs t
            JOIN trip_workspaces tw ON tw.room_id = t.room_id
            WHERE tw.workspace_id = $1
        """
        args: list[Any] = [workspace_id]
        if task_id:
            query += " AND t.task_id = $2"
            args.append(task_id)
        query += " ORDER BY t.task_revision DESC LIMIT 1"
        async with pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
        return TripTaskSpec.model_validate(_json_value(row["spec_json"])) if row else None


class InMemoryAuditRepository:
    def __init__(
        self,
        workspaces: dict[str, Any] | None = None,
        *,
        place_records: dict[str, dict[str, dict[str, Any]]] | None = None,
        immutable_place_records: dict[str, dict[int, dict[str, dict[str, Any]]]] | None = None,
        revision_parents: dict[str, dict[int, int | None]] | None = None,
        revision_stop_maps: dict[str, dict[int, dict[str, str]]] | None = None,
    ):
        self.workspaces = workspaces
        self.snapshots: dict[str, EvidenceSnapshot] = {}
        self.reports: dict[str, AuditReport] = {}
        self.current_revisions: dict[str, int] = {}
        self.current_task_revisions: dict[str, int] = {}
        self.current_member_constraint_revisions: dict[str, int] = {}
        self.current_reports: dict[str, str | None] = {}
        self.place_records = place_records if place_records is not None else {}
        # Test/local parity for itinerary_place_receipts.  Records are keyed
        # workspace -> revision -> stop_id and contain place_id, receipt_json,
        # place_data_json, receipt_hash, and optionally created_at.
        self.immutable_place_records = immutable_place_records if immutable_place_records is not None else {}
        self.revision_parents = revision_parents if revision_parents is not None else {}
        self.revision_stop_maps = revision_stop_maps if revision_stop_maps is not None else {}
        self.task_specs: dict[str, TripTaskSpec] = {}

    async def save_snapshot(self, snapshot: EvidenceSnapshot) -> EvidenceSnapshot:
        existing = self.snapshots.get(snapshot.snapshot_id)
        if existing is not None:
            if sha256_canonical(existing.model_dump(mode="json")) != sha256_canonical(
                snapshot.model_dump(mode="json")
            ):
                raise AuditInputStaleError(
                    "evidence snapshot id already exists with different content",
                    context={"snapshot_id": snapshot.snapshot_id},
                )
            return existing
        self.snapshots[snapshot.snapshot_id] = snapshot
        return snapshot

    async def get_snapshot(self, snapshot_id: str) -> EvidenceSnapshot | None:
        return self.snapshots.get(snapshot_id)

    async def get_latest_snapshot(self, workspace_id: str) -> EvidenceSnapshot | None:
        candidates = [item for item in self.snapshots.values() if item.workspace_id == workspace_id]
        return max(candidates, key=lambda item: item.created_at) if candidates else None

    async def save_report(self, report: AuditReport) -> AuditReport:
        workspace = self.workspaces.get(report.workspace_id) if self.workspaces is not None else None
        _assert_current_input_tokens(
            report,
            itinerary_revision=(
                workspace.current_itinerary_revision
                if workspace is not None
                else self.current_revisions.get(report.workspace_id)
            ),
            task_revision=(
                workspace.current_task_spec_revision
                if workspace is not None
                else self.current_task_revisions.get(report.workspace_id)
            ),
            member_constraint_revision=(
                workspace.current_member_constraint_revision
                if workspace is not None
                else self.current_member_constraint_revisions.get(report.workspace_id)
            ),
        )
        for existing in self.reports.values():
            if existing.workspace_id == report.workspace_id and existing.report_input_hash == report.report_input_hash:
                return existing
        supersedes = self.current_reports.get(report.workspace_id)
        stored = report.model_copy(update={"supersedes_report_id": supersedes})
        self.reports[stored.report_id] = stored
        self.current_reports[stored.workspace_id] = stored.report_id
        if self.workspaces is not None and workspace is not None:
            self.workspaces[stored.workspace_id] = workspace.model_copy(
                update={
                    "current_report_id": stored.report_id,
                }
            )
        return stored

    async def save_audit_bundle(
        self,
        snapshot: EvidenceSnapshot,
        report: AuditReport,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> AuditReport:
        workspace = self.workspaces.get(report.workspace_id) if self.workspaces is not None else None
        actual_basis = {
            "current_itinerary_revision": (
                workspace.current_itinerary_revision
                if workspace is not None
                else self.current_revisions.get(report.workspace_id)
            ),
            "current_task_spec_revision": (
                workspace.current_task_spec_revision
                if workspace is not None
                else self.current_task_revisions.get(report.workspace_id)
            ),
            "current_member_constraint_revision": (
                workspace.current_member_constraint_revision
                if workspace is not None
                else self.current_member_constraint_revisions.get(report.workspace_id)
            ),
            # The workspace revision command invalidates ``current_report_id``
            # before a fresh bundle is created.  In-memory parity must read the
            # same authoritative workspace token as PostgreSQL, rather than a
            # stale convenience index left by an older report.
            "current_report_id": (
                workspace.current_report_id
                if workspace is not None
                else self.current_reports.get(report.workspace_id)
            ),
        }
        if actual_basis != basis:
            raise AuditInputStaleError(
                "workspace inputs changed after idempotent audit claim",
                context={"expected_basis": basis, "actual_basis": actual_basis},
            )
        self.snapshots[snapshot.snapshot_id] = snapshot
        return await self.save_report(report)

    async def save_preview_report(self, report: AuditReport) -> AuditReport:
        for existing in self.reports.values():
            if existing.workspace_id == report.workspace_id and existing.report_input_hash == report.report_input_hash:
                return existing
        self.reports[report.report_id] = report
        return report

    async def save_preview_bundle(
        self,
        snapshot: EvidenceSnapshot,
        report: AuditReport,
        *,
        conn: Any | None = None,
    ) -> AuditReport:
        self.snapshots[snapshot.snapshot_id] = snapshot
        return await self.save_preview_report(report)

    async def get_report(self, report_id: str) -> AuditReport | None:
        return self.reports.get(report_id)

    async def get_latest_report(self, workspace_id: str) -> AuditReport | None:
        report_id = self.current_reports.get(workspace_id)
        return self.reports.get(report_id) if report_id else None

    async def assert_current_confirmation_audit(
        self,
        workspace: TripWorkspace,
        revision: ItineraryRevision,
        *,
        conn: Any | None = None,
    ) -> None:
        report_id = workspace.current_report_id
        if not report_id:
            raise CurrentAuditRequiredError(
                "final confirmation requires a current full audit report",
                context={"reason": "CURRENT_REPORT_MISSING", "current_revision": revision.revision},
            )
        report = self.reports.get(report_id)
        if report is None:
            raise CurrentAuditRequiredError(
                "current audit report is missing",
                context={"reason": "CURRENT_REPORT_NOT_FOUND", "report_id": report_id},
            )
        PostgresAuditRepository._assert_confirmation_audit_valid(
            workspace,
            revision,
            report,
            self.snapshots.get(report.evidence_snapshot_id),
        )

    async def load_place_records(
        self,
        workspace_id: str,
        place_ids: list[str],
        *,
        target_itinerary_revision: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        workspace = self.workspaces.get(workspace_id) if self.workspaces is not None else None
        current_revision = (
            workspace.current_itinerary_revision
            if workspace is not None
            else self.current_revisions.get(workspace_id)
        )
        target_revision = (
            current_revision
            if target_itinerary_revision is None
            else target_itinerary_revision
        )
        if target_revision is None:
            return {}
        requested = set(place_ids)
        target_map = self.revision_stop_maps.get(workspace_id, {}).get(target_revision)
        if target_map is not None:
            target_stop_map: dict[str, set[str]] = {}
            for stop_id, place_id in target_map.items():
                target_stop_map.setdefault(place_id, set()).add(stop_id)
            requested &= set(target_stop_map)
        else:
            # Existing in-memory callers predate immutable receipt fixtures.
            # They remain compatible, while explicit revision fixtures can
            # opt into exact target-map filtering above.
            target_stop_map = {place_id: set() for place_id in requested}

        lineage: list[int] = []
        seen: set[int] = set()
        cursor: int | None = target_revision
        parent_map = self.revision_parents.get(workspace_id, {})
        while cursor is not None and cursor not in seen:
            lineage.append(cursor)
            seen.add(cursor)
            cursor = parent_map.get(cursor)

        result: dict[str, dict[str, Any]] = {}
        immutable = self.immutable_place_records.get(workspace_id, {})
        for revision in lineage:
            candidates: dict[str, list[tuple[str, dict[str, Any], dict[str, Any]]]] = {}
            for stop_id, raw in immutable.get(revision, {}).items():
                place_id = raw.get("place_id")
                if place_id not in requested or place_id in result:
                    continue
                row = {
                    "place_id": place_id,
                    "receipt_json": raw.get("receipt_json"),
                    "place_data_json": raw.get("place_data_json"),
                    "receipt_hash": raw.get("receipt_hash", ""),
                }
                valid = PostgresAuditRepository._valid_immutable_receipt(row)
                if valid is not None:
                    receipt, place_data = valid
                    candidates.setdefault(place_id, []).append((stop_id, receipt, place_data))
            for place_id, rows in candidates.items():
                stop_id, receipt, place_data = min(
                    rows,
                    key=lambda item: (
                        0 if item[0] in target_stop_map.get(place_id, set()) else 1,
                        item[0],
                    ),
                )
                record = dict(place_data)
                record["resolved_place_receipt"] = receipt
                record["receipt_hash"] = sha256_canonical(receipt)
                record["immutable_receipt"] = True
                record["receipt_itinerary_revision"] = revision
                record["receipt_stop_id"] = stop_id
                if raw_created_at := immutable[revision][stop_id].get("created_at"):
                    record.setdefault("updated_at", raw_created_at)
                result[place_id] = record

        # As in PostgreSQL, the mutable projection is a compatibility source
        # only for the current revision and never overwrites an ancestor row.
        if target_revision == current_revision:
            records = self.place_records.get(workspace_id, {})
            for place_id in requested - set(result):
                if place_id in records:
                    result[place_id] = records[place_id]
        return result

    async def load_task_spec(self, workspace_id: str, task_id: str | None = None) -> TripTaskSpec | None:
        spec = self.task_specs.get(workspace_id)
        if spec and (task_id is None or spec.task_id == task_id):
            return spec
        return None
