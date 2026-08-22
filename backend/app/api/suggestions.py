from __future__ import annotations

import hashlib
import json
import re
import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.audit.repositories import PostgresAuditRepository
from app.audit.suggestion_gate import SuggestionAuditGate
from app.constraints.amap_types import typecodes_for_category
from app.itineraries.errors import (
    InvalidEditCommandError,
    ItineraryDomainError,
    ResourceNotFound,
    RevisionConflictError,
)
from app.itineraries.map_projection import MapStopProjection, build_map_projection
from app.itineraries.models import ItineraryRevision
from app.itineraries.repositories import ItineraryRepository, PostgresItineraryRepository
from app.operations.http import require_idempotency_key
from app.members.repositories import PostgresMemberConstraintRepository
from app.route_priors.loader import RoutePriorLoader
from app.schemas.place import Coordinates, PlaceCategory
from app.services.room_access import require_room_member
from app.suggestions.errors import SuggestionProviderUnavailableError
from app.suggestions.frozen_snapshot import (
    FrozenSnapshotCandidateSource,
    FrozenSnapshotRouteSource,
    snapshot_spec_from_settings,
    validate_suggestion_provider_configuration,
)
from app.suggestions.models import (
    AcceptSuggestionResult,
    FreshnessStatus,
    RecommendationEvent,
    RecommendationEventCommandResult,
    SuggestionIntent,
    SuggestionSet,
    SuggestionSetCreateInput,
)
from app.suggestions.providers import (
    AmapCandidateSource,
    AmapRouteSource,
    AnchorRef,
    CandidateRouteSource,
    ControlledCandidateFact,
    ControlledSnapshotCandidateSource,
    ProviderCandidateQuery,
    ProviderCandidateSource,
)
from app.suggestions.ranking import AnchorCandidateRanker, RankingContext
from app.suggestions.repositories import PostgresSuggestionRepository, SuggestionRepository
from app.suggestions.service import SuggestionSetService
from app.utils.auth import get_current_user


router = APIRouter()


class CreateSuggestionSetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_revision: int = Field(gt=0)
    day_index: int = Field(ge=0, le=4)
    insert_after_stop_id: str | None = None
    insert_before_stop_id: str | None = None
    intents: list[SuggestionIntent] = Field(min_length=1)
    session_id: str = Field(min_length=1)


class DismissCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: str = Field(min_length=1, max_length=100)


class RankedSuggestionProvider(Protocol):
    async def rank(
        self,
        *,
        workspace_id: str,
        request: CreateSuggestionSetRequest,
        actor_user_id: str,
    ) -> SuggestionSetCreateInput: ...


CandidateSourceFactory = Callable[[str, datetime], ProviderCandidateSource]
RouteSourceFactory = Callable[[], CandidateRouteSource | None]


_INTENT_CATEGORIES: dict[SuggestionIntent, tuple[PlaceCategory, ...]] = {
    SuggestionIntent.NEARBY: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
    SuggestionIntent.POPULAR: (PlaceCategory.ATTRACTION, PlaceCategory.FOOD),
    SuggestionIntent.FUN: (PlaceCategory.ATTRACTION,),
    SuggestionIntent.FOOD: (PlaceCategory.FOOD,),
}
_INTENT_KEYWORDS: dict[SuggestionIntent, tuple[str, ...]] = {
    SuggestionIntent.NEARBY: ("附近",),
    SuggestionIntent.POPULAR: ("热门", "口碑"),
    SuggestionIntent.FUN: ("景点", "好玩"),
    SuggestionIntent.FOOD: ("美食", "餐厅"),
}
_INTENT_RADIUS_M: dict[SuggestionIntent, int] = {
    SuggestionIntent.NEARBY: 5_000,
    SuggestionIntent.POPULAR: 15_000,
    SuggestionIntent.FUN: 12_000,
    SuggestionIntent.FOOD: 5_000,
}
_FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "amap_mock_places.json"


def _unique(values):
    return tuple(dict.fromkeys(values))


