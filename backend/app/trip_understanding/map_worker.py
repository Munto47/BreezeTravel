from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.config import Settings, get_settings
from app.db.connection import close_pool
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.errors import JobLeaseLostError, RouteProviderUnavailableError
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.stay import (
    AmapStayCandidateProvider,
    ControlledStayCandidateProvider,
    ControlledStayRouteProvider,
    StayRecommendationEngine,
)


logger = logging.getLogger(__name__)


class _LeaseTakeoverRouteProvider:
    async def route(self, origin, destination, mode, *, observed_at):
        del origin, destination, mode, observed_at
        raise RouteProviderUnavailableError(
            "LEASE_TAKEOVER_UNKNOWN_OUTCOME",
            provider_binding={
                "provider": "NOT_RETRIED_AFTER_LEASE_TAKEOVER",
                "external_calls": 0,
                "outcome": "UNKNOWN",
            },
            external_call_count=0,
        )


def build_configured_renderer(settings: Settings) -> MapRenderer:
    if settings.trip_understanding_provider_mode != "live":
        return MapRenderer()
    return MapRenderer(AmapRouteProvider(api_key=settings.amap_api_key))


def build_configured_stay_engine(settings: Settings) -> StayRecommendationEngine:
    if settings.trip_understanding_provider_mode != "live":
        return StayRecommendationEngine(
            ControlledStayCandidateProvider(),
            ControlledStayRouteProvider(),
        )
    return StayRecommendationEngine(
        AmapStayCandidateProvider(api_key=settings.amap_api_key),
        AmapRouteProvider(api_key=settings.amap_api_key),
    )


class _LeaseTakeoverStayCandidateProvider:
    async def search(self, **_kwargs):
        return []


class MapRenderWorker:
    def __init__(
        self,
        repository,
        *,
        renderer: MapRenderer | None = None,
        stay_engine: StayRecommendationEngine | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.renderer = renderer or MapRenderer()
        self.lease_takeover_renderer = MapRenderer(_LeaseTakeoverRouteProvider())
        self.stay_engine = stay_engine or StayRecommendationEngine()
        self.lease_takeover_stay_engine = StayRecommendationEngine(
            _LeaseTakeoverStayCandidateProvider(),
            ControlledStayRouteProvider(),
        )
        self.lease_seconds = lease_seconds

    async def _heartbeat(self, job, now_provider) -> None:
        interval_seconds = max(0.01, min(10.0, self.lease_seconds / 3))
        while True:
            await asyncio.sleep(interval_seconds)
            renewed = await self.repository.renew_map_lease(
                job,
                now=now_provider(),
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                raise JobLeaseLostError("map render job lease heartbeat was rejected")

    async def _run_with_heartbeat(self, job, operation, now_provider):
        operation_task = asyncio.create_task(operation)
        heartbeat_task = asyncio.create_task(self._heartbeat(job, now_provider))
        try:
            done, _pending = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is None:
                    raise JobLeaseLostError("map render heartbeat stopped")
                raise error
            return operation_task.result()
        finally:
            for task in (operation_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation_task, heartbeat_task, return_exceptions=True)

    async def _stay_heartbeat(self, job, now_provider) -> None:
        interval_seconds = max(0.01, min(10.0, self.lease_seconds / 3))
        while True:
            await asyncio.sleep(interval_seconds)
            renewed = await self.repository.renew_stay_lease(
                job,
                now=now_provider(),
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                raise JobLeaseLostError("stay recommendation job lease heartbeat was rejected")

    async def _run_stay_with_heartbeat(self, job, operation, now_provider):
        operation_task = asyncio.create_task(operation)
        heartbeat_task = asyncio.create_task(self._stay_heartbeat(job, now_provider))
        try:
            done, _pending = await asyncio.wait(
                {operation_task, heartbeat_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                if error is None:
                    raise JobLeaseLostError("stay recommendation heartbeat stopped")
                raise error
            return operation_task.result()
        finally:
            for task in (operation_task, heartbeat_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation_task, heartbeat_task, return_exceptions=True)

    async def run_once(self, worker_id: str, *, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(timezone.utc)
        monotonic_started = time.monotonic()

        def operation_now() -> datetime:
            return observed_at + timedelta(seconds=time.monotonic() - monotonic_started)

        job = await self.repository.claim_next_map(
            worker_id=worker_id,
            now=observed_at,
            lease_seconds=self.lease_seconds,
        )
        if job is not None:
            try:
                async def execute_render():
                    plan = await self.repository.load_map_plan(job)
                    renderer = (
                        self.lease_takeover_renderer if job.attempt > 1 else self.renderer
                    )
                    return await renderer.render(
                        plan,
                        observed_at=operation_now(),
                    )

                output = await self._run_with_heartbeat(
                    job,
                    execute_render(),
                    operation_now,
                )
                await self.repository.complete_map_job(
                    job,
                    output,
                    now=operation_now(),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                await self.repository.fail_map_job(
                    job,
                    category="MAP_RENDER_ERROR",
                    now=operation_now(),
                )
                logger.exception("map render job failed")
            return True

        stay_job = await self.repository.claim_next_stay(
            worker_id=worker_id,
            now=observed_at,
            lease_seconds=self.lease_seconds,
        )
        if stay_job is None:
            return False
        try:
            async def execute_stay():
                plan = await self.repository.load_stay_plan(stay_job)
                engine = (
                    self.lease_takeover_stay_engine
                    if stay_job.attempt > 1
                    else self.stay_engine
                )
                return await engine.recommend(
                    plan,
                    observed_at=operation_now(),
                )

            output = await self._run_stay_with_heartbeat(
                stay_job,
                execute_stay(),
                operation_now,
            )
            await self.repository.complete_stay_job(
                stay_job,
                output,
                now=operation_now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.repository.fail_stay_job(
                stay_job,
                category="STAY_RECOMMENDATION_ERROR",
                now=operation_now(),
            )
            logger.exception("stay recommendation job failed")
        return True


async def run_forever() -> None:
    settings = get_settings()
    worker_id = f"map-render:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    worker = MapRenderWorker(
        PostgresTripUnderstandingRepository(),
        renderer=build_configured_renderer(settings),
        stay_engine=build_configured_stay_engine(settings),
        lease_seconds=settings.map_render_job_lease_seconds,
    )
    try:
        while True:
            processed = await worker.run_once(worker_id)
            if not processed:
                await asyncio.sleep(settings.map_render_worker_poll_seconds)
    finally:
        await close_pool()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
