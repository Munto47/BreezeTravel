from unittest.mock import AsyncMock
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import experience_main as runtime
from app.api.places_persist import _sanitize_shared_place
from app.config import Settings
from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.worker import build_configured_full_pipeline
from app.trip_understanding.worker import TripUnderstandingWorker
from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import CreateFullRequest
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.utils.auth import get_current_user


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
    graph_init = AsyncMock()
    graph_close = AsyncMock()
    monkeypatch.setattr(runtime.agent_graph, "init_persistent_graph", graph_init)
    monkeypatch.setattr(runtime.agent_graph, "close_checkpointer", graph_close)
    app = runtime.create_app()

    @app.get("/test-private-failure")
    async def private_failure():
        raise ValueError("SECRET-and-private-itinerary")

    with TestClient(app, raise_server_exceptions=False) as http:
        yield http, cache
    cache.aclose.assert_awaited_once()
    graph_init.assert_awaited_once()
    graph_close.assert_awaited_once()


def test_experience_runtime_exposes_only_text_account_and_collaboration_routes():
    app = runtime.create_app()
    routes = {route.path for route in app.routes}
    method_routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert "/api/v3/trip-understandings" in routes
    assert "/api/auth/email-login" in routes
    assert "/health" in routes
    assert {
        "/api/v3/me/data-consents", "/api/v3/me/data-consents/{purpose}",
        "/api/v3/me/travel-preferences",
    } <= routes
    expected_collaboration = {
        ("GET", "/api/user/rooms"),
        ("POST", "/api/room"),
        ("POST", "/api/room/{room_id}/join"),
        ("GET", "/api/room/{room_id}/state"),
        ("GET", "/api/room/{room_id}/members"),
        ("POST", "/api/room/{room_id}/ws-token"),
        ("GET", "/api/room/{room_id}/places"),
        ("POST", "/api/room/{room_id}/places/sync"),
        ("GET", "/api/room/{room_id}/itinerary"),
        ("POST", "/api/room/{room_id}/itinerary"),
        ("POST", "/api/chat"),
        ("POST", "/api/optimize"),
        ("POST", "/api/room/{room_id}/task/parse"),
        ("GET", "/api/weather"),
        ("POST", "/api/v3/trip-understandings/from-collaboration"),
    }
    assert expected_collaboration <= method_routes
    actual_room_routes = {
        item for item in method_routes
        if item[1].startswith("/api/room")
    }
    assert actual_room_routes == {
        item for item in expected_collaboration if item[1].startswith("/api/room")
    }
    assert not any("/shares" in path or "/feedback" in path for path in routes)
    assert not any(any(word in path for word in ("planner", "screenshot", "ocr", "test-login", "send-code", "wechat", "docs", "openapi")) for path in routes)


def test_shared_place_persistence_keeps_only_public_facts_and_selection_bit():
    sanitized = _sanitize_shared_place(
        {
            "place_id": "place_001",
            "name": "西湖博物馆",
            "category": "attraction",
            "address": "杭州市",
            "coords": {"lng": 120.14, "lat": 30.25, "user_id": "nested-secret"},
            "city": "杭州",
            "source": "amap_poi",
            "votedBy": ["member-secret"],
            "creatorId": "creator-secret",
            "meta": {"nickname": "private-name"},
            "recommendation": {"source_chunk_ids": ["private-chunk"]},
            "constraint_evidence": [{"source": "internal-provider"}],
            "tags": ["博物馆"],
        }
    )

    assert sanitized["room_selected"] is True
    assert sanitized["coords"] == {"lng": 120.14, "lat": 30.25}
    assert sanitized["constraint_evidence"] == []
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert all(
        secret not in serialized
        for secret in (
            "nested-secret",
            "member-secret",
            "creator-secret",
            "private-name",
            "private-chunk",
            "internal-provider",
        )
    )
    assert _sanitize_shared_place(
        {"place_id": "place_002", "name": "缺坐标", "category": "attraction"}
    ) == {}


