from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, Field

from app.schemas.place import Place, PlaceCategory, PlaceSource
from app.itineraries.errors import ItineraryDomainError
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.operations.http import require_idempotency_key
from app.operations.repositories import CreationCommandRepository, PostgresCreationCommandRepository
from app.services.room_access import require_room_member
from app.templates.application_service import TemplateApplicationService
from app.templates.models import CandidateGate, CandidateSuggestion, CityRouteTemplate, EvidenceFreshness, HotelAreaScore, TemplateStatus
from app.templates.repositories import PostgresTemplateRepository, TemplateRepository
from app.templates.service import AmapRouteEstimator, CandidateSuggestionService, HotelScoringService
from app.utils.auth import get_current_user
from app.config import settings


router = APIRouter()


def get_template_repository() -> TemplateRepository:
    return PostgresTemplateRepository()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_creation_command_repository() -> CreationCommandRepository:
    return PostgresCreationCommandRepository()


TemplateRepositoryDep = Annotated[TemplateRepository, Depends(get_template_repository)]
ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
CreationCommandRepositoryDep = Annotated[CreationCommandRepository, Depends(get_creation_command_repository)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


class CandidateSuggestionRequest(BaseModel):
    city: str
    candidate: Place
    previous: Place | None = None
    next_stop: Place | None = None
    gate: CandidateGate = Field(default_factory=CandidateGate)
    transport_mode: str = "transit"


class HotelAreaScoreRequest(BaseModel):
    city: str
    area: Place
    days: list[list[Place]]
    transport_mode: str = "transit"


class TemplateApplyResponse(BaseModel):
    workspace_id: str
    template_id: str
    template_version: int
    revision: dict
    workspace: dict
    template_provenance: str
    human_review_evidence: bool


class WorkspaceCandidatesResponse(BaseModel):
    workspace_id: str
    revision: int
    day: int
    candidates: list[CandidateSuggestion]
    route_context_status: str


class WorkspaceHotelAreasResponse(BaseModel):
    workspace_id: str
    revision: int
    areas: list[HotelAreaScore]
    route_context_status: str


async def _workspace_access(workspace_id: str, user_id: str, repository: ItineraryRepository):
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    await require_room_member(workspace.room_id, user_id)
    return workspace


def _raise_domain(exc: ItineraryDomainError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail()) from exc


@router.get("/route-templates", response_model=list[CityRouteTemplate])
async def list_route_templates(
    _: CurrentUserDep,
    repository: TemplateRepositoryDep,
    city: str | None = Query(default=None),
    status: TemplateStatus | None = Query(default=None),
):
    return await repository.list_templates(city=city, status=status)


@router.get("/cities/{city}/route-templates", response_model=list[CityRouteTemplate])
async def list_city_route_templates(
    city: str,
    _: CurrentUserDep,
    repository: TemplateRepositoryDep,
    status: TemplateStatus | None = Query(default=None),
):
    """Compatibility-first city collection route from the Final API contract."""
    return await repository.list_templates(city=city, status=status)


@router.get("/route-templates/{template_id}", response_model=CityRouteTemplate)
async def get_route_template(
    template_id: str,
    _: CurrentUserDep,
    repository: TemplateRepositoryDep,
    required_version: int | None = Query(default=None, ge=1),
):
    template = await repository.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    if required_version is not None and template.template_version != required_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TEMPLATE_VERSION_STALE",
                "message": "template version has changed; reload the latest skeleton before editing",
                "current_version": template.template_version,
                "requested_version": required_version,
            },
        )
    return template


