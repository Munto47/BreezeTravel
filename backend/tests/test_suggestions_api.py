from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import suggestions as api
from app.api import trip_workspaces as workspace_api
from app.audit.repositories import InMemoryAuditRepository
from app.importing.models import ResolvedPlaceReceipt
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.members.repositories import InMemoryMemberConstraintRepository
from app.schemas.place import Coordinates, RetrievalExecutionMode
from app.suggestions.models import (
    EvidenceFreshness,
    FreshnessStatus,
    FrozenCanonicalPlace,
    HardGate,
    RouteDelta,
    RouteReceipt,
    RouteReceiptLeg,
    SuggestionCandidateDraft,
    SuggestionClassification,
    SuggestionIntent,
    SuggestionSetCreateInput,
)
from app.suggestions.repositories import InMemorySuggestionRepository
from app.utils.auth import get_current_user


def _ranked_input() -> SuggestionSetCreateInput:
    now = datetime.now(timezone.utc)
    coords = Coordinates(lng=116.397, lat=39.918)
    receipt = ResolvedPlaceReceipt(
        canonical_place_id="poi-api-next",
        provider="amap",
        provider_place_id="amap-poi-api-next",
        name="景山公园",
        city="北京",
        district="东城区",
        address="景山西街44号",
        category="attraction",
        longitude=coords.lng,
        latitude=coords.lat,
        request_hash="d" * 64,
        response_hash="e" * 64,
        observed_at=now,
        execution_mode=RetrievalExecutionMode.FIXTURE,
    )
    candidate = SuggestionCandidateDraft(
        candidate_id="candidate-api",
        canonical_place=FrozenCanonicalPlace(
            place_id=receipt.canonical_place_id,
            name=receipt.name,
            city=receipt.city,
            district=receipt.district,
            address=receipt.address,
            category="attraction",
            coords=coords,
        ),
        provider_receipt=receipt,
        provider_receipt_id="receipt-api",
        rank_position=1,
        classification=SuggestionClassification.ON_ROUTE,
        source_prior_refs=["official-route:beijing:v1"],
        score_components={"route": 0.8, "popularity": 0.7},
        total_score=0.77,
        hard_gate=HardGate(passed=True),
        route_delta=RouteDelta(
            status="AVAILABLE",
            delta_route_minutes=9,
            previous_to_candidate_minutes=9,
            route_receipts=(RouteReceipt(
                leg=RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
                transport_mode="walking",
                origin_place_id="poi-anchor",
                origin_coords=Coordinates(lng=116.391, lat=39.916),
                destination_place_id=receipt.canonical_place_id,
                destination_coords=coords,
                duration_minutes=9,
                provider="controlled_route_snapshot",
                request_hash="1" * 64,
                response_hash="2" * 64,
                observed_at=now,
                snapshot_id="controlled-route-api-20260821",
                execution_mode=RetrievalExecutionMode.FIXTURE,
                max_age_seconds=3600,
                source_url="fixture://route/api",
            ),),
        ),
        evidence_freshness=EvidenceFreshness(
            status=FreshnessStatus.FRESH,
            observed_at=now,
            max_age_seconds=3600,
        ),
        explanation_codes=["NEARBY", "POPULAR"],
    )
    return SuggestionSetCreateInput(
        suggestion_set_id="set-api",
        workspace_id="workspace-api-suggestions",
        base_revision=1,
        day_index=0,
        insert_after_stop_id="anchor-api",
        intents=[SuggestionIntent.NEARBY, SuggestionIntent.POPULAR],
        context_hash="f" * 64,
        policy_version="ranker-api-v1",
        provider_snapshot_id="fixture-run-api-20260821",
        expires_at=now + timedelta(hours=1),
        candidates=[candidate],
        session_id="session-api",
        created_by="user-api",
    )


