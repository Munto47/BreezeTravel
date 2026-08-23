from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from app.audit.models import EvidenceFreshness
from app.config import Settings
from app.itineraries.hash_service import with_content_hash
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
)
from app.trip_check.models import RunBudget, RunSpec, TripCheckRun, TripCheckRunStatus, TripCheckStage
from app.trip_check.provider_integrity import (
    ProviderQueryBudgetExceededError,
    ProviderSnapshotMismatchError,
    TripCheckProviderIntegrityCollector,
    provider_snapshot_sha256,
)


def _revision():
    dates = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    return with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="p3-provider-itinerary",
            workspace_id="p3-provider-workspace",
            revision=1,
            source_type=RevisionSource.IMPORT,
            city="北京",
            date_range=dates,
            days=[
                ItineraryDay(
                    day_index=0,
                    date=dates.start,
                    stops=[
                        ItineraryStop(
                            stop_id="s1",
                            place_id="p1",
                            day_index=0,
                            order_index=0,
                            raw_name="受控地点一",
                        ),
                        ItineraryStop(
                            stop_id="s2",
                            place_id="p2",
                            day_index=0,
                            order_index=1,
                            raw_name="受控地点二",
                        ),
                    ],
                ),
                ItineraryDay(day_index=1, date=dates.end),
            ],
            change_summary={
                "map_stop_projections": {
                    "s1": {"coords": {"lng": 116.397, "lat": 39.918}},
                    "s2": {"coords": {"lng": 116.407, "lat": 39.928}},
                }
            },
            created_by="p3-provider-user",
        )
    )


def _run(*, mode: str = "snapshot", fault_profile: str = "none", max_queries: int = 6):
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    run_spec = RunSpec(
        commit_sha="4cf5d10",
        prompt_version="none-p3",
        model_version="none-p3",
        provider_version="p3-provider-integrity-v1",
        rule_set_version="audit-v1",
        execution_mode=mode,
        dataset_hash="a" * 64,
        snapshot_hash=provider_snapshot_sha256(),
        fault_profile=fault_profile,
        random_seed=7,
        budget=RunBudget(timeout_seconds=30, max_provider_queries=max_queries, max_cost_usd=0),
    )
    return TripCheckRun(
        run_id=f"p3-provider-{mode}-{fault_profile}",
        workspace_id="p3-provider-workspace",
        itinerary_revision=1,
        brief_id="p3-provider-brief",
        brief_revision=1,
        stage=TripCheckStage.COLLECT_EVIDENCE,
        run_spec=run_spec,
        config_hash=sha256_canonical(run_spec.model_dump(mode="json")),
        status=TripCheckRunStatus.RUNNING,
        created_by="p3-provider-user",
        created_at=now,
        updated_at=now,
    )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeProviderSession:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if "/direction/transit/" in url:
            return FakeResponse(
                {"status": "1", "route": {"transits": [{"duration": "900", "distance": "3100"}]}}
            )
        if "/direction/" in url:
            return FakeResponse(
                {"status": "1", "route": {"paths": [{"duration": "600", "distance": "2200"}]}}
            )
        if "/v7/weather/7d" in url:
            return FakeResponse(
                {
                    "code": "200",
                    "daily": [
                        {"fxDate": "2026-09-01", "textDay": "晴", "tempMax": "26", "tempMin": "16", "precip": "0"},
                        {"fxDate": "2026-09-02", "textDay": "小雨", "tempMax": "24", "tempMin": "18", "precip": "2.5"},
                    ],
                }
            )
        return FakeResponse(
            {
                "results": [
                    {
                        "title": "受控 live adapter 新闻结果",
                        "url": "https://example.gov.cn/travel-risk",
                        "age": "2026-08-23",
                        "description": "用于验证结构和脱敏，不作为真实 Provider 证据。",
                    }
                ]
            }
        )
