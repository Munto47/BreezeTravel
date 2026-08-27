from __future__ import annotations

import asyncio
import json

import pytest


fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import trip_understandings_v3  # noqa: E402
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository  # noqa: E402
from app.trip_understanding.worker import TripUnderstandingWorker  # noqa: E402


def _client():
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(trip_understandings_v3.router, prefix="/api")
    app.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    return TestClient(app), repository, app


def test_demo_api_create_events_result_refresh_and_session_isolation() -> None:
    client, repository, app = _client()
    missing_key = client.post("/api/v3/trip-understandings", json={"mode": "DEMO"})
    assert missing_key.status_code == 400
    assert client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "full"},
        json={"mode": "FULL"},
    ).status_code == 422

    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "browser-demo"},
        json={"mode": "DEMO"},
    )
    assert created.status_code == 202
    payload = created.json()
    public_resource_id = payload["public_resource_id"]
    assert set(payload) == {"public_resource_id", "status", "message", "result_url", "events_url"}
    cookie = created.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/v3/trip-understandings" in cookie
    assert public_resource_id not in cookie

    replay = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "browser-demo"},
        json={"mode": "DEMO"},
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json()["public_resource_id"] == public_resource_id

    progress = client.get(payload["result_url"])
    assert progress.status_code == 202
    assert set(progress.json()) == {"status", "message", "retry_after_ms"}
    asyncio.run(TripUnderstandingWorker(repository).run_once("api-test-worker"))

    result = client.get(payload["result_url"])
    assert result.status_code == 200
    assert result.headers["etag"].startswith('"tu3_')
    assert [len(day["activities"]) for day in result.json()["days"]] == [2, 2, 2]
    refreshed = client.get(payload["result_url"])
    assert refreshed.json() == result.json()
    assert refreshed.headers["etag"] == result.headers["etag"]

    event_stream = client.get(payload["events_url"])
    assert event_stream.status_code == 200
    assert "event: progress" in event_stream.text
    assert "event: result_available" in event_stream.text
    resumed = client.get(payload["events_url"], headers={"Last-Event-ID": "2"})
    assert "event: progress" not in resumed.text
    assert "event: result_available" in resumed.text
    assert "id: 3" in resumed.text

    other_browser = TestClient(app)
    denied = other_browser.get(payload["result_url"])
    assert denied.status_code == 404

    openapi_text = json.dumps(app.openapi(), ensure_ascii=False)
    assert '"DEMO"' in openapi_text
    assert '"FULL"' not in openapi_text
