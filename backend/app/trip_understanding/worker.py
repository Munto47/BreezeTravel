from __future__ import annotations

import asyncio
import logging
import os
import socket
from datetime import datetime, timezone
from uuid import uuid4

from app.config import get_settings
from app.db.connection import close_pool
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.repository import (
    PostgresTripUnderstandingRepository,
    TripUnderstandingRepository,
)


logger = logging.getLogger(__name__)


class TripUnderstandingWorker:
    def __init__(
        self,
        repository: TripUnderstandingRepository,
        *,
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.pipeline = build_demo_pipeline()

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
            output = await self.pipeline.run(DEMO_SOURCE_TEXT)
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
            logger.exception("trip understanding fixture job failed")
        return True


async def run_forever() -> None:
    settings = get_settings()
    worker_id = f"trip-understanding:{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    worker = TripUnderstandingWorker(
        PostgresTripUnderstandingRepository(),
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
