from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from app.trip_understanding.errors import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    JobLeaseLostError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
    ResourceNotReadyError,
    RevisionConflictError,
)
from app.trip_understanding.map_render import MapRenderPlan
from app.trip_understanding.map_repository import plan_with_stay_anchor
from app.trip_understanding.models import (
    PublicResourceRecord,
    StayCandidateView,
    StaySelectionAppliedView,
    StaySelectionOutcome,
    StaySuggestionView,
    StoredResult,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.stay import (
    STAY_POLICY_SHA256,
    StayRecommendationJobRecord,
    StayRecommendationOutput,
    StayRecommendationPlan,
    stay_plan_from_map,
)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate_view(row: Any, *, selected: bool = False) -> StayCandidateView:
    missing = int(row["missing_leg_count"])
    maximum = int(row["max_single_leg_minutes"])
    transfers = int(row["transfer_count"])
    return StayCandidateView(
        candidate_token=row["public_candidate_token"],
        name=row["name"],
        brand=row["brand"],
        category="住宿",
        area_or_address=row["area_or_address"],
        commute_summary=f"全程首末站通勤中，最久一程约 {maximum} 分钟",
        max_single_leg_minutes=maximum,
        transfer_count=transfers,
        evidence_gap=(f"有 {missing} 段路线暂缺完整依据" if missing else None),
        reason=(
            "已作为所有过夜日的住宿"
            if selected
            else f"综合全程往返和换乘后排在前列，共 {transfers} 次换乘"
        ),
        available_actions=[] if selected else ["CHOOSE_STAY"],
        selected=selected,
    )


class StayRecommendationRepository(Protocol):
    async def get_stay_view(self, resource: PublicResourceRecord) -> StaySuggestionView: ...

    async def select_stay(
        self,
        resource: PublicResourceRecord,
        *,
        candidate_token: str,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> StaySelectionOutcome: ...

    async def claim_next_stay(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> StayRecommendationJobRecord | None: ...

    async def renew_stay_lease(
        self,
        job: StayRecommendationJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool: ...

    async def load_stay_plan(self, job: StayRecommendationJobRecord) -> StayRecommendationPlan: ...

    async def complete_stay_job(
        self,
        job: StayRecommendationJobRecord,
        output: StayRecommendationOutput,
        *,
        now: datetime,
    ) -> bool: ...

    async def fail_stay_job(
        self,
        job: StayRecommendationJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None: ...


class PostgresStayRecommendationRepositoryMixin:
    async def _ensure_plan_ref(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
        map_plan: MapRenderPlan | None = None,
    ) -> tuple[Any, Any]:
        if map_plan is None:
            map_plan = await self._read_map_plan(conn, understanding_id, revision)
        await conn.execute(
            """
            INSERT INTO trip_plan_revision_refs (
                plan_ref_id, understanding_id, revision_kind, aggregate_id,
                revision, stop_set_hash, created_at
            ) VALUES ($1, $2, 'UNDERSTANDING', $2, $3, $4, $5)
            ON CONFLICT (understanding_id, revision_kind, aggregate_id, revision) DO NOTHING
            """,
            str(uuid4()),
            understanding_id,
            revision,
            map_plan.plan_ref.stop_set_hash,
            now,
        )
        plan_ref = await conn.fetchrow(
            """
            SELECT * FROM trip_plan_revision_refs
            WHERE understanding_id = $1 AND revision_kind = 'UNDERSTANDING'
              AND aggregate_id = $1 AND revision = $2
            """,
            understanding_id,
            revision,
        )
        if plan_ref is None or plan_ref["stop_set_hash"].strip() != map_plan.plan_ref.stop_set_hash:
            raise IdempotencyConflictError("stay plan revision binding changed")
        return plan_ref, map_plan

    async def _ensure_stay_job(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> Any | None:
        plan_ref, map_plan = await self._ensure_plan_ref(
            conn,
            understanding_id,
            revision,
            now=now,
        )
        stay_plan = stay_plan_from_map(map_plan)
        if stay_plan is None:
            return None
        logical_key = canonical_sha256(
            {
                "plan_ref": stay_plan.plan_ref.model_dump(mode="json"),
                "policy_hash": STAY_POLICY_SHA256,
            }
        )
        await conn.execute(
            """
            INSERT INTO trip_stay_recommendation_jobs (
                stay_job_id, plan_ref_id, understanding_id, policy_hash,
                logical_key_hash, status, available_at, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, 'QUEUED', $6, $6, $6)
            ON CONFLICT (plan_ref_id, policy_hash) DO NOTHING
            """,
            str(uuid4()),
            plan_ref["plan_ref_id"],
            understanding_id,
            STAY_POLICY_SHA256,
            logical_key,
            now,
        )
        return await conn.fetchrow(
            "SELECT * FROM trip_stay_recommendation_jobs WHERE plan_ref_id = $1 AND policy_hash = $2",
            plan_ref["plan_ref_id"],
            STAY_POLICY_SHA256,
        )

    async def _enqueue_initial_stay_job(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> None:
        await self._ensure_stay_job(conn, understanding_id, revision, now=now)

    async def _snapshot_stay_view(self, conn: Any, snapshot: Any) -> StaySuggestionView:
        rows = await conn.fetch(
            """
            SELECT * FROM trip_stay_candidates
            WHERE snapshot_id = $1
            ORDER BY rank LIMIT 3
            """,
            snapshot["snapshot_id"],
        )
        candidates = [_candidate_view(row) for row in rows]
        scopes = _json(snapshot["searched_scopes_json"])
        if snapshot["status"] == "READY":
            return StaySuggestionView(
                status="AVAILABLE",
                message="已按全程首末站通勤准备住宿候选",
                area_summary=snapshot["area_summary"],
                searched_scopes=scopes,
                candidates=candidates,
                available_actions=["CHOOSE_STAY"],
            )
        if candidates:
            return StaySuggestionView(
                status="LIMITED",
                message="已找到可比较的住宿，部分通勤依据暂缺",
                area_summary=snapshot["area_summary"],
                searched_scopes=scopes,
                candidates=candidates,
                available_actions=["CHOOSE_STAY"],
            )
        return StaySuggestionView(
            status="UNAVAILABLE",
            message="暂未找到符合条件的连锁酒店，可以稍后再试",
            area_summary=snapshot["area_summary"],
            searched_scopes=scopes,
        )

    async def _selected_stay_view(self, conn: Any, selection: Any) -> StaySuggestionView:
        candidate = await conn.fetchrow(
            "SELECT * FROM trip_stay_candidates WHERE candidate_id = $1",
            selection["candidate_id"],
        )
        if candidate is None:
            raise ResourceNotFoundError("selected stay candidate is unavailable")
        return StaySuggestionView(
            status="AVAILABLE",
            message=f"整程住宿已选择：{selection['selected_name']}",
            area_summary=selection["selected_address"],
            candidates=[_candidate_view(candidate, selected=True)],
        )

    async def _project_stay_view(
        self,
        conn: Any,
        understanding_id: str,
        revision: int,
    ) -> StaySuggestionView:
        current_ref = await conn.fetchrow(
            """
            SELECT * FROM trip_plan_revision_refs
            WHERE understanding_id = $1 AND revision_kind = 'UNDERSTANDING'
              AND aggregate_id = $1 AND revision = $2
            """,
            understanding_id,
            revision,
        )
        if current_ref is not None:
            selection = await conn.fetchrow(
                "SELECT * FROM trip_stay_selections WHERE target_plan_ref_id = $1",
                current_ref["plan_ref_id"],
            )
            if selection is not None:
                return await self._selected_stay_view(conn, selection)
            current = await conn.fetchrow(
                """
                SELECT j.status AS job_status, s.*
                FROM trip_stay_recommendation_jobs j
                LEFT JOIN trip_stay_recommendation_snapshots s ON s.stay_job_id = j.stay_job_id
                WHERE j.plan_ref_id = $1 AND j.policy_hash = $2
                ORDER BY j.created_at DESC LIMIT 1
                """,
                current_ref["plan_ref_id"],
                STAY_POLICY_SHA256,
            )
            if current is not None:
                if current["job_status"] in {"QUEUED", "BUILDING"}:
                    return StaySuggestionView(
                        status="PREPARING",
                        message="正在按全程首末站准备住宿候选",
                    )
                if current["snapshot_id"] is not None:
                    return await self._snapshot_stay_view(conn, current)
                if current["job_status"] == "UNAVAILABLE":
                    return StaySuggestionView(
                        status="UNAVAILABLE",
                        message="住宿建议暂不可用，不影响查看行程",
                    )
        older = await conn.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM trip_stay_recommendation_jobs WHERE understanding_id = $1
                UNION ALL
                SELECT 1 FROM trip_stay_selections WHERE understanding_id = $1
            )
            """,
            understanding_id,
        )
        if older:
            return StaySuggestionView(
                status="NEEDS_UPDATE",
                message="行程已修改，住宿通勤尚未更新",
            )
        return StaySuggestionView(
            status="UNAVAILABLE",
            message="住宿待选择；需要至少两个有坐标的行程日",
        )

    async def get_stay_view(self, resource: PublicResourceRecord) -> StaySuggestionView:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            aggregate = await conn.fetchrow(
                "SELECT state, current_revision FROM trip_understandings WHERE understanding_id = $1",
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            return await self._project_stay_view(
                conn,
                resource.understanding_id,
                int(aggregate["current_revision"]),
            )

    async def claim_next_stay(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> StayRecommendationJobRecord | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                SELECT j.*, p.revision_kind, p.aggregate_id, p.revision, p.stop_set_hash
                FROM trip_stay_recommendation_jobs j
                JOIN trip_plan_revision_refs p ON p.plan_ref_id = j.plan_ref_id
                WHERE j.attempt < j.max_attempts
                  AND NOT EXISTS (
                    SELECT 1 FROM trip_map_render_jobs map_job
                    WHERE map_job.understanding_id = j.understanding_id
                      AND map_job.status = 'BUILDING'
                      AND map_job.lease_until > $1
                  )
                  AND (
                    (j.status = 'QUEUED' AND j.available_at <= $1)
                    OR (j.status = 'BUILDING' AND j.lease_until <= $1)
                  )
                ORDER BY j.available_at, j.created_at
                FOR UPDATE OF j SKIP LOCKED LIMIT 1
                """,
                now,
            )
            if row is None:
                return None
            started_at = row["started_at"] or now
            updated = await conn.fetchrow(
                """
                UPDATE trip_stay_recommendation_jobs
                SET status = 'BUILDING', lease_owner = $2, lease_until = $3,
                    attempt = attempt + 1, started_at = COALESCE(started_at, $1), updated_at = $1
                WHERE stay_job_id = $4
                RETURNING attempt, max_attempts
                """,
                now,
                worker_id,
                now + timedelta(seconds=lease_seconds),
                row["stay_job_id"],
            )
        return StayRecommendationJobRecord(
            stay_job_id=row["stay_job_id"],
            understanding_id=row["understanding_id"],
            plan_ref_id=row["plan_ref_id"],
            plan_ref={
                "kind": row["revision_kind"],
                "aggregate_id": row["aggregate_id"],
                "revision": row["revision"],
                "stop_set_hash": row["stop_set_hash"],
            },
            policy_hash=row["policy_hash"],
            status="BUILDING",
            lease_owner=worker_id,
            lease_until=now + timedelta(seconds=lease_seconds),
            attempt=updated["attempt"],
            max_attempts=updated["max_attempts"],
            started_at=started_at,
        )

    async def renew_stay_lease(
        self,
        job: StayRecommendationJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            value = await conn.fetchval(
                """
                UPDATE trip_stay_recommendation_jobs
                SET lease_until = $4, updated_at = $3
                WHERE stay_job_id = $1 AND status = 'BUILDING'
                  AND lease_owner = $2 AND attempt = $5 AND lease_until > $3
                RETURNING lease_until
                """,
                job.stay_job_id,
                job.lease_owner,
                now,
                now + timedelta(seconds=lease_seconds),
                job.attempt,
            )
        return value is not None

    async def load_stay_plan(self, job: StayRecommendationJobRecord) -> StayRecommendationPlan:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            map_plan = await self._read_map_plan(
                conn,
                job.understanding_id,
                job.plan_ref.revision,
            )
        plan = stay_plan_from_map(map_plan)
        if plan is None or plan.plan_ref != job.plan_ref:
            raise ResourceNotFoundError("stay recommendation plan does not exist")
        return plan

    async def complete_stay_job(
        self,
        job: StayRecommendationJobRecord,
        output: StayRecommendationOutput,
        *,
        now: datetime,
    ) -> bool:
        if output.plan_ref != job.plan_ref or output.policy_hash != job.policy_hash:
            raise ValueError("stay output is not bound to the claimed plan")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            current = await conn.fetchrow(
                "SELECT * FROM trip_stay_recommendation_jobs WHERE stay_job_id = $1 FOR UPDATE",
                job.stay_job_id,
            )
            existing = await conn.fetchrow(
                "SELECT snapshot_sha256 FROM trip_stay_recommendation_snapshots WHERE stay_job_id = $1",
                job.stay_job_id,
            )
            if existing is not None:
                if existing["snapshot_sha256"].strip() != output.snapshot_sha256:
                    raise IdempotencyConflictError("stay snapshot binding mismatch")
                return True
            if (
                current is None
                or current["status"] != "BUILDING"
                or current["lease_owner"] != job.lease_owner
                or current["lease_until"] <= now
            ):
                raise JobLeaseLostError("stay job lease was lost before completion")
            snapshot_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO trip_stay_recommendation_snapshots (
                    snapshot_id, stay_job_id, plan_ref_id, status, policy_hash,
                    area_summary, searched_scopes_json, candidate_count,
                    snapshot_sha256, provider_binding_json, failure_json,
                    started_at, finished_at, observed_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9,
                          $10::jsonb, $11::jsonb, $12, $13, $14, $15)
                """,
                snapshot_id,
                job.stay_job_id,
                job.plan_ref_id,
                output.status,
                output.policy_hash,
                output.area_summary,
                json.dumps(output.searched_scopes, ensure_ascii=False),
                len(output.candidates),
                output.snapshot_sha256,
                json.dumps(output.provider_binding, ensure_ascii=False),
                json.dumps(output.failure, ensure_ascii=False),
                output.started_at,
                output.finished_at,
                output.observed_at,
                now,
            )
            for rank, scored in enumerate(output.candidates, start=1):
                candidate_id = str(uuid4())
                candidate = scored.candidate
                await conn.execute(
                    """
                    INSERT INTO trip_stay_candidates (
                        candidate_id, snapshot_id, public_candidate_token, rank,
                        canonical_place_id, name, brand, category, area_or_address,
                        city, longitude, latitude, search_radius_m, total_score,
                        max_single_leg_minutes, transfer_count, missing_leg_count,
                        evidence_penalty, provider_binding_json, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, '住宿', $8, $9,
                              $10, $11, $12, $13, $14, $15, $16, $17, $18::jsonb, $19)
                    """,
                    candidate_id,
                    snapshot_id,
                    secrets.token_urlsafe(24),
                    rank,
                    candidate.canonical_place_id,
                    candidate.name,
                    candidate.brand,
                    candidate.area_or_address,
                    candidate.city,
                    candidate.longitude,
                    candidate.latitude,
                    candidate.search_radius_m,
                    scored.total_score,
                    scored.max_single_leg_minutes,
                    scored.transfer_count,
                    scored.missing_leg_count,
                    scored.evidence_penalty,
                    json.dumps(candidate.provider_binding, ensure_ascii=False),
                    now,
                )
                for leg in scored.legs:
                    leg_id = str(uuid4())
                    await conn.execute(
                        """
                        INSERT INTO trip_stay_commute_legs (
                            leg_id, candidate_id, day_index, direction,
                            endpoint_name, selected_mode, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        leg_id,
                        candidate_id,
                        leg.day_index,
                        leg.direction,
                        leg.endpoint_name,
                        leg.selected_mode,
                        now,
                    )
                    for fact in (leg.walking, leg.transit):
                        await conn.execute(
                            """
                            INSERT INTO trip_stay_commute_mode_facts (
                                leg_id, mode, status, duration_minutes, distance_meters,
                                transfer_count, request_hash, response_hash,
                                provider_receipt_json, external_call_count,
                                observed_at, expires_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                                      $9::jsonb, $10, $11, $12)
                            """,
                            leg_id,
                            fact.mode,
                            fact.status,
                            fact.duration_minutes,
                            fact.distance_meters,
                            fact.transfer_count,
                            fact.request_hash,
                            fact.response_hash,
                            json.dumps(fact.provider_binding, ensure_ascii=False),
                            fact.external_call_count,
                            fact.observed_at,
                            fact.expires_at,
                        )
            await conn.execute(
                """
                UPDATE trip_stay_recommendation_jobs
                SET status = $2, lease_owner = NULL, lease_until = NULL,
                    finished_at = $3, updated_at = $3
                WHERE stay_job_id = $1
                """,
                job.stay_job_id,
                output.status,
                now,
            )
        return False

    async def fail_stay_job(
        self,
        job: StayRecommendationJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT * FROM trip_stay_recommendation_jobs WHERE stay_job_id = $1 FOR UPDATE",
                job.stay_job_id,
            )
            if row is None or row["status"] != "BUILDING" or row["lease_owner"] != job.lease_owner:
                return
            if row["attempt"] < row["max_attempts"]:
                await conn.execute(
                    """
                    UPDATE trip_stay_recommendation_jobs
                    SET status = 'QUEUED', lease_owner = NULL, lease_until = NULL,
                        available_at = $2, last_error_category = $3, updated_at = $1
                    WHERE stay_job_id = $4
                    """,
                    now,
                    now + timedelta(seconds=2),
                    category,
                    job.stay_job_id,
                )
                return
            await conn.execute(
                """
                UPDATE trip_stay_recommendation_jobs
                SET status = 'UNAVAILABLE', lease_owner = NULL, lease_until = NULL,
                    last_error_category = $2, finished_at = $3, updated_at = $3
                WHERE stay_job_id = $1
                """,
                job.stay_job_id,
                category,
                now,
            )

    async def _copy_stay_selection_to_revision(
        self,
        conn: Any,
        understanding_id: str,
        source_revision: int,
        target_revision: int,
        *,
        now: datetime,
    ) -> None:
        source = await conn.fetchrow(
            """
            SELECT s.* FROM trip_stay_selections s
            JOIN trip_plan_revision_refs p ON p.plan_ref_id = s.target_plan_ref_id
            WHERE p.understanding_id = $1 AND p.revision_kind = 'UNDERSTANDING'
              AND p.aggregate_id = $1 AND p.revision = $2
            """,
            understanding_id,
            source_revision,
        )
        if source is None:
            return
        base_plan = await self._read_map_plan(conn, understanding_id, target_revision)
        selected_plan = plan_with_stay_anchor(
            base_plan,
            selected_place_id=source["selected_place_id"],
            selected_name=source["selected_name"],
            selected_city=source["selected_city"],
            longitude=float(source["longitude"]),
            latitude=float(source["latitude"]),
            overnight_days=list(source["overnight_days"]),
        )
        target_ref, _map_plan = await self._ensure_plan_ref(
            conn,
            understanding_id,
            target_revision,
            now=now,
            map_plan=selected_plan,
        )
        await conn.execute(
            """
            INSERT INTO trip_stay_selections (
                selection_id, understanding_id, source_snapshot_id,
                source_plan_ref_id, target_plan_ref_id, candidate_id,
                selected_place_id, selected_name, selected_brand, selected_address,
                selected_city, longitude, latitude, overnight_days,
                selection_request_hash, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                      $11, $12, $13, $14, $15, $16)
            ON CONFLICT (target_plan_ref_id) DO NOTHING
            """,
            str(uuid4()),
            understanding_id,
            source["source_snapshot_id"],
            source["source_plan_ref_id"],
            target_ref["plan_ref_id"],
            source["candidate_id"],
            source["selected_place_id"],
            source["selected_name"],
            source["selected_brand"],
            source["selected_address"],
            source["selected_city"],
            source["longitude"],
            source["latitude"],
            source["overnight_days"],
            canonical_sha256(
                {"kind": "CARRY_FORWARD", "source_revision": source_revision, "target_revision": target_revision}
            ),
            now,
        )

    async def select_stay(
        self,
        resource: PublicResourceRecord,
        *,
        candidate_token: str,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> StaySelectionOutcome:
        scope = f"understanding:{resource.understanding_id}:stay-selection"
        key_hash = _sha256_text(idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            aggregate = await conn.fetchrow(
                "SELECT * FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            if aggregate["public_resource_id"] != resource.public_resource_id:
                raise ResourceAccessDeniedError("trip resource binding changed")
            claimed = await conn.fetchval(
                """
                INSERT INTO trip_understanding_idempotency_records (
                    scope, key_hash, request_hash, state, lease_until, created_at
                ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
                ON CONFLICT (scope, key_hash) DO NOTHING RETURNING scope
                """,
                scope,
                key_hash,
                request_hash,
                now + timedelta(seconds=30),
                now,
            )
            if claimed is None:
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, state, response_json, response_headers_json
                    FROM trip_understanding_idempotency_records
                    WHERE scope = $1 AND key_hash = $2
                    """,
                    scope,
                    key_hash,
                )
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("stay selection idempotency key was reused")
                if existing["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("matching stay selection is in progress")
                headers = _json(existing["response_headers_json"])
                return StaySelectionOutcome(
                    applied=StaySelectionAppliedView.model_validate(_json(existing["response_json"])),
                    opaque_etag=str(headers["ETag"]).strip('"'),
                    replayed=True,
                )
            parent_revision = int(aggregate["current_revision"])
            current = await conn.fetchrow(
                """
                SELECT r.*, result.public_json, result.opaque_etag
                FROM trip_understanding_revisions r
                JOIN trip_understanding_results result
                  ON result.understanding_id = r.understanding_id AND result.revision = r.revision
                WHERE r.understanding_id = $1 AND r.revision = $2
                """,
                resource.understanding_id,
                parent_revision,
            )
            if current is None:
                raise ResourceNotReadyError("trip cards are not ready for stay selection")
            if not hmac.compare_digest(current["opaque_etag"], expected_etag):
                raise RevisionConflictError("stay selection precondition does not match current result")
            source_ref = await conn.fetchrow(
                """
                SELECT * FROM trip_plan_revision_refs
                WHERE understanding_id = $1 AND revision_kind = 'UNDERSTANDING'
                  AND aggregate_id = $1 AND revision = $2
                """,
                resource.understanding_id,
                parent_revision,
            )
            candidate = await conn.fetchrow(
                """
                SELECT c.*, s.snapshot_id, s.plan_ref_id
                FROM trip_stay_candidates c
                JOIN trip_stay_recommendation_snapshots s ON s.snapshot_id = c.snapshot_id
                WHERE c.public_candidate_token = $1 AND c.rank <= 3
                """,
                candidate_token,
            )
            if source_ref is None or candidate is None or candidate["plan_ref_id"] != source_ref["plan_ref_id"]:
                raise ResourceNotReadyError("stay candidate is no longer current")
            source_plan = await self._read_map_plan(conn, resource.understanding_id, parent_revision)
            stay_plan = stay_plan_from_map(source_plan)
            if stay_plan is None:
                raise ResourceNotReadyError("stay plan is no longer available")
            current_result = UserFacingTripResult.model_validate(_json(current["public_json"]))
            selected_view = _candidate_view(candidate, selected=True)
            next_result = current_result.model_copy(
                update={
                    "stay": StaySuggestionView(
                        status="AVAILABLE",
                        message=f"整程住宿已选择：{candidate['name']}",
                        area_summary=candidate["area_or_address"],
                        candidates=[selected_view],
                    )
                }
            )
            token_map: dict[str, str] = {}
            next_days = []
            for day in next_result.days:
                cards = []
                for card in day.activities:
                    token = secrets.token_urlsafe(24)
                    token_map[card.activity_token] = token
                    cards.append(card.model_copy(update={"activity_token": token}))
                next_days.append(day.model_copy(update={"activities": cards}))
            next_result = next_result.model_copy(update={"days": next_days})
            public_payload = next_result.model_dump(mode="json")
            public_hash = canonical_sha256(public_payload)
            result_revision = parent_revision + 1
            terminal_state = "READY" if next_result.status == "READY" else "PARTIAL"
            await conn.execute(
                """
                INSERT INTO trip_understanding_revisions (
                    understanding_id, revision, parent_revision, source_id, status,
                    content_hash, destination_json, assumptions_json, proposal_json,
                    inference_binding_json, compiler_receipt_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb,
                          $9::jsonb, $10::jsonb, $11::jsonb, $12)
                """,
                resource.understanding_id,
                result_revision,
                parent_revision,
                current["source_id"],
                terminal_state,
                canonical_sha256({"parent_revision": parent_revision, "selection": request_hash, "public": public_hash}),
                json.dumps(_json(current["destination_json"]), ensure_ascii=False),
                json.dumps(_json(current["assumptions_json"]), ensure_ascii=False),
                json.dumps({"kind": "STAY_SELECTION", "source_quotes": "PARENT_REVISION_ONLY"}, ensure_ascii=False),
                json.dumps({"provider_calls": 0, "route_provider_calls": 0}, ensure_ascii=False),
                json.dumps({"kind": "STAY_SELECTION", "source_claims_copied": 0}, ensure_ascii=False),
                now,
            )
            activities = await conn.fetch(
                "SELECT * FROM trip_understanding_activities WHERE understanding_id = $1 AND revision = $2",
                resource.understanding_id,
                parent_revision,
            )
            for old in activities:
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_activities (
                        activity_id, understanding_id, revision, public_activity_token,
                        day_index, sequence_index, role, mention_text, atomic_place_name,
                        category_hint, time_hint, eligible_for_place_search,
                        resolution_status, canonical_place_id, resolver_receipt_json, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                              $12, $13, $14, $15::jsonb, $16)
                    """,
                    str(uuid4()),
                    resource.understanding_id,
                    result_revision,
                    token_map.get(old["public_activity_token"], secrets.token_urlsafe(24)),
                    old["day_index"],
                    old["sequence_index"],
                    old["role"],
                    old["mention_text"],
                    old["atomic_place_name"],
                    old["category_hint"],
                    old["time_hint"],
                    old["eligible_for_place_search"],
                    old["resolution_status"],
                    old["canonical_place_id"],
                    json.dumps(_json(old["resolver_receipt_json"]), ensure_ascii=False),
                    now,
                )
            result_id = str(uuid4())
            opaque_etag = f"tu3_{secrets.token_urlsafe(32)}"
            await conn.execute(
                """
                INSERT INTO trip_understanding_results (
                    result_id, understanding_id, revision, public_json,
                    public_sha256, opaque_etag, created_at
                ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                """,
                result_id,
                resource.understanding_id,
                result_revision,
                json.dumps(public_payload, ensure_ascii=False),
                public_hash,
                opaque_etag,
                now,
            )
            selected_plan = plan_with_stay_anchor(
                await self._read_map_plan(conn, resource.understanding_id, result_revision),
                selected_place_id=candidate["canonical_place_id"],
                selected_name=candidate["name"],
                selected_city=candidate["city"],
                longitude=float(candidate["longitude"]),
                latitude=float(candidate["latitude"]),
                overnight_days=stay_plan.overnight_days,
            )
            target_ref, _map_plan = await self._ensure_plan_ref(
                conn,
                resource.understanding_id,
                result_revision,
                now=now,
                map_plan=selected_plan,
            )
            await conn.execute(
                """
                INSERT INTO trip_stay_selections (
                    selection_id, understanding_id, source_snapshot_id,
                    source_plan_ref_id, target_plan_ref_id, candidate_id,
                    selected_place_id, selected_name, selected_brand, selected_address,
                    selected_city, longitude, latitude, overnight_days,
                    selection_request_hash, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                          $11, $12, $13, $14, $15, $16)
                """,
                str(uuid4()),
                resource.understanding_id,
                candidate["snapshot_id"],
                source_ref["plan_ref_id"],
                target_ref["plan_ref_id"],
                candidate["candidate_id"],
                candidate["canonical_place_id"],
                candidate["name"],
                candidate["brand"],
                candidate["area_or_address"],
                candidate["city"],
                candidate["longitude"],
                candidate["latitude"],
                stay_plan.overnight_days,
                request_hash,
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understandings
                SET state = $2, current_revision = $3, result_revision = $3,
                    current_result_id = $4, updated_at = $5
                WHERE understanding_id = $1
                """,
                resource.understanding_id,
                terminal_state,
                result_revision,
                result_id,
                now,
            )
            applied = StaySelectionAppliedView(
                selected_stay=candidate["name"],
                overnight_days=[f"Day {day}" for day in stay_plan.overnight_days],
            )
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 200,
                    response_json = $3::jsonb, response_headers_json = $4::jsonb,
                    lease_until = NULL, completed_at = $5
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                json.dumps(applied.model_dump(mode="json"), ensure_ascii=False),
                json.dumps({"ETag": f'"{opaque_etag}"'}, ensure_ascii=False),
                now,
            )
        return StaySelectionOutcome(applied=applied, opaque_etag=opaque_etag)


class InMemoryStayRecommendationRepositoryMixin:
    def _init_stay_store(self) -> None:
        self.stay_jobs: dict[str, dict[str, Any]] = {}
        self.stay_jobs_by_key: dict[str, str] = {}
        self.stay_selection_idempotency: dict[tuple[str, str], tuple[str, StaySelectionOutcome]] = {}
        self.stay_selections: dict[tuple[str, int], dict[str, Any]] = {}

    def _ensure_memory_stay_job(
        self,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> dict[str, Any] | None:
        map_plan = self._memory_plan(understanding_id, revision)
        plan = stay_plan_from_map(map_plan)
        if plan is None:
            return None
        logical_key = canonical_sha256(
            {"plan_ref": plan.plan_ref.model_dump(mode="json"), "policy_hash": STAY_POLICY_SHA256}
        )
        existing_id = self.stay_jobs_by_key.get(logical_key)
        if existing_id:
            return self.stay_jobs[existing_id]
        job_id = str(uuid4())
        item = {
            "stay_job_id": job_id,
            "understanding_id": understanding_id,
            "plan_ref_id": str(uuid4()),
            "plan": plan,
            "policy_hash": STAY_POLICY_SHA256,
            "status": "QUEUED",
            "lease_owner": None,
            "lease_until": None,
            "attempt": 0,
            "max_attempts": 3,
            "available_at": now,
            "started_at": None,
            "output": None,
            "tokens": {},
        }
        self.stay_jobs[job_id] = item
        self.stay_jobs_by_key[logical_key] = job_id
        return item

    def _enqueue_initial_stay_job_memory(
        self,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
    ) -> None:
        self._ensure_memory_stay_job(understanding_id, revision, now=now)

    def _memory_stay_candidate_view(self, item: dict[str, Any], scored: Any, *, selected: bool = False) -> StayCandidateView:
        token = item["tokens"].setdefault(scored.candidate.canonical_place_id, secrets.token_urlsafe(24))
        row = {
            "public_candidate_token": token,
            "name": scored.candidate.name,
            "brand": scored.candidate.brand,
            "area_or_address": scored.candidate.area_or_address,
            "max_single_leg_minutes": scored.max_single_leg_minutes,
            "transfer_count": scored.transfer_count,
            "missing_leg_count": scored.missing_leg_count,
        }
        return _candidate_view(row, selected=selected)

    def _memory_stay_view(self, understanding_id: str, revision: int) -> StaySuggestionView:
        selection = self.stay_selections.get((understanding_id, revision))
        if selection is not None:
            return StaySuggestionView(
                status="AVAILABLE",
                message=f"整程住宿已选择：{selection['view'].name}",
                area_summary=selection["view"].area_or_address,
                candidates=[selection["view"]],
            )
        matching = [
            item
            for item in self.stay_jobs.values()
            if item["understanding_id"] == understanding_id
            and item["plan"].plan_ref.revision == revision
        ]
        if matching:
            item = matching[-1]
            if item["status"] in {"QUEUED", "BUILDING"}:
                return StaySuggestionView(status="PREPARING", message="正在按全程首末站准备住宿候选")
            output = item["output"]
            if output is not None:
                views = [self._memory_stay_candidate_view(item, scored) for scored in output.candidates[:3]]
                if output.status == "READY":
                    return StaySuggestionView(
                        status="AVAILABLE",
                        message="已按全程首末站通勤准备住宿候选",
                        area_summary=output.area_summary,
                        searched_scopes=output.searched_scopes,
                        candidates=views,
                        available_actions=["CHOOSE_STAY"],
                    )
                if views:
                    return StaySuggestionView(
                        status="LIMITED",
                        message="已找到可比较的住宿，部分通勤依据暂缺",
                        area_summary=output.area_summary,
                        searched_scopes=output.searched_scopes,
                        candidates=views,
                        available_actions=["CHOOSE_STAY"],
                    )
                return StaySuggestionView(
                    status="UNAVAILABLE",
                    message="暂未找到符合条件的连锁酒店，可以稍后再试",
                    area_summary=output.area_summary,
                    searched_scopes=output.searched_scopes,
                )
        if any(item["understanding_id"] == understanding_id for item in self.stay_jobs.values()) or any(
            key[0] == understanding_id for key in self.stay_selections
        ):
            return StaySuggestionView(status="NEEDS_UPDATE", message="行程已修改，住宿通勤尚未更新")
        return StaySuggestionView(status="UNAVAILABLE", message="住宿待选择；需要至少两个有坐标的行程日")

    async def get_stay_view(self, resource: PublicResourceRecord) -> StaySuggestionView:
        public_id = self.resources_by_understanding.get(resource.understanding_id)
        if public_id is None or public_id != resource.public_resource_id:
            raise ResourceNotFoundError("trip resource does not exist")
        aggregate = self.resources[public_id]
        return self._memory_stay_view(resource.understanding_id, int(aggregate["current_revision"]))

    async def claim_next_stay(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> StayRecommendationJobRecord | None:
        eligible = [
            item
            for item in self.stay_jobs.values()
            if item["attempt"] < item["max_attempts"]
            and item["available_at"] <= now
            and (
                item["status"] == "QUEUED"
                or (item["status"] == "BUILDING" and item["lease_until"] <= now)
            )
            and not any(
                map_item["understanding_id"] == item["understanding_id"]
                and map_item["status"] == "BUILDING"
                and map_item["lease_until"] is not None
                and map_item["lease_until"] > now
                for map_item in self.map_jobs.values()
            )
        ]
        if not eligible:
            return None
        item = sorted(eligible, key=lambda value: (value["available_at"], value["stay_job_id"]))[0]
        item.update(
            {
                "status": "BUILDING",
                "lease_owner": worker_id,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "attempt": item["attempt"] + 1,
                "started_at": item["started_at"] or now,
            }
        )
        return StayRecommendationJobRecord(
            stay_job_id=item["stay_job_id"],
            understanding_id=item["understanding_id"],
            plan_ref_id=item["plan_ref_id"],
            plan_ref=item["plan"].plan_ref,
            policy_hash=item["policy_hash"],
            status="BUILDING",
            lease_owner=worker_id,
            lease_until=item["lease_until"],
            attempt=item["attempt"],
            max_attempts=item["max_attempts"],
            started_at=item["started_at"],
        )

    async def renew_stay_lease(
        self,
        job: StayRecommendationJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        item = self.stay_jobs.get(job.stay_job_id)
        if (
            item is None
            or item["status"] != "BUILDING"
            or item["lease_owner"] != job.lease_owner
            or item["attempt"] != job.attempt
            or item["lease_until"] <= now
        ):
            return False
        item["lease_until"] = now + timedelta(seconds=lease_seconds)
        return True

    async def load_stay_plan(self, job: StayRecommendationJobRecord) -> StayRecommendationPlan:
        item = self.stay_jobs.get(job.stay_job_id)
        if item is None or item["plan"].plan_ref != job.plan_ref:
            raise ResourceNotFoundError("stay recommendation plan does not exist")
        return item["plan"]

    async def complete_stay_job(
        self,
        job: StayRecommendationJobRecord,
        output: StayRecommendationOutput,
        *,
        now: datetime,
    ) -> bool:
        item = self.stay_jobs.get(job.stay_job_id)
        if item is None:
            raise ResourceNotFoundError("stay job does not exist")
        if item["output"] is not None:
            if item["output"].snapshot_sha256 != output.snapshot_sha256:
                raise IdempotencyConflictError("stay snapshot binding mismatch")
            return True
        if item["status"] != "BUILDING" or item["lease_owner"] != job.lease_owner or item["lease_until"] <= now:
            raise JobLeaseLostError("stay job lease was lost before completion")
        if output.plan_ref != job.plan_ref or output.policy_hash != job.policy_hash:
            raise ValueError("stay output is not bound to the claimed plan")
        item.update(
            {
                "status": output.status,
                "lease_owner": None,
                "lease_until": None,
                "output": output,
                "finished_at": now,
            }
        )
        return False

    async def fail_stay_job(
        self,
        job: StayRecommendationJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None:
        item = self.stay_jobs.get(job.stay_job_id)
        if item is None or item["status"] != "BUILDING" or item["lease_owner"] != job.lease_owner:
            return
        if item["attempt"] < item["max_attempts"]:
            item.update(
                {
                    "status": "QUEUED",
                    "lease_owner": None,
                    "lease_until": None,
                    "available_at": now + timedelta(seconds=2),
                    "last_error_category": category,
                }
            )
        else:
            item.update(
                {
                    "status": "UNAVAILABLE",
                    "lease_owner": None,
                    "lease_until": None,
                    "last_error_category": category,
                }
            )

    def _copy_stay_selection_memory(
        self,
        understanding_id: str,
        source_revision: int,
        target_revision: int,
    ) -> None:
        source = self.stay_selections.get((understanding_id, source_revision))
        if source is not None:
            self.stay_selections[(understanding_id, target_revision)] = dict(source)

    async def select_stay(
        self,
        resource: PublicResourceRecord,
        *,
        candidate_token: str,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> StaySelectionOutcome:
        del now
        scope = f"understanding:{resource.understanding_id}:stay-selection"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.stay_selection_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError("stay selection idempotency key was reused")
            return existing[1].model_copy(update={"replayed": True})
        public_id = self.resources_by_understanding[resource.understanding_id]
        aggregate = self.resources[public_id]
        stored = self.results.get(aggregate["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready for stay selection")
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("stay selection precondition does not match current result")
        revision = int(aggregate["current_revision"])
        job = next(
            (
                item
                for item in self.stay_jobs.values()
                if item["understanding_id"] == resource.understanding_id
                and item["plan"].plan_ref.revision == revision
                and item["output"] is not None
            ),
            None,
        )
        if job is None:
            raise ResourceNotReadyError("stay suggestions are not ready")
        scored = next(
            (
                item
                for item in job["output"].candidates[:3]
                if job["tokens"].get(item.candidate.canonical_place_id) == candidate_token
            ),
            None,
        )
        if scored is None:
            raise ResourceNotReadyError("stay candidate is no longer current")
        selected_view = self._memory_stay_candidate_view(job, scored, selected=True)
        next_result = stored.result.model_copy(
            update={
                "stay": StaySuggestionView(
                    status="AVAILABLE",
                    message=f"整程住宿已选择：{scored.candidate.name}",
                    area_summary=scored.candidate.area_or_address,
                    candidates=[selected_view],
                )
            }
        )
        result_id = str(uuid4())
        opaque_etag = f"tu3_{secrets.token_urlsafe(32)}"
        target_revision = revision + 1
        self.results[result_id] = StoredResult(result=next_result, opaque_etag=opaque_etag)
        self.result_owners[result_id] = resource.understanding_id
        self.result_revisions[result_id] = target_revision
        aggregate.update(
            {
                "current_result_id": result_id,
                "current_revision": target_revision,
                "state": "READY" if next_result.status == "READY" else "PARTIAL",
            }
        )
        self.stay_selections[(resource.understanding_id, target_revision)] = {
            "view": selected_view,
            "overnight_days": job["plan"].overnight_days,
            "selected_place_id": scored.candidate.canonical_place_id,
            "selected_name": scored.candidate.name,
            "selected_city": scored.candidate.city,
            "longitude": scored.candidate.longitude,
            "latitude": scored.candidate.latitude,
        }
        applied = StaySelectionAppliedView(
            selected_stay=scored.candidate.name,
            overnight_days=[f"Day {day}" for day in job["plan"].overnight_days],
        )
        outcome = StaySelectionOutcome(applied=applied, opaque_etag=opaque_etag)
        self.stay_selection_idempotency[key] = (request_hash, outcome)
        return outcome

    def _delete_stay_memory(self, understanding_id: str) -> None:
        for job_id, item in list(self.stay_jobs.items()):
            if item["understanding_id"] == understanding_id:
                logical_key = canonical_sha256(
                    {"plan_ref": item["plan"].plan_ref.model_dump(mode="json"), "policy_hash": STAY_POLICY_SHA256}
                )
                self.stay_jobs_by_key.pop(logical_key, None)
                self.stay_jobs.pop(job_id, None)
        for key in list(self.stay_selections):
            if key[0] == understanding_id:
                self.stay_selections.pop(key, None)
        scope = f"understanding:{understanding_id}:stay-selection"
        for key in list(self.stay_selection_idempotency):
            if key[0] == scope:
                self.stay_selection_idempotency.pop(key, None)

    @property
    def stay_job_count(self) -> int:
        return len(self.stay_jobs)