def test_experience_optimize_response_excludes_internal_receipts(client, monkeypatch):
    http, _cache = client
    place = {
        "place_id": "place_001",
        "name": "西湖博物馆",
        "category": "attraction",
        "address": "杭州市",
        "coords": {"lng": 120.14, "lat": 30.25},
        "city": "杭州",
        "source": "amap_poi",
        "retrieval_provider": "internal-provider",
        "retrieval_request_hash": "private-request-hash",
        "retrieval_response_hash": "private-response-hash",
        "rag_meta": {"tip_snippets": [], "source_note_ids": ["private-note"]},
        "constraint_evidence": [{
            "constraint": "private-constraint",
            "label": "内部约束",
            "status": "VERIFIED",
            "detail": "private-detail",
            "source": "internal-source",
        }],
        "tags": ["博物馆"],
    }
    itinerary = {
        "itinerary_id": "private-itinerary-id",
        "thread_id": "private-thread-id",
        "city": "杭州",
        "generated_at": "2026-09-05T08:00:00Z",
        "version": 9,
        "days": [{
            "day_index": 0,
            "cluster_id": 0,
            "slots": [{
                "place_id": "place_001",
                "place": place,
                "start_time": "09:00",
                "end_time": "11:00",
                "transport": None,
                "tips": [],
            }],
        }],
    }

    async def fake_optimize(_request, _current_user):
        return SimpleNamespace(
            itinerary=itinerary,
            backup_pool=[place],
            planning_input_hash="private-planning-hash",
            workspace_id="private-workspace-id",
            itinerary_revision=9,
            audit_report_id="private-report-id",
            tips_basis_report_id="private-tips-report-id",
        )

    monkeypatch.setattr(runtime.optimize, "optimize", fake_optimize)
    response = http.post("/api/optimize", json={
        "thread_id": "request-thread",
        "places": [place],
        "trip_days": 1,
    })
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"itinerary", "backup_pool"}
    assert set(body["itinerary"]) == {"city", "days", "generated_at"}

    def keys(value):
        if isinstance(value, dict):
            return set(value).union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value), set())
        return set()

    assert keys(body).isdisjoint({
        "planning_input_hash", "workspace_id", "itinerary_revision",
        "audit_report_id", "audit_status", "audit_error_code",
        "tips_status", "tips_basis_revision", "tips_basis_report_id",
        "verification_report", "task_spec", "critic_violations",
        "optimization_method", "duration_ms", "itinerary_id", "thread_id",
        "version", "retrieval_provider", "retrieval_request_hash",
        "retrieval_response_hash", "rag_meta", "constraint_evidence",
    })
    assert body["itinerary"]["days"][0]["slots"][0]["place"]["name"] == "西湖博物馆"

    async def fake_internal_failure(_request, _current_user):
        raise HTTPException(
            status_code=422,
            detail={"message": "[ClustererAgent] private planning hash leaked"},
        )

    monkeypatch.setattr(runtime.optimize, "optimize", fake_internal_failure)
    failed = http.post("/api/optimize", json={
        "thread_id": "request-thread",
        "places": [place],
        "trip_days": 1,
    })
    assert failed.status_code == 422
    assert failed.json() == {
        "detail": {"message": "当前地点暂时无法排成可用路线，请调整后重试"}
    }

    too_many_places = http.post("/api/optimize", json={
        "thread_id": "request-thread",
        "places": [place] * 31,
        "trip_days": 1,
    })
    assert too_many_places.status_code == 422
    assert too_many_places.json() == {
        "detail": {"message": "请检查填写内容后重试"}
    }


