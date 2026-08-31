from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest


pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import memory_share_v3, trip_understandings_v3  # noqa: E402
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository  # noqa: E402
from app.trip_understanding.worker import TripUnderstandingWorker  # noqa: E402
from app.utils.auth import get_current_user, get_optional_user, get_recent_user  # noqa: E402


OWNER = "g06-owner"


def _client() -> tuple[TestClient, InMemoryTripUnderstandingRepository, FastAPI]:
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(trip_understandings_v3.router, prefix="/api")
    app.include_router(trip_understandings_v3.account_router, prefix="/api")
    app.include_router(memory_share_v3.router, prefix="/api")
    app.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: OWNER
    app.dependency_overrides[get_optional_user] = lambda: OWNER
    app.dependency_overrides[get_recent_user] = lambda: OWNER
    return TestClient(app), repository, app


def _ready_trip(
    client: TestClient, repository: InMemoryTripUnderstandingRepository
) -> str:
    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "g06-trip-create"},
        json={
            "mode": "FULL",
            "source": {
                "type": "TEXT",
                "text": "北京两日行程。Day 1 故宫博物院、景山公园。Day 2 天坛公园。",
            },
        },
    )
    assert created.status_code == 202
    asyncio.run(TripUnderstandingWorker(repository).run_once("g06-worker"))
    assert client.get(created.json()["result_url"]).status_code == 200
    return created.json()["public_resource_id"]


def test_g06_consents_are_default_off_separate_and_memory_is_structured() -> None:
    client, repository, _ = _client()

    default = client.get("/api/v3/me/data-consents")
    assert default.status_code == 200
    assert default.json() == {
        "memory_enabled": False,
        "feedback_enabled": False,
        "training_eval_enabled": False,
    }
    preference = {
        "walking_tolerance_minutes": 25,
        "preferred_start_time": "08:30",
        "dining_preferences": ["LOCAL", "NO_SPICY"],
        "hotel_preferences": ["CHAIN", "NEAR_TRANSIT"],
        "intensity": "BALANCED",
    }
    assert client.put("/api/v3/me/travel-preferences", json=preference).status_code == 409

    memory_on = client.put(
        "/api/v3/me/data-consents/memory", json={"enabled": True}
    )
    assert memory_on.json() == {
        "memory_enabled": True,
        "feedback_enabled": False,
        "training_eval_enabled": False,
    }
    saved = client.put("/api/v3/me/travel-preferences", json=preference)
    assert saved.status_code == 200
    assert saved.json() == preference
    assert client.get("/api/v3/me/travel-preferences").json() == preference

    assert client.put(
        "/api/v3/me/data-consents/feedback", json={"enabled": True}
    ).json()["training_eval_enabled"] is False
    assert client.put(
        "/api/v3/me/data-consents/training-eval", json={"enabled": True}
    ).json()["feedback_enabled"] is True
    memory_off = client.put(
        "/api/v3/me/data-consents/memory", json={"enabled": False}
    )
    assert memory_off.json()["feedback_enabled"] is True
    assert client.get("/api/v3/me/travel-preferences").json() is None
    assert OWNER not in repository.g06_preferences

    forbidden = repr((repository.g06_consents, repository.g06_preferences))
    for raw_field in ("source_text", "screenshot", "chat", "confidence", "source_message"):
        assert raw_field not in forbidden


def test_g06_feedback_does_not_enable_training_and_is_idempotent() -> None:
    client, repository, _ = _client()
    resource_ref = _ready_trip(client, repository)
    body = {"event_type": "VOLUNTARY", "subject_type": "TRIP"}
    endpoint = f"/api/v3/trip-understandings/{resource_ref}/feedback"
    assert client.post(endpoint, headers={"Idempotency-Key": "feedback-1"}, json=body).status_code == 409
    client.put("/api/v3/me/data-consents/feedback", json={"enabled": True})
    first = client.post(endpoint, headers={"Idempotency-Key": "feedback-1"}, json=body)
    replay = client.post(endpoint, headers={"Idempotency-Key": "feedback-1"}, json=body)
    assert first.status_code == replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert len(repository.g06_feedback) == 1
    assert client.get("/api/v3/me/data-consents").json()["training_eval_enabled"] is False
    stored = repr(repository.g06_feedback)
    assert resource_ref not in stored


