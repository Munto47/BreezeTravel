from __future__ import annotations

import asyncio
import copy
import logging
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.trip_understanding.model_adapter import execution_config, record_model_calls
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
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded, inference_allowance


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


def build_configured_inference_provider(settings: Settings, config=None):
    """Use explicit itinerary configuration, independent of other model clients."""
    config = config or execution_config(settings)
    key = getattr(settings, config.credential_ref, "")
    return ExperienceQwenProvider(
        api_key=key, base_url=config.base_url, model=config.model,
        deadline_seconds=config.deadline_seconds, max_output_tokens=config.max_output_tokens,
        execution_config=config, enable_source_visits=True, relative_only=True,
    )


def build_configured_full_pipeline(settings: Settings):
    if settings.trip_understanding_provider_mode != "live":
        if settings.runtime_profile not in {"test", "local_fixture"}:
            raise ValueError("custom text requires live providers")
        return build_full_text_pipeline()
    qwen = build_configured_inference_provider(settings)
    amap = AmapPlaceResolver(
        api_key=settings.amap_api_key,
        deadline_seconds=settings.trip_understanding_amap_place_deadline_seconds,
    )
    pipeline = TripUnderstandingPipeline(
        qwen,
        amap,
        relative_only=True,
        max_place_concurrency=(
            settings.trip_understanding_amap_place_max_concurrency
        ),
    )

    pipeline.uses_execution_config = True
    return pipeline