def test_account_preferences_are_available_private_and_default_off(client):
    http, _cache = client
    consent_url = "/api/v3/me/data-consents"
    preferences_url = "/api/v3/me/travel-preferences"
    repository = InMemoryTripUnderstandingRepository()
    http.app.dependency_overrides[
        runtime.trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    assert http.get(consent_url).status_code == 401
    assert http.get(preferences_url).status_code == 401
    http.app.dependency_overrides[get_current_user] = lambda: "profile-owner"
    default = http.get(consent_url)
    assert default.status_code == 200
    assert default.json() == {
        "memory_enabled": False, "feedback_enabled": False,
        "training_eval_enabled": False,
    }
    assert default.headers["Cache-Control"] == "no-store"
    empty = http.get(preferences_url)
    assert empty.status_code == 200 and empty.json() is None
    preference = {
        "walking_tolerance_minutes": 25, "preferred_start_time": "09:00",
        "dining_preferences": ["LOCAL"], "hotel_preferences": ["QUIET"],
        "intensity": "RELAXED",
    }
    assert http.put(preferences_url, json=preference).status_code == 409
    enabled = http.put(f"{consent_url}/memory", json={"enabled": True})
    assert enabled.status_code == 200 and enabled.json()["memory_enabled"] is True
    assert http.put(preferences_url, json=preference).json() == preference
    assert http.get(preferences_url).json() == preference
    http.app.dependency_overrides[get_current_user] = lambda: "other-owner"
    assert http.get(consent_url).json()["memory_enabled"] is False
    assert http.get(preferences_url).json() is None
    http.app.dependency_overrides[get_current_user] = lambda: "profile-owner"
    assert http.delete(preferences_url).status_code == 204
    assert http.get(preferences_url).json() is None
    assert http.put(preferences_url, json=preference).status_code == 200
    disabled = http.put(f"{consent_url}/memory", json={"enabled": False})
    assert disabled.status_code == 200 and disabled.json()["memory_enabled"] is False
    assert http.get(preferences_url).json() is None


def test_account_preferences_read_failure_is_not_reported_as_default_off(client):
    http, _cache = client
    repository = InMemoryTripUnderstandingRepository()
    repository.get_data_consents = AsyncMock(side_effect=RuntimeError("private-database-detail"))
    http.app.dependency_overrides[
        runtime.trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    http.app.dependency_overrides[get_current_user] = lambda: "profile-owner"
    failed = http.get("/api/v3/me/data-consents")
    assert failed.status_code == 503
    assert "memory_enabled" not in failed.text and "private-database-detail" not in failed.text
    assert failed.headers["Cache-Control"] == "no-store"


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


def test_runtime_ports_accept_valid_process_or_persisted_overrides(launcher, monkeypatch, tmp_path):
    private_env = tmp_path / "experience.env"
    private_env.write_text('EXPERIENCE_API_PORT="18106"\n', encoding="utf-8")
    monkeypatch.setattr(launcher, "ENV_FILE", private_env)
    assert launcher.resolve_port("EXPERIENCE_API_PORT", 8006) == 18106

    monkeypatch.setenv("EXPERIENCE_API_PORT", "18116")
    assert launcher.resolve_port("EXPERIENCE_API_PORT", 8006) == 18116
    monkeypatch.setenv("EXPERIENCE_API_PORT", "70000")
    with pytest.raises(RuntimeError, match="between 1 and 65535"):
        launcher.resolve_port("EXPERIENCE_API_PORT", 8006)


def test_node_shim_is_resolved_to_the_long_lived_executable(
    launcher, monkeypatch, tmp_path,
):
    executable = tmp_path / "node.exe"
    executable.touch()
    captured = {}

    def probe(*_args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=f"{executable}\n",
        )

    monkeypatch.setenv("QWEN_API_KEY", "must-not-reach-node-probe")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        probe,
    )

    assert launcher.resolve_node_executable("node-shim") == str(executable.resolve())
    assert "QWEN_API_KEY" not in captured["env"]


def test_windows_node_probe_failure_is_fail_closed(launcher, monkeypatch):
    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )

    with pytest.raises(RuntimeError, match="no local service was started"):
        launcher.resolve_node_executable("node-shim")


def test_process_stamp_rejects_a_missing_process(launcher):
    assert isinstance(launcher.process_stamp(os.getpid()), int)
    assert launcher.process_stamp(2_147_483_647) is None


def test_runtime_ports_must_be_distinct_before_initialization(launcher, monkeypatch):
    monkeypatch.setattr(
        launcher,
        "RUNTIME_PORTS",
        {
            "EXPERIENCE_API_PORT": 18106,
            "EXPERIENCE_WEB_PORT": 18106,
        },
    )
    with pytest.raises(RuntimeError, match="must be distinct"):
        launcher.validate_runtime_ports()


def test_active_runtime_rejects_a_changed_port_binding(launcher, monkeypatch):
    state = {
        "processes": {"api": {"pid": 1, "stamp": 1}},
        "ports": {name: port + 1 for name, port in launcher.RUNTIME_PORTS.items()},
    }
    monkeypatch.setattr(launcher, "owned_runtime_is_active", lambda _state: True)
    with pytest.raises(RuntimeError, match="different port set"):
        launcher.validate_runtime_binding(state)


def test_start_preflights_every_required_port_before_configuration(launcher, monkeypatch):
    state = {"processes": {}}
    monkeypatch.setattr(launcher, "load_state", lambda: state)
    monkeypatch.setattr(launcher, "validate_runtime_binding", lambda _state: None)
    monkeypatch.setattr(
        launcher,
        "preflight_runtime_ports",
        lambda _state, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("occupied before initialization")
        ),
    )
    monkeypatch.setattr(
        launcher,
        "configure",
        lambda: pytest.fail("configuration must not run after a failed preflight"),
    )
    with pytest.raises(RuntimeError, match="occupied before initialization"):
        launcher.start()


def test_custom_ports_feed_private_service_urls(launcher, monkeypatch):
    monkeypatch.setattr(launcher, "API_PORT", 18106)
    monkeypatch.setattr(launcher, "PG_PORT", 55449)
    monkeypatch.setattr(launcher, "REDIS_PORT", 56399)
    monkeypatch.setattr(launcher, "YJS_PORT", 1244)
    env = launcher.environment(
        {
            "EXPERIENCE_PG_PASSWORD": "unit-postgres",
            "EXPERIENCE_REDIS_PASSWORD": "unit-redis",
            "EXPERIENCE_DATABASE": "unit_database",
        }
    )
    assert "127.0.0.1:55449" in env["DATABASE_URL"]
    assert "127.0.0.1:56399" in env["REDIS_URL"]
    assert env["BACKEND_INTERNAL_URL"] == "http://127.0.0.1:18106"
    assert env["NEXT_PUBLIC_Y_WEBSOCKET_URL"] == "ws://127.0.0.1:1244"
    assert env["PORT"] == "1244"


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


