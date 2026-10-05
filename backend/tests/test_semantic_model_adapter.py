from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.trip_understanding.model_adapter import ExecutionConfig, SemanticModelAdapter, record_model_calls, usage_summary
from app.trip_understanding.worker import build_configured_inference_provider
from tests.test_experience_v3_journey import create, repository_for


def config(**overrides):
    return ExecutionConfig(**(dict(provider="KIMI_CODE", model="k3-256k", base_url="https://api.kimi.com/coding/v1",
        credential_ref="kimi_for_code") | overrides))


class Client:
    def __init__(self, response):
        self.response = response
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
        self.requests = []

    async def create(self, **options):
        self.requests.append(options)
        return self.response


@pytest.mark.parametrize("model", ["k3", "k3-256k"])
@pytest.mark.asyncio
async def test_k3_request_and_usage_are_recorded_after_parameter_conversion(model):
    reply = SimpleNamespace(model=model, choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{}'))],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=9, completion_tokens_details=SimpleNamespace(reasoning_tokens=5)))
    client = Client(reply)
    adapter = SemanticModelAdapter(config(model=model))
    saved = []
    async def sink(record):
        saved.append(record)
    with record_model_calls(sink):
        assert await adapter.complete(client, messages=[], max_tokens=1024, response_format={"type":"json_object"}) is reply
    request = client.requests[0]
    assert request["model"] == model and request["reasoning_effort"] == "high"
    assert "extra_body" not in request and "temperature" not in request
    assert saved[0]["status"] == "DISPATCHING" and saved[1]["status"] == "RECEIVED"
    assert saved[1]["reported_model"] == model and saved[1]["reasoning_tokens"] == 5
    assert not {"messages", "api_key", "reasoning_content"} & saved[1].keys()


def test_json_only_adapter_keeps_full_schema_and_does_not_change_validation_contract():
    schema = {"type":"object","properties":{"day":{"type":"integer"}},"required":["day"]}
    request = SemanticModelAdapter(config(output_mode="json_object")).request_options(
        messages=[{"role":"user","content":"Day2"}], max_tokens=1024,
        response_format={"type":"json_schema","json_schema":{"name":"visit","strict":True,"schema":schema}})
    assert request["response_format"] == {"type":"json_object"}
    assert '"required": ["day"]' in request["messages"][0]["content"]


def test_execution_snapshot_owns_provider_identity_and_limits():
    from app.trip_understanding.experience_inference import ExperienceQwenProvider
    snapshot = config(deadline_seconds=90, max_output_tokens=1024)
    provider = ExperienceQwenProvider(api_key="controlled", base_url="https://unused.invalid", model="ignored",
        deadline_seconds=30, max_output_tokens=4096, execution_config=snapshot, client=Client(None))
    assert provider.model == snapshot.model
    assert provider.deadline_seconds == 90 and provider.max_output_tokens == 1024


def test_k3_rejects_disabled_reasoning_and_provider_inference():
    for changes in ({"reasoning_effort":"none"}, {"provider":"QWEN"}):
        with pytest.raises(ValueError):
            config(**changes)


def test_itinerary_configuration_does_not_select_model_from_available_keys():
    settings = Settings(_env_file=None, kimi_for_code="configured", qwen_api_key="also-configured", kimi_model="unchanged-chat")
    provider = build_configured_inference_provider(settings)
    assert provider.model == "k3-256k" and provider.provider_name == "KIMI_CODE"
    assert settings.kimi_model == "unchanged-chat"


def test_partial_usage_keeps_known_tokens_and_unknown_count():
    result = usage_summary([{"input_tokens":7,"output_tokens":3}, {"input_tokens":None}])
    assert result["input_tokens"] == 7 and result["input_tokens_unknown_calls"] == 1
    assert result["reasoning_tokens"] is None and result["reasoning_tokens_unknown_calls"] == 2


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_source_configuration_survives_settings_change_and_task_takeover(kind, monkeypatch):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "snapshot", now)
        job = await repo.claim_next(worker_id="first", now=now, lease_seconds=60)
        first, dispatched = await repo.load_execution(job, now=now)
        assert first.model == "k3-256k" and not dispatched
        from app.trip_understanding import execution_repository
        monkeypatch.setattr(execution_repository, "get_settings", lambda: Settings(_env_file=None, trip_semantic_model="k3"))
        await repo.reserve_inference_call(job, now=now)
        second, dispatched = await repo.load_execution(job, now=now)
        assert second == first and dispatched
        assert "api_key" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_transport_usage_survives_late_response_and_downstream_failure():
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "usage", now)
        job = await repo.claim_next(worker_id="first", now=now, lease_seconds=60)
        await repo.reserve_inference_call(job, now=now)
        call = {"call_id":"one", "status":"DISPATCHING", "input_tokens":None}
        await repo.record_model_call(job, call)
        await repo._pool.execute("UPDATE trip_understanding_jobs SET lease_until=clock_timestamp()-interval '1 second'")
        await repo.record_model_call(job, call | {"status":"RECEIVED", "input_tokens":17})
        import json
        saved = json.loads(await repo._pool.fetchval("SELECT model_calls_json FROM trip_understanding_jobs"))
        assert saved["one"]["input_tokens"] == 17
        assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results") == 0


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_snapshot_budget_is_enforced_after_settings_change(kind, monkeypatch):
    from app.trip_understanding import execution_repository, repository
    from app.trip_understanding.inference_allowance import InferenceAllowanceExceeded
    settings = Settings(_env_file=None, trip_semantic_max_calls=1, trip_semantic_total_seconds=20)
    monkeypatch.setattr(execution_repository, "get_settings", lambda: settings)
    monkeypatch.setattr(repository, "get_settings", lambda: settings)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "bounded-snapshot", now)
        job = await repo.claim_next(worker_id="budget", now=now, lease_seconds=60)
        settings = Settings(_env_file=None, trip_semantic_max_calls=31, trip_semantic_total_seconds=600)
        await repo.reserve_inference_call(job, now=now)
        with pytest.raises(InferenceAllowanceExceeded, match="MODEL_CALL_BUDGET_EXHAUSTED"):
            await repo.reserve_inference_call(job, now=now)
        if kind == "postgres":
            deadline = await repo._pool.fetchval("SELECT inference_deadline_at FROM trip_understanding_sources")
        else:
            _, deadline = repo.inference_allowances[(job.understanding_id, job.input_hash)]
        assert now + timedelta(seconds=20) <= deadline < now + timedelta(seconds=21)


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_legacy_execution_requires_an_explicit_compatible_profile_without_resetting_budget(kind, monkeypatch):
    from app.trip_understanding import execution_repository
    settings = Settings(_env_file=None)
    monkeypatch.setattr(execution_repository, "get_settings", lambda: settings)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        await create(repo, "legacy", now)
        job = await repo.claim_next(worker_id="legacy", now=now, lease_seconds=60)
        await repo.reserve_inference_call(job, now=now)
        if kind == "postgres":
            await repo._pool.execute("UPDATE trip_understanding_sources SET execution_config_json=NULL")
        else:
            repo.execution_configs.clear()
        with pytest.raises(ValueError, match="legacy semantic execution configuration is missing"):
            await repo.load_execution(job, now=now)
        legacy = config(model="kimi-for-coding", reasoning_effort="none")
        settings = Settings(_env_file=None, trip_semantic_legacy_config=legacy.model_dump_json())
        restored, dispatched = await repo.load_execution(job, now=now)
        assert restored == legacy and dispatched
        if kind == "postgres":
            remaining = await repo._pool.fetchval("SELECT inference_calls_remaining FROM trip_understanding_sources")
        else:
            remaining, _ = repo.inference_allowances[(job.understanding_id, job.input_hash)]
        assert remaining == 30
