"""Worker interruption paths against isolated PostgreSQL with controlled providers."""
import asyncio
from datetime import datetime, timezone
import os

import pytest

from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.errors import JobLeaseLostError
from app.trip_understanding.inference_allowance import reserve_model_call
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_experience_v3_journey import create, repository_for


ROUNDS = range(int(os.environ.get('TRIPCHECK_RELIABILITY_ROUNDS', '1')))


class ControlledPipeline:
    place_resolver = None

    def __init__(self, output, *, wait=False):
        self.output = output
        self.wait = wait
        self.calls = 0
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run(self, source, **options):
        await reserve_model_call()
        self.calls += 1
        self.started.set()
        if self.wait:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise
        return self.output


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number', ROUNDS)
@pytest.mark.parametrize('stage', ['after_claim', 'after_response'])
async def test_interrupted_initial_attempt_distinguishes_unsent_from_unknown_outcome(round_number, stage, monkeypatch):
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    async with repository_for('postgres') as repo:
        now = datetime.now(timezone.utc)
        await create(repo, 'crash', now)
        pipeline = ControlledPipeline(output)
        worker = TripUnderstandingWorker(repo, full_pipeline=pipeline)
        method = 'load_source' if stage == 'after_claim' else 'complete_job'
        original = getattr(repo, method)

        async def crash(*args, **kwargs):
            raise asyncio.CancelledError()

        monkeypatch.setattr(repo, method, crash)
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once('interrupted')
        monkeypatch.setattr(repo, method, original)
        before = await repo._pool.fetchrow('SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources')
        assert await repo._pool.fetchval('SELECT status FROM trip_understanding_jobs') == 'RUNNING'
        assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_results') == 0
        assert pipeline.calls == (stage == 'after_response')
        await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
        assert await worker.run_once('successor')
        assert pipeline.calls == 1
        if stage == 'after_response':
            assert await repo._pool.fetchrow('SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources') == before
            assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_results') == 0
            assert await repo._pool.fetchval('SELECT status FROM trip_understanding_jobs') == 'FAILED'
        else:
            assert await repo._pool.fetchval('SELECT inference_calls_remaining FROM trip_understanding_sources') == before['inference_calls_remaining'] - 1
            assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_results') == 1
            assert await repo._pool.fetchval('SELECT status FROM trip_understanding_jobs') == 'SUCCEEDED'


@pytest.mark.asyncio
@pytest.mark.parametrize('round_number', ROUNDS)
@pytest.mark.parametrize('renewal', ['rejected', 'connection_lost'])
async def test_heartbeat_failure_cancels_external_work_and_expired_attempt_cannot_publish(round_number, renewal, monkeypatch):
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    async with repository_for('postgres') as repo:
        now = datetime.now(timezone.utc)
        await create(repo, 'heartbeat', now)
        pipeline = ControlledPipeline(output, wait=True)
        worker = TripUnderstandingWorker(repo, full_pipeline=pipeline, lease_seconds=1)
        captured = []

        async def renew(job, **kwargs):
            await pipeline.started.wait()
            captured.append(job)
            await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
            if renewal == 'connection_lost':
                raise ConnectionError('controlled renewal connection loss')
            return False

        monkeypatch.setattr(repo, 'renew_lease', renew)
        async with asyncio.timeout(5):
            assert await worker.run_once('heartbeat-worker')
        assert pipeline.cancelled.is_set() and pipeline.calls == 1 and len(captured) == 1
        with pytest.raises(JobLeaseLostError):
            await repo.complete_job(captured[0], output, now=datetime.now(timezone.utc))
        before = await repo._pool.fetchrow('SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources')
        assert await worker.run_once('replacement-worker')
        assert pipeline.calls == 1
        assert await repo._pool.fetchrow('SELECT inference_calls_remaining,inference_deadline_at FROM trip_understanding_sources') == before
        assert await repo._pool.fetchval('SELECT count(*) FROM trip_understanding_results') == 0
        assert await repo._pool.fetchval('SELECT status FROM trip_understanding_jobs') == 'FAILED'
