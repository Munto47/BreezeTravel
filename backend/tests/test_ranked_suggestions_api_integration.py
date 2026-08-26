from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import suggestions as api
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
from app.schemas.place import Coordinates, PlaceCategory
from app.suggestions.providers import (
    ControlledCandidateFact,
    ControlledRouteSource,
    ControlledSnapshotCandidateSource,
    ProviderCandidateBatch,
    ProviderCandidateQuery,
    RouteTimes,
)
from app.suggestions.repositories import InMemorySuggestionRepository
from app.utils.auth import get_current_user


CITY_ANCHORS = {
    "北京": ("B000A7BD6T", "故宫博物院", Coordinates(lng=116.3913, lat=39.9163)),
    "上海": ("B00155H52F", "外滩", Coordinates(lng=121.4896, lat=31.2393)),
    "杭州": ("B0FFHZ0001", "西湖风景名胜区", Coordinates(lng=120.1551, lat=30.2523)),
}
FIXTURE_PATH = Path(__file__).parents[1] / "app" / "data" / "amap_mock_places.json"


class AvailableRoutes:
    def __init__(self):
        self.queries: list[ProviderCandidateQuery] = []

    async def route_times(self, query, candidate):
        self.queries.append(query)
        if query.next_anchor is not None:
            route = RouteTimes(
                status="AVAILABLE",
                previous_to_candidate_minutes=8,
                candidate_to_next_minutes=9,
                previous_to_next_minutes=7,
            )
        elif query.anchor_role == "NEXT":
            route = RouteTimes(status="AVAILABLE", candidate_to_next_minutes=8)
        else:
            route = RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=8)
        return await ControlledRouteSource({
            candidate.canonical_place.place_id: route,
        }).route_times(query, candidate)


class EmptySource:
    async def search(self, query):
        del query
        return ProviderCandidateBatch(
            provider_snapshot_id="empty-snapshot-20260821",
            candidates=(),
            retrieved_at=datetime.now(timezone.utc),
        )


class RecordingSource:
    def __init__(self, facts: list[ControlledCandidateFact], now: datetime):
        self.delegate = ControlledSnapshotCandidateSource(
            facts,
            snapshot_id="controlled-api-snapshot-20260821",
            observed_at=now,
        )

    @property
    def queries(self):
        return self.delegate.queries

    async def search(self, query):
        return await self.delegate.search(query)


def _projection(stop_id: str, place_id: str, name: str, coords: Coordinates):
    return {
        "place_id": place_id,
        "canonical_name": name,
        "coords": coords.model_dump(mode="json"),
        "coordinate_role": "CANONICAL_PROVIDER_POI",
        "provenance": "IMMUTABLE_PROVIDER_RECEIPT",
        "receipt_hash": "a" * 64,
    }


def _repositories(
    city: str,
    *,
    two_anchors: bool = False,
    anchor_name_override: str | None = None,
):
    dates = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    place_id, name, coords = CITY_ANCHORS[city]
    name = anchor_name_override or name
    stops = [
        ItineraryStop(
            stop_id="anchor-a",
            place_id=place_id,
            raw_name=name,
            day_index=0,
            order_index=0,
        )
    ]
    projections = {"anchor-a": _projection("anchor-a", place_id, name, coords)}
    if two_anchors:
        second_coords = Coordinates(lng=coords.lng + 0.04, lat=coords.lat + 0.02)
        stops.append(ItineraryStop(
            stop_id="anchor-b",
            place_id=f"{place_id}-second",
            raw_name=f"{name}第二锚点",
            day_index=0,
            order_index=1,
        ))
        projections["anchor-b"] = _projection(
            "anchor-b", f"{place_id}-second", f"{name}第二锚点", second_coords,
        )
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id=f"itin-{city}",
        workspace_id=f"workspace-{city}",
        revision=1,
        source_type=RevisionSource.IMPORT,
        city=city,
        date_range=dates,
        days=[
            ItineraryDay(day_index=0, date=dates.start, stops=stops),
            ItineraryDay(day_index=1, date=dates.end, stops=[]),
        ],
        change_summary={"map_stop_projections": projections},
        created_by="user-ranked-api",
    ))
    workspace = TripWorkspace(
        workspace_id=f"workspace-{city}",
        room_id=f"room-{city}",
        city=city,
        trip_date_range=dates,
        current_itinerary_revision=1,
        created_by="user-ranked-api",
    )
    itineraries = InMemoryItineraryRepository()
    asyncio.run(itineraries.create_workspace(workspace, revision))
    return itineraries, InMemorySuggestionRepository(itineraries)


