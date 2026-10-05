"""Private model configuration and transport accounting for existing jobs."""
import json

from app.config import get_settings
from app.trip_understanding.errors import JobLeaseLostError, SourceUnavailableError
from app.trip_understanding.model_adapter import ExecutionConfig, execution_config


async def set_source_execution(conn, source_id, source_text=None, example_reference=None):
    config = execution_config(get_settings(), source_text=source_text, example_reference=example_reference)
    await conn.execute("""UPDATE trip_understanding_sources
        SET execution_config_json=$2::jsonb,inference_calls_remaining=$3 WHERE source_id=$1""",
                       source_id, config.model_dump_json(), config.max_calls)


def source_budget_seconds(source):
    value = source["execution_config_json"]
    if value is None:
        return 600  # Existing sources retain their original allowance.
    config = ExecutionConfig.model_validate_json(value) if isinstance(value, str) else ExecutionConfig.model_validate(value)
    return config.total_seconds


class PostgresExecutionRepositoryMixin:
    async def recover_interrupted_progress(self, job, *, now):
        from app.trip_understanding.models import PublicResourceRecord
        pool = await self._get_pool()
        row = await pool.fetchrow("""SELECT understanding_id,public_resource_id,state,current_result_id
            FROM trip_understandings WHERE understanding_id=$1 AND EXISTS (
                SELECT 1 FROM trip_understanding_events WHERE understanding_id=$1
                AND jsonb_typeof(public_payload_json->'snapshot')='object')""", job.understanding_id)
        if row is None:
            return False
        await self.cancel_understanding(PublicResourceRecord(**dict(row)),
            idempotency_key=f"interrupted:{job.job_id}:{job.attempt}", request_hash=job.input_hash,
            now=now, interrupted_job=job)
        return True

    async def load_execution(self, job, *, now):
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("""SELECT s.execution_config_json,s.source_id,s.deleted_at,s.retention_until,
                j.inference_dispatched_at FROM trip_understanding_jobs j
                JOIN trip_understanding_revisions r ON r.understanding_id=j.understanding_id AND r.revision=j.revision
                JOIN trip_understanding_sources s ON s.source_id=r.source_id
                WHERE j.job_id=$1 FOR UPDATE OF s""", job.job_id)
            if row is None or row["deleted_at"] is not None or row["retention_until"] <= now:
                raise SourceUnavailableError("semantic execution source unavailable")
            value = row["execution_config_json"]
            if value is None:
                config = execution_config(get_settings(), legacy=True)
                await conn.execute("UPDATE trip_understanding_sources SET execution_config_json=$2::jsonb WHERE source_id=$1",
                                   row["source_id"], config.model_dump_json())
            else:
                config = ExecutionConfig.model_validate_json(value) if isinstance(value, str) else ExecutionConfig.model_validate(value)
            return config, row["inference_dispatched_at"] is not None

    async def has_dispatched_inference(self, job):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval("SELECT inference_dispatched_at IS NOT NULL FROM trip_understanding_jobs WHERE job_id=$1", job.job_id)

    async def record_model_call(self, job, record):
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            # Accounting cannot publish a result or revive a deleted aggregate.
            # Late responses remain chargeable even after their lease expires.
            if record["status"] == "DISPATCHING":
                result = await conn.execute("""UPDATE trip_understanding_jobs
                    SET model_calls_json=model_calls_json || jsonb_build_object($2::text,$3::jsonb)
                    WHERE job_id=$1 AND status='RUNNING' AND lease_owner=$4 AND attempt=$5 AND lease_until>clock_timestamp()""",
                    job.job_id, record["call_id"], json.dumps(record), job.lease_owner, job.attempt)
                if result != "UPDATE 1":
                    raise JobLeaseLostError("semantic dispatch lease lost")
            else:
                await conn.execute("""UPDATE trip_understanding_jobs
                    SET model_calls_json=model_calls_json || jsonb_build_object($2::text,$3::jsonb)
                    WHERE job_id=$1 AND model_calls_json ? $2""", job.job_id, record["call_id"], json.dumps(record))


class InMemoryExecutionRepositoryMixin:
    async def recover_interrupted_progress(self, job, *, now):
        from app.trip_understanding.models import PublicResourceRecord
        if not any(e.payload.snapshot is not None for e in self.events.get(job.understanding_id, [])):
            return False
        public_id = self.resources_by_understanding[job.understanding_id]
        row = self.resources[public_id]
        await self.cancel_understanding(PublicResourceRecord(understanding_id=job.understanding_id,
            public_resource_id=public_id, state=row['state'], current_result_id=row.get('current_result_id')),
            idempotency_key=f"interrupted:{job.job_id}:{job.attempt}", request_hash=job.input_hash,
            now=now, interrupted_job=job)
        return True

    async def has_dispatched_inference(self, job):
        return self.jobs[job.job_id].get("inference_dispatched_at") is not None

    async def load_execution(self, job, *, now):
        await self.load_source(job, now=now)
        key = (job.understanding_id, job.input_hash)
        if key not in self.execution_configs:
            self.execution_configs[key] = execution_config(get_settings(), legacy=True)
        return self.execution_configs[key], await self.has_dispatched_inference(job)

    async def record_model_call(self, job, record):
        item = self.jobs.get(job.job_id)
        if item is not None:
            item.setdefault("model_calls", {})[record["call_id"]] = dict(record)
