from __future__ import annotations

import json
from typing import Any, Protocol

from app.db.connection import get_pool
from app.itineraries.errors import ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.trip_check.errors import TripCheckRunConflictError
from app.trip_check.models import AdviceBundle, TripCheckPostcheckLineage


def advice_content_hash(bundle: AdviceBundle) -> str:
    return sha256_canonical(bundle.model_dump(mode="json"))


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class AdviceRepository(Protocol):
    async def save_bundle(self, bundle: AdviceBundle, *, brief_id: str) -> AdviceBundle: ...

    async def get_bundle(self, advice_bundle_id: str) -> AdviceBundle | None: ...

    async def get_bundle_for_report(self, workspace_id: str, report_id: str) -> AdviceBundle | None: ...

    async def get_bundle_for_repair(self, repair_id: str) -> AdviceBundle | None: ...

    async def save_postcheck_lineage(
        self,
        lineage: TripCheckPostcheckLineage,
    ) -> tuple[TripCheckPostcheckLineage, bool]: ...


class PostgresAdviceRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def save_bundle(self, bundle: AdviceBundle, *, brief_id: str) -> AdviceBundle:
        pool = await self._get_pool()
        content_hash = advice_content_hash(bundle)
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                "SELECT bundle_json, content_hash FROM advice_bundles WHERE run_id = $1 FOR UPDATE",
                bundle.run_id,
            )
            if existing is not None:
                if existing["content_hash"].strip() != content_hash:
                    raise TripCheckRunConflictError(
                        "trip check run already has a different Advice bundle",
                        context={"run_id": bundle.run_id},
                    )
                return AdviceBundle.model_validate(_json_value(existing["bundle_json"]))
            await conn.execute(
                """
                INSERT INTO advice_bundles (
                    advice_bundle_id, workspace_id, run_id, report_id,
                    itinerary_revision, brief_id, brief_revision,
                    evidence_snapshot_id, bundle_json, content_hash, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11)
                """,
                bundle.advice_bundle_id,
                bundle.workspace_id,
                bundle.run_id,
                bundle.report_id,
                bundle.itinerary_revision,
                brief_id,
                bundle.brief_revision,
                bundle.evidence_snapshot_id,
                json.dumps(bundle.model_dump(mode="json"), ensure_ascii=False),
                content_hash,
                bundle.created_at,
            )
            for action in bundle.actions:
                await conn.execute(
                    """
                    INSERT INTO advice_actions (
                        advice_bundle_id, advice_id, finding_id, action_text,
                        expected_impact, uncertainty, candidate_set_id,
                        evidence_fact_ids, provider_receipt_ids, route_delta_json,
                        repair_id, tradeoffs_json
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11, $12::jsonb)
                    """,
                    bundle.advice_bundle_id,
                    action.advice_id,
                    action.finding_id,
                    action.action,
                    action.expected_impact,
                    action.uncertainty,
                    action.candidate_set_id,
                    action.evidence_fact_ids,
                    action.provider_receipt_ids,
                    json.dumps(action.route_delta, ensure_ascii=False) if action.route_delta is not None else None,
                    action.repair_id,
                    json.dumps(action.tradeoffs, ensure_ascii=False),
                )
        return bundle

    async def get_bundle(self, advice_bundle_id: str) -> AdviceBundle | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT bundle_json FROM advice_bundles WHERE advice_bundle_id = $1",
                advice_bundle_id,
            )
        return AdviceBundle.model_validate(_json_value(value)) if value is not None else None

    async def get_bundle_for_report(self, workspace_id: str, report_id: str) -> AdviceBundle | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT bundle_json FROM advice_bundles
                WHERE workspace_id = $1 AND report_id = $2
                """,
                workspace_id,
                report_id,
            )
        return AdviceBundle.model_validate(_json_value(value)) if value is not None else None

    async def get_bundle_for_repair(self, repair_id: str) -> AdviceBundle | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                SELECT bundle.bundle_json
                FROM advice_bundles AS bundle
                JOIN advice_actions AS action
                  ON action.advice_bundle_id = bundle.advice_bundle_id
                WHERE action.repair_id = $1
                """,
                repair_id,
            )
        return AdviceBundle.model_validate(_json_value(value)) if value is not None else None

    async def save_postcheck_lineage(
        self,
        lineage: TripCheckPostcheckLineage,
    ) -> tuple[TripCheckPostcheckLineage, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT * FROM trip_check_postcheck_lineage
                WHERE run_id = $1 AND repair_id = $2 FOR UPDATE
                """,
                lineage.run_id,
                lineage.repair_id,
            )
            if existing is not None:
                stored = TripCheckPostcheckLineage(**dict(existing))
                if sha256_canonical(stored.model_dump(mode="json")) != sha256_canonical(
                    lineage.model_dump(mode="json")
                ):
                    raise TripCheckRunConflictError(
                        "repair already has different TripCheck postcheck lineage",
                        context={"run_id": lineage.run_id, "repair_id": lineage.repair_id},
                    )
                return stored, True
            await conn.execute(
                """
                INSERT INTO trip_check_postcheck_lineage (
                    lineage_id, run_id, advice_bundle_id, repair_id,
                    source_report_id, source_itinerary_revision,
                    result_itinerary_revision, postcheck_report_id,
                    postcheck_snapshot_id, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                lineage.lineage_id,
                lineage.run_id,
                lineage.advice_bundle_id,
                lineage.repair_id,
                lineage.source_report_id,
                lineage.source_itinerary_revision,
                lineage.result_itinerary_revision,
                lineage.postcheck_report_id,
                lineage.postcheck_snapshot_id,
                lineage.created_at,
            )
        return lineage, False


class InMemoryAdviceRepository:
    def __init__(self):
        self.bundles: dict[str, AdviceBundle] = {}
        self.run_bundles: dict[str, str] = {}
        self.lineage: dict[tuple[str, str], TripCheckPostcheckLineage] = {}

    async def save_bundle(self, bundle: AdviceBundle, *, brief_id: str) -> AdviceBundle:
        del brief_id
        existing_id = self.run_bundles.get(bundle.run_id)
        if existing_id is not None:
            existing = self.bundles[existing_id]
            if advice_content_hash(existing) != advice_content_hash(bundle):
                raise TripCheckRunConflictError(
                    "trip check run already has a different Advice bundle",
                    context={"run_id": bundle.run_id},
                )
            return existing
        self.bundles[bundle.advice_bundle_id] = bundle
        self.run_bundles[bundle.run_id] = bundle.advice_bundle_id
        return bundle

    async def get_bundle(self, advice_bundle_id: str) -> AdviceBundle | None:
        return self.bundles.get(advice_bundle_id)

    async def get_bundle_for_report(self, workspace_id: str, report_id: str) -> AdviceBundle | None:
        return next(
            (
                item
                for item in self.bundles.values()
                if item.workspace_id == workspace_id and item.report_id == report_id
            ),
            None,
        )

    async def get_bundle_for_repair(self, repair_id: str) -> AdviceBundle | None:
        return next(
            (
                item
                for item in self.bundles.values()
                if any(action.repair_id == repair_id for action in item.actions)
            ),
            None,
        )

    async def save_postcheck_lineage(
        self,
        lineage: TripCheckPostcheckLineage,
    ) -> tuple[TripCheckPostcheckLineage, bool]:
        key = (lineage.run_id, lineage.repair_id)
        existing = self.lineage.get(key)
        if existing is not None:
            if sha256_canonical(existing.model_dump(mode="json")) != sha256_canonical(
                lineage.model_dump(mode="json")
            ):
                raise TripCheckRunConflictError(
                    "repair already has different TripCheck postcheck lineage",
                    context={"run_id": lineage.run_id, "repair_id": lineage.repair_id},
                )
            return existing, True
        self.lineage[key] = lineage
        return lineage, False


async def require_advice_bundle_for_report(
    repository: AdviceRepository,
    *,
    workspace_id: str,
    report_id: str,
) -> AdviceBundle:
    bundle = await repository.get_bundle_for_report(workspace_id, report_id)
    if bundle is None:
        raise ResourceNotFound("Advice bundle does not exist")
    return bundle
