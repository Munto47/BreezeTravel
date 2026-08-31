from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Protocol
from uuid import uuid4

from app.trip_understanding.knowledge import (
    KnowledgeClaimCandidate,
    project_knowledge_suggestions,
)
from app.trip_understanding.models import PublicResourceRecord, UserFacingTripResult


def _projection_hash(claim_revision_ids: tuple[str, ...]) -> str:
    payload = json.dumps(list(claim_revision_ids), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class KnowledgeProjectionRepository(Protocol):
    async def project_current_knowledge(
        self,
        resource: PublicResourceRecord,
        result: UserFacingTripResult,
        *,
        now: datetime,
    ) -> UserFacingTripResult: ...


class PostgresKnowledgeProjectionRepositoryMixin:
    async def project_current_knowledge(
        self,
        resource: PublicResourceRecord,
        result: UserFacingTripResult,
        *,
        now: datetime,
    ) -> UserFacingTripResult:
        if resource.current_result_id is None:
            return result
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            binding_rows = await conn.fetch(
                """
                SELECT a.public_activity_token, a.canonical_place_id, r.revision
                FROM trip_understanding_results r
                JOIN trip_understanding_activities a
                  ON a.understanding_id = r.understanding_id
                 AND a.revision = r.revision
                WHERE r.result_id = $1 AND r.understanding_id = $2
                  AND a.role = 'PLANNED'
                """,
                resource.current_result_id,
                resource.understanding_id,
            )
            if not binding_rows:
                return result
            revision = int(binding_rows[0]["revision"])
            bindings = {
                row["public_activity_token"]: row["canonical_place_id"]
                for row in binding_rows
            }
            place_ids = sorted({value for value in bindings.values() if value})
            if not place_ids:
                return result
            rows = await conn.fetch(
                """
                WITH latest_claims AS (
                    SELECT DISTINCT ON (claim_key) *
                    FROM knowledge_claim_versions
                    WHERE effective_at <= $2
                    ORDER BY claim_key, version DESC
                ), latest_sources AS (
                    SELECT DISTINCT ON (source_key) *
                    FROM knowledge_source_versions
                    WHERE reviewed_at <= $2
                    ORDER BY source_key, version DESC
                )
                SELECT
                    claim.claim_revision_id,
                    claim.claim_key,
                    claim.version AS claim_version,
                    claim.canonical_place_id,
                    claim.claim_type,
                    claim.conditions_hash,
                    claim.suggestion_text,
                    claim.short_evidence,
                    claim.effective_at,
                    claim.expires_at,
                    claim_withdrawal.withdrawn_at AS claim_withdrawn_at,
                    source.source_version_id,
                    source.publisher_name AS source_name,
                    source.canonical_url AS source_url,
                    source.observed_at AS source_observed_at,
                    source.expires_at AS source_expires_at,
                    source.admission_status AS source_admission_status,
                    source.license_status AS source_license_status,
                    source_withdrawal.withdrawn_at AS source_withdrawn_at
                FROM latest_claims claim
                JOIN knowledge_source_versions source
                  ON source.source_version_id = claim.source_version_id
                JOIN latest_sources current_source
                  ON current_source.source_key = source.source_key
                 AND current_source.source_version_id = source.source_version_id
                LEFT JOIN knowledge_claim_withdrawals claim_withdrawal
                  ON claim_withdrawal.claim_revision_id = claim.claim_revision_id
                 AND claim_withdrawal.withdrawn_at <= $2
                LEFT JOIN knowledge_source_withdrawals source_withdrawal
                  ON source_withdrawal.source_version_id = source.source_version_id
                 AND source_withdrawal.withdrawn_at <= $2
                WHERE claim.canonical_place_id = ANY($1::text[])
                """,
                place_ids,
                now,
            )
            candidates = [
                KnowledgeClaimCandidate(
                    claim_revision_id=row["claim_revision_id"],
                    claim_key=row["claim_key"],
                    claim_version=int(row["claim_version"]),
                    canonical_place_id=row["canonical_place_id"],
                    claim_type=row["claim_type"],
                    conditions_hash=row["conditions_hash"].strip(),
                    suggestion_text=row["suggestion_text"],
                    short_evidence=row["short_evidence"],
                    effective_at=row["effective_at"],
                    expires_at=row["expires_at"],
                    claim_withdrawn_at=row["claim_withdrawn_at"],
                    source_version_id=row["source_version_id"],
                    source_name=row["source_name"],
                    source_url=row["source_url"],
                    source_observed_at=row["source_observed_at"],
                    source_expires_at=row["source_expires_at"],
                    source_admission_status=row["source_admission_status"],
                    source_license_status=row["source_license_status"],
                    source_withdrawn_at=row["source_withdrawn_at"],
                )
                for row in rows
            ]
            projection = project_knowledge_suggestions(
                result,
                canonical_place_by_activity_token=bindings,
                candidates=candidates,
                now=now,
            )
            if projection.selected_claim_revision_ids:
                projection_hash = _projection_hash(projection.selected_claim_revision_ids)
                await conn.execute(
                    """
                    INSERT INTO knowledge_usage_receipts (
                        usage_receipt_id, understanding_id, result_revision,
                        projection_hash, claim_revision_ids, selected_count, created_at
                    ) VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
                    ON CONFLICT (understanding_id, result_revision, projection_hash) DO NOTHING
                    """,
                    str(uuid4()),
                    resource.understanding_id,
                    revision,
                    projection_hash,
                    json.dumps(list(projection.selected_claim_revision_ids)),
                    len(projection.selected_claim_revision_ids),
                    now,
                )
        return projection.result


class InMemoryKnowledgeProjectionRepositoryMixin:
    def _init_knowledge_store(self) -> None:
        self.knowledge_candidates: list[KnowledgeClaimCandidate] = []
        self.knowledge_usage_receipts: list[dict[str, Any]] = []

    def _delete_knowledge_memory(self, understanding_id: str) -> None:
        self.knowledge_usage_receipts = [
            receipt
            for receipt in self.knowledge_usage_receipts
            if receipt["understanding_id"] != understanding_id
        ]

    async def project_current_knowledge(
        self,
        resource: PublicResourceRecord,
        result: UserFacingTripResult,
        *,
        now: datetime,
    ) -> UserFacingTripResult:
        revision = self.result_revisions.get(resource.current_result_id or "")
        if revision is None:
            return result
        pipeline_input = self.g03_pipeline_inputs.get((resource.understanding_id, revision), {})
        raw_bindings = pipeline_input.get("bindings", {})
        bindings = {
            token: value.get("canonical_place_id")
            for token, value in raw_bindings.items()
            if isinstance(value, dict)
        }
        projection = project_knowledge_suggestions(
            result,
            canonical_place_by_activity_token=bindings,
            candidates=self.knowledge_candidates,
            now=now,
        )
        if projection.selected_claim_revision_ids:
            receipt = {
                "understanding_id": resource.understanding_id,
                "result_revision": revision,
                "projection_hash": _projection_hash(projection.selected_claim_revision_ids),
                "claim_revision_ids": projection.selected_claim_revision_ids,
            }
            if receipt not in self.knowledge_usage_receipts:
                self.knowledge_usage_receipts.append(receipt)
        return projection.result
