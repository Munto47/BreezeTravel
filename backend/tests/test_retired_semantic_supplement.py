"""Retired semantic providers can receive one explicit bounded replacement job."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.trip_understanding import execution_repository, repository, supplement_jobs, worker
from app.trip_understanding.errors import JobLeaseLostError, SourceUnavailableError
from app.trip_understanding.execution_repository import retired_kimi_supplement_execution
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.model_adapter import execution_config
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.supplement_jobs import SupplementRejectedError
from tests.test_bounded_supplement import CountingPlaces, add
from tests.test_experience_v3_journey import create, repository_for
from tests.test_supplement_jobs import edit_note, prepared, refresh


def qwen_settings(**changes):
    return Settings(_env_file=None, **(dict(trip_semantic_provider="QWEN", trip_semantic_model="qwen-controlled",
        trip_semantic_base_url="https://qwen.invalid/v1", trip_semantic_credential_ref="qwen_api_key",
        trip_semantic_reasoning_effort="none", qwen_api_key="controlled-secret", kimi_for_code="",
        trip_semantic_deadline_seconds=30, trip_semantic_total_seconds=60) | changes))


def use_settings(monkeypatch, settings):
    for module in (execution_repository, repository, supplement_jobs, worker):
        monkeypatch.setattr(module, "get_settings", lambda: settings)


@pytest.mark.parametrize("case", ["missing_key", "current_kimi", "source_qwen", "unknown_source"])
def test_replacement_requires_explicit_qwen_and_identified_kimi(case):
    source = execution_config(Settings(_env_file=None)).model_dump()
    settings = qwen_settings()
    if case == "missing_key":
        settings = qwen_settings(qwen_api_key="")
    elif case == "current_kimi":
        settings = Settings(_env_file=None, kimi_for_code="controlled")
    elif case == "source_qwen":
        source = execution_config(settings).model_dump()
    else:
        source = None
    assert retired_kimi_supplement_execution(source, settings) is None


@pytest.mark.asyncio
async def test_memory_replacement_snapshot_does_not_mutate_source_or_old_allowance(monkeypatch):
    use_settings(monkeypatch, Settings(_env_file=None))
    async with repository_for("memory") as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "legacy-execution", now)
        original = await repo.claim_next(worker_id="memory", now=now, lease_seconds=60)
        key = (original.understanding_id, original.input_hash)
        old = repo.execution_configs[key]
        allowance = (0, now - timedelta(hours=1))
        repo.inference_allowances[key] = allowance
        outcome = retired_kimi_supplement_execution(old.model_dump(), qwen_settings())
        item = repo.jobs[original.job_id]
        item.update(job_type="SUPPLEMENT", supplement_outcome_json=outcome)
        job = original.model_copy(update={"job_type": "SUPPLEMENT"})
        use_settings(monkeypatch, qwen_settings(trip_semantic_model="later-model"))
        config, dispatched = await repo.load_execution(job, now=now)
        assert config.provider == "QWEN" and config.model == "qwen-controlled" and not dispatched
        assert "controlled-secret" not in json.dumps(outcome)
        assert repo.execution_configs[key] == old and repo.inference_allowances[key] == allowance
        item["lease_until"] = now - timedelta(seconds=1)
        with pytest.raises(JobLeaseLostError):
            await repo.load_execution(job, now=now)
        item["lease_until"] = now + timedelta(seconds=60)
        repo.sources.pop(job.job_id)
        with pytest.raises(SourceUnavailableError):
            await repo.load_execution(job, now=now)


async def source_state(repo, resource):
    return dict(await repo._pool.fetchrow("""SELECT execution_config_json,inference_calls_remaining,
        inference_deadline_at FROM trip_understanding_sources WHERE understanding_id=$1""", resource.understanding_id))


async def old_trip(repo, monkeypatch, *, expired=True):
    use_settings(monkeypatch, Settings(_env_file=None))
    now, resource, stored = await prepared(repo)
    await edit_note(repo, resource, stored)
    resource, stored = await refresh(repo, resource)
    if expired:
        await repo._pool.execute("""UPDATE trip_understanding_sources SET inference_calls_remaining=0,
            inference_deadline_at=clock_timestamp()-interval '1 hour' WHERE understanding_id=$1""", resource.understanding_id)
    use_settings(monkeypatch, qwen_settings())
    return now, resource, stored


class ControlledClient:
    def __init__(self, operations=None, *, failure=None):
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.requests = []
        self.operations = operations or []
        self.failure = failure

    async def create(self, **options):
        self.requests.append(options)
        if self.failure:
            raise self.failure
        return SimpleNamespace(model=options["model"], usage=None, choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"operations": self.operations})))])


def configured_worker(repo, monkeypatch, client):
    def provider(settings, config=None):
        return ExperienceQwenProvider(api_key=getattr(settings, config.credential_ref), base_url=config.base_url,
            model=config.model, execution_config=config, client=client)
    monkeypatch.setattr(worker, "build_configured_inference_provider", provider)
    places = CountingPlaces()
    pipeline = TripUnderstandingPipeline(None, places)
    pipeline.uses_execution_config = True
    return worker.TripUnderstandingWorker(repo, full_pipeline=pipeline), places


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_old_kimi_supplement_uses_new_snapshot_preserving_edits_and_source(monkeypatch, failure):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch)
        original_source = await source_state(repo, resource)
        assert (await repo.get_supplement_state(resource, now=now)).available_actions == ["REQUEST"]
        accepted = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="replace-kimi", retry=False, now=now)
        repeated = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="replace-kimi", retry=False, now=now)
        assert repeated == accepted
        use_settings(monkeypatch, qwen_settings(trip_semantic_model="settings-changed-after-request"))
        client = ControlledClient([add(after_visit_id=stored.result.days[0].activities[0].visit_id)],
            failure=TimeoutError("controlled timeout") if failure else None)
        runner, places = configured_worker(repo, monkeypatch, client)
        assert await runner.run_once("replacement")
        resource, saved = await refresh(repo, resource)
        state = await repo.get_supplement_state(resource, now=now)
        assert state.status == ("FAILED" if failure else "APPLIED")
        assert len(client.requests) == 1 and client.requests[0]["model"] == "qwen-controlled"
        assert places.queries == ([] if failure else ["景山公园"])
        assert saved.result.days[0].activities[0].note == "用户刚刚保存的备注"
        assert await source_state(repo, resource) == original_source
        if failure:
            assert saved == stored
        else:
            assert [card.name for day in saved.result.days for card in day.activities] == ["故宫博物院", "景山公园", "故宫博物院"]
        outcome = json.loads(await repo._pool.fetchval("SELECT supplement_outcome_json FROM trip_understanding_jobs WHERE job_id=$1", accepted.job_id))
        assert outcome["execution_config"]["model"] == "qwen-controlled"
        assert "controlled-secret" not in json.dumps(outcome)
        deadline = datetime.fromisoformat(outcome["inference_deadline_at"])
        assert now < deadline <= datetime.now(timezone.utc) + timedelta(seconds=30)


@pytest.mark.asyncio
@pytest.mark.parametrize("configuration", ["missing_key", "current_kimi"])
async def test_unconfigured_replacement_fails_without_calling_kimi_or_changing_result(monkeypatch, configuration):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch, expired=False)
        settings = qwen_settings(qwen_api_key="") if configuration == "missing_key" else Settings(_env_file=None, kimi_for_code="controlled")
        use_settings(monkeypatch, settings)
        await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="unavailable", retry=False, now=now)
        client = ControlledClient()
        runner, _ = configured_worker(repo, monkeypatch, client)
        assert await runner.run_once("unavailable")
        resource, saved = await refresh(repo, resource)
        assert saved == stored and client.requests == []
        state = await repo.get_supplement_state(resource, now=now)
        assert state.status == "FAILED" and state.reason == "EXECUTION_CONFIG_UNAVAILABLE"


async def failed_legacy_requests(repo, resource, stored, monkeypatch, *, category="EXECUTION_CONFIG_UNAVAILABLE", error=None):
    use_settings(monkeypatch, Settings(_env_file=None))
    for index in range(2):
        now = datetime.now(timezone.utc)
        await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key=f"old-{index}", retry=bool(index), now=now)
        job = await repo.claim_next(worker_id="old-worker", now=now, lease_seconds=60)
        if error:
            await repo.reserve_inference_call(job, now=now)
            await repo.record_model_call(job, {"call_id": f"call-{index}", "status": "DISPATCHING", "provider": "KIMI_CODE"})
            await repo.record_model_call(job, {"call_id": f"call-{index}", "status": "FAILED", "provider": "KIMI_CODE", "error_category": error})
        await repo.fail_supplement_job(job, category=category, now=now, provider_binding={"external_calls": 1 if error else 0})
    use_settings(monkeypatch, qwen_settings())


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [None, "PermissionDeniedError", "AuthenticationError"])
async def test_two_definite_kimi_failures_get_only_one_concurrent_migration_request(monkeypatch, error):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch, expired=False)
        await failed_legacy_requests(repo, resource, stored, monkeypatch,
            category="SUPPLEMENT_PROVIDER_FAILED" if error else "EXECUTION_CONFIG_UNAVAILABLE", error=error)
        assert (await repo.get_supplement_state(resource, now=now)).available_actions == ["RETRY"]
        async def request(key):
            return await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
                idempotency_key=key, retry=True, now=now)
        results = await asyncio.gather(request("extra-one"), request("extra-two"), return_exceptions=True)
        accepted = [value for value in results if not isinstance(value, Exception)]
        rejected = [value for value in results if isinstance(value, Exception)]
        assert len(accepted) == len(rejected) == 1 and isinstance(rejected[0], SupplementRejectedError)
        success_key = "extra-one" if not isinstance(results[0], Exception) else "extra-two"
        assert await request(success_key) == accepted[0]
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_jobs WHERE understanding_id=$1 AND job_type='SUPPLEMENT'", resource.understanding_id) == 3
        assert await repo._pool.fetchval("SELECT supplement_requests FROM trip_understanding_sources WHERE understanding_id=$1", resource.understanding_id) == 2
        await repo.cancel_supplement(resource, accepted[0].job_id, now=now)
        from app.trip_understanding.models import ActivityMoveCommand
        await repo.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=2, target_position=0),
            expected_etag=stored.opaque_etag, idempotency_key="new-version-after-extra", request_hash="f" * 64, now=now)
        resource, stored = await refresh(repo, resource)
        with pytest.raises(SupplementRejectedError, match="SUPPLEMENT_LIMIT"):
            await request("extra-fourth")
        assert (await repo.get_supplement_state(resource, now=now)).available_actions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("category,error", [("LEASE_TAKEOVER_UNKNOWN_OUTCOME", None),
    ("SUPPLEMENT_PROVIDER_FAILED", "APITimeoutError"), ("SUPPLEMENT_PROVIDER_FAILED", "APIConnectionError")])
async def test_unknown_kimi_outcome_never_receives_an_extra_request(monkeypatch, category, error):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch, expired=False)
        await failed_legacy_requests(repo, resource, stored, monkeypatch, category=category, error=error)
        assert (await repo.get_supplement_state(resource, now=now)).available_actions == []
        with pytest.raises(SupplementRejectedError, match="SUPPLEMENT_LIMIT"):
            await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
                idempotency_key="not-authorized", retry=True, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["edit", "cancel", "source_delete", "expired_lease", "deadline"])
async def test_replacement_still_rejects_stale_or_deleted_work(monkeypatch, change):
    from app.trip_understanding.bounded_supplement import build_supplement_patch
    from app.trip_understanding.errors import RevisionConflictError
    from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch)
        await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="bounded-replacement", retry=False, now=now)
        job = await repo.claim_next(worker_id="bounded", now=now, lease_seconds=60)
        work = await repo.load_supplement_work(job, now=now)
        config, dispatched = await repo.load_execution(job, now=now)
        assert config.provider == "QWEN" and not dispatched
        await repo.reserve_inference_call(job, now=now)
        with pytest.raises(SupplementRejectedError, match="SUPPLEMENT_ALREADY_DISPATCHED"):
            await repo.reserve_inference_call(job, now=now)
        patch = await build_supplement_patch(work.source, work.plan, work.result,
            [add(after_visit_id=work.result.days[0].activities[0].visit_id)], TripUnderstandingPipeline(None, CountingPlaces()))
        expected = JobLeaseLostError
        if change == "edit":
            from app.trip_understanding.models import ActivityMoveCommand
            await repo.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=2, target_position=0),
                expected_etag=stored.opaque_etag, idempotency_key="concurrent-edit", request_hash="e" * 64, now=now)
            expected = RevisionConflictError
        elif change == "cancel":
            await repo.cancel_supplement(resource, job.job_id, now=now)
        elif change == "source_delete":
            await repo.delete_source(resource, user_id="experience-owner", request_hash="d" * 64,
                idempotency_key="delete-source", now=now)
            expected = SourceUnavailableError
        elif change == "expired_lease":
            await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=$1", job.job_id)
        else:
            await repo._pool.execute("""UPDATE trip_understanding_jobs SET supplement_outcome_json=
                jsonb_set(supplement_outcome_json,'{inference_deadline_at}',to_jsonb((clock_timestamp()-interval '1 second')::text)) WHERE job_id=$1""", job.job_id)
            expected = InferenceAllowanceExceeded
        resource, before = await refresh(repo, resource)
        with pytest.raises(expected):
            await repo.complete_supplement_job(job, work, patch, now=now, provider_binding={"external_calls": 1})
        resource, after = await refresh(repo, resource)
        assert after == before


@pytest.mark.asyncio
async def test_legacy_source_profile_is_not_rewritten_when_supplement_migrates(monkeypatch):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch)
        old_config = execution_config(Settings(_env_file=None))
        await repo._pool.execute("UPDATE trip_understanding_sources SET execution_config_json=NULL WHERE understanding_id=$1", resource.understanding_id)
        use_settings(monkeypatch, qwen_settings(trip_semantic_legacy_config=old_config.model_dump_json()))
        original_source = await source_state(repo, resource)
        await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="legacy-config", retry=False, now=now)
        job = await repo.claim_next(worker_id="legacy-config", now=now, lease_seconds=60)
        selected, _ = await repo.load_execution(job, now=now)
        assert selected.provider == "QWEN"
        await repo.reserve_inference_call(job, now=now)
        assert await source_state(repo, resource) == original_source


@pytest.mark.asyncio
async def test_existing_qwen_source_cannot_reset_its_budget_by_requesting_supplement(monkeypatch):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch)
        await repo._pool.execute("UPDATE trip_understanding_sources SET execution_config_json=$2::jsonb WHERE understanding_id=$1",
            resource.understanding_id, execution_config(qwen_settings()).model_dump_json())
        assert (await repo.get_supplement_state(resource, now=now)).available_actions == []
        with pytest.raises(SupplementRejectedError, match="MODEL_CALL_DEADLINE_EXCEEDED"):
            await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
                idempotency_key="no-reset", retry=False, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("prior", ["success", "migration", "cancelled"])
async def test_prior_success_migration_or_cancel_never_creates_extra_allowance(monkeypatch, prior):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch, expired=False)
        await failed_legacy_requests(repo, resource, stored, monkeypatch)
        job_id = await repo._pool.fetchval("""SELECT job_id FROM trip_understanding_jobs WHERE understanding_id=$1
            AND job_type='SUPPLEMENT' ORDER BY created_at LIMIT 1""", resource.understanding_id)
        if prior == "success":
            await repo._pool.execute("UPDATE trip_understanding_jobs SET status='SUCCEEDED',supplement_outcome_json=$2::jsonb WHERE job_id=$1",
                job_id, json.dumps({"added_count": 1}))
        elif prior == "cancelled":
            await repo._pool.execute("UPDATE trip_understanding_jobs SET status='CANCELLED' WHERE job_id=$1", job_id)
        else:
            outcome = retired_kimi_supplement_execution(execution_config(Settings(_env_file=None)).model_dump(), qwen_settings())
            await repo._pool.execute("UPDATE trip_understanding_jobs SET supplement_outcome_json=$2::jsonb WHERE job_id=$1", job_id, json.dumps(outcome))
        with pytest.raises(SupplementRejectedError, match="SUPPLEMENT_LIMIT"):
            await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
                idempotency_key="prior-event", retry=True, now=now)


@pytest.mark.asyncio
async def test_migration_round_constraints_reject_unmarked_extra_and_keep_existing_limits(monkeypatch):
    import asyncpg
    from pathlib import Path
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch, expired=False)
        await failed_legacy_requests(repo, resource, stored, monkeypatch)
        accepted = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="valid-extra", retry=True, now=now)
        await repo._pool.execute(Path("app/db/migrations/046_retired_semantic_supplement.sql").read_text(encoding="utf8"))
        for assignment in ("supplement_outcome_json='{}'::jsonb", "supplement_round=4", "job_type='UNDERSTAND'", "supplement_round=0"):
            with pytest.raises(asyncpg.CheckViolationError):
                await repo._pool.execute(f"UPDATE trip_understanding_jobs SET {assignment} WHERE job_id=$1", accepted.job_id)
        with pytest.raises(asyncpg.CheckViolationError):
            await repo._pool.execute("UPDATE trip_understanding_sources SET supplement_requests=3 WHERE understanding_id=$1", resource.understanding_id)
        with pytest.raises(asyncpg.UniqueViolationError):
            await repo._pool.execute("""INSERT INTO trip_understanding_jobs(job_id,understanding_id,revision,job_type,status,
                input_hash,supplement_round,base_public_resource_id,base_etag,base_owner_user_id,base_anonymous_session_id,
                available_at,created_at,updated_at,supplement_outcome_json)
                SELECT gen_random_uuid()::text,understanding_id,revision,job_type,status,input_hash,supplement_round,
                    base_public_resource_id,base_etag,base_owner_user_id,base_anonymous_session_id,
                    available_at,created_at,updated_at,supplement_outcome_json FROM trip_understanding_jobs WHERE job_id=$1""", accepted.job_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [False, True])
async def test_replacement_takeover_keeps_snapshot_and_never_repeats_unknown_call(monkeypatch, dispatched):
    async with repository_for("postgres") as repo:
        now, resource, stored = await old_trip(repo, monkeypatch)
        accepted = await repo.request_supplement(resource, expected_etag=stored.opaque_etag,
            idempotency_key="replacement-takeover", retry=False, now=now)
        old = await repo.claim_next(worker_id="interrupted", now=now, lease_seconds=60)
        await repo.load_supplement_work(old, now=now)
        if dispatched:
            await repo.reserve_inference_call(old, now=now)
        await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE job_id=$1", old.job_id)
        use_settings(monkeypatch, qwen_settings(trip_semantic_model="changed-during-takeover"))
        client = ControlledClient()
        runner, _ = configured_worker(repo, monkeypatch, client)
        assert await runner.run_once("replacement-worker")
        state = await repo.get_supplement_state(resource, now=now)
        assert state.status == ("FAILED" if dispatched else "NO_CHANGES")
        assert len(client.requests) == (0 if dispatched else 1)
        if dispatched:
            assert state.reason == "LEASE_TAKEOVER_UNKNOWN_OUTCOME"
        else:
            assert client.requests[0]["model"] == "qwen-controlled"
        outcome = json.loads(await repo._pool.fetchval("SELECT supplement_outcome_json FROM trip_understanding_jobs WHERE job_id=$1", accepted.job_id))
        assert outcome["execution_reason"] == "RETIRED_KIMI_TO_QWEN"
        assert outcome["execution_config"]["model"] == "qwen-controlled"
        resource, after = await refresh(repo, resource)
        assert after == stored
