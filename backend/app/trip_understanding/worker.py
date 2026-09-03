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
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.demo import build_demo_pipeline
from app.trip_understanding.errors import (
    InferenceProviderUnavailableError,
    JobLeaseLostError,
)
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.repository import (
    PostgresTripUnderstandingRepository,
    TripUnderstandingRepository,
)
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider


logger = logging.getLogger(__name__)


class _LeaseTakeoverInferenceProvider:
    async def propose(self, source_text: str):
        del source_text
        raise InferenceProviderUnavailableError(
            "LEASE_TAKEOVER_UNKNOWN_OUTCOME",
            provider_binding={
                "provider": "NOT_RETRIED_AFTER_LEASE_TAKEOVER",
                "external_calls": 0,
                "outcome": "UNKNOWN",
            },
            external_call_count=0,
        )


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
        self.lease_takeover_pipeline = build_full_text_pipeline(
            _LeaseTakeoverInferenceProvider()
        )

    async def _heartbeat(self, job, now_provider) -> None:
        interval_seconds = max(0.01, min(10.0, self.lease_seconds / 3))
        while True:
            await asyncio.sleep(interval_seconds)
            renewed = await self.repository.renew_lease(
                job,
                now=now_provider(),
                lease_seconds=self.lease_seconds,
            )
            if not renewed:
                raise JobLeaseLostError("understanding job lease heartbeat was rejected")

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
                    raise JobLeaseLostError("understanding job heartbeat stopped")
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
            if now is None:
                return datetime.now(timezone.utc)
            return observed_at + timedelta(seconds=time.monotonic() - monotonic_started)

        job = await self.repository.claim_next(
            worker_id=worker_id,
            now=observed_at,
            lease_seconds=self.lease_seconds,
        )
        if job is None:
            return False
        try:
            async def execute_pipeline():
                source = await self.repository.load_source(job, now=observed_at)
                if source.source_type == "FIXED_DEMO":
                    pipeline = self.demo_pipeline
                elif job.attempt > 1:
                    pipeline = self.lease_takeover_pipeline
                else:
                    pipeline = self.full_pipeline
                return await pipeline.run(
                    source.text,
                    requires_confirmation_spans=tuple(
                        (span.start, span.end)
                        for span in source.requires_confirmation_spans
                    ),
                    partial_source=source.partial_source,
                )

            output = await self._run_with_heartbeat(
                job,
                execute_pipeline(),
                operation_now,
            )
            await self.repository.complete_job(
                job,
                output,
                now=operation_now(),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.repository.fail_job(
                job,
                category="PIPELINE_ERROR",
                now=operation_now(),
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
    next_maintenance = time.monotonic()
    try:
        while True:
            processed = await worker.run_once(worker_id)
            if time.monotonic() >= next_maintenance:
                try:
                    await worker.repository.purge_expired_private_data(
                        now=datetime.now(timezone.utc),
                        limit=settings.screenshot_maintenance_batch_size,
                    )
                except Exception:
                    logger.exception("private source maintenance failed")
                next_maintenance = (
                    time.monotonic()
                    + settings.screenshot_maintenance_interval_seconds
                )
            if not processed:
                await asyncio.sleep(settings.trip_understanding_worker_poll_seconds)
    finally:
        try:
            await full_pipeline.aclose()
        finally:
            await close_pool()


def main() -> None:
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