def _fixture_candidate_source(city: str, observed_at: datetime) -> ProviderCandidateSource:
    """Freeze the existing three-city POI fixture as an explicit snapshot.

    This adapter does not label fixture facts as live and does not call the
    chat graph, an LLM or the full itinerary planner.
    """
    if settings.runtime_profile not in {"demo", "test", "local_fixture"}:
        raise SuggestionProviderUnavailableError(
            "AMAP fixture suggestions are forbidden in this runtime profile",
            context={"event_type": "suggestion_failed", "reason_code": "FIXTURE_RUNTIME_FORBIDDEN"},
        )
    del observed_at
    try:
        raw_bytes = _FIXTURE_PATH.read_bytes()
        snapshot_observed_at = datetime.fromtimestamp(_FIXTURE_PATH.stat().st_mtime, timezone.utc)
        payload = json.loads(raw_bytes.decode("utf-8"))
        rows = payload[city]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SuggestionProviderUnavailableError(
            "controlled AMAP fixture snapshot is unavailable",
            context={"event_type": "suggestion_failed", "reason_code": "FIXTURE_SNAPSHOT_UNAVAILABLE"},
        ) from exc
    facts: list[ControlledCandidateFact] = []
    for row in rows:
        try:
            category = PlaceCategory(str(row["category"]))
            if category is PlaceCategory.UNKNOWN:
                continue
            rating = row.get("amap_rating")
            facts.append(ControlledCandidateFact(
                place_id=str(row["place_id"]),
                name=str(row["name"]),
                city=str(row["city"]),
                category=category,
                coords=Coordinates.model_validate(row["coords"]),
                district=str(row.get("district") or "") or None,
                address=str(row.get("address") or "") or None,
                popularity=(min(1.0, max(0.0, float(rating) / 5)) if rating is not None else 0.0),
                content_relevance=0.5,
                diversity_tags=tuple(str(item) for item in row.get("tags") or [] if str(item).strip()),
            ))
        except (KeyError, TypeError, ValueError):
            # One malformed fixture row is not allowed to become a candidate.
            continue
    snapshot_hash = hashlib.sha256(raw_bytes).hexdigest()
    return ControlledSnapshotCandidateSource(
        facts,
        snapshot_id=f"amap-fixture-{city}-{snapshot_hash[:24]}",
        observed_at=snapshot_observed_at,
    )


def _default_candidate_source(city: str, observed_at: datetime) -> ProviderCandidateSource:
    validate_suggestion_provider_configuration(settings)
    if settings.suggestion_provider_mode == "frozen_snapshot":
        return FrozenSnapshotCandidateSource(snapshot_spec_from_settings(settings))
    if settings.suggestion_provider_mode == "fixture":
        return _fixture_candidate_source(city, observed_at)
    if settings.suggestion_provider_mode == "live":
        return AmapCandidateSource()
    if settings.amap_mock or settings.demo_mode:
        return _fixture_candidate_source(city, observed_at)
    return AmapCandidateSource()


def _default_route_source() -> CandidateRouteSource | None:
    validate_suggestion_provider_configuration(settings)
    if settings.suggestion_provider_mode == "frozen_snapshot":
        return FrozenSnapshotRouteSource(snapshot_spec_from_settings(settings))
    if settings.suggestion_provider_mode == "fixture":
        return None
    if settings.suggestion_provider_mode == "live":
        return AmapRouteSource()
    # The POI fixture contains no route snapshot.  Do not run the live adapter
    # against it and do not turn straight-line distance into route evidence.
    if settings.amap_mock or settings.demo_mode:
        return None
    return AmapRouteSource()


def _suggestion_now() -> datetime:
    if settings.suggestion_provider_mode == "frozen_snapshot":
        validate_suggestion_provider_configuration(settings)
        replay_at = settings.suggestion_snapshot_replay_at
        assert replay_at is not None
        return replay_at
    return datetime.now(timezone.utc)


def _suggestion_service(
    repository: SuggestionRepository,
    itinerary_repository: ItineraryRepository,
) -> SuggestionSetService:
    return SuggestionSetService(repository, itinerary_repository, clock=_suggestion_now)


def _stop_index(revision: ItineraryRevision, day_index: int) -> dict[str, int]:
    if day_index >= len(revision.days):
        raise InvalidEditCommandError("suggestion day is outside the base revision")
    return {stop.stop_id: index for index, stop in enumerate(revision.days[day_index].stops)}


