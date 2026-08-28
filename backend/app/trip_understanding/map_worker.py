from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings, get_settings
from app.db.connection import close_pool
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.repository import PostgresTripUnderstandingRepository


logger = logging.getLogger(__name__)


def build_configured_renderer(settings: Settings) -> MapRenderer:
    if settings.trip_understanding_provider_mode != "live":
        return MapRenderer()
    return MapRenderer(AmapRouteProvider(api_key=settings.amap_api_key))


class MapRenderWorker:
    def __init__(
        self,
        repository,
        *,
        renderer: MapRenderer | None = None,
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.renderer = renderer or MapRenderer()
        self.lease_seconds = lease_seconds

    async def run_once(self, worker_id: str, *, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(timezone.utc)
        job = await self.repository.claim_next_map(
            worker_id=worker_id,
            now=observed_at,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        try:
            plan = await self.repository.load_map_plan(job)
            output = await self.renderer.render(plan, observed_at=observed_at)
            await self.repository.complete_map_job(
                job,
                output,
                now=observed_at,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.repository.fail_map_job(
                job,
                category="MAP_RENDER_ERROR",
                now=observed_at,
            )
            logger.exception("map render job failed")
        return True


async def run_forever() -> None:
    settings = get_settings()
    worker_id = f"map-render:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    worker = MapRenderWorker(
        PostgresTripUnderstandingRepository(),
        renderer=build_configured_renderer(settings),
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
