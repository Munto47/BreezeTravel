from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
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
from app.trip_understanding.lodging_recovery import result_cards
from app.trip_understanding.models import (
    PublicResourceRecord,
    StayCandidateView,
    StaySelectionAppliedView,
    StaySelectionOutcome,
    StaySuggestionView,
    StaySegmentView,
    MapReadinessView,
    StoredResult,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.stay import (
    STAY_POLICY_SHA256,
    StayRecommendationJobRecord,
    StayRecommendationOutput,
    StayRecommendationPlan,
    StayCommuteAssessment,
    assess_stay_commute,
    load_stay_commute_assessment,
    stay_plan_from_map,
)
from app.trip_understanding.overnight_context import (
    excludes_hotel, lodging_exclusion_message, lodging_role_message, overnight_segments, stay_context_hash,
)


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _candidate_view(row: Any, *, selected: bool = False, assessment: StayCommuteAssessment | None = None) -> StayCandidateView:
    # Never reuse the historical maximum: older rows mixed missing-leg score
    # penalties into this number. Only recorded route legs can recover minutes.
    missing = assessment.missing_leg_count if assessment else max(1, int(row["missing_leg_count"]))
    maximum = assessment.maximum_minutes if assessment else None
    transfers = assessment.transfer_count if assessment else 0
    base_reason = (
        "已选择为这段行程的住宿"
        if selected
        else (f"综合已核对的往返和换乘后排在前列，共 {transfers} 次换乘" if not missing else "地点可作为住宿候选，部分通勤路线仍需确认")
    )
    route_limit = f"；有 {missing} 段路线暂时无法完整比较" if missing else ""
    binding = _json(row.get("provider_binding_json", {}))
    official = binding.get("property_identity") == "OFFICIAL_NAME_ADDRESS"
    area_reason = f"；也可作为按「{binding['area_search_seed']}」查找的住宿候选" if binding.get("area_search_seed") else ""
    return StayCandidateView(
        candidate_token=row["public_candidate_token"],
        name=row["name"],
        brand=row["brand"] if official else "",
        category="住宿",
        area_or_address=row["area_or_address"],
        commute_summary=(
            "住宿往返路线尚未得到有效核对" if maximum is None
            else f"{'已核对的路段中' if missing else '全程首末站通勤中'}，最久一程约 {maximum} 分钟"
        ),
        max_single_leg_minutes=maximum,
        transfer_count=transfers,
        reason=f"{base_reason}{route_limit}{area_reason}",
        available_actions=[] if selected else ["CHOOSE_STAY"],
        selected=selected,
        brand_group=binding.get("brand_group") if official else None,
        brand_note=("门店归属已与品牌官网和地图身份交叉核对；房态请以预订时为准" if official else
                    "酒店地点已核对，尚未核实品牌归属"),
    )


def _segmented_view(metadata: list[dict], candidates: list[tuple[StayCandidateView, dict]], *, stale: bool = False) -> StaySuggestionView:
    if not metadata:
        metadata = [{"segment_key": "legacy", "city": None, "overnight_days": [], "status": "READY"}]
    segments = []
    for info in metadata:
        key = info.get("segment_key", "legacy")
        choices = [view for view, binding in candidates if (
            bool(set(binding["overnight_days"]) & set(info.get("overnight_days", [])))
            if view.selected and "overnight_days" in binding else binding.get("segment_key", "legacy") == key)]
        unverified = {view.candidate_token for view, binding in candidates
            if not view.selected and binding.get("property_identity") != "OFFICIAL_NAME_ADDRESS"}
        removed = any(view.candidate_token in unverified for view in choices)
        choices = [view for view in choices if view.candidate_token not in unverified]
        if removed:
            info = {**info, "status": "LIMITED", "message": "旧住宿候选的连锁门店归属尚未核实，请更新住宿建议"}
        selected = [view for view in choices if view.selected]
        if selected:
            choices = selected
        else:
            choices = choices[:3]
        preserved = info.get("preserved_hotels", [])
        conflict = info.get("lodging_conflict", False)
        if conflict:
            choices = []
        status = ("LIMITED" if conflict else "NEEDS_UPDATE" if stale else "LIMITED" if info.get("missing_boundary_count", 0) else "AVAILABLE" if selected or preserved else
                  "AVAILABLE" if choices and info.get("status") == "READY" else "LIMITED" if choices or info.get("status") == "LIMITED" else "UNAVAILABLE")
        segments.append(StaySegmentView(segment_token=key, city=info.get("city"),
            overnight_days=[f"Day {day}" for day in info.get("overnight_days", [])],
            status=status, message=(info["message"] if conflict else "行程已修改，住宿通勤需重新核对" if stale else
                info["message"] if info.get("pending_lodging_roles") or info.get("unconfirmed_exclusions") else
                "已保留这段行程的住宿选择" if selected else info.get("message", "按每晚返回和次日出发比较住宿")),
            candidates=choices, preserved_hotels=preserved,
            expected_boundary_count=info.get("expected_boundary_count", 0), missing_boundary_count=info.get("missing_boundary_count", 0)))
    available = any(s.candidates or s.preserved_hotels for s in segments)
    status = "NEEDS_UPDATE" if stale else "AVAILABLE" if all(s.status == "AVAILABLE" for s in segments) else "LIMITED" if available or any(s.status == "LIMITED" for s in segments) else "UNAVAILABLE"
    return StaySuggestionView(status=status, message="行程已修改，住宿通勤尚未更新" if stale else "按每晚返回和次日出发比较住宿",
        candidates=[c for s in segments for c in s.candidates][:3], segments=segments,
        available_actions=["CHOOSE_STAY"] if any(not c.selected for s in segments for c in s.candidates) and not stale else [])


def _binding(row: Any) -> dict:
    return _json(row.get("provider_binding_json", {}))


def _memory_selected_view(selection: dict, *, stale: bool = False) -> StayCandidateView:
    scored = selection["scored"]
    candidate = scored.candidate
    row = {"public_candidate_token": selection["view"].candidate_token,
        "name": candidate.name, "brand": candidate.brand, "area_or_address": candidate.area_or_address,
        "missing_leg_count": scored.missing_leg_count, "provider_binding_json": candidate.provider_binding}
    assessment = None if stale else assess_stay_commute(scored.legs, now=datetime.now(timezone.utc),
        expected_missing=scored.missing_leg_count)
    return _candidate_view(row, selected=True, assessment=assessment)


def _current_selections(selections: Any, plan: MapRenderPlan) -> list[dict]:
    """Project history onto eligible current nights without mutating old rows."""
    segments = overnight_segments(plan)
    current = []
    for selection in selections:
        allowed = {night for segment in segments
            if segment.city == str(selection["selected_city"]).removesuffix("市")
            and not segment.preserved_hotels and not excludes_hotel(segment, selection["selected_place_id"])
            for night in segment.overnight_days}
        nights = sorted(set(selection["overnight_days"]) & allowed)
        if nights:
            current.append({**dict(selection), "overnight_days": nights})
    return current


def _selection_binding(binding: dict, selection: Any) -> dict:
    # Candidate evidence belongs to its original snapshot; only the selection's
    # current night range determines where a carried hotel remains selected.
    return {**binding, "overnight_days": list(selection["overnight_days"])}


def _has_selected_night(binding: dict, selections: Any) -> bool:
    return any(bool(set(binding["overnight_days"]) & set(selection["overnight_days"]))
        if "overnight_days" in binding else binding.get("segment_key", "legacy") == selection["segment_key"]
        for selection in selections)


def _overnight_metadata(plan: MapRenderPlan) -> list[dict]:
    return [{"segment_key": s.key, "city": s.city, "overnight_days": s.overnight_days,
        "preserved_hotels": s.preserved_hotels, "status": "LIMITED" if s.uncertain else "UNAVAILABLE",
        "pending_lodging_roles": s.pending_lodging_roles,
        "lodging_conflict": len(s.preserved_place_ids) > 1,
        "excluded_place_ids": s.excluded_place_ids, "unconfirmed_exclusions": s.unconfirmed_exclusions,
        "expected_boundary_count": s.expected_boundary_count, "missing_boundary_count": len(s.missing_boundaries),
        "message": lodging_exclusion_message(s.unconfirmed_exclusions) if s.unconfirmed_exclusions else
            lodging_role_message(s.pending_lodging_roles) if s.pending_lodging_roles else
            "这晚有多家不同酒店，请确认保留哪一家后再更新住宿与路线" if len(s.preserved_place_ids) > 1 else
            "已保留原住宿；酒店位置或行程首末站还需确认" if s.preserved_hotels and s.uncertain else
            "保留已有住宿" if s.preserved_hotels else "首末站或过夜城市待确认" if s.uncertain else "住宿建议待更新"}
        for s in overnight_segments(plan)]


class StayRecommendationRepository(Protocol):
    async def get_stay_view(self, resource: PublicResourceRecord) -> StaySuggestionView: ...

    async def refresh_stay_suggestions(self, resource: PublicResourceRecord, *, expected_etag: str,
        idempotency_key: str, now: datetime) -> tuple[StaySuggestionView, str, bool]: ...

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
    async def refresh_stay_suggestions(self, resource: PublicResourceRecord, *, expected_etag: str,
        idempotency_key: str, now: datetime) -> tuple[StaySuggestionView, str, bool]:
        scope, key_hash = f"understanding:{resource.understanding_id}:stay-refresh", _sha256_text(idempotency_key)
        request_hash = canonical_sha256({"expected_etag": expected_etag})
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            aggregate = await conn.fetchrow("SELECT * FROM trip_understandings WHERE understanding_id=$1 FOR UPDATE", resource.understanding_id)
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            if aggregate["public_resource_id"] != resource.public_resource_id:
                raise ResourceAccessDeniedError("trip resource binding changed")
            previous = await conn.fetchrow("SELECT * FROM trip_understanding_idempotency_records WHERE scope=$1 AND key_hash=$2", scope, key_hash)
            if previous:
                if previous["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("stay refresh idempotency key was reused")
                if previous["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("stay refresh is in progress")
                return (StaySuggestionView.model_validate(_json(previous["response_json"])["view"]),
                    _json(previous["response_headers_json"])["ETag"], True)
            revision = int(aggregate["current_revision"])
            etag = await conn.fetchval("SELECT opaque_etag FROM trip_understanding_results WHERE understanding_id=$1 AND revision=$2", resource.understanding_id, revision)
            if etag is None:
                raise ResourceNotReadyError("trip cards are not ready")
            if not hmac.compare_digest(etag, expected_etag):
                raise RevisionConflictError("stay refresh precondition changed")
            await self._ensure_stay_job(conn, resource.understanding_id, revision, now=now, refresh=True)
            view = await self._project_stay_view(conn, resource.understanding_id, revision)
            await conn.execute("""INSERT INTO trip_understanding_idempotency_records
                (scope,key_hash,request_hash,state,response_status,response_json,response_headers_json,created_at,completed_at)
                VALUES($1,$2,$3,'COMPLETED',200,$4::jsonb,$5::jsonb,$6,$6)""", scope, key_hash, request_hash,
                json.dumps({"view": view.model_dump(mode="json"), "public_resource_id": resource.public_resource_id}, ensure_ascii=False),
                json.dumps({"ETag": etag}), now)
            return view, etag, False

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
        refresh: bool = False,
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
        existing = await conn.fetchrow("""SELECT * FROM trip_stay_recommendation_jobs
            WHERE plan_ref_id=$1 AND policy_hash=$2 ORDER BY refresh_generation DESC LIMIT 1""",
            plan_ref["plan_ref_id"], STAY_POLICY_SHA256)
        generation = 0
        if existing:
            finished = existing["finished_at"]
            cooldown = timedelta(minutes=15) if existing["status"] == "READY" else timedelta(seconds=30)
            if not refresh or existing["status"] in {"QUEUED", "BUILDING"} or finished is None or now - finished < cooldown:
                return existing
            count = await conn.fetchval("""SELECT count(*) FROM trip_stay_recommendation_jobs
                WHERE plan_ref_id=$1 AND policy_hash=$2 AND refresh_generation>0 AND created_at>$3""",
                plan_ref["plan_ref_id"], STAY_POLICY_SHA256, now - timedelta(minutes=15))
            if count >= 3:
                return existing
            generation = int(existing["refresh_generation"]) + 1
        logical_key = canonical_sha256(
            {
                "plan_ref": stay_plan.plan_ref.model_dump(mode="json"),
                "policy_hash": STAY_POLICY_SHA256,
                "refresh_generation": generation,
            }
        )
        await conn.execute(
            """
            INSERT INTO trip_stay_recommendation_jobs (
                stay_job_id, plan_ref_id, understanding_id, policy_hash,
                logical_key_hash, status, available_at, created_at, updated_at, refresh_generation
            ) VALUES ($1, $2, $3, $4, $5, 'QUEUED', $6, $6, $6, $7)
            ON CONFLICT (plan_ref_id, policy_hash, refresh_generation) DO NOTHING
            """,
            str(uuid4()),
            plan_ref["plan_ref_id"],
            understanding_id,
            STAY_POLICY_SHA256,
            logical_key,
            now,
            generation,
        )
        return await conn.fetchrow(
            "SELECT * FROM trip_stay_recommendation_jobs WHERE plan_ref_id = $1 AND policy_hash = $2 ORDER BY refresh_generation DESC LIMIT 1",
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

    async def _snapshot_stay_view(self, conn: Any, snapshot: Any, selections=()) -> StaySuggestionView:
        rows = await conn.fetch("SELECT * FROM trip_stay_candidates WHERE snapshot_id = $1 ORDER BY rank", snapshot["snapshot_id"])
        metadata = list(_binding(snapshot).get("segments", []))
        candidates = []
        for row in rows:
            binding = _binding(row)
            if int(binding.get("segment_rank", row["rank"])) > 3 or _has_selected_night(binding, selections):
                continue
            assessment = await load_stay_commute_assessment(conn, row["candidate_id"], now=datetime.now(timezone.utc), expected_missing=int(row["missing_leg_count"]))
            candidates.append((_candidate_view(row, assessment=assessment), binding))
            if not assessment.complete:
                for info in metadata:
                    if info.get("segment_key") == binding.get("segment_key") and info.get("status") == "READY":
                        info["status"] = "PARTIAL"
        for selection in selections:
            row = await conn.fetchrow("SELECT * FROM trip_stay_candidates WHERE candidate_id = $1", selection["candidate_id"])
            if row:
                assessment = None
                if _binding(row).get("context_hash") == _binding(snapshot).get("context_hash"):
                    assessment = await load_stay_commute_assessment(conn, row["candidate_id"], now=datetime.now(timezone.utc), expected_missing=int(row["missing_leg_count"]))
                candidates.append((_candidate_view(row, selected=True, assessment=assessment), _selection_binding(_binding(row), selection)))
        return _segmented_view(metadata, candidates)

    async def _project_stay_view(self, conn: Any, understanding_id: str, revision: int) -> StaySuggestionView:
        try:
            map_plan = await self._read_map_plan(conn, understanding_id, revision)
        except ResourceNotReadyError:
            return StaySuggestionView(status="UNAVAILABLE", message="住宿建议需要先有行程地点")
        context = stay_context_hash(map_plan)
        selections = await conn.fetch("""SELECT s.* FROM trip_stay_selections s JOIN trip_plan_revision_refs p
            ON p.plan_ref_id = s.target_plan_ref_id WHERE p.understanding_id = $1 AND p.revision = $2""", understanding_id, revision)
        selections = _current_selections(selections, map_plan)
        current = await conn.fetchval("""SELECT j.status FROM trip_stay_recommendation_jobs j
            JOIN trip_plan_revision_refs p ON p.plan_ref_id = j.plan_ref_id
            WHERE p.understanding_id=$1 AND p.revision=$2 AND j.policy_hash=$3
            ORDER BY j.refresh_generation DESC LIMIT 1""", understanding_id, revision, STAY_POLICY_SHA256)
        if current in {"QUEUED", "BUILDING"}:
            return StaySuggestionView(status="PREPARING", message="正在按每晚返回和次日出发准备住宿候选")
        snapshots = await conn.fetch("""SELECT s.*, p.revision FROM trip_stay_recommendation_snapshots s
            JOIN trip_plan_revision_refs p ON p.plan_ref_id = s.plan_ref_id
            WHERE p.understanding_id = $1 ORDER BY p.revision DESC, s.created_at DESC LIMIT 20""", understanding_id)
        for snapshot in snapshots:
            binding = _binding(snapshot)
            if binding.get("context_hash") == context or (not binding.get("context_hash") and snapshot["revision"] == revision):
                return await self._snapshot_stay_view(conn, snapshot, selections)
        if snapshots or selections:
            pairs, metadata = [], _overnight_metadata(map_plan)
            for selection in selections:
                row = await conn.fetchrow("SELECT * FROM trip_stay_candidates WHERE candidate_id=$1", selection["candidate_id"])
                if row:
                    pairs.append((_candidate_view(row, selected=True), _selection_binding(_binding(row), selection)))
            return _segmented_view(metadata, pairs, stale=True)
        plan = stay_plan_from_map(map_plan)
        if plan and (all(s.preserved_hotels or s.uncertain for s in plan.segments)
                or any(len(s.preserved_place_ids) > 1 for s in plan.segments)):
            return _segmented_view(_overnight_metadata(map_plan), [])
        return StaySuggestionView(status="UNAVAILABLE", message="住宿待选择；需要相邻两日的已确认地点")

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
                JOIN trip_understandings u ON u.understanding_id=j.understanding_id AND u.current_revision=p.revision
                WHERE j.attempt < j.max_attempts
                  AND u.state NOT IN ('DELETED', 'FAILED')
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
                or current["attempt"] != job.attempt
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
                        evidence_penalty, provider_binding_json, created_at, segment_key
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, '住宿', $8, $9,
                              $10, $11, $12, $13, $14, $15, $16, $17, $18::jsonb, $19, $20)
                    """,
                    candidate_id,
                    snapshot_id,
                    secrets.token_urlsafe(24),
                    rank,
                    candidate.canonical_place_id,
                    candidate.name,
                    candidate.brand or "",
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
                    candidate.provider_binding.get("segment_key", "legacy"),
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
            if (
                row is None
                or row["status"] != "BUILDING"
                or row["lease_owner"] != job.lease_owner
                or row["attempt"] != job.attempt
            ):
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

    async def _copy_stay_selection_to_revision(self, conn: Any, understanding_id: str, source_revision: int, target_revision: int, *, now: datetime) -> None:
        sources = await conn.fetch("""SELECT s.* FROM trip_stay_selections s JOIN trip_plan_revision_refs p
            ON p.plan_ref_id = s.target_plan_ref_id WHERE p.understanding_id = $1 AND p.revision = $2""", understanding_id, source_revision)
        base_plan = await self._read_map_plan(conn, understanding_id, target_revision)
        sources = _current_selections(sources, base_plan)
        if not sources:
            return
        for row in sources:
            base_plan = plan_with_stay_anchor(base_plan, selected_place_id=row["selected_place_id"], selected_name=row["selected_name"],
                selected_city=row["selected_city"], longitude=float(row["longitude"]), latitude=float(row["latitude"]), overnight_days=list(row["overnight_days"]))
        target_ref, _ = await self._ensure_plan_ref(conn, understanding_id, target_revision, now=now, map_plan=base_plan)
        for row in sources:
            await self._insert_carried_stay(conn, row, target_ref["plan_ref_id"], now=now)

    async def _insert_carried_stay(self, conn, row, target_plan_ref_id, *, now):
        await conn.execute("""INSERT INTO trip_stay_selections (
            selection_id, understanding_id, source_snapshot_id, source_plan_ref_id, target_plan_ref_id, candidate_id,
            selected_place_id, selected_name, selected_brand, selected_address, selected_city, longitude, latitude,
            overnight_days, selection_request_hash, created_at, segment_key)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            ON CONFLICT (target_plan_ref_id, segment_key) DO NOTHING""", str(uuid4()), row["understanding_id"],
            row["source_snapshot_id"], row["source_plan_ref_id"], target_plan_ref_id, row["candidate_id"],
            row["selected_place_id"], row["selected_name"], row["selected_brand"], row["selected_address"],
            row["selected_city"], row["longitude"], row["latitude"], row["overnight_days"],
            canonical_sha256({"kind":"CARRY_FORWARD", "target":target_plan_ref_id, "segment":row["segment_key"]}), now, row["segment_key"])

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
            source_plan = await self._read_map_plan(conn, resource.understanding_id, int(aggregate["current_revision"]))
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
            source_available = await conn.fetchval("""SELECT 1 FROM trip_understanding_sources
                WHERE source_id=$1 AND deleted_at IS NULL
                  AND retention_until>GREATEST($2::timestamptz,clock_timestamp())""", current["source_id"], now)
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
                SELECT c.*, s.snapshot_id, s.plan_ref_id, p.understanding_id AS candidate_owner
                FROM trip_stay_candidates c
                JOIN trip_stay_recommendation_snapshots s ON s.snapshot_id = c.snapshot_id
                JOIN trip_plan_revision_refs p ON p.plan_ref_id = s.plan_ref_id
                WHERE c.public_candidate_token = $1 AND COALESCE((c.provider_binding_json->>'segment_rank')::int, c.rank) <= 3
                """,
                candidate_token,
            )
            if (candidate is None or candidate["candidate_owner"] != resource.understanding_id
                or _binding(candidate).get("property_identity") != "OFFICIAL_NAME_ADDRESS"
                or (_binding(candidate).get("context_hash") != stay_context_hash(source_plan)
                    if _binding(candidate).get("context_hash") else source_ref is None or candidate["plan_ref_id"] != source_ref["plan_ref_id"])):
                raise ResourceNotReadyError("stay candidate is no longer current")
            stay_plan = stay_plan_from_map(source_plan)
            if stay_plan is None:
                raise ResourceNotReadyError("stay plan is no longer available")
            segment_days = list(_binding(candidate).get("overnight_days", stay_plan.overnight_days))
            if not any(s.overnight_days == segment_days and s.city == candidate["city"] and not s.preserved_hotels
                    and not s.uncertain and not excludes_hotel(s, candidate["canonical_place_id"]) for s in stay_plan.segments):
                raise ResourceNotReadyError("stay segment is no longer available")
            existing_selections = await conn.fetch("SELECT * FROM trip_stay_selections WHERE target_plan_ref_id=$1", source_ref["plan_ref_id"]) if source_ref else []
            existing_selections = _current_selections(
                [row for row in existing_selections if row["segment_key"] != candidate["segment_key"]], source_plan)
            current_result = UserFacingTripResult.model_validate(_json(current["public_json"]))
            selected_view = _candidate_view(candidate, selected=True, assessment=await load_stay_commute_assessment(
                conn, candidate["candidate_id"], now=now, expected_missing=int(candidate["missing_leg_count"])))
            next_result = current_result.model_copy(
                deep=True,
                update={
                    "can_undo": True,
                    "map": MapReadinessView(status="NEEDS_UPDATE", message="住宿已选择，请更新路线", available_actions=["RENDER_MAP"]),
                    "stay": StaySuggestionView(
                        status="AVAILABLE",
                        message=f"整程住宿已选择：{candidate['name']}",
                        area_summary=candidate["area_or_address"],
                        candidates=[selected_view],
                    )
                }
            )
            token_map: dict[str, str] = {}
            retained_place_tokens = {card.activity_token for card in result_cards(next_result)}
            for card in result_cards(next_result):
                token = secrets.token_urlsafe(24)
                token_map[card.activity_token] = token
                card.activity_token = token
            for pending in next_result.pending_lodgings:
                token = secrets.token_urlsafe(24)
                token_map[pending.pending_token] = token
                pending.pending_token = token
            for day in next_result.days:
                current_tokens = {card.activity_token for card in day.activities}
                for slot in day.meal_slots:
                    for field in ("after_activity_token", "before_activity_token"):
                        refreshed = token_map.get(getattr(slot, field))
                        setattr(slot, field, refreshed if refreshed in current_tokens else None)
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
                if not source_available and old["public_activity_token"] not in retained_place_tokens:
                    # Expired or erased source material cannot enter a new revision.
                    continue
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
                overnight_days=segment_days,
            )
            for row in existing_selections:
                selected_plan = plan_with_stay_anchor(selected_plan, selected_place_id=row["selected_place_id"], selected_name=row["selected_name"],
                    selected_city=row["selected_city"], longitude=float(row["longitude"]), latitude=float(row["latitude"]), overnight_days=list(row["overnight_days"]))
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
                    selection_request_hash, created_at, segment_key
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                          $11, $12, $13, $14, $15, $16, $17)
                """,
                str(uuid4()),
                resource.understanding_id,
                candidate["snapshot_id"],
                candidate["plan_ref_id"],
                target_ref["plan_ref_id"],
                candidate["candidate_id"],
                candidate["canonical_place_id"],
                candidate["name"],
                candidate["brand"],
                candidate["area_or_address"],
                candidate["city"],
                candidate["longitude"],
                candidate["latitude"],
                segment_days,
                request_hash,
                now,
                candidate["segment_key"],
            )
            for row in existing_selections:
                await self._insert_carried_stay(conn, row, target_ref["plan_ref_id"], now=now)
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
                overnight_days=[f"Day {day}" for day in segment_days],
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
        self.stay_refresh_idempotency: dict[tuple[str, str], tuple[str, StaySuggestionView, str]] = {}

    async def refresh_stay_suggestions(self, resource: PublicResourceRecord, *, expected_etag: str,
        idempotency_key: str, now: datetime) -> tuple[StaySuggestionView, str, bool]:
        public_id = self.resources_by_understanding.get(resource.understanding_id)
        aggregate = self.resources.get(public_id)
        if aggregate is None:
            raise ResourceNotFoundError("trip resource does not exist")
        if aggregate["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        if public_id != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        key = (resource.understanding_id, _sha256_text(idempotency_key))
        request_hash = canonical_sha256({"expected_etag": expected_etag})
        previous = self.stay_refresh_idempotency.get(key)
        if previous:
            if previous[0] != request_hash:
                raise IdempotencyConflictError("stay refresh idempotency key was reused")
            return previous[1], previous[2], True
        stored = self.results.get(aggregate["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready")
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("stay refresh precondition changed")
        revision = int(aggregate["current_revision"])
        self._ensure_memory_stay_job(resource.understanding_id, revision, now=now, refresh=True)
        view = self._memory_stay_view(resource.understanding_id, revision)
        self.stay_refresh_idempotency[key] = (request_hash, view, stored.opaque_etag)
        return view, stored.opaque_etag, False

    def _ensure_memory_stay_job(
        self,
        understanding_id: str,
        revision: int,
        *,
        now: datetime,
        refresh: bool = False,
    ) -> dict[str, Any] | None:
        map_plan = self._memory_plan(understanding_id, revision)
        plan = stay_plan_from_map(map_plan)
        if plan is None:
            return None
        previous = [j for j in self.stay_jobs.values() if j["understanding_id"] == understanding_id
            and j["plan"].plan_ref.revision == revision and j["policy_hash"] == STAY_POLICY_SHA256]
        generation = 0
        if previous:
            existing = previous[-1]
            finished = existing.get("finished_at")
            cooldown = timedelta(minutes=15) if existing["status"] == "READY" else timedelta(seconds=30)
            if not refresh or existing["status"] in {"QUEUED", "BUILDING"} or finished is None or now - finished < cooldown:
                return existing
            if len([j for j in previous if j.get("refresh_generation", 0) > 0 and j["created_at"] > now - timedelta(minutes=15)]) >= 3:
                return existing
            generation = existing.get("refresh_generation", 0) + 1
        logical_key = canonical_sha256(
            {"plan_ref": plan.plan_ref.model_dump(mode="json"), "policy_hash": STAY_POLICY_SHA256, "refresh_generation": generation}
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
            "refresh_generation": generation,
            "created_at": now,
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
        key = f"{scored.candidate.provider_binding.get('segment_key', 'legacy')}:{scored.candidate.canonical_place_id}"
        token = item["tokens"].setdefault(key, secrets.token_urlsafe(24))
        row = {"public_candidate_token": token, "name": scored.candidate.name, "brand": scored.candidate.brand,
            "area_or_address": scored.candidate.area_or_address, "max_single_leg_minutes": scored.max_single_leg_minutes,
            "transfer_count": scored.transfer_count, "missing_leg_count": scored.missing_leg_count,
            "provider_binding_json": scored.candidate.provider_binding}
        assessment = assess_stay_commute(scored.legs, now=datetime.now(timezone.utc), expected_missing=scored.missing_leg_count)
        return _candidate_view(row, selected=selected, assessment=assessment)

    def _memory_stay_view(self, understanding_id: str, revision: int) -> StaySuggestionView:
        try:
            plan = self._memory_plan(understanding_id, revision)
        except ResourceNotReadyError:
            return StaySuggestionView(status="UNAVAILABLE", message="住宿建议需要先有行程地点")
        context = stay_context_hash(plan)
        primary = self.stay_selections.get((understanding_id, revision))
        selections = _current_selections([primary, *primary.get("additional_selections", [])], plan) if primary else []
        matching = [j for j in self.stay_jobs.values() if j["understanding_id"] == understanding_id
                    and (j["plan"].context_hash == context or (not j["plan"].context_hash and j["plan"].plan_ref.revision == revision))]
        if matching:
            job = matching[-1]
            if job["status"] in {"QUEUED", "BUILDING"}:
                return StaySuggestionView(status="PREPARING", message="正在按每晚返回和次日出发准备住宿候选")
            if job["output"]:
                output = job["output"]
                metadata = [dict(x) for x in output.provider_binding.get("segments", [])]
                pairs = []
                for scored in output.candidates:
                    binding = scored.candidate.provider_binding
                    if binding.get("segment_rank", 1) > 3 or _has_selected_night(binding, selections):
                        continue
                    pairs.append((self._memory_stay_candidate_view(job, scored), binding))
                    if not assess_stay_commute(scored.legs, now=datetime.now(timezone.utc), expected_missing=scored.missing_leg_count).complete:
                        for info in metadata:
                            if info.get("segment_key") == binding.get("segment_key") and info.get("status") == "READY":
                                info["status"] = "PARTIAL"
                pairs.extend((_memory_selected_view(x, stale=x["scored"].candidate.provider_binding.get("context_hash") != context),
                    _selection_binding(x["scored"].candidate.provider_binding, x)) for x in selections)
                return _segmented_view(metadata, pairs)
        if selections:
            metadata = _overnight_metadata(plan)
            return _segmented_view(metadata, [(_memory_selected_view(x, stale=True),
                _selection_binding(x["scored"].candidate.provider_binding, x)) for x in selections], stale=True)
        contexts = overnight_segments(plan)
        if any(len(x.preserved_place_ids) > 1 for x in contexts):
            return _segmented_view(_overnight_metadata(plan), [])
        if any(j["understanding_id"] == understanding_id for j in self.stay_jobs.values()):
            return StaySuggestionView(status="NEEDS_UPDATE", message="行程已修改，住宿通勤尚未更新")
        if contexts and all(x.preserved_hotels or x.uncertain for x in contexts):
            return _segmented_view(_overnight_metadata(plan), [])
        return StaySuggestionView(status="UNAVAILABLE", message="住宿待选择；需要相邻两日的已确认地点")

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
            and item["plan"].plan_ref.revision == self.resources[self.resources_by_understanding[item["understanding_id"]]]["current_revision"]
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
        if (
            item["status"] != "BUILDING"
            or item["lease_owner"] != job.lease_owner
            or item["attempt"] != job.attempt
            or item["lease_until"] <= now
        ):
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
        if (
            item is None
            or item["status"] != "BUILDING"
            or item["lease_owner"] != job.lease_owner
            or item["attempt"] != job.attempt
        ):
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

    def _copy_stay_selection_memory(self, understanding_id: str, source_revision: int, target_revision: int) -> None:
        source = self.stay_selections.get((understanding_id, source_revision))
        if not source:
            return
        plan = self._memory_plan(understanding_id, target_revision)
        selections = _current_selections([source, *source.get("additional_selections", [])], plan)
        if selections:
            selections[0]["additional_selections"] = selections[1:]
            self.stay_selections[(understanding_id, target_revision)] = selections[0]

    async def select_stay(self, resource: PublicResourceRecord, *, candidate_token: str, expected_etag: str,
                          idempotency_key: str, request_hash: str, now: datetime) -> StaySelectionOutcome:
        public_id = self.resources_by_understanding[resource.understanding_id]
        aggregate = self.resources[public_id]
        if aggregate["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        if public_id != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        revision = int(aggregate["current_revision"])
        scope = f"understanding:{resource.understanding_id}:stay-selection"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.stay_selection_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError("stay selection idempotency key was reused")
            return existing[1].model_copy(update={"replayed": True})
        stored = self.results.get(aggregate["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready for stay selection")
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("stay selection precondition does not match current result")
        map_plan = self._memory_plan(resource.understanding_id, revision)
        context = stay_context_hash(map_plan)
        candidates = [(job, scored) for job in self.stay_jobs.values()
            if job["understanding_id"] == resource.understanding_id and job["output"] is not None
            and job["plan"].context_hash == context for scored in job["output"].candidates
            if scored.candidate.provider_binding.get("segment_rank", 1) <= 3
            and scored.candidate.provider_binding.get("property_identity") == "OFFICIAL_NAME_ADDRESS"]
        selected = next(((job, scored) for job, scored in candidates if job["tokens"].get(
            f"{scored.candidate.provider_binding.get('segment_key', 'legacy')}:{scored.candidate.canonical_place_id}") == candidate_token), None)
        if selected is None:
            raise ResourceNotReadyError("stay candidate is no longer current")
        job, scored = selected
        binding = scored.candidate.provider_binding
        segment_days = list(binding.get("overnight_days", job["plan"].overnight_days))
        if not any(s.city == scored.candidate.city and s.overnight_days == segment_days and not s.preserved_hotels
                   and not s.uncertain and not excludes_hotel(s, scored.candidate.canonical_place_id) for s in overnight_segments(map_plan)):
            raise ResourceNotReadyError("stay segment is no longer available")
        selected_view = self._memory_stay_candidate_view(job, scored, selected=True)
        next_result = stored.result.model_copy(update={"map": MapReadinessView(status="NEEDS_UPDATE", message="住宿已选择，请更新路线", available_actions=["RENDER_MAP"]), "can_undo": True})
        result_id, opaque_etag, target_revision = str(uuid4()), f"tu3_{secrets.token_urlsafe(32)}", revision + 1
        self.results[result_id] = StoredResult(result=next_result, opaque_etag=opaque_etag)
        self.result_owners[result_id] = resource.understanding_id
        self.result_revisions[result_id] = target_revision
        if (resource.understanding_id, revision) in getattr(self, "g03_pipeline_inputs", {}):
            copied_input = dict(self.g03_pipeline_inputs[(resource.understanding_id, revision)])
            effective_now = max(now, datetime.now(timezone.utc))
            source_available = any(item["understanding_id"] == resource.understanding_id and job_id in self.sources
                and self.source_expiries.get(job_id, now) > effective_now for job_id, item in self.jobs.items())
            if not source_available:
                copied_input.pop("pending_lodgings", None)
            self.g03_pipeline_inputs[(resource.understanding_id, target_revision)] = copied_input
        old = self.stay_selections.get((resource.understanding_id, revision))
        retained = _current_selections([x for x in [old, *old.get("additional_selections", [])]
            if x["segment_key"] != binding.get("segment_key", "legacy")], map_plan) if old else []
        selection = {"view": selected_view, "scored": scored, "overnight_days": segment_days,
            "selected_place_id": scored.candidate.canonical_place_id, "selected_name": scored.candidate.name,
            "selected_city": scored.candidate.city, "longitude": scored.candidate.longitude, "latitude": scored.candidate.latitude,
            "segment_key": binding.get("segment_key", "legacy"), "additional_selections": retained}
        self.stay_selections[(resource.understanding_id, target_revision)] = selection
        aggregate.update(current_result_id=result_id, current_revision=target_revision, state="READY" if next_result.status == "READY" else "PARTIAL")
        applied = StaySelectionAppliedView(selected_stay=scored.candidate.name, overnight_days=[f"Day {d}" for d in segment_days])
        outcome = StaySelectionOutcome(applied=applied, opaque_etag=opaque_etag)
        self.stay_selection_idempotency[key] = (request_hash, outcome)
        return outcome

    def _delete_stay_memory(self, understanding_id: str) -> None:
        for job_id, item in list(self.stay_jobs.items()):
            if item["understanding_id"] == understanding_id:
                for logical_key, value in list(self.stay_jobs_by_key.items()):
                    if value == job_id:
                        self.stay_jobs_by_key.pop(logical_key, None)
                self.stay_jobs.pop(job_id, None)
        for key in list(self.stay_selections):
            if key[0] == understanding_id:
                self.stay_selections.pop(key, None)
        scope = f"understanding:{understanding_id}:stay-selection"
        for key in list(self.stay_selection_idempotency):
            if key[0] == scope:
                self.stay_selection_idempotency.pop(key, None)
        for key in list(self.stay_refresh_idempotency):
            if key[0] == understanding_id:
                self.stay_refresh_idempotency.pop(key, None)

    @property
    def stay_job_count(self) -> int:
        return len(self.stay_jobs)
