"""Private model configuration and transport accounting for existing jobs."""
import json

from app.config import get_settings
from app.trip_understanding.errors import JobLeaseLostError, SourceUnavailableError
from app.trip_understanding.model_adapter import ExecutionConfig, execution_config


async def set_source_execution(conn, source_id):
    config = execution_config(get_settings())
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
