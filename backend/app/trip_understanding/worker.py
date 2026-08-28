from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from uuid import uuid4

from app.config import Settings, get_settings
from app.db.connection import close_pool
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.demo import build_demo_pipeline
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.repository import (
    PostgresTripUnderstandingRepository,
    TripUnderstandingRepository,
)
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider


logger = logging.getLogger(__name__)


def build_configured_full_pipeline(settings: Settings):
    if settings.trip_understanding_provider_mode != "live":
        return build_full_text_pipeline()
    qwen = QwenStructuredInferenceProvider(
        api_key=settings.qwen_api_key,
        base_url=settings.qwen_api_url,
        model=settings.trip_understanding_qwen_model,
        deadline_seconds=settings.trip_understanding_qwen_deadline_seconds,
        max_output_tokens=settings.trip_understanding_qwen_max_output_tokens,
        input_cny_per_million=(
            settings.trip_understanding_qwen_input_cny_per_million
        ),
        output_cny_per_million=(
            settings.trip_understanding_qwen_output_cny_per_million
        ),
    )
    amap = AmapPlaceResolver(
        api_key=settings.amap_api_key,
        deadline_seconds=settings.trip_understanding_amap_place_deadline_seconds,
    )
    return build_full_text_pipeline(
        qwen,
        amap,
        max_place_concurrency=(
            settings.trip_understanding_amap_place_max_concurrency
        ),
    )


class TripUnderstandingWorker:
    def __init__(
        self,
        repository: TripUnderstandingRepository,
        *,
        full_pipeline=None,
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.demo_pipeline = build_demo_pipeline()
        self.full_pipeline = full_pipeline or build_full_text_pipeline()

    async def run_once(self, worker_id: str, *, now: datetime | None = None) -> bool:
        observed_at = now or datetime.now(timezone.utc)
        job = await self.repository.claim_next(
            worker_id=worker_id,
            now=observed_at,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        try:
            source = await self.repository.load_source(job, now=observed_at)
            pipeline = self.demo_pipeline if source.source_type == "FIXED_DEMO" else self.full_pipeline
            output = await pipeline.run(source.text)
            await self.repository.complete_job(
                job,
                output,
                now=datetime.now(timezone.utc),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.repository.fail_job(
                job,
                category="PIPELINE_ERROR",
                now=datetime.now(timezone.utc),
            )
            logger.exception("trip understanding job failed")
        return True


async def run_forever() -> None:
    settings = get_settings()
    worker_id = f"trip-understanding:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    full_pipeline = build_configured_full_pipeline(settings)
    worker = TripUnderstandingWorker(
        PostgresTripUnderstandingRepository(),
        full_pipeline=full_pipeline,
        lease_seconds=settings.trip_understanding_job_lease_seconds,
    )
    try:
        while True:
            processed = await worker.run_once(worker_id)
            if not processed:
                await asyncio.sleep(settings.trip_understanding_worker_poll_seconds)
    finally:
        await close_pool()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