def _client(monkeypatch, city: str, provider: api.RankedSuggestionProvider, *, two_anchors: bool = False):
    itineraries, suggestions = _repositories(city, two_anchors=two_anchors)
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[api.get_ranked_suggestion_provider] = lambda: provider
    app.dependency_overrides[get_current_user] = lambda: "user-ranked-api"
    monkeypatch.setattr(api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), suggestions


def _post(client: TestClient, city: str, **updates):
    payload = {
        "base_revision": 1,
        "day_index": 0,
        "insert_after_stop_id": "anchor-a",
        "intents": ["NEARBY", "POPULAR"],
        "session_id": f"session-{city}",
    }
    payload.update(updates)
    return client.post(f"/api/trip-workspaces/workspace-{city}/suggestion-sets", json=payload)


@pytest.mark.parametrize("city", ["北京", "上海", "杭州"])
def test_default_fixture_provider_creates_four_to_six_real_frozen_candidates(monkeypatch, city):
    itineraries, suggestions = _repositories(city)
    routes = AvailableRoutes()
    now = datetime.now(timezone.utc)

    def controlled_fixture_source(candidate_city, observed_at):
        archived = api._fixture_candidate_source(candidate_city, observed_at)
        return ControlledSnapshotCandidateSource(
            list(archived.facts),
            snapshot_id=f"controlled-route-test-{archived.snapshot_id}",
            observed_at=observed_at,
        )

    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=controlled_fixture_source,
        route_source_factory=lambda: routes,
        clock=lambda: now,
    )
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[api.get_ranked_suggestion_provider] = lambda: provider
    app.dependency_overrides[get_current_user] = lambda: "user-ranked-api"
    monkeypatch.setattr(api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(api.settings, "runtime_profile", "test")
    monkeypatch.setattr(api.settings, "amap_mock", True)

    response = _post(TestClient(app), city)

    assert response.status_code == 201, response.text
    body = response.json()
    assert 4 <= len(body["candidates"]) <= 6
    assert body["provider_snapshot_id"].startswith(f"controlled-route-test-amap-fixture-{city}-")
    assert all(item["canonical_place"]["city"] == city for item in body["candidates"])
    assert all(item["provider_receipt"]["provider"] == "controlled_snapshot" for item in body["candidates"])
    assert all(item["provider_receipt"]["execution_mode"] == "fixture" for item in body["candidates"])
    assert CITY_ANCHORS[city][0] not in {
        item["canonical_place"]["place_id"] for item in body["candidates"]
    }
    assert routes.queries
    assert routes.queries[0].anchor_coords == CITY_ANCHORS[city][2]
    assert set(routes.queries[0].keywords) == {"附近", "热门", "口碑"}
    assert len(suggestions.sets) == 1


def test_complete_insert_edge_queries_midpoint_and_binds_both_canonical_anchors(monkeypatch):
    city = "北京"
    itineraries, _ = _repositories(city, two_anchors=True)
    now = datetime.now(timezone.utc)
    fact = ControlledCandidateFact(
        place_id="candidate-edge",
        name="边中候选",
        city=city,
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lng=116.4113, lat=39.9263),
        popularity=0.8,
    )
    source = RecordingSource([fact], now)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=AvailableRoutes,
        clock=lambda: now,
    )
    monkeypatch.setattr(api, "_suggestion_now", lambda: now)
    client, _ = _client(monkeypatch, city, provider, two_anchors=True)

    response = _post(
        client,
        city,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
        intents=["FUN"],
    )

    assert response.status_code == 201, response.text
    query = source.queries[0]
    assert query.anchor_coords is None
    assert query.previous_anchor.stop_id == "anchor-a"
    assert query.next_anchor.stop_id == "anchor-b"
    assert query.search_center.lng == pytest.approx(116.4113)
    assert query.search_center.lat == pytest.approx(39.9263)


