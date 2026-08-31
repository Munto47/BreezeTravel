from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest


pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import trip_understandings_v3  # noqa: E402
from app.trip_understanding.knowledge import KnowledgeClaimCandidate  # noqa: E402
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository  # noqa: E402
from app.trip_understanding.worker import TripUnderstandingWorker  # noqa: E402


def _client() -> tuple[TestClient, InMemoryTripUnderstandingRepository]:
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(trip_understandings_v3.router, prefix="/api")
    app.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    return TestClient(app), repository


def _candidate(*, now: datetime) -> KnowledgeClaimCandidate:
    return KnowledgeClaimCandidate(
        claim_revision_id="g05-api-claim-v1",
        claim_key="g05-api-claim",
        claim_version=1,
        canonical_place_id="fixture-bj-palace-museum",
        claim_type="RESERVATION_ADVICE",
        conditions_hash="a" * 64,
        suggestion_text="建议至少提前一天实名预约，并在出发前复核官方预约规则。",
        short_evidence="北京市政府页面说明访客需要实名预约。",
        effective_at=now - timedelta(days=1),
        expires_at=now + timedelta(days=30),
        source_version_id="g05-api-source-v1",
        source_name="北京市人民政府国际版门户网站",
        source_url="https://english.beijing.gov.cn/latest/news/202306/t20230629_3150036.html",
        source_observed_at=now - timedelta(days=1),
        source_expires_at=now + timedelta(days=30),
        source_admission_status="ADMITTED",
        source_license_status="FACTS_ONLY_WITH_ATTRIBUTION",
    )


def test_g05_result_read_dynamically_adds_and_withdraws_advice_without_etag_change() -> None:
    client, repository = _client()
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "g05-api-demo"},
        json={"mode": "DEMO"},
    )
    assert created.status_code == 202
    asyncio.run(TripUnderstandingWorker(repository).run_once("g05-api-worker"))
    result_url = created.json()["result_url"]

    before = client.get(result_url)
    assert before.status_code == 200
    assert all(
        not card["knowledge_suggestions"]
        for day in before.json()["days"]
        for card in day["activities"]
    )
    stored_before = next(iter(repository.results.values())).result.model_dump(mode="json")

    now = datetime.now(timezone.utc)
    candidate = _candidate(now=now)
    repository.knowledge_candidates = [candidate]
    enriched = client.get(result_url)
    palace = next(
        card
        for day in enriched.json()["days"]
        for card in day["activities"]
        if card["name"] == "故宫博物院"
    )
    assert palace["knowledge_suggestions"][0]["type"] == "RESERVATION_ADVICE"
    assert enriched.headers["etag"] == before.headers["etag"]
    assert next(iter(repository.results.values())).result.model_dump(mode="json") == stored_before
    assert len(repository.knowledge_usage_receipts) == 1

    repository.knowledge_candidates = [
        candidate.model_copy(update={"claim_withdrawn_at": now})
    ]
    withdrawn = client.get(result_url)
    assert withdrawn.headers["etag"] == before.headers["etag"]
    assert all(
        not card["knowledge_suggestions"]
        for day in withdrawn.json()["days"]
        for card in day["activities"]
    )

    deleted = client.delete(
        f"/api/v3/trip-understandings/{created.json()['public_resource_id']}",
        headers={"Idempotency-Key": "g05-api-delete"},
    )
    assert deleted.status_code == 204
    assert repository.knowledge_usage_receipts == []


def test_g05_knowledge_failure_returns_authoritative_cards_without_internal_error() -> None:
    client, repository = _client()
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "g05-api-failure"},
        json={"mode": "DEMO"},
    )
    asyncio.run(TripUnderstandingWorker(repository).run_once("g05-api-failure-worker"))
    repository.project_current_knowledge = AsyncMock(side_effect=RuntimeError("internal-db-detail"))

    response = client.get(created.json()["result_url"])

    assert response.status_code == 200
    assert [len(day["activities"]) for day in response.json()["days"]] == [2, 2, 2]
    assert "internal-db-detail" not in response.text