def _client(monkeypatch, *, with_provider: bool = True):
    dates = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-api-suggestions",
        workspace_id="workspace-api-suggestions",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=dates,
        days=[
            ItineraryDay(day_index=0, date=dates.start, stops=[
                ItineraryStop(
                    stop_id="anchor-api", place_id="poi-anchor", day_index=0, order_index=0,
                )
            ]),
            ItineraryDay(day_index=1, date=dates.end, stops=[]),
        ],
        created_by="user-api",
    ))
    workspace = TripWorkspace(
        workspace_id="workspace-api-suggestions",
        room_id="room-api",
        city="北京",
        trip_date_range=dates,
        current_itinerary_revision=1,
        created_by="user-api",
    )
    itineraries = InMemoryItineraryRepository()
    asyncio.run(itineraries.create_workspace(workspace, revision))
    suggestions = InMemorySuggestionRepository(itineraries)
    audits = InMemoryAuditRepository(itineraries.workspaces, place_records=suggestions.place_records)
    members = InMemoryMemberConstraintRepository(itineraries.workspaces)

    class Provider:
        calls = 0

        async def rank(self, **kwargs):
            self.calls += 1
            assert set(kwargs) == {"workspace_id", "request", "actor_user_id"}
            return _ranked_input()

    provider = Provider()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.include_router(workspace_api.router, prefix="/api")
    app.dependency_overrides[api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[workspace_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[workspace_api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[workspace_api.get_audit_repository] = lambda: audits
    app.dependency_overrides[workspace_api.get_member_constraint_repository] = lambda: members
    if with_provider:
        app.dependency_overrides[api.get_ranked_suggestion_provider] = lambda: provider
    app.dependency_overrides[get_current_user] = lambda: "user-api"
    monkeypatch.setattr(api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(workspace_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), itineraries, suggestions, provider


def _create(client: TestClient):
    return client.post(
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets",
        json={
            "base_revision": 1,
            "day_index": 0,
            "insert_after_stop_id": "anchor-api",
            "intents": ["NEARBY", "POPULAR"],
            "session_id": "session-api",
        },
    )


def test_api_create_get_accept_replay_and_event_readback(monkeypatch):
    client, itineraries, _, provider = _client(monkeypatch)
    created = _create(client)
    assert created.status_code == 201
    assert provider.calls == 1
    assert created.json()["candidates"][0]["provider_receipt"]["request_hash"] == "d" * 64

    readback = client.get(
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets/set-api"
    )
    assert readback.status_code == 200
    assert readback.json() == created.json()

    path = (
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets/"
        "set-api/candidates/candidate-api:accept"
    )
    missing_headers = client.post(path)
    assert missing_headers.status_code == 428
    assert missing_headers.json()["detail"]["code"] == "IF_MATCH_REQUIRED"

    headers = {"If-Match": '"1"', "Idempotency-Key": "accept-api"}
    accepted = client.post(path, headers=headers)
    replay = client.post(path, headers=headers)
    assert accepted.status_code == 200
    assert accepted.headers["etag"] == '"2"'
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["idempotent_replay"] is True
    assert asyncio.run(itineraries.get_revision("workspace-api-suggestions", 2)) is not None

    events = client.get(
        "/api/trip-workspaces/workspace-api-suggestions/recommendation-events"
    )
    assert events.status_code == 200
    assert [event["event_type"] for event in events.json()] == [
        "suggestions_shown",
        "candidate_accepted",
    ]


def test_public_undo_atomically_emits_server_frozen_stop_undone_and_replays(monkeypatch):
    client, itineraries, suggestions, _ = _client(monkeypatch)
    assert _create(client).status_code == 201
    accept_path = (
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets/"
        "set-api/candidates/candidate-api:accept"
    )
    accepted = client.post(
        accept_path,
        headers={"If-Match": '"1"', "Idempotency-Key": "accept-before-public-undo"},
    )
    assert accepted.status_code == 200
    accepted_event = accepted.json()["event"]

    path = "/api/trip-workspaces/workspace-api-suggestions/undo"
    body = {"command_id": "public-undo-1", "base_revision": 2, "target_revision": 1}
    headers = {"If-Match": '"2"', "Idempotency-Key": "public-undo-key"}
    forged = client.post(
        path,
        headers=headers,
        json={
            **body,
            "suggestion_set_id": "forged-set",
            "candidate_id": "forged-candidate",
            "rank_position": 999,
        },
    )
    assert forged.status_code == 422

    undone = client.post(path, headers=headers, json=body)
    replay = client.post(path, headers=headers, json=body)
    assert undone.status_code == 200
    assert undone.headers["etag"] == '"3"'
    assert replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["idempotent_replay"] is True
    assert len(asyncio.run(itineraries.list_revisions("workspace-api-suggestions"))) == 3

    events = client.get(
        "/api/trip-workspaces/workspace-api-suggestions/recommendation-events"
    ).json()
    stop_events = [event for event in events if event["event_type"] == "stop_undone"]
    assert len(stop_events) == 1
    event = stop_events[0]
    for field in (
        "session_id",
        "suggestion_set_id",
        "candidate_id",
        "context_hash",
        "policy_version",
        "provider_snapshot_id",
        "rank_position",
    ):
        assert event[field] == accepted_event[field]
    assert event["revision_before"] == 2
    assert event["revision_after"] == 3
    assert event["reason_code"] == "UNDO_ACCEPTED_SUGGESTION"
    assert event["payload"]["source_accept_event_id"] == accepted_event["event_id"]
    assert suggestions.undo_links["public-undo-1"]["event_id"] == event["event_id"]

    conflicting_replay = client.post(
        path,
        headers=headers,
        json={**body, "command_id": "public-undo-different-request"},
    )
    assert conflicting_replay.status_code == 409
    assert conflicting_replay.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    duplicate = client.post(
        path,
        headers={"If-Match": '"2"', "Idempotency-Key": "public-undo-new-key"},
        json={**body, "command_id": "public-undo-duplicate"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "ITINERARY_REVISION_CONFLICT"
    assert client.post(
        "/api/trip-workspaces/another-workspace/undo",
        headers=headers,
        json=body,
    ).status_code == 404


def test_accept_rejects_non_strong_or_ambiguous_if_match_tags(monkeypatch):
    client, _, _, _ = _client(monkeypatch)
    assert _create(client).status_code == 201
    path = (
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets/"
        "set-api/candidates/candidate-api:accept"
    )
    for index, value in enumerate(('W/"1"', '"1", "2"', "*", "", "1", '"abc"', '"0"')):
        response = client.post(
            path,
            headers={"If-Match": value, "Idempotency-Key": f"invalid-etag-{index}"},
        )
        assert response.status_code in {400, 428}, (value, response.text)
        assert response.json()["detail"]["code"] in {"INVALID_IF_MATCH", "IF_MATCH_REQUIRED"}


def test_public_interaction_commands_are_scoped_idempotent_and_server_authored(monkeypatch):
    client, _, suggestions, _ = _client(monkeypatch)
    assert _create(client).status_code == 201
    candidate_path = (
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets/"
        "set-api/candidates/candidate-api"
    )

    missing_key = client.post(f"{candidate_path}:preview")
    assert missing_key.status_code == 400
    assert missing_key.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    preview = client.post(
        f"{candidate_path}:preview",
        headers={"Idempotency-Key": "event-api-1"},
        json={
            "event_type": "candidate_accepted",
            "session_id": "forged-session",
            "context_hash": "0" * 64,
            "policy_version": "forged-policy",
            "provider_snapshot_id": "forged-snapshot",
            "rank_position": 99,
            "occurred_at": "2000-01-01T00:00:00Z",
        },
    )
    replay = client.post(
        f"{candidate_path}:preview",
        headers={"Idempotency-Key": "event-api-1"},
    )
    assert preview.status_code == replay.status_code == 200
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json()["event"] == preview.json()["event"]
    event = preview.json()["event"]
    assert event["event_type"] == "candidate_previewed"
    assert event["session_id"] == "session-api"
    assert event["context_hash"] == "f" * 64
    assert event["policy_version"] == "ranker-api-v1"
    assert event["provider_snapshot_id"] == "fixture-run-api-20260821"
    assert event["rank_position"] == 1
    assert not event["occurred_at"].startswith("2000-")

    conflicting = client.post(
        f"{candidate_path}:dismiss",
        headers={"Idempotency-Key": "event-api-1"},
        json={"reason_code": "TOO_FAR"},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    dismissed = client.post(
        f"{candidate_path}:dismiss",
        headers={"Idempotency-Key": "event-api-dismiss"},
        json={"reason_code": "NOT_INTERESTED"},
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["event"]["reason_code"] == "NOT_INTERESTED"

    completed = client.post(
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets/set-api:line-completed",
        headers={"Idempotency-Key": "event-api-line"},
    )
    assert completed.status_code == 200
    assert completed.json()["event"]["event_type"] == "line_completed"
    assert completed.json()["event"]["candidate_id"] is None

    foreign = client.post(
        f"{candidate_path.replace('candidate-api', 'foreign-candidate')}:preview",
        headers={"Idempotency-Key": "event-api-foreign"},
    )
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "RESOURCE_NOT_FOUND"
    assert [event.event_type.value for event in suggestions.events] == [
        "suggestions_shown",
        "candidate_previewed",
        "candidate_dismissed",
        "line_completed",
    ]


def test_api_rejects_client_candidate_authority_and_missing_anchor_projection(monkeypatch):
    client, _, suggestions, _ = _client(monkeypatch, with_provider=False)
    injected = client.post(
        "/api/trip-workspaces/workspace-api-suggestions/suggestion-sets",
        json={
            "base_revision": 1,
            "day_index": 0,
            "intents": ["NEARBY"],
            "session_id": "session-api",
            "candidates": [{"place_id": "client-invented"}],
        },
    )
    assert injected.status_code == 422
    assert suggestions.sets == {}

    unavailable = _create(client)
    assert unavailable.status_code == 422
    assert unavailable.json()["detail"]["code"] == "INVALID_ITINERARY_EDIT_COMMAND"
    assert unavailable.json()["detail"]["reason_code"] == "SUGGESTION_ANCHOR_COORDINATES_REQUIRED"
    assert suggestions.sets == {}