def test_before_only_anchor_routes_candidate_to_next_and_accepts_exact_edge(monkeypatch):
    city = "北京"
    itineraries, _ = _repositories(city)
    now = datetime.now(timezone.utc)
    anchor_id, _, coords = CITY_ANCHORS[city]
    fact = ControlledCandidateFact(
        place_id="candidate-before",
        name="前置候选",
        city=city,
        category=PlaceCategory.ATTRACTION,
        coords=Coordinates(lng=coords.lng - 0.002, lat=coords.lat),
        popularity=0.8,
    )
    source = RecordingSource([fact], now)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=AvailableRoutes,
        clock=lambda: now,
    )
    client, _ = _client(monkeypatch, city, provider)

    created = _post(
        client,
        city,
        insert_after_stop_id=None,
        insert_before_stop_id="anchor-a",
        intents=["FUN"],
    )
    assert created.status_code == 201, created.text
    frozen = created.json()
    route_receipt = frozen["candidates"][0]["route_delta"]["route_receipts"][0]
    assert route_receipt["leg"] == "CANDIDATE_TO_NEXT"
    assert route_receipt["destination_place_id"] == anchor_id

    set_id = frozen["suggestion_set_id"]
    candidate_id = frozen["candidates"][0]["candidate_id"]
    accepted = client.post(
        f"/api/trip-workspaces/workspace-{city}/suggestion-sets/"
        f"{set_id}/candidates/{candidate_id}:accept",
        headers={"If-Match": '"1"', "Idempotency-Key": "accept-before-only"},
    )
    assert accepted.status_code == 200, accepted.text
    assert [stop["place_id"] for stop in accepted.json()["revision"]["days"][0]["stops"]] == [
        "candidate-before",
        anchor_id,
    ]


def test_default_mock_without_frozen_route_snapshot_fails_closed(monkeypatch):
    city = "北京"
    itineraries, _ = _repositories(city)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
    )
    client, suggestions = _client(monkeypatch, city, provider)
    monkeypatch.setattr(api.settings, "runtime_profile", "test")
    monkeypatch.setattr(api.settings, "amap_mock", True)

    response = _post(client, city)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["reason_code"] == "SUGGESTION_PROVIDER_UNAVAILABLE"
    assert "ROUTE_PROVIDER_NOT_CONFIGURED" in detail["candidate_reason_codes"]
    assert suggestions.sets == {}


def test_fixture_receipt_observed_at_is_snapshot_mtime_not_request_time(monkeypatch):
    monkeypatch.setattr(api.settings, "runtime_profile", "test")
    request_time = datetime.now(timezone.utc)

    source = api._fixture_candidate_source("北京", request_time)

    expected = datetime.fromtimestamp(FIXTURE_PATH.stat().st_mtime, timezone.utc)
    assert source.observed_at == expected
    assert source.observed_at != request_time