def _anchor_ref(stop_id: str, projection: dict[str, MapStopProjection]) -> AnchorRef:
    item = projection.get(stop_id)
    if item is None:
        raise InvalidEditCommandError(
            "selected suggestion anchor has no canonical coordinate projection",
            context={"reason_code": "SUGGESTION_ANCHOR_COORDINATES_REQUIRED", "stop_id": stop_id},
        )
    return AnchorRef(stop_id=item.stop_id, place_id=item.place_id, name=item.name, coords=item.coords)


class DefaultRankedSuggestionProvider:
    """Product adapter from an authoritative revision to AnchorCandidateRanker."""

    def __init__(
        self,
        itinerary_repository: ItineraryRepository,
        *,
        candidate_source_factory: CandidateSourceFactory = _default_candidate_source,
        route_source_factory: RouteSourceFactory = _default_route_source,
        route_prior_loader: RoutePriorLoader | None = None,
        clock: Callable[[], datetime] | None = None,
        expires_in: timedelta = timedelta(minutes=30),
        audit_gate: SuggestionAuditGate | None = None,
    ):
        self.itinerary_repository = itinerary_repository
        self.candidate_source_factory = candidate_source_factory
        self.route_source_factory = route_source_factory
        self.route_prior_loader = route_prior_loader or RoutePriorLoader()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.expires_in = expires_in
        self.audit_gate = audit_gate

    async def rank(
        self,
        *,
        workspace_id: str,
        request: CreateSuggestionSetRequest,
        actor_user_id: str,
    ) -> SuggestionSetCreateInput:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.current_itinerary_revision != request.base_revision:
            raise RevisionConflictError(
                "suggestion base revision is stale",
                context={
                    "expected_revision": request.base_revision,
                    "actual_revision": workspace.current_itinerary_revision,
                },
            )
        revision = await self.itinerary_repository.get_revision(workspace_id, request.base_revision)
        if revision is None:
            raise ResourceNotFound("base revision does not exist")
        if revision.city != workspace.city:
            raise InvalidEditCommandError("workspace and base revision cities differ")

        day_stop_indexes = _stop_index(revision, request.day_index)
        after, before = request.insert_after_stop_id, request.insert_before_stop_id
        if after is None and before is None:
            raise InvalidEditCommandError(
                "a selected stop or complete insertion edge is required",
                context={"reason_code": "SUGGESTION_ANCHOR_REQUIRED"},
            )
        for value in (after, before):
            if value is not None and value not in day_stop_indexes:
                raise InvalidEditCommandError(
                    "suggestion anchor is not in the requested day",
                    context={"reason_code": "SUGGESTION_ANCHOR_NOT_IN_DAY", "stop_id": value},
                )
        if after is not None and before is not None and day_stop_indexes[before] != day_stop_indexes[after] + 1:
            raise InvalidEditCommandError(
                "suggestion insertion anchors must form one complete route edge",
                context={"reason_code": "SUGGESTION_INSERT_EDGE_REQUIRED"},
            )

        revisions = await self.itinerary_repository.list_revisions(workspace_id)
        lineage = sorted(
            (item for item in revisions if item.revision <= request.base_revision),
            key=lambda item: item.revision,
            reverse=True,
        )
        map_projection = build_map_projection(revision, lineage=lineage)
        projected_by_stop = {item.stop_id: item for item in map_projection.stops}

        previous_anchor = _anchor_ref(after, projected_by_stop) if after is not None else None
        next_anchor = _anchor_ref(before, projected_by_stop) if before is not None else None
        if previous_anchor is not None and next_anchor is not None:
            anchor_name = f"{previous_anchor.name}→{next_anchor.name}"
            anchor_coords = None
        else:
            selected = previous_anchor or next_anchor
            assert selected is not None
            anchor_name = selected.name
            anchor_coords = selected.coords

        allowed_categories = frozenset(
            category for intent in request.intents for category in _INTENT_CATEGORIES[intent]
        )
        typecodes = _unique(
            typecode for category in sorted(allowed_categories, key=lambda item: item.value)
            for typecode in typecodes_for_category(category)
        )
        keywords = _unique(keyword for intent in request.intents for keyword in _INTENT_KEYWORDS[intent])
        query = ProviderCandidateQuery(
            city=workspace.city,
            intents=tuple(request.intents),
            typecodes=typecodes,
            radius_m=max(_INTENT_RADIUS_M[intent] for intent in request.intents),
            anchor_name=anchor_name,
            anchor_place_id=(selected.place_id if previous_anchor is None or next_anchor is None else None),
            anchor_coords=anchor_coords,
            anchor_role=("PREVIOUS" if after is not None else "NEXT"),
            previous_anchor=previous_anchor if next_anchor is not None else None,
            next_anchor=next_anchor if previous_anchor is not None else None,
            keywords=keywords,
            transport_mode="walking",
        )
        selected_place_ids = frozenset(stop.place_id for day in revision.days for stop in day.stops)
        selected_place_names = frozenset(
            item.name for item in map_projection.stops if item.name.strip()
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeError("suggestion provider clock must be timezone-aware")
        ranker = AnchorCandidateRanker(
            self.candidate_source_factory(workspace.city, now),
            self.route_source_factory(),
            route_prior_loader=self.route_prior_loader,
        )
        result = await ranker.rank(RankingContext(
            query=query,
            allowed_categories=allowed_categories,
            selected_place_ids=selected_place_ids,
            selected_place_names=selected_place_names,
            canonical_duplicate_names=selected_place_names,
            as_of=now,
        ))

        ranked_visible = list(result.visible_candidates if self.audit_gate is not None else result.candidates)
        if self.audit_gate is not None and ranked_visible:
            ranked_visible = list(await asyncio.gather(*(
                self.audit_gate.evaluate_candidate(
                    workspace=workspace,
                    base=revision,
                    candidate=candidate,
                    day_index=request.day_index,
                    insert_after_stop_id=after,
                    insert_before_stop_id=before,
                )
                for candidate in ranked_visible[:6]
            )))
        acceptable = [
            candidate
            for candidate in ranked_visible
            if candidate.hard_gate.passed
            and candidate.route_delta.status == "AVAILABLE"
            and candidate.evidence_freshness.status is FreshnessStatus.FRESH
            and candidate.classification.value in {"ON_ROUTE", "ACCEPTABLE_DETOUR"}
        ]
        if not ranked_visible or (self.audit_gate is None and not acceptable):
            failed = result.provider_status in {"ERROR", "TIMEOUT"}
            candidate_reason_codes = sorted({
                reason
                for candidate in result.candidates
                for reason in (
                    candidate.route_delta.reason_code,
                    candidate.evidence_freshness.reason_code,
                )
                if reason
            })
            raise SuggestionProviderUnavailableError(
                "ranked suggestion provider failed" if failed else "no acceptable provider candidate is available",
                context={
                    "event_type": "suggestion_failed",
                    "reason_code": "SUGGESTION_PROVIDER_FAILED" if failed else "SUGGESTION_PROVIDER_UNAVAILABLE",
                    "provider_status": result.provider_status,
                    "shortage_reason_codes": list(result.shortage_reason_codes),
                    "candidate_reason_codes": candidate_reason_codes,
                    "excluded_counts": result.excluded_counts,
                },
            )
        if not result.provider_snapshot_id:
            raise SuggestionProviderUnavailableError(
                "ranked candidates are not bound to a provider snapshot",
                context={"event_type": "suggestion_failed", "reason_code": "PROVIDER_SNAPSHOT_REQUIRED"},
            )
        # With the Audit authority enabled, UNKNOWN and BLOCKER/HIGH candidates
        # remain separately visible and non-acceptable.  Stable-partition the
        # display as directly acceptable, other HARD-pass (for example a real
        # but distant DEFER option), then blocked.  This keeps a HARD-blocked
        # item from leapfrogging a usable deferred option merely because its
        # original provider rank was higher.  No candidate is fabricated or
        # silently discarded.
        acceptable_ids = {candidate.candidate_id for candidate in acceptable}
        visible = (
            sorted(
                ranked_visible,
                key=lambda candidate: (
                    0
                    if candidate.candidate_id in acceptable_ids
                    else 1
                    if candidate.hard_gate.passed
                    else 2,
                    candidate.rank_position,
                ),
            )
            if self.audit_gate is not None
            else acceptable
        )
        frozen = [candidate.model_copy(update={"rank_position": index}) for index, candidate in enumerate(visible, 1)]
        if self.audit_gate is not None:
            # The gate receipt binds every frozen candidate field, including
            # its final displayed rank. Re-evaluate after authoritative
            # partitioning so accept-time verification sees the exact bytes
            # persisted in the SuggestionSet.
            frozen = list(await asyncio.gather(*(
                self.audit_gate.evaluate_candidate(
                    workspace=workspace,
                    base=revision,
                    candidate=candidate,
                    day_index=request.day_index,
                    insert_after_stop_id=after,
                    insert_before_stop_id=before,
                )
                for candidate in frozen
            )))
        shortages = list(result.shortage_reason_codes)
        if len(frozen) < 4:
            shortages.append("VISIBLE_RESULTS_BELOW_MINIMUM")
        if self.audit_gate is not None and len(acceptable) < min(3, len(frozen)):
            shortages.append("AUDIT_SATISFIED_OPTIONS_BELOW_TOP3")
        result_status = "PARTIAL" if shortages or len(frozen) < 4 else "COMPLETE"
        return SuggestionSetCreateInput(
            workspace_id=workspace_id,
            base_revision=request.base_revision,
            day_index=request.day_index,
            insert_after_stop_id=after,
            insert_before_stop_id=before,
            intents=list(request.intents),
            context_hash=result.context_hash,
            policy_version=result.policy_version,
            provider_snapshot_id=result.provider_snapshot_id,
            expires_at=now + self.expires_in,
            candidates=frozen,
            session_id=request.session_id,
            created_by=actor_user_id,
            result_status=result_status,
            shortage_reason_codes=list(dict.fromkeys(shortages)),
            excluded_counts=result.excluded_counts,
        )


def get_suggestion_repository() -> SuggestionRepository:
    return PostgresSuggestionRepository()


def get_itinerary_repository() -> ItineraryRepository:
    return PostgresItineraryRepository()


def get_ranked_suggestion_provider(
    itinerary_repository: Annotated[ItineraryRepository, Depends(get_itinerary_repository)],
) -> RankedSuggestionProvider:
    return DefaultRankedSuggestionProvider(
        itinerary_repository,
        audit_gate=SuggestionAuditGate(
            PostgresAuditRepository(),
            PostgresMemberConstraintRepository(),
            clock=_suggestion_now,
        ),
        clock=_suggestion_now,
    )


SuggestionRepositoryDep = Annotated[SuggestionRepository, Depends(get_suggestion_repository)]
ItineraryRepositoryDep = Annotated[ItineraryRepository, Depends(get_itinerary_repository)]
RankedProviderDep = Annotated[RankedSuggestionProvider, Depends(get_ranked_suggestion_provider)]
CurrentUserDep = Annotated[str, Depends(get_current_user)]


def _error(exc: ItineraryDomainError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail())


def _parse_if_match(raw: str | None) -> int:
    if raw is None or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "IF_MATCH_REQUIRED", "message": "If-Match header is required"},
        )
    candidate = raw.strip()
    match = re.fullmatch(r'"([1-9][0-9]*)"', candidate)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "INVALID_IF_MATCH",
                "message": "If-Match must be one strong quoted positive revision tag",
            },
        )
    return int(match.group(1))