class TripUnderstandingWorker:
    def __init__(
        self,
        repository: TripUnderstandingRepository,
        *,
        full_pipeline=None,
        supplement_provider=None,
        lease_seconds: int = 30,
    ) -> None:
        self.repository = repository
        self.lease_seconds = lease_seconds
        self.demo_pipeline = build_demo_pipeline()
        self.full_pipeline = full_pipeline if full_pipeline is not None else build_configured_full_pipeline(get_settings())
        self.supplement_provider = supplement_provider
        self.lease_takeover_pipeline = TripUnderstandingPipeline(
            _LeaseTakeoverInferenceProvider(), getattr(self.full_pipeline, "place_resolver", None),
        )

    async def _execution_pipeline(self, job, now):
        if not getattr(self.full_pipeline, "uses_execution_config", False):
            return self.full_pipeline, None
        try:
            config, _ = await self.repository.load_execution(job, now=now)
            provider = build_configured_inference_provider(get_settings(), config)
        except ValueError as exc:
            raise InferenceProviderUnavailableError("EXECUTION_CONFIG_UNAVAILABLE",
                provider_binding={"external_calls": 0}, external_call_count=0) from exc
        pipeline = copy.copy(self.full_pipeline)
        pipeline.inference_provider = provider
        return pipeline, provider

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
        if job.job_type == "SUPPLEMENT":
            await self._run_supplement(job, operation_now)
            return True
        owned_provider = None
        try:
            async def execute_pipeline():
                nonlocal owned_provider
                source = await self.repository.load_source(job, now=observed_at)
                source_binding = dict(source.internal_binding)
                collaboration_guard_active = (
                    source_binding.get("source_origin") == "COLLABORATION"
                )
                raw_guard_tokens = source_binding.get(
                    "collaboration_place_guard_tokens"
                )
                collaboration_guard_tokens = (
                    tuple(
                        token
                        for token in raw_guard_tokens
                        if isinstance(token, str)
                    )
                    if collaboration_guard_active
                    and isinstance(raw_guard_tokens, list)
                    else (() if collaboration_guard_active else None)
                )
                raw_city_guard = source_binding.get(
                    "collaboration_city_guard_token"
                )
                collaboration_city_guard = (
                    raw_city_guard if isinstance(raw_city_guard, str) else None
                )
                if source.source_type == "FIXED_DEMO":
                    pipeline = self.demo_pipeline
                elif job.attempt > 1 and source.initial_plan is None and await self.repository.has_dispatched_inference(job):
                    if await self.repository.recover_interrupted_progress(job, now=operation_now()):
                        raise JobLeaseLostError("interrupted job retained its saved preview")
                    pipeline = self.lease_takeover_pipeline
                else:
                    pipeline, owned_provider = await self._execution_pipeline(job, operation_now())

                progress_sequence = job.progress_sequence
                progress_lock = asyncio.Lock()

                async def persist_progress(update):
                    nonlocal progress_sequence
                    async with progress_lock:
                        progress_sequence += 1
                        await persist_ordered_progress(update.model_copy(update={"update_sequence": progress_sequence}))

                async def persist_ordered_progress(update):
                    if source_binding:
                        update = update.model_copy(
                            update={
                                "internal_binding": {
                                    **source_binding,
                                    **update.internal_binding,
                                }
                            }
                        )
                    accepted = await self.repository.record_progress(
                        job,
                        update,
                        now=operation_now(),
                    )
                    if not accepted:
                        raise JobLeaseLostError(
                            "understanding progress write was rejected"
                        )

                pipeline_options = {
                    "requires_confirmation_spans": tuple(
                        (span.start, span.end)
                        for span in source.requires_confirmation_spans
                    ),
                    "partial_source": source.partial_source,
                    "progress_callback": persist_progress,
                }
                if source.initial_plan is not None:
                    pipeline_options["prepared_plan"] = source.initial_plan
                if collaboration_guard_active:
                    pipeline_options.update(
                        {
                            "collaboration_guard_tokens": collaboration_guard_tokens,
                            "collaboration_city_guard_token": collaboration_city_guard,
                        }
                    )
                async def reserve_call():
                    return await self.repository.reserve_inference_call(job, now=operation_now())

                async def record_call(record):
                    await self.repository.record_model_call(job, record)

                with inference_allowance(reserve_call), record_model_calls(record_call):
                    return (
                        await pipeline.run(source.text, **pipeline_options),
                        source_binding,
                    )

            output, source_binding = await self._run_with_heartbeat(
                job,
                execute_pipeline(),
                operation_now,
            )
            if source_binding:
                output = output.model_copy(
                    update={
                        "inference_binding": {
                            **source_binding,
                            **output.inference_binding,
                        }
                    }
                )
            await self.repository.complete_job(
                job,
                output,
                now=operation_now(),
            )
        except asyncio.CancelledError:
            raise
        except JobLeaseLostError:
            # Cancellation or lease takeover already owns the durable outcome.
            # A late worker must not turn it into a failure or write more facts.
            logger.info("trip understanding worker stopped after losing its lease")
        except InferenceProviderUnavailableError as exc:
            await self.repository.fail_job(
                job,
                category=exc.category,
                now=operation_now(),
                allow_retry=False,
                provider_binding=exc.provider_binding,
            )
            logger.warning("trip inference unavailable; a new user request can retry")
        except InferenceAllowanceExceeded as exc:
            await self.repository.fail_job(job, category=str(exc), now=operation_now(), allow_retry=False)
        except Exception:
            await self.repository.fail_job(
                job,
                category="PIPELINE_ERROR",
                now=operation_now(),
            )
            logger.warning("trip understanding job failed safely")
        finally:
            if owned_provider is not None:
                await owned_provider.aclose()
        return True

    async def _run_supplement(self, job, operation_now):
        from app.trip_understanding.bounded_supplement import build_supplement_patch
        from app.trip_understanding.errors import ResourceAccessDeniedError, ResourceGoneError, RevisionConflictError, SourceUnavailableError
        from app.trip_understanding.supplement_jobs import SupplementRejectedError
        from app.trip_understanding.supplement_provider import BoundedSupplementProvider

        usage = {}
        owned_provider = None
        try:
            async def execute():
                nonlocal usage, owned_provider
                work = await self.repository.load_supplement_work(job, now=operation_now())
                pipeline, owned_provider = await self._execution_pipeline(job, operation_now())
                provider = self.supplement_provider or BoundedSupplementProvider(pipeline.inference_provider)
                async def reserve():
                    return await self.repository.reserve_inference_call(job, now=operation_now())
                async def record_call(record):
                    await self.repository.record_model_call(job, record)
                with inference_allowance(reserve), record_model_calls(record_call):
                    rows, usage = await provider.propose(work)
                    patch = await build_supplement_patch(work.source, work.plan, work.result, rows, pipeline)
                return work, patch
            work, patch = await self._run_with_heartbeat(job, execute(), operation_now)
            await self.repository.complete_supplement_job(job, work, patch, now=operation_now(), provider_binding=usage)
        except asyncio.CancelledError:
            raise
        except JobLeaseLostError:
            logger.info("supplement worker stopped after losing its lease")
        except Exception as exc:
            category = ({RevisionConflictError: "BASE_VERSION_CHANGED", ResourceGoneError: "RESOURCE_EXPIRED",
                ResourceAccessDeniedError: "OWNER_CHANGED", SourceUnavailableError: "SOURCE_UNAVAILABLE"}.get(type(exc), "SUPPLEMENT_FAILED"))
            if isinstance(exc, SupplementRejectedError):
                category = exc.reason
            elif isinstance(exc, InferenceAllowanceExceeded):
                category = str(exc)
            elif isinstance(exc, InferenceProviderUnavailableError):
                category, usage = exc.category, exc.provider_binding
            await self.repository.fail_supplement_job(job, category=category, now=operation_now(), provider_binding=usage)
            logger.warning("supplement did not complete; saved trip preserved")
        finally:
            if owned_provider is not None:
                await owned_provider.aclose()


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
                    logger.warning("private source maintenance failed safely")
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
