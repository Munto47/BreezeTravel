from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_workspaces
from app.itineraries.hash_service import with_content_hash
from app.itineraries.map_projection import build_map_projection
from app.itineraries.models import (
    ItineraryDay, ItineraryRevisionContent, ItineraryStop, RevisionSource,
    TripDateRange, TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.schemas.place import Coordinates
from app.utils.auth import get_current_user


def _revision(*, revision: int, parent_revision: int | None = None, place_id: str = "poi-a"):
    date_range = TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2))
    stop = ItineraryStop(stop_id="stop-a", place_id=place_id, raw_name="地点 A", day_index=0, order_index=0)
    return with_content_hash(ItineraryRevisionContent(
        itinerary_id="map-itinerary", workspace_id="map-workspace", revision=revision,
        parent_revision=parent_revision, source_type=RevisionSource.TEMPLATE if revision == 1 else RevisionSource.MANUAL,
        city="北京", date_range=date_range,
        days=[ItineraryDay(day_index=0, date=date_range.start, stops=[stop]), ItineraryDay(day_index=1, date=date_range.end, stops=[])],
        change_summary={
            "template_anchor_places": {
                "stop-a": {
                    "place": {"place_id": "poi-a", "coords": Coordinates(lng=116.4, lat=39.9).model_dump()},
                    "coordinate_role": "SYNTHETIC_TEMPLATE_ANCHOR", "provenance": "MODEL_GENERATED",
                },
            },
        } if revision == 1 else {"operation": "LOCK_STOP"},
        created_by="map-user",
    ))


def test_projection_inherits_exact_anchor_but_never_reuses_it_after_replace():
    first = _revision(revision=1)
    second = _revision(revision=2, parent_revision=1)
    inherited = build_map_projection(second, lineage=[second, first])
    assert inherited.status == "AVAILABLE"
    assert inherited.stops[0].coords.lng == 116.4
    assert inherited.stops[0].projection_revision == 1

    replaced = _revision(revision=3, parent_revision=2, place_id="poi-replacement")
    result = build_map_projection(replaced, lineage=[replaced, second, first])
    assert result.status == "UNAVAILABLE"
    assert result.stops == []
    assert result.missing_stop_ids == ["stop-a"]
    assert result.coordinate_links == []


def test_coordinate_link_requires_two_explicit_endpoint_projections():
    first = _revision(revision=1)
    second = ItineraryStop(stop_id="stop-b", place_id="poi-b", raw_name="地点 B", day_index=0, order_index=1)
    day = first.days[0].model_copy(update={"stops": [first.days[0].stops[0], second]})
    with_two_points = first.model_copy(update={
        "days": [day, *first.days[1:]],
        "change_summary": {
            "template_anchor_places": {
                **first.change_summary["template_anchor_places"],
                "stop-b": {
                    "place": {"place_id": "poi-b", "coords": {"lng": 116.42, "lat": 39.91}},
                    "coordinate_role": "SYNTHETIC_TEMPLATE_ANCHOR", "provenance": "MODEL_GENERATED",
                },
            },
        },
    })
    result = build_map_projection(with_two_points, lineage=[with_two_points])
    assert result.status == "AVAILABLE"
    assert [(link.from_stop_id, link.to_stop_id, link.kind) for link in result.coordinate_links] == [
        ("stop-a", "stop-b", "CANONICAL_COORDINATE_LINK"),
    ]


def test_projection_api_has_access_control_and_returns_explicit_unavailable(monkeypatch):
    repository = InMemoryItineraryRepository()
    first = _revision(revision=1)
    asyncio.run(repository.create_workspace(TripWorkspace(
        workspace_id="map-workspace", room_id="map-room", city="北京", trip_date_range=first.date_range,
        current_itinerary_revision=1, created_by="map-user",
    ), first))
    app = FastAPI()
    app.include_router(trip_workspaces.router, prefix="/api")
    app.dependency_overrides[trip_workspaces.get_itinerary_repository] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: "map-user"
    monkeypatch.setattr(trip_workspaces, "require_room_member", AsyncMock(return_value=None))
    client = TestClient(app)

    response = client.get("/api/trip-workspaces/map-workspace/revisions/1/map-projection")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "AVAILABLE"
    assert payload["stops"][0]["coordinate_role"] == "SYNTHETIC_TEMPLATE_ANCHOR"
    assert payload["coordinate_links"] == []