def test_yjs_and_package_install_children_receive_only_required_environment(launcher):
    private = {
        "Path": "node-bin",
        "TEMP": "temporary-files",
        "HTTPS_PROXY": "http://proxy.invalid",
        "JWT_SECRET_KEY": "room-token-secret",
        "HOST": "127.0.0.1",
        "PORT": "1234",
        "YPERSISTENCE": "private-yjs-path",
        "YJS_MAX_PAYLOAD_BYTES": "262144",
        "QWEN_API_KEY": "model-secret",
        "AMAP_API_KEY": "route-secret",
        "DATABASE_URL": "database-secret",
        "REDIS_URL": "redis-secret",
        "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY": "source-secret",
        "NEXT_PUBLIC_AMAP_KEY": "browser-key",
    }

    install_env = launcher.install_environment(private)
    yjs_env = launcher.yjs_environment(private)

    assert install_env == {
        "Path": "node-bin",
        "TEMP": "temporary-files",
        "HTTPS_PROXY": "http://proxy.invalid",
    }
    assert yjs_env == {
        "Path": "node-bin",
        "TEMP": "temporary-files",
        "HTTPS_PROXY": "http://proxy.invalid",
        "JWT_SECRET_KEY": "room-token-secret",
        "HOST": "127.0.0.1",
        "PORT": "1234",
        "YPERSISTENCE": "private-yjs-path",
        "YJS_MAX_PAYLOAD_BYTES": "262144",
    }
    forbidden = {
        "QWEN_API_KEY", "AMAP_API_KEY", "DATABASE_URL", "REDIS_URL",
        "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY", "NEXT_PUBLIC_AMAP_KEY",
    }
    assert forbidden.isdisjoint(install_env)
    assert forbidden.isdisjoint(yjs_env)


def test_postgres_status_uses_owned_data_directory_not_a_shared_port(launcher, monkeypatch):
    state = {
        "processes": {},
        "postgres_data": str(launcher.LOCAL / "postgres"),
        "postgres_bin": str(launcher.LOCAL / "postgres-bin"),
    }
    monkeypatch.setattr(launcher, "port_ready", lambda _port: True)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    assert launcher.postgres_running(state) is False


def test_failed_postgres_stop_never_reports_success(launcher, monkeypatch, capsys):
    state = {
        "processes": {},
        "postgres_data": str(launcher.LOCAL / "postgres"),
        "postgres_bin": str(launcher.LOCAL / "postgres-bin"),
    }
    outcomes = iter((0, 1))
    monkeypatch.setattr(launcher, "load_state", lambda: state)
    monkeypatch.setattr(launcher, "stop_process", lambda *_args: None)
    monkeypatch.setattr(launcher, "save_state", lambda *_args: None)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=next(outcomes)),
    )

    with pytest.raises(RuntimeError, match="Private PostgreSQL did not stop"):
        launcher.stop()
    assert "Local services stopped" not in capsys.readouterr().out


def test_windows_yjs_ctrl_break_failure_falls_back_to_owned_process_tree(
    launcher, monkeypatch,
):
    state = {
        "processes": {
            "yjs": {"pid": 42, "stamp": 99, "process_group": True},
        }
    }
    alive = {"value": True}
    taskkill_calls = []

    monkeypatch.setattr(launcher.os, "name", "nt")
    monkeypatch.setattr(launcher.os, "kill", lambda *_args: (_ for _ in ()).throw(SystemError()))
    monkeypatch.setattr(launcher, "running", lambda _record: alive["value"])
    monkeypatch.setattr(launcher, "save_state", lambda _state: None)
    monkeypatch.setattr(launcher, "port_ready", lambda _port: False)

    def taskkill(args, **_kwargs):
        taskkill_calls.append(args)
        alive["value"] = False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(launcher.subprocess, "run", taskkill)
    launcher.stop_process("yjs", state)

    assert taskkill_calls == [["taskkill", "/PID", "42", "/T", "/F"]]
    assert "yjs" not in state["processes"]


def test_stale_yjs_record_is_retained_while_its_port_remains_occupied(
    launcher, monkeypatch,
):
    state = {
        "processes": {
            "yjs": {"pid": 42, "stamp": 99, "process_group": True},
        }
    }
    saves = []
    monkeypatch.setattr(launcher, "running", lambda _record: False)
    monkeypatch.setattr(launcher, "port_ready", lambda _port: True)
    monkeypatch.setattr(launcher, "save_state", lambda _state: saves.append(_state))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    ticks = iter((0.0, 3.0, 3.0))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RuntimeError, match="process record retained"):
        launcher.stop_process("yjs", state)

    assert state["processes"]["yjs"]["pid"] == 42
    assert saves == []