@router.post("/trip-workspaces/{workspace_id}/templates/{template_id}/apply", response_model=TemplateApplyResponse, status_code=201)
async def apply_route_template(
    workspace_id: str,
    template_id: str,
    response: Response,
    current_user: CurrentUserDep,
    template_repository: TemplateRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
    command_repository: CreationCommandRepositoryDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    template = await template_repository.get_template(template_id)
    if template is None:
        raise HTTPException(status_code=404, detail={"code": "RESOURCE_NOT_FOUND"})
    try:
        result, replayed = await TemplateApplicationService(itinerary_repository).apply_idempotent(
            workspace_id=workspace_id,
            template=template,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            command_repository=command_repository,
        )
    except ItineraryDomainError as exc:
        _raise_domain(exc)
    response.headers["ETag"] = '"1"'
    response.headers["Cache-Control"] = "no-store"
    if replayed:
        response.headers["Idempotency-Replayed"] = "true"
    return result


_SLOT_QUERY = {
    "ATTRACTION": "热门景点",
    "FOOD": "餐厅",
    "HOTEL": "酒店",
    "BREAK": "休息区",
    "TRANSIT": "交通枢纽",
}


async def _provider_candidates(city: str, slot_type: str) -> list[Place]:
    if settings.demo_mode or not settings.amap_api_key:
        return []
    # Reuse the existing provider-only POI parser. It does not invoke an LLM.
    from app.api.recommend import _fetch_amap_poi
    return await _fetch_amap_poi(f"{city}{_SLOT_QUERY[slot_type]}", city, limit=8)


def _template_anchor_projection(revision, templates: list[CityRouteTemplate]) -> dict[str, Place]:
    """Resolve only persisted model-anchor projections; names/ids are never geocoded.

    Revision one stores its frozen projection in change_summary.  The fallback
    by synthetic id supports later immutable workspace revisions: an edit may
    retain a stop while replacing its change summary, but cannot silently turn
    that `model-draft:` id into a provider-confirmed place.
    """
    projected: dict[str, Place] = {}
    raw_projection = revision.change_summary.get("template_anchor_places", {})
    if isinstance(raw_projection, dict):
        for stop_id, item in raw_projection.items():
            if not isinstance(item, dict) or item.get("coordinate_role") != "SYNTHETIC_TEMPLATE_ANCHOR":
                continue
            try:
                projected[str(stop_id)] = Place.model_validate(item["place"])
            except (KeyError, TypeError, ValueError):
                # Corrupt or legacy projection is intentionally unavailable,
                # not recovered from a display label or guessed coordinate.
                continue

    anchors_by_place_id = {
        place.place_id: place
        for template in templates
        for place in template.anchor_places
        if template.provenance.value == "MODEL_GENERATED" and template.status.value == "DRAFT"
    }
    for day in revision.days:
        for stop in day.stops:
            if stop.stop_id not in projected and stop.place_id in anchors_by_place_id:
                projected[stop.stop_id] = anchors_by_place_id[stop.place_id]
    return projected


def _synthetic_hotel_area(city: str, zone) -> Place:
    """Convert a template zone into an explicitly synthetic scoring endpoint."""
    return Place(
        place_id=zone.zone_id,
        name=f"{zone.district or zone.zone_id}模板酒店区域",
        category=PlaceCategory.HOTEL,
        address="模型生成模板区域中心，仅用于本地草稿通勤投影",
        coords=zone.center,
        city=city,
        source=PlaceSource.SYNTHESIZED,
        description="MODEL_GENERATED DRAFT 区域中心，不是已核验酒店或真实住宿推荐。",
        tags=["MODEL_GENERATED_DRAFT", "SYNTHETIC_HOTEL_AREA"],
    )


@router.get("/trip-workspaces/{workspace_id}/candidates", response_model=WorkspaceCandidatesResponse)
async def list_workspace_candidates(
    workspace_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    template_repository: TemplateRepositoryDep,
    day: int = Query(ge=0, le=4),
    before: str | None = Query(default=None),
    after: str | None = Query(default=None),
    slot_type: str = Query(default="ATTRACTION"),
):
    """Rank provider POIs against explicit revision coordinate projections.

    A model-draft anchor can provide local route context, but response status
    and explanation codes retain that provenance.  Unknown revision stops are
    never geocoded from their labels as a shortcut.
    """
    workspace = await _workspace_access(workspace_id, current_user, itinerary_repository)
    if workspace.current_itinerary_revision is None:
        raise HTTPException(status_code=409, detail={"code": "ITINERARY_REVISION_REQUIRED"})
    revision = await itinerary_repository.get_revision(workspace_id, workspace.current_itinerary_revision)
    if revision is None:
        raise HTTPException(status_code=409, detail={"code": "WORKSPACE_REVISION_INCONSISTENT"})
    if day >= len(revision.days):
        raise HTTPException(status_code=422, detail={"code": "INVALID_ITINERARY_EDIT_COMMAND", "message": "day is outside itinerary"})
    known_stop_ids = {stop.stop_id for item in revision.days for stop in item.stops}
    if (before and before not in known_stop_ids) or (after and after not in known_stop_ids):
        raise HTTPException(status_code=422, detail={"code": "INVALID_ITINERARY_EDIT_COMMAND", "message": "before/after must be stops in the workspace revision"})
    normalized_slot = slot_type.upper()
    if normalized_slot not in _SLOT_QUERY:
        raise HTTPException(status_code=422, detail={"code": "INVALID_ITINERARY_EDIT_COMMAND", "message": "unsupported slot_type"})
    templates = await template_repository.list_templates(city=workspace.city)
    projection = _template_anchor_projection(revision, templates)
    day_stops = revision.days[day].stops
    if before is None and after is None:
        # With two or more projected anchors, use the day's stable boundary
        # pair as a transparent default insertion context.
        if len(day_stops) >= 2:
            before, after = day_stops[0].stop_id, day_stops[-1].stop_id
        elif day_stops:
            before = day_stops[0].stop_id
    previous = projection.get(before) if before else None
    next_stop = projection.get(after) if after else None
    context_available = (previous is not None or next_stop is not None)
    # Provider failure produces an empty candidate list, never invented POIs.
    places = await _provider_candidates(workspace.city, normalized_slot)
    suggestions: list[CandidateSuggestion] = []
    for place in places:
        if context_available:
            result = await CandidateSuggestionService(
                AmapRouteEstimator([place, *([previous] if previous else []), *([next_stop] if next_stop else [])], city=workspace.city)
            ).suggest(candidate=place, previous=previous, next_stop=next_stop)
            suggestions.append(result.model_copy(update={
                "explanation_codes": [*result.explanation_codes, "MODEL_GENERATED_DRAFT_CONTEXT"],
            }))
        else:
            result = await CandidateSuggestionService(AmapRouteEstimator([place], city=workspace.city)).suggest(
                candidate=place, previous=None, next_stop=None,
            )
            suggestions.append(result.model_copy(update={
                "explanation_codes": [*result.explanation_codes, "REVISION_STOP_COORDINATES_REQUIRED"],
            }))
    return WorkspaceCandidatesResponse(
        workspace_id=workspace_id,
        revision=revision.revision,
        day=day,
        candidates=suggestions,
        route_context_status=("SYNTHETIC_TEMPLATE_ANCHOR_CONTEXT" if context_available else "REVISION_STOP_COORDINATES_REQUIRED"),
    )


@router.get("/trip-workspaces/{workspace_id}/hotel-areas", response_model=WorkspaceHotelAreasResponse)
async def list_workspace_hotel_areas(
    workspace_id: str,
    current_user: CurrentUserDep,
    itinerary_repository: ItineraryRepositoryDep,
    template_repository: TemplateRepositoryDep,
):
    workspace = await _workspace_access(workspace_id, current_user, itinerary_repository)
    if workspace.current_itinerary_revision is None:
        raise HTTPException(status_code=409, detail={"code": "ITINERARY_REVISION_REQUIRED"})
    revision = await itinerary_repository.get_revision(workspace_id, workspace.current_itinerary_revision)
    if revision is None:
        raise HTTPException(status_code=409, detail={"code": "WORKSPACE_REVISION_INCONSISTENT"})
    templates = await template_repository.list_templates(city=workspace.city)
    projection = _template_anchor_projection(revision, templates)
    days: list[list[Place]] = [
        [projection[stop.stop_id] for stop in item.stops if stop.stop_id in projection]
        for item in revision.days
    ]
    has_complete_projection = all(
        bool(item.stops) and len(days[index]) == len(item.stops)
        for index, item in enumerate(revision.days)
    )
    seen: set[str] = set()
    areas: list[HotelAreaScore] = []
    for template in templates:
        for zone in template.route_zones:
            if zone.zone_id in seen:
                continue
            seen.add(zone.zone_id)
            if not has_complete_projection:
                areas.append(HotelAreaScore(
                    area_id=zone.zone_id,
                    score_minutes=None,
                    all_days_covered=False,
                    evidence_freshness=EvidenceFreshness.UNAVAILABLE,
                    explanation_codes=["REVISION_STOP_COORDINATES_REQUIRED", "HOTEL_ALL_DAY_BOUNDARIES_INCOMPLETE"],
                ))
                continue
            area = _synthetic_hotel_area(workspace.city, zone)
            estimate = await HotelScoringService(
                AmapRouteEstimator([area, *(stop for stops in days for stop in stops)], city=workspace.city)
            ).score_area(area.place_id, days)
            areas.append(estimate.model_copy(update={
                "explanation_codes": [*estimate.explanation_codes, "MODEL_GENERATED_DRAFT_CONTEXT"],
            }))
    return WorkspaceHotelAreasResponse(
        workspace_id=workspace_id,
        revision=revision.revision,
        areas=areas,
        route_context_status=("SYNTHETIC_TEMPLATE_ANCHOR_CONTEXT" if has_complete_projection else "REVISION_STOP_COORDINATES_REQUIRED"),
    )


@router.post("/route-candidates:rank", response_model=CandidateSuggestion)
async def rank_route_candidate(body: CandidateSuggestionRequest, _: CurrentUserDep):
    places = [body.candidate, *([body.previous] if body.previous else []), *([body.next_stop] if body.next_stop else [])]
    estimator = AmapRouteEstimator(places, city=body.city, mode=body.transport_mode)
    return await CandidateSuggestionService(estimator).suggest(
        candidate=body.candidate,
        previous=body.previous,
        next_stop=body.next_stop,
        gate=body.gate,
    )


@router.post("/route-hotels:score-area", response_model=HotelAreaScore)
async def score_hotel_area(body: HotelAreaScoreRequest, _: CurrentUserDep):
    places = [body.area, *(stop for day in body.days for stop in day)]
    estimator = AmapRouteEstimator(places, city=body.city, mode=body.transport_mode)
    return await HotelScoringService(estimator).score_area(body.area.place_id, body.days)