def test_api_filters_wrong_city_selected_duplicate_hard_and_unknown_without_padding(monkeypatch):
    city = "杭州"
    itineraries, _ = _repositories(city)
    now = datetime.now(timezone.utc)
    anchor_id, anchor_name, coords = CITY_ANCHORS[city]
    common = dict(city=city, category=PlaceCategory.ATTRACTION)
    facts = [
        ControlledCandidateFact(
            place_id="good-one", name="合法候选", coords=Coordinates(lng=coords.lng + 0.001, lat=coords.lat),
            official_prior_refs=("official-route:hangzhou:v1",), **common,
        ),
        ControlledCandidateFact(
            place_id=anchor_id, name="已选地点不同显示名", coords=coords, **common,
        ),
        ControlledCandidateFact(
            place_id="same-name", name=anchor_name, coords=Coordinates(lng=coords.lng + 0.002, lat=coords.lat), **common,
        ),
        ControlledCandidateFact(
            place_id="duplicate-a", name="规范重名", coords=Coordinates(lng=coords.lng + 0.003, lat=coords.lat), **common,
        ),
        ControlledCandidateFact(
            place_id="duplicate-b", name="规范重名", coords=Coordinates(lng=coords.lng + 0.004, lat=coords.lat), **common,
        ),
        ControlledCandidateFact(
            place_id="wrong-city", name="错城候选", city="上海", category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lng=coords.lng + 0.005, lat=coords.lat),
        ),
        ControlledCandidateFact(
            place_id="hard-blocked", name="硬阻止候选", coords=Coordinates(lng=coords.lng + 0.006, lat=coords.lat),
            hard_block_codes=("MEMBER_HARD_CONSTRAINT",), **common,
        ),
        ControlledCandidateFact(
            place_id="unknown-route", name="路线未知候选", coords=Coordinates(lng=coords.lng + 0.007, lat=coords.lat), **common,
        ),
    ]
    source = RecordingSource(facts, now)
    routes = ControlledRouteSource({
        "good-one": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=8),
        "duplicate-a": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=9),
        "duplicate-b": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=10),
        "hard-blocked": RouteTimes(status="AVAILABLE", previous_to_candidate_minutes=5),
        "unknown-route": RouteTimes(status="UNKNOWN", reason_code="CONTROLLED_PROVIDER_DOWN"),
    })
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=lambda: routes,
        clock=lambda: now,
    )
    client, _ = _client(monkeypatch, city, provider)

    response = _post(client, city, intents=["FUN"])

    assert response.status_code == 201, response.text
    candidates = response.json()["candidates"]
    assert 1 <= len(candidates) <= 3
    ids = {item["canonical_place"]["place_id"] for item in candidates}
    assert "good-one" in ids
    assert len(ids & {"duplicate-a", "duplicate-b"}) == 1
    assert not ids & {anchor_id, "same-name", "wrong-city", "hard-blocked", "unknown-route"}
    assert [item["rank_position"] for item in candidates] == list(range(1, len(candidates) + 1))
    good = next(item for item in candidates if item["canonical_place"]["place_id"] == "good-one")
    # Provider-controlled strings cannot self-assert official provenance;
    # Hangzhou's official archive is unavailable, so the candidate remains
    # usable without an official ref or score boost.
    assert good["source_prior_refs"] == []
    assert good["score_components"]["official_route_prior"] == 0
    assert "OFFICIAL_ROUTE_PRIOR_UNAVAILABLE" in good["explanation_codes"]


def test_api_freezes_hash_verified_community_route_prior_refs(monkeypatch):
    city = "北京"
    itineraries, suggestions = _repositories(city, anchor_name_override="Forbidden City")
    now = datetime.now(timezone.utc)
    source = RecordingSource([
        ControlledCandidateFact(
            place_id="tiananmen-community-candidate",
            name="Tiananmen Square",
            city=city,
            category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lng=116.3975, lat=39.9031),
        )
    ], now)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=AvailableRoutes,
        clock=lambda: now,
    )
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[api.get_ranked_suggestion_provider] = lambda: provider
    app.dependency_overrides[get_current_user] = lambda: "user-ranked-api"
    monkeypatch.setattr(api, "require_room_member", AsyncMock(return_value=None))

    response = _post(TestClient(app), city, intents=["FUN"])

    assert response.status_code == 201, response.text
    assert response.json()["candidates"][0]["source_prior_refs"] == [
        "wikivoyage:open-wikivoyage-beijing-5331911@5331911"
        "#content-sha256=5e8157689cf4aff71d8d33eda6231137a508faa6a115c7f1cd2d7fbd2701ee5d"
    ]


