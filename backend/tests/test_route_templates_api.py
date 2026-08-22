from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from datetime import date
from unittest.mock import AsyncMock

from app.api import templates as template_api
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.templates.repositories import InMemoryTemplateRepository
from app.templates.seed import model_generated_template_drafts
from app.templates.models import EvidenceFreshness, RouteEstimate
from app.schemas.place import Coordinates, Place, PlaceCategory
from app.utils.auth import get_current_user


def test_template_api_lists_drafts_and_returns_stale_version_code():
    app = FastAPI()
    app.include_router(template_api.router, prefix="/api")
    repository = InMemoryTemplateRepository(model_generated_template_drafts())
    app.dependency_overrides[template_api.get_template_repository] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: "template-user"
    client = TestClient(app)

    listing = client.get("/api/route-templates?city=杭州")
    assert listing.status_code == 200
    assert len(listing.json()) == 5
    assert {item["provenance"] for item in listing.json()} == {"MODEL_GENERATED"}

    city_listing = client.get("/api/cities/杭州/route-templates")
    assert city_listing.status_code == 200
    assert [item["template_id"] for item in city_listing.json()] == [item["template_id"] for item in listing.json()]

    template_id = listing.json()[0]["template_id"]
    stale = client.get(f"/api/route-templates/{template_id}?required_version=9")
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "TEMPLATE_VERSION_STALE"


def test_template_apply_creates_authoritative_revision_and_is_idempotent(monkeypatch):
    app = FastAPI()
    app.include_router(template_api.router, prefix="/api")
    templates = InMemoryTemplateRepository(model_generated_template_drafts())
    itineraries = InMemoryItineraryRepository()
    creation_commands = InMemoryCreationCommandRepository()
    import asyncio
    asyncio.run(itineraries.create_workspace(TripWorkspace(
        workspace_id="template-workspace", room_id="template-room", city="北京",
        trip_date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        created_by="template-user",
    )))
    app.dependency_overrides[template_api.get_template_repository] = lambda: templates
    app.dependency_overrides[template_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[template_api.get_creation_command_repository] = lambda: creation_commands
    app.dependency_overrides[get_current_user] = lambda: "template-user"
    monkeypatch.setattr(template_api, "require_room_member", AsyncMock(return_value=None))
    async def provider_candidates(_city, _slot_type):
        return [Place(
            place_id="provider-candidate", name="Provider candidate", category=PlaceCategory.ATTRACTION,
            address="provider address", coords=Coordinates(lng=116.41, lat=39.91), city="北京",
        )]

    async def route(_self, _origin, _destination):
        return RouteEstimate(minutes=12, source="controlled_provider_route", freshness=EvidenceFreshness.FRESH)

    monkeypatch.setattr(template_api, "_provider_candidates", provider_candidates)
    monkeypatch.setattr(template_api.AmapRouteEstimator, "route", route)
    client = TestClient(app)
    template_id = model_generated_template_drafts()[0].template_id
    headers = {"Idempotency-Key": "template-apply-1"}

    created = client.post(f"/api/trip-workspaces/template-workspace/templates/{template_id}/apply", headers=headers)
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.json()["revision"]["source_type"] == "TEMPLATE"
    assert created.json()["revision"]["change_summary"]["human_review_evidence"] is False
    assert created.json()["revision"]["change_summary"]["template_anchor_places"]
    assert len(created.json()["revision"]["days"][0]["stops"]) == 2
    assert created.json()["revision"]["days"][0]["stops"][0]["resolution_status"] == "AMBIGUOUS"
    assert created.json()["template_provenance"] == "MODEL_GENERATED"

    replay = client.post(f"/api/trip-workspaces/template-workspace/templates/{template_id}/apply", headers=headers)
    assert replay.status_code == 201
    assert replay.headers["idempotency-replayed"] == "true"
    assert asyncio.run(itineraries.get_workspace("template-workspace")).current_itinerary_revision == 1

    candidates = client.get("/api/trip-workspaces/template-workspace/candidates?day=0&slot_type=ATTRACTION")
    assert candidates.status_code == 200
    assert candidates.json()["route_context_status"] == "SYNTHETIC_TEMPLATE_ANCHOR_CONTEXT"
    assert candidates.json()["candidates"][0]["delta_route_minutes"] == 12
    assert "MODEL_GENERATED_DRAFT_CONTEXT" in candidates.json()["candidates"][0]["explanation_codes"]

    hotel_areas = client.get("/api/trip-workspaces/template-workspace/hotel-areas")
    assert hotel_areas.status_code == 200
    assert hotel_areas.json()["route_context_status"] == "SYNTHETIC_TEMPLATE_ANCHOR_CONTEXT"
    assert hotel_areas.json()["areas"]
    assert hotel_areas.json()["areas"][0]["score_minutes"] == 48
    assert "MODEL_GENERATED_DRAFT_CONTEXT" in hotel_areas.json()["areas"][0]["explanation_codes"]

    # A missing projection must stay unavailable.  The endpoint is forbidden
    # from reverse-geocoding the replacement id or borrowing an old anchor.
    revision = asyncio.run(itineraries.get_revision("template-workspace", 1))
    assert revision is not None
    unknown_stop = revision.days[0].stops[0].model_copy(update={"place_id": "unprojected-stop"})
    first_day = revision.days[0].model_copy(update={"stops": [unknown_stop, *revision.days[0].stops[1:]]})
    projection = dict(revision.change_summary["template_anchor_places"])
    projection.pop(unknown_stop.stop_id)
    itineraries.revisions[("template-workspace", 1)] = revision.model_copy(update={
        "days": [first_day, *revision.days[1:]],
        "change_summary": {**revision.change_summary, "template_anchor_places": projection},
    })
    unavailable_candidates = client.get(
        f"/api/trip-workspaces/template-workspace/candidates?day=0&before={unknown_stop.stop_id}"
    )
    assert unavailable_candidates.status_code == 200
    assert unavailable_candidates.json()["route_context_status"] == "REVISION_STOP_COORDINATES_REQUIRED"
    assert "REVISION_STOP_COORDINATES_REQUIRED" in unavailable_candidates.json()["candidates"][0]["explanation_codes"]

    unavailable_hotel_areas = client.get("/api/trip-workspaces/template-workspace/hotel-areas")
    assert unavailable_hotel_areas.status_code == 200
    assert unavailable_hotel_areas.json()["route_context_status"] == "REVISION_STOP_COORDINATES_REQUIRED"
    assert unavailable_hotel_areas.json()["areas"][0]["score_minutes"] is None

    conflict = client.post(
        f"/api/trip-workspaces/template-workspace/templates/{template_id}/apply",
        headers={"Idempotency-Key": "template-apply-2"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "ITINERARY_REVISION_CONFLICT"