async def _workspace_access(workspace_id: str, user_id: str, repository: ItineraryRepository):
    workspace = await repository.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "workspace does not exist"},
        )
    await require_room_member(workspace.room_id, user_id)
    return workspace


@router.post(
    "/trip-workspaces/{workspace_id}/suggestion-sets",
    response_model=SuggestionSet,
    status_code=status.HTTP_201_CREATED,
)
async def create_suggestion_set(
    workspace_id: str,
    body: CreateSuggestionSetRequest,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
    ranked_provider: RankedProviderDep,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        ranked = await ranked_provider.rank(
            workspace_id=workspace_id,
            request=body,
            actor_user_id=current_user,
        )
        expected = {
            "workspace_id": workspace_id,
            "base_revision": body.base_revision,
            "day_index": body.day_index,
            "insert_after_stop_id": body.insert_after_stop_id,
            "insert_before_stop_id": body.insert_before_stop_id,
            "intents": body.intents,
            "session_id": body.session_id,
            "created_by": current_user,
        }
        mismatches = [key for key, value in expected.items() if getattr(ranked, key) != value]
        if mismatches:
            raise SuggestionProviderUnavailableError(
                "ranked suggestion batch does not match the requested context",
                context={"mismatched_fields": mismatches},
            )
        return await _suggestion_service(repository, itinerary_repository).create_from_ranked(ranked)
    except SuggestionProviderUnavailableError as exc:
        reason_code = str(exc.context.get("reason_code") or exc.code)
        await _suggestion_service(repository, itinerary_repository).record_suggestion_failed(
            workspace_id=workspace_id,
            session_id=body.session_id,
            actor_user_id=current_user,
            revision_before=body.base_revision,
            reason_code=reason_code,
            request_context={
                "base_revision": body.base_revision,
                "day_index": body.day_index,
                "insert_after_stop_id": body.insert_after_stop_id,
                "insert_before_stop_id": body.insert_before_stop_id,
                "intents": [intent.value for intent in body.intents],
                "provider_status": exc.context.get("provider_status"),
            },
        )
        raise _error(exc) from exc
    except ItineraryDomainError as exc:
        raise _error(exc) from exc


@router.get(
    "/trip-workspaces/{workspace_id}/suggestion-sets/{suggestion_set_id}",
    response_model=SuggestionSet,
)
async def get_suggestion_set(
    workspace_id: str,
    suggestion_set_id: str,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    result = await repository.get_set(workspace_id, suggestion_set_id)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": "suggestion set does not exist"},
        )
    return result


@router.get(
    "/trip-workspaces/{workspace_id}/recommendation-events",
    response_model=list[RecommendationEvent],
)
async def list_recommendation_events(
    workspace_id: str,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    return await repository.list_events(workspace_id)


def _mark_replay_header(response: Response, result: RecommendationEventCommandResult) -> None:
    if result.idempotent_replay:
        response.headers["Idempotency-Replayed"] = "true"


@router.post(
    "/trip-workspaces/{workspace_id}/suggestion-sets/{suggestion_set_id}/candidates/{candidate_id}:preview",
    response_model=RecommendationEventCommandResult,
)
async def preview_suggestion_candidate(
    workspace_id: str,
    suggestion_set_id: str,
    candidate_id: str,
    response: Response,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result = await _suggestion_service(repository, itinerary_repository).record_candidate_previewed(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        raise _error(exc) from exc
    _mark_replay_header(response, result)
    return result


@router.post(
    "/trip-workspaces/{workspace_id}/suggestion-sets/{suggestion_set_id}/candidates/{candidate_id}:dismiss",
    response_model=RecommendationEventCommandResult,
)
async def dismiss_suggestion_candidate(
    workspace_id: str,
    suggestion_set_id: str,
    candidate_id: str,
    body: DismissCandidateRequest,
    response: Response,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result = await _suggestion_service(repository, itinerary_repository).record_candidate_dismissed(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
            reason_code=body.reason_code,
        )
    except ItineraryDomainError as exc:
        raise _error(exc) from exc
    _mark_replay_header(response, result)
    return result


@router.post(
    "/trip-workspaces/{workspace_id}/suggestion-sets/{suggestion_set_id}:line-completed",
    response_model=RecommendationEventCommandResult,
)
async def complete_suggestion_line(
    workspace_id: str,
    suggestion_set_id: str,
    response: Response,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result = await _suggestion_service(repository, itinerary_repository).record_line_completed(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            actor_user_id=current_user,
            idempotency_key=require_idempotency_key(idempotency_key),
        )
    except ItineraryDomainError as exc:
        raise _error(exc) from exc
    _mark_replay_header(response, result)
    return result


@router.post(
    "/trip-workspaces/{workspace_id}/suggestion-sets/{suggestion_set_id}/candidates/{candidate_id}:accept",
    response_model=AcceptSuggestionResult,
)
async def accept_suggestion_candidate(
    workspace_id: str,
    suggestion_set_id: str,
    candidate_id: str,
    response: Response,
    current_user: CurrentUserDep,
    repository: SuggestionRepositoryDep,
    itinerary_repository: ItineraryRepositoryDep,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    await _workspace_access(workspace_id, current_user, itinerary_repository)
    try:
        result = await _suggestion_service(repository, itinerary_repository).accept(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            if_match_revision=_parse_if_match(if_match),
            idempotency_key=require_idempotency_key(idempotency_key),
            actor_user_id=current_user,
        )
    except ItineraryDomainError as exc:
        raise _error(exc) from exc
    response.headers["ETag"] = f'"{result.new_revision}"'
    if result.idempotent_replay:
        response.headers["Idempotency-Replayed"] = "true"
    return result