def test_g06_share_fragment_exchange_minimal_projection_revoke_and_owner_checks() -> None:
    client, repository, app = _client()
    resource_ref = _ready_trip(client, repository)
    endpoint = f"/api/v3/trip-understandings/{resource_ref}/shares"
    first = client.post(
        endpoint,
        headers={"Idempotency-Key": "share-1"},
        json={"expires_in_days": 7},
    )
    assert first.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    share_url = first.json()["share_url"]
    path, fragment = share_url.split("#s=", 1)
    share_ref = path.rsplit("/", 1)[1]
    assert len(fragment) >= 40
    assert fragment not in repr(repository.g06_shares)

    replay = client.post(
        endpoint,
        headers={"Idempotency-Key": "share-1"},
        json={"expires_in_days": 7},
    )
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.json() == first.json()

    recipient = TestClient(app)
    assert recipient.get(f"/api/v3/shares/{share_ref}").status_code == 404
    bad = recipient.post(
        f"/api/v3/shares/{share_ref}/exchange",
        json={"secret": "x" * 43},
    )
    assert bad.status_code == 404
    exchanged = recipient.post(
        f"/api/v3/shares/{share_ref}/exchange",
        json={"secret": fragment},
    )
    assert exchanged.status_code == 204
    cookie = exchanged.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/api/v3/shares" in cookie
    shared = recipient.get(f"/api/v3/shares/{share_ref}")
    assert shared.status_code == 200
    assert shared.headers["cache-control"] == "no-store"
    assert set(shared.json()) == {
        "title", "destination", "schedule", "party_size", "days",
        "accommodation", "message",
    }
    serialized = json.dumps(shared.json(), ensure_ascii=False)
    for forbidden in (
        "activity_token", "candidate_token", "revision", "hash", "receipt",
        "confidence", "license", "source", "audit", "finding", resource_ref,
    ):
        assert forbidden not in serialized.lower()

    app.dependency_overrides[get_current_user] = lambda: "other-user"
    assert client.delete(f"/api/v3/me/shares/{share_ref}").status_code == 404
    app.dependency_overrides[get_current_user] = lambda: OWNER
    assert client.delete(f"/api/v3/me/shares/{share_ref}").status_code == 204
    assert recipient.get(f"/api/v3/shares/{share_ref}").status_code == 404


def test_g06_trip_and_account_deletion_remove_memory_feedback_and_share_access() -> None:
    client, repository, _ = _client()
    resource_ref = _ready_trip(client, repository)
    client.put("/api/v3/me/data-consents/memory", json={"enabled": True})
    client.put("/api/v3/me/data-consents/feedback", json={"enabled": True})
    client.put(
        "/api/v3/me/travel-preferences",
        json={
            "walking_tolerance_minutes": 20,
            "preferred_start_time": "09:00",
            "dining_preferences": ["LOCAL"],
            "hotel_preferences": ["QUIET"],
            "intensity": "RELAXED",
        },
    )
    client.post(
        f"/api/v3/trip-understandings/{resource_ref}/feedback",
        headers={"Idempotency-Key": "delete-feedback"},
        json={"event_type": "ADOPTED", "subject_type": "TRIP"},
    )
    created_share = client.post(
        f"/api/v3/trip-understandings/{resource_ref}/shares",
        headers={"Idempotency-Key": "delete-share"},
        json={"expires_in_days": 2},
    ).json()["share_url"]
    share_ref, secret = created_share.split("/share/", 1)[1].split("#s=", 1)
    recipient = TestClient(client.app)
    assert recipient.post(
        f"/api/v3/shares/{share_ref}/exchange", json={"secret": secret}
    ).status_code == 204

    deleted = client.request(
        "DELETE",
        "/api/v3/me/travel-data",
        headers={"Idempotency-Key": "g06-account-clear"},
        json={"confirmation": "DELETE_ALL_TRAVEL_DATA"},
    )
    assert deleted.status_code == 202
    assert client.get("/api/v3/me/travel-preferences").json() is None
    assert not repository.g06_feedback
    assert all(row["revoked_at"] is not None for row in repository.g06_shares.values())
    assert recipient.get(f"/api/v3/shares/{share_ref}").status_code == 404


@pytest.mark.asyncio
async def test_g06_expired_share_fails_closed() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime.now(timezone.utc)
    repository.g06_shares["a" * 32] = {
        "understanding_id": "understanding",
        "owner_user_id": OWNER,
        "secret_hash": "b" * 64,
        "projection": None,
        "request_hash": "c" * 64,
        "expires_at": now - timedelta(seconds=1),
        "revoked_at": None,
    }
    with pytest.raises(ResourceNotFoundError):
        await repository.exchange_share_secret("a" * 32, "x" * 43, now=now)


from app.trip_understanding.errors import ResourceNotFoundError  # noqa: E402