def test_api_and_exposure_event_freeze_hash_bound_official_prior_refs(monkeypatch):
    city = "北京"
    itineraries, suggestions = _repositories(city, anchor_name_override="故宫博物院")
    now = datetime.now(timezone.utc)
    source = RecordingSource([
        ControlledCandidateFact(
            place_id="provider-resolved-jingshan",
            name="景山公园",
            city=city,
            category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lng=116.3975, lat=39.9251),
        )
    ], now)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=AvailableRoutes,
        clock=lambda: now,
    )
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[api.get_ranked_suggestion_provider] = lambda: provider
    app.dependency_overrides[get_current_user] = lambda: "user-ranked-api"
    monkeypatch.setattr(api, "require_room_member", AsyncMock(return_value=None))
    client = TestClient(app)

    response = _post(client, city, intents=["FUN"])

    assert response.status_code == 201, response.text
    candidate = response.json()["candidates"][0]
    assert "OFFICIAL_ROUTE_PRIOR_HASH_BOUND" in candidate["explanation_codes"]
    assert len(candidate["source_prior_refs"]) == 1
    official_ref = candidate["source_prior_refs"][0]
    assert official_ref.startswith("official-route:official-beijing-route-library-20260821#raw-sha256=")
    assert "&extract-sha256=" in official_ref
    assert "&body-sha256=" in official_ref

    events_response = client.get(
        "/api/trip-workspaces/workspace-北京/recommendation-events"
    )
    assert events_response.status_code == 200, events_response.text
    shown = events_response.json()[0]
    assert shown["event_type"] == "suggestions_shown"
    assert shown["payload"]["source_prior_refs"] == {
        candidate["candidate_id"]: [official_ref]
    }


def test_empty_provider_has_stable_failed_event_and_creates_no_set(monkeypatch):
    city = "上海"
    itineraries, _ = _repositories(city)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: EmptySource(),
        route_source_factory=AvailableRoutes,
    )
    client, suggestions = _client(monkeypatch, city, provider)

    response = _post(client, city)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "SUGGESTION_PROVIDER_UNAVAILABLE"
    assert detail["event_type"] == "suggestion_failed"
    assert detail["reason_code"] == "SUGGESTION_PROVIDER_UNAVAILABLE"
    assert detail["provider_status"] == "EMPTY"
    assert suggestions.sets == {}
    assert len(suggestions.events) == 1
    failed = suggestions.events[0]
    assert failed.event_type.value == "suggestion_failed"
    assert failed.suggestion_set_id is None
    assert failed.candidate_id is None
    assert failed.session_id == "session-上海"
    assert failed.reason_code == "SUGGESTION_PROVIDER_UNAVAILABLE"
    assert failed.payload["request_context"] == {
        "base_revision": 1,
        "day_index": 0,
        "insert_after_stop_id": "anchor-a",
        "insert_before_stop_id": None,
        "intents": ["NEARBY", "POPULAR"],
        "provider_status": "EMPTY",
    }


def test_new_selected_anchor_builds_a_new_spatial_query(monkeypatch):
    city = "北京"
    itineraries, _ = _repositories(city, two_anchors=True)
    now = datetime(2026, 8, 21, 5, 0, tzinfo=timezone.utc)
    facts = [
        ControlledCandidateFact(
            place_id="fresh-candidate",
            name="新候选",
            city=city,
            category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lng=116.42, lat=39.93),
        )
    ]
    source = RecordingSource(facts, now)
    provider = api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=AvailableRoutes,
        clock=lambda: now,
    )
    monkeypatch.setattr(api, "_suggestion_now", lambda: now)
    client, _ = _client(monkeypatch, city, provider, two_anchors=True)

    first = _post(client, city, insert_after_stop_id="anchor-a", intents=["FUN"])
    second = _post(client, city, insert_after_stop_id="anchor-b", intents=["FUN"], session_id="second-session")

    assert first.status_code == second.status_code == 201
    assert len(source.queries) == 2
    assert source.queries[0].anchor_coords != source.queries[1].anchor_coords
    assert source.queries[0].anchor_name != source.queries[1].anchor_name


def test_incomplete_edge_and_client_place_payload_are_both_rejected(monkeypatch):
    city = "北京"
    itineraries, _ = _repositories(city, two_anchors=True)
    provider = api.DefaultRankedSuggestionProvider(itineraries)
    client, suggestions = _client(monkeypatch, city, provider, two_anchors=True)

    not_an_edge = _post(
        client,
        city,
        insert_after_stop_id="anchor-b",
        insert_before_stop_id="anchor-a",
    )
    client_place = _post(client, city, place={"place_id": "client-invented", "coords": {"lng": 0, "lat": 0}})

    assert not_an_edge.status_code == 422
    assert not_an_edge.json()["detail"]["reason_code"] == "SUGGESTION_INSERT_EDGE_REQUIRED"
    assert client_place.status_code == 422
    assert suggestions.sets == {}