@pytest.mark.asyncio
async def test_snapshot_collects_four_route_modes_weather_risk_and_deterministic_receipts():
    collector = TripCheckProviderIntegrityCollector(settings=Settings(runtime_profile="test"))
    first = await collector.collect(_run(), _revision(), {})
    replay = await collector.collect(_run(), _revision(), {})

    assert first == replay
    assert first.provider_attempt_count == 6
    assert len(first.provider_receipts) == 6
    assert {item.operation for item in first.provider_receipts} == {
        "route.walking",
        "route.transit",
        "route.bicycling",
        "route.driving",
        "weather.daily",
        "risk.news_search",
    }
    assert all(item.execution_mode == "snapshot" for item in first.provider_receipts)
    assert all(item.request_hash and item.response_hash for item in first.provider_receipts)
    route_options = [item for item in first.observations if item.subject_type == "ROUTE_OPTION"]
    assert {item.value["mode"] for item in route_options} == {
        "walking",
        "transit",
        "bicycling",
        "driving",
    }
    assert len([item for item in first.observations if item.fact_type == "WEATHER"]) == 2
    assert all(
        item.value["evidence_class"] == "DETERMINISTIC_PROVIDER_FACT"
        for item in first.observations
        if item.fact_type == "WEATHER"
    )
    risk = [item for item in first.observations if item.fact_type == "RISK_SOURCE"]
    assert len(risk) == 1
    assert risk[0].value["source_tier"] == "CONTROLLED_FIXTURE"
    assert risk[0].value["evidence_class"] == "ADVISORY_SOURCE"
    assert first.provider_failures == []
    assert first.partial_failures == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_profile", "category", "fact_type"),
    [
        ("route_mode_unavailable", "PROVIDER_ROUTE_MODE_UNAVAILABLE", "ROUTE_OPTION"),
        ("weather_unavailable", "PROVIDER_WEATHER_UNAVAILABLE", "WEATHER"),
        ("risk_unavailable", "PROVIDER_RISK_UNAVAILABLE", "RISK_SOURCE"),
    ],
)
async def test_snapshot_partial_failures_preserve_successful_facts(fault_profile, category, fact_type):
    result = await TripCheckProviderIntegrityCollector(settings=Settings(runtime_profile="test")).collect(
        _run(fault_profile=fault_profile),
        _revision(),
        {},
    )

    assert [item.error_category for item in result.provider_failures] == [category]
    assert [item.category for item in result.partial_failures] == [category]
    assert any(
        item.fact_type == fact_type and item.freshness_status == EvidenceFreshness.UNAVAILABLE
        for item in result.observations
    )
    assert any(item.freshness_status != EvidenceFreshness.UNAVAILABLE for item in result.observations)
    assert sum(item.status == "UNAVAILABLE" for item in result.provider_receipts) == 1


@pytest.mark.asyncio
async def test_live_missing_credentials_never_calls_network_and_keeps_all_fields_unavailable():
    settings = Settings(
        runtime_profile="local_real",
        amap_api_key="",
        qweather_api_key="",
        qweather_private_key="",
        brave_api_key="",
    )
    result = await TripCheckProviderIntegrityCollector(settings=settings).collect(
        _run(mode="live"),
        _revision(),
        {},
    )

    assert len(result.provider_receipts) == 6
    assert all(item.status == "UNAVAILABLE" for item in result.provider_receipts)
    assert {item.provider for item in result.provider_failures} == {"amap", "qweather", "brave_news"}
    assert all(item.freshness_status == EvidenceFreshness.UNAVAILABLE for item in result.observations)


@pytest.mark.asyncio
async def test_live_adapters_parse_success_without_persisting_credentials_or_raw_response():
    session = FakeProviderSession()
    settings = Settings(
        runtime_profile="local_real",
        amap_api_key="controlled-amap-secret",
        qweather_auth_type="apikey",
        qweather_api_key="controlled-weather-secret",
        brave_api_key="controlled-brave-secret",
    )
    result = await TripCheckProviderIntegrityCollector(
        settings=settings,
        session_factory=lambda: session,
    ).collect(
        _run(mode="live"),
        _revision(),
        {},
    )

    assert len(session.requests) == 6
    assert result.provider_failures == []
    assert result.partial_failures == []
    assert all(item.status == "SUCCEEDED" for item in result.provider_receipts)
    assert all(item.execution_mode == "live" for item in result.provider_receipts)
    assert len([item for item in result.observations if item.fact_type == "WEATHER"]) == 2
    risk = next(item for item in result.observations if item.fact_type == "RISK_SOURCE")
    assert risk.value["source_tier"] == "GOVERNMENT"
    serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert "controlled-amap-secret" not in serialized
    assert "controlled-weather-secret" not in serialized
    assert "controlled-brave-secret" not in serialized
    assert "route\": {" not in serialized


@pytest.mark.asyncio
async def test_snapshot_hash_and_query_budget_fail_closed_before_collection():
    collector = TripCheckProviderIntegrityCollector(settings=Settings(runtime_profile="test"))
    mismatched = _run().model_copy(
        update={
            "run_spec": _run().run_spec.model_copy(update={"snapshot_hash": "0" * 64}),
        }
    )
    with pytest.raises(ProviderSnapshotMismatchError):
        await collector.collect(mismatched, _revision(), {})
    with pytest.raises(ProviderQueryBudgetExceededError):
        await collector.collect(_run(max_queries=5), _revision(), {})
