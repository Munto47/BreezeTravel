"""Durable daily dining jobs; reads never enqueue and edits never auto-recompute."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.daily_dining import DailyDiningView, build_daily_meals, project_daily_meals
from app.trip_understanding.models import UserFacingTripResult
from app.trip_understanding.errors import RevisionConflictError


async def enqueue_initial_dining(conn, understanding_id: str, revision: int, request_key: str = "initial") -> None:
    await conn.execute("""INSERT INTO trip_daily_dining_jobs(understanding_id,revision,request_key)
        VALUES($1,$2,$3) ON CONFLICT DO NOTHING""", understanding_id, revision, request_key)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def read_daily_dining(repository, resource, *, request_key: str | None = None,
                            expected_etag: str | None = None):
    plan, etag = await repository.get_current_place_plan(resource)
    if expected_etag is not None and expected_etag != etag:
        raise RevisionConflictError()
    revision = plan.plan_ref.revision
    if not hasattr(repository, "_get_pool"):
        # Test repositories remain explicitly unavailable, never synthesize live restaurants.
        return DailyDiningView(status="UNAVAILABLE", message="附近餐饮暂时不可用。"), etag
    pool = await repository._get_pool()
    async with pool.acquire() as conn, conn.transaction():
        current = await conn.fetchval("SELECT current_revision FROM trip_understandings WHERE understanding_id=$1 AND state<>'DELETED' FOR UPDATE",
                                     resource.understanding_id)
        if current != revision:
            if request_key is not None:
                raise RevisionConflictError()
            return DailyDiningView(status="NEEDS_UPDATE", message="行程已调整，请刷新后更新建议。"), etag
        if request_key is not None:
            await enqueue_initial_dining(conn, resource.understanding_id, revision, request_key)
            await conn.execute("""UPDATE trip_daily_dining_jobs SET status='QUEUED',request_key=$3,
                attempts=CASE WHEN finished_at<NOW()-INTERVAL '15 minutes' THEN 0 ELSE attempts END
                WHERE understanding_id=$1 AND revision=$2 AND request_key<>$3
                AND ((status='UNAVAILABLE' AND finished_at < NOW()-INTERVAL '30 seconds'
                      AND (attempts<3 OR finished_at<NOW()-INTERVAL '15 minutes'))
                    OR (status='READY' AND finished_at<NOW()-INTERVAL '15 minutes'))""",
                resource.understanding_id, revision, request_key)
        row = await conn.fetchrow("SELECT * FROM trip_daily_dining_jobs WHERE understanding_id=$1 AND revision=$2",
                                  resource.understanding_id, revision)
    if row is None:
        return DailyDiningView(status="NEEDS_UPDATE", message="行程已调整，更新中途用餐建议。"), etag
    if row["status"] in ("QUEUED", "BUILDING"):
        return DailyDiningView(status="PREPARING", message="正在准备中途用餐建议，地点卡片已可使用。"), etag
    if row["status"] == "UNAVAILABLE":
        return DailyDiningView(status="UNAVAILABLE", message="附近餐饮暂时不可用，可稍后更新。"), etag
    if row["finished_at"] is None or row["finished_at"] < datetime.now(timezone.utc) - timedelta(minutes=15):
        return DailyDiningView(status="NEEDS_UPDATE", message="用餐建议已过期，请更新后再选择。"), etag
    return DailyDiningView(status="AVAILABLE", message="中途用餐建议",
        days=project_daily_meals(_json(row["payload_json"]), public_resource_id=resource.public_resource_id, etag=etag)), etag


class DailyDiningWorker:
    def __init__(self, repository, *, builder=build_daily_meals):
        self.repository = repository
        self.builder = builder

    async def run_once(self, worker_id: str, **_kwargs) -> bool:
        pool = await self.repository._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            # Expired dispatched jobs do not repeat unknown billable effects automatically.
            await conn.execute("""UPDATE trip_daily_dining_jobs SET status='UNAVAILABLE',
                lease_owner=NULL,lease_until=NULL,finished_at=NOW()
                WHERE status='BUILDING' AND lease_until<NOW()""")
            row = await conn.fetchrow("""SELECT j.* FROM trip_daily_dining_jobs j
                JOIN trip_understandings u ON u.understanding_id=j.understanding_id
                WHERE j.status='QUEUED' AND u.state NOT IN ('DELETED','FAILED')
                    AND u.current_revision=j.revision
                ORDER BY j.created_at FOR UPDATE OF j SKIP LOCKED LIMIT 1""")
            if row is None:
                return False
            await conn.execute("""UPDATE trip_daily_dining_jobs SET status='BUILDING',
                lease_owner=$3,lease_until=NOW()+INTERVAL '120 seconds',attempts=attempts+1
                WHERE understanding_id=$1 AND revision=$2""", row["understanding_id"], row["revision"], worker_id)
            plan = await self.repository._read_map_plan(conn, row["understanding_id"], row["revision"])
            raw = await conn.fetchval("""SELECT public_json FROM trip_understanding_results
                WHERE understanding_id=$1 AND revision=$2""", row["understanding_id"], row["revision"])
            is_demo = await conn.fetchval("""SELECT EXISTS(SELECT 1 FROM trip_understanding_sources
                WHERE understanding_id=$1 AND source_type='FIXED_DEMO')""", row["understanding_id"])
        settings = get_settings()
        payload, status = [], "UNAVAILABLE"
        stats = {"execution_mode": "LIVE" if settings.trip_understanding_provider_mode == "live" and not is_demo else "CONTROLLED"}
        started = time.monotonic()
        if raw is not None and not is_demo:
            try:
                routes = AmapRouteProvider(api_key=settings.amap_api_key) if settings.trip_understanding_provider_mode == "live" and settings.amap_api_key else None
                async with asyncio.timeout(90):
                    payload = await self.builder(UserFacingTripResult.model_validate(_json(raw)), plan, routes=routes, stats=stats)
                status = "UNAVAILABLE" if payload and all(d["status"] == "UNAVAILABLE" for d in payload) else "READY"
            except asyncio.CancelledError:
                raise
            except Exception:
                status = "UNAVAILABLE"
        async with pool.acquire() as conn:
            stats["latency_ms"] = round((time.monotonic()-started)*1000, 2)
            # Deleted resources cannot be recreated. A late result stays bound to its old revision.
            await conn.execute("""UPDATE trip_daily_dining_jobs SET status=$4,payload_json=$5::jsonb,metrics_json=$7::jsonb,
                finished_at=$6,lease_owner=NULL,lease_until=NULL
                WHERE understanding_id=$1 AND revision=$2 AND lease_owner=$3
                    AND status='BUILDING' AND lease_until>$6""",
                row["understanding_id"], row["revision"], worker_id, status,
                json.dumps(payload, ensure_ascii=False), datetime.now(timezone.utc), json.dumps(stats))
        return True
