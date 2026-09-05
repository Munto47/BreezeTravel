from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import experience_main as runtime
from app.config import Settings
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.worker import build_configured_full_pipeline
from app.trip_understanding.worker import TripUnderstandingWorker
from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import CreateFullRequest
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService


@pytest.fixture
def client(monkeypatch):
    cfg = Settings(_env_file=None, runtime_profile="test", experience_workers_enabled=False,
                   require_schema_check=False, jwt_secret_key="unit-only-key")
    cache = AsyncMock()
    cache.eval.return_value = 1
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    monkeypatch.setattr(runtime, "get_settings", lambda: cfg)
    monkeypatch.setattr(runtime.Redis, "from_url", lambda *_a, **_kw: cache)
    monkeypatch.setattr(runtime.connection, "get_pool", AsyncMock(return_value=pool))
    monkeypatch.setattr(runtime.connection, "close_pool", AsyncMock())
    app = runtime.create_app()

    @app.get("/test-private-failure")
    async def private_failure():
        raise ValueError("SECRET-and-private-itinerary")

    with TestClient(app, raise_server_exceptions=False) as http:
        yield http, cache
    cache.aclose.assert_awaited_once()


def test_minimal_runtime_exposes_only_text_account_and_health_routes():
    routes = {route.path for route in runtime.create_app().routes}
    assert "/api/v3/trip-understandings" in routes
    assert "/api/auth/email-login" in routes
    assert "/health" in routes
    assert not any(any(word in path for word in ("room", "planner", "screenshot", "ocr", "test-login", "send-code", "wechat", "docs", "openapi")) for path in routes)


def test_health_and_invalid_input_hide_details(client):
    http, _cache = client
    health = http.get("/health")
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    response = http.post("/api/auth/email-login", json={"email": ["private@example.invalid"], "password": {"secret": "private-value"}})
    assert response.status_code == 422
    assert "private" not in response.text
    for key, value in runtime.PRIVATE_HEADERS.items():
        assert response.headers[key] == value


def test_exception_and_rate_limit_fail_closed_without_details(client):
    http, cache = client
    failed = http.get("/test-private-failure")
    assert failed.status_code == 503 and "SECRET" not in failed.text
    cache.eval.return_value = 0
    limited = http.post("/api/auth/email-login", json={})
    assert limited.status_code == 429 and limited.headers["Retry-After"] == "60"
    cache.eval.side_effect = RuntimeError("redis://private-password")
    unavailable = http.post("/api/auth/email-login", json={})
    assert unavailable.status_code == 503 and "password" not in unavailable.text
    assert unavailable.headers["Cache-Control"] == "no-store"


def test_cross_origin_write_is_rejected_before_processing(client):
    http, cache = client
    response = http.post("/api/auth/email-login", json={}, headers={"Origin": "https://outside.invalid"})
    assert response.status_code == 403
    cache.eval.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_text_uses_direct_model_provider_without_rule_fallback():
    cfg = Settings(_env_file=None, runtime_profile="test", trip_understanding_provider_mode="live",
                   qwen_api_key="unit-only", amap_api_key="unit-only",
                   trip_understanding_qwen_model="unit-only-model")
    pipeline = build_configured_full_pipeline(cfg)
    try:
        assert isinstance(pipeline.inference_provider, ExperienceQwenProvider)
        assert pipeline.inference_provider.deadline_seconds == 30
        assert pipeline.inference_provider.max_output_tokens == 4096
    finally:
        await pipeline.aclose()


def test_fixture_text_requires_explicit_test_or_fixture_profile():
    cfg = Settings(_env_file=None, runtime_profile="local_real", trip_understanding_provider_mode="fixture")
    with pytest.raises(ValueError, match="requires live"):
        build_configured_full_pipeline(cfg)


