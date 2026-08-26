from __future__ import annotations

import json
from typing import Any, Protocol

from app.db.connection import get_pool
from app.itineraries.errors import ResourceNotFound, TipsInputConflictError, TipsNotEligibleError
from app.itineraries.tips_models import FinalTipsArtifact


class FinalTipsRepository(Protocol):
    async def get_by_report(self, report_id: str) -> FinalTipsArtifact | None: ...

    async def save(self, artifact: FinalTipsArtifact) -> FinalTipsArtifact: ...

    async def save_with_basis(
        self,
        artifact: FinalTipsArtifact,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> FinalTipsArtifact: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresFinalTipsRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    @staticmethod
    def _from_row(row: Any) -> FinalTipsArtifact:
        return FinalTipsArtifact(
            report_id=row["report_id"],
            workspace_id=row["workspace_id"],
            itinerary_revision=row["itinerary_revision"],
            basis_content_hash=row["basis_content_hash"],
            generation_input_hash=row["generation_input_hash"].strip(),
            artifact_hash=row["artifact_hash"],
            itinerary=_json_value(row["itinerary_json"]),
            created_at=row["created_at"],
        )

    async def get_by_report(self, report_id: str) -> FinalTipsArtifact | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM final_tips_artifacts WHERE report_id = $1",
                report_id,
            )
        return self._from_row(row) if row else None

    async def save(self, artifact: FinalTipsArtifact) -> FinalTipsArtifact:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            return await self._save_with_conn(conn, artifact)

    async def _save_with_conn(self, conn: Any, artifact: FinalTipsArtifact) -> FinalTipsArtifact:
        if True:  # execute inside the caller-owned transaction
            await conn.execute(
                """
                INSERT INTO final_tips_artifacts (
                    report_id, workspace_id, itinerary_revision,
                    basis_content_hash, generation_input_hash, artifact_hash,
                    itinerary_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
                ON CONFLICT (report_id) DO NOTHING
                """,
                artifact.report_id,
                artifact.workspace_id,
                artifact.itinerary_revision,
                artifact.basis_content_hash,
                artifact.generation_input_hash,
                artifact.artifact_hash,
                json.dumps(artifact.itinerary.model_dump(mode="json"), ensure_ascii=False),
                artifact.created_at,
            )
            row = await conn.fetchrow(
                "SELECT * FROM final_tips_artifacts WHERE report_id = $1",
                artifact.report_id,
            )
        if row is None:
            raise RuntimeError("final tips artifact was not persisted")
        stored = self._from_row(row)
        if stored.generation_input_hash != artifact.generation_input_hash:
            raise TipsInputConflictError("tips already exist for this report with different generation input")
        return stored

    async def save_with_basis(
        self,
        artifact: FinalTipsArtifact,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> FinalTipsArtifact:
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as acquired, acquired.transaction():
                return await self.save_with_basis(artifact, basis=basis, conn=acquired)
        workspace = await conn.fetchrow(
            """
            SELECT current_itinerary_revision, current_report_id
            FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE
            """,
            artifact.workspace_id,
        )
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        actual_basis = {
            "current_itinerary_revision": workspace["current_itinerary_revision"],
            "current_report_id": workspace["current_report_id"],
        }
        if actual_basis != basis:
            raise TipsNotEligibleError(
                "tips basis changed during generation",
                context={"expected_basis": basis, "actual_basis": actual_basis},
            )
        return await self._save_with_conn(conn, artifact)


class InMemoryFinalTipsRepository:
    def __init__(self):
        self.artifacts: dict[str, FinalTipsArtifact] = {}

    async def get_by_report(self, report_id: str) -> FinalTipsArtifact | None:
        return self.artifacts.get(report_id)

    async def save(self, artifact: FinalTipsArtifact) -> FinalTipsArtifact:
        stored = self.artifacts.setdefault(artifact.report_id, artifact)
        if stored.generation_input_hash != artifact.generation_input_hash:
            raise TipsInputConflictError("tips already exist for this report with different generation input")
        return stored

    async def save_with_basis(
        self,
        artifact: FinalTipsArtifact,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> FinalTipsArtifact:
        return await self.save(artifact)
