"""Durable daily dining jobs; reads never enqueue and edits never auto-recompute."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.daily_dining import DailyDiningView, DailyMealView, build_daily_meals, meal_context, project_daily_meals
from app.trip_understanding.map_render import MapRenderPlan
from app.trip_understanding.models import UserFacingTripResult


@dataclass(frozen=True)
class RecommendationTripView:
    revision: int
    etag: str
    result: UserFacingTripResult
    plan: MapRenderPlan
    source_type: str
    source_lunch_gaps: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DailyDiningJob:
    understanding_id: str
    revision: int
    attempt: int
    trip: RecommendationTripView


async def enqueue_initial_dining(conn, understanding_id: str, revision: int, request_key: str = "initial") -> None:
    await conn.execute("""INSERT INTO trip_daily_dining_jobs(understanding_id,revision,request_key)
        VALUES($1,$2,$3) ON CONFLICT DO NOTHING""", understanding_id, revision, request_key)


def _json(value):
    return json.loads(value) if isinstance(value, str) else value


async def read_daily_dining(repository, resource, *, request_key: str | None = None,
                            expected_etag: str | None = None, replay_info: dict | None = None):
    return await repository.read_daily_dining(resource, request_key=request_key,
                                              expected_etag=expected_etag, replay_info=replay_info)


def _project_daily_row(row, *, resource, etag, trip: RecommendationTripView | None = None):
    now = datetime.now(timezone.utc)
    protected = {}
    if trip is not None:
        for index, day in enumerate(trip.result.days, 1):
            meal, _, _ = meal_context(day, trip.plan.stops, source_gaps=trip.source_lunch_gaps)
            if meal.get("existing_activity_token") or sum(card.activity_token in trip.source_lunch_gaps for card in day.activities) > 1:
                protected[index] = DailyMealView.model_validate({**meal, "day_index": index})
    days = list(protected.values())
    if row is None:
        return DailyDiningView(status="NEEDS_UPDATE", message="行程已调整，更新中途用餐建议。", days=days)
    if row["status"] in ("QUEUED", "BUILDING"):
        return DailyDiningView(status="PREPARING", message="正在准备中途用餐建议，地点卡片已可使用。", days=days)
    if row["status"] == "UNAVAILABLE":
        return DailyDiningView(status="UNAVAILABLE", message="附近餐饮暂时不可用，可稍后更新。", days=days)
    if trip and dining_context_changed(row, trip):
        return DailyDiningView(status="NEEDS_UPDATE", message="用餐位置需要按原文调整，请更新中途用餐建议。", days=days)
    if row["finished_at"] is None or row["finished_at"] <= now - timedelta(minutes=15):
        return DailyDiningView(status="NEEDS_UPDATE", message="用餐建议已过期，请更新后再选择。", days=days)
    projected = {d.day_index: d for d in project_daily_meals(_json(row["payload_json"]),
        public_resource_id=resource.public_resource_id, etag=etag,
        now=now, expires_at=row["finished_at"] + timedelta(minutes=15))}
    projected.update(protected)
    return DailyDiningView(status="AVAILABLE", message="中途用餐建议", days=[projected[i] for i in sorted(projected)])


def dining_context_changed(row, trip: RecommendationTripView) -> bool:
    if not row or row["status"] != "READY":
        return False
    cached = {item["day_index"]: item for item in _json(row["payload_json"])}
    for index, day in enumerate(trip.result.days, 1):
        if not any(trip.source_lunch_gaps.get(card.activity_token) == "DAY_MIDPOINT" for card in day.activities):
            continue
        expected, _, _ = meal_context(day, trip.plan.stops, source_gaps=trip.source_lunch_gaps)
        previous = cached.get(index, {})
        if previous.get("candidates") and any(previous.get(key, False if key == "insert_before" else None) != expected.get(key, False if key == "insert_before" else None)
                                              for key in ("after_activity_token", "insert_before")):
            return True
    return False


class DailyDiningWorker:
    def __init__(self, repository, *, builder=build_daily_meals):
        self.repository = repository
        self.builder = builder

    async def run_once(self, worker_id: str, **_kwargs) -> bool:
        job = await self.repository.claim_daily_dining(worker_id)
        if job is None:
            return False
        is_demo = job.trip.source_type == "FIXED_DEMO"
        settings = get_settings()
        payload, status = [], "UNAVAILABLE"
        stats = {"execution_mode": "LIVE" if settings.trip_understanding_provider_mode == "live" and not is_demo else "CONTROLLED"}
        started = time.monotonic()
        if not is_demo:
            try:
                routes = AmapRouteProvider(api_key=settings.amap_api_key) if settings.trip_understanding_provider_mode == "live" and settings.amap_api_key else None
                async with asyncio.timeout(90):
                    payload = await self.builder(job.trip.result, job.trip.plan, routes=routes, stats=stats,
                                                 source_gaps=job.trip.source_lunch_gaps)
                status = "UNAVAILABLE" if payload and all(d["status"] == "UNAVAILABLE" for d in payload) else "READY"
            except asyncio.CancelledError:
                raise
            except Exception:
                status = "UNAVAILABLE"
        stats["latency_ms"] = round((time.monotonic()-started)*1000, 2)
        await self.repository.complete_daily_dining(job, worker_id=worker_id, status=status,
                                                   payload=payload, stats=stats, now=datetime.now(timezone.utc))
        return True