@pytest.mark.asyncio
async def test_failed_inference_retains_metrics_but_never_fake_result_or_auto_retry():
    class UnavailablePipeline:
        calls = 0

        async def run(self, *_args, **_kwargs):
            self.calls += 1
            raise InferenceProviderUnavailableError(
                "DEADLINE_EXCEEDED", external_call_count=1,
                provider_binding={"provider": "UNIT", "external_calls": 1,
                                  "latency_ms": 30_000, "estimated_cost_cny": None,
                                  "source_text": "never persist this text"},
            )

    repo = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repo)
    now = datetime.now(timezone.utc)
    created = await service.create_full(
        CreateFullRequest(mode="FULL", source={"type": "TEXT", "text": "北京第一天游览故宫"}),
        owner_user_id="unit-owner", idempotency_key="failed-model-unit", now=now,
    )
    pipeline = UnavailablePipeline()
    worker = TripUnderstandingWorker(repo, full_pipeline=pipeline)
    assert await worker.run_once("unit-worker", now=now)
    assert not await worker.run_once("unit-worker", now=now + timedelta(minutes=1))
    assert pipeline.calls == 1 and not repo.results
    job = next(iter(repo.jobs.values()))
    assert job["status"] == "FAILED" and job["last_error_category"] == "DEADLINE_EXCEEDED"
    assert job["provider_binding"] == {"provider": "UNIT", "external_calls": 1,
                                       "latency_ms": 30_000, "estimated_cost_cny": None}
    assert repo.resources[created.accepted.public_resource_id]["state"] == "FAILED"


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[2] / "scripts/experience.py"
    spec = importlib.util.spec_from_file_location("experience_launcher_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_production_start_rebuilds_with_current_browser_config_without_backend_secrets(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "running", lambda _record: False)
    monkeypatch.setattr(launcher, "port_ready", lambda _port: False)
    monkeypatch.setattr(launcher, "save_state", lambda _state: None)
    monkeypatch.setattr(launcher, "stop_process", lambda name, _state: calls.append(("stop", name)))
    monkeypatch.setattr(launcher, "command", lambda args, env, **_kw: calls.append(("build", args, dict(env))))
    monkeypatch.setattr(launcher, "launch", lambda _name, args, _cwd, env, _state: calls.append(("serve", args, dict(env))))
    monkeypatch.setattr(launcher, "wait_ready", lambda _port, **_kw: None)
    env = {"PATH": "node-bin", "NEXT_PUBLIC_AMAP_KEY": "browser-key-one",
           "BACKEND_INTERNAL_URL": "http://127.0.0.1:8006", "QWEN_API_KEY": "private-model-key",
           "DATABASE_URL": "private-database", "NEXT_PUBLIC_UNRELATED_SECRET": "not-an-allowed-setting"}
    state = {"processes": {}}
    launcher.start_web("node", env, state)
    env["NEXT_PUBLIC_AMAP_KEY"] = "browser-key-two"
    launcher.start_web("node", env, state)
    assert [item[0] for item in calls] == ["stop", "build", "serve", "stop", "build", "serve"]
    assert [calls[i][2]["NEXT_PUBLIC_AMAP_KEY"] for i in (1, 4)] == ["browser-key-one", "browser-key-two"]
    for item in (calls[1], calls[2], calls[4], calls[5]):
        assert item[2]["NODE_ENV"] == "production"
        assert item[2]["EXPERIENCE_WEB_RUNTIME"] == "1"
        assert not {"QWEN_API_KEY", "DATABASE_URL", "NEXT_PUBLIC_UNRELATED_SECRET"} & item[2].keys()
    assert calls[2][1][2] == "start" and state["web_mode"] == "production"


def test_failed_production_build_never_launches_an_old_bundle(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "running", lambda _record: False)
    monkeypatch.setattr(launcher, "port_ready", lambda _port: False)
    monkeypatch.setattr(launcher, "stop_process", lambda *_args: None)

    def failed_build(*_args, **_kwargs):
        raise RuntimeError("controlled build failure")

    def unexpected_launch(*_args, **_kwargs):
        pytest.fail("failed build must not serve an old bundle")

    monkeypatch.setattr(launcher, "command", failed_build)
    monkeypatch.setattr(launcher, "launch", unexpected_launch)
    with pytest.raises(RuntimeError, match="controlled build failure"):
        launcher.start_web("node", {}, {"processes": {}})


def test_development_server_requires_explicit_option(launcher, monkeypatch):
    calls = []
    monkeypatch.setattr(launcher, "running", lambda _record: False)
    monkeypatch.setattr(launcher, "port_ready", lambda _port: False)
    monkeypatch.setattr(launcher, "stop_process", lambda *_args: None)
    monkeypatch.setattr(launcher, "save_state", lambda *_args: None)
    monkeypatch.setattr(launcher, "wait_ready", lambda *_args, **_kw: None)
    monkeypatch.setattr(launcher, "command", lambda *_args, **_kw: pytest.fail("dev must not build production assets"))
    monkeypatch.setattr(launcher, "launch", lambda _name, args, _cwd, env, _state: calls.append((args, env)))
    launcher.start_web("node", {}, {"processes": {}}, dev=True)
    assert calls[0][0][2] == "dev" and calls[0][1]["NODE_ENV"] == "development"
