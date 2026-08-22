from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.importing.models import ResolvedPlaceReceipt
from app.itineraries.errors import (
    IdempotencyKeyReusedError,
    ResourceNotFound,
    RevisionConflictError,
)
from app.itineraries.hash_service import with_content_hash
from app.itineraries.command_service import RevisionCommandService
from app.itineraries.map_projection import build_map_projection
from app.itineraries.models import (
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryRevisionContent,
    ItineraryStop,
    EditOperation,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.schemas.place import Coordinates, RetrievalExecutionMode
from app.suggestions.errors import (
    SuggestionCandidateBlockedError,
    SuggestionCandidateEvidenceUnavailableError,
    SuggestionSetExpiredError,
)
from app.suggestions.models import (
    EvidenceFreshness,
    FreshnessStatus,
    FrozenCanonicalPlace,
    HardGate,
    RecommendationEventType,
    RouteDelta,
    RouteReceipt,
    RouteReceiptLeg,
    SuggestionCandidateDraft,
    SuggestionClassification,
    SuggestionIntent,
    SuggestionSetCreateInput,
)
from app.suggestions.repositories import InMemorySuggestionRepository
from app.suggestions.service import AtomicSuggestionUndoService, SuggestionSetService


NOW = datetime.now(timezone.utc)


def _route_receipts(candidate_id: str, candidate_coords: Coordinates) -> tuple[RouteReceipt, ...]:
    previous = Coordinates(lng=116.391, lat=39.916)
    next_coords = Coordinates(lng=116.401, lat=39.921)

    def receipt(
        leg: RouteReceiptLeg,
        origin_id: str,
        origin: Coordinates,
        destination_id: str,
        destination: Coordinates,
        duration: int,
        char: str,
    ) -> RouteReceipt:
        return RouteReceipt(
            leg=leg,
            transport_mode="walking",
            origin_place_id=origin_id,
            origin_coords=origin,
            destination_place_id=destination_id,
            destination_coords=destination,
            duration_minutes=duration,
            provider="controlled_route_snapshot",
            request_hash=char * 64,
            response_hash=chr(ord(char) + 3) * 64,
            observed_at=NOW,
            snapshot_id=f"controlled-route-{char * 16}",
            execution_mode=RetrievalExecutionMode.FIXTURE,
            max_age_seconds=3600,
            source_url=f"fixture://route/{char}",
        )

    return (
        receipt(
            RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
            "poi-a",
            previous,
            candidate_id,
            candidate_coords,
            8,
            "1",
        ),
        receipt(
            RouteReceiptLeg.CANDIDATE_TO_NEXT,
            candidate_id,
            candidate_coords,
            "poi-b",
            next_coords,
            6,
            "2",
        ),
        receipt(
            RouteReceiptLeg.PREVIOUS_TO_NEXT,
            "poi-a",
            previous,
            "poi-b",
            next_coords,
            18,
            "3",
        ),
    )


async def _seed() -> tuple[InMemoryItineraryRepository, InMemorySuggestionRepository, SuggestionSetService]:
    dates = TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2))
    revision = with_content_hash(ItineraryRevisionContent(
        itinerary_id="itin-suggest",
        workspace_id="workspace-suggest",
        revision=1,
        source_type=RevisionSource.MANUAL,
        city="北京",
        date_range=dates,
        days=[
            ItineraryDay(day_index=0, date=dates.start, stops=[
                ItineraryStop(
                    stop_id="anchor-a", place_id="poi-a", day_index=0, order_index=0,
                    category="attraction",
                ),
                ItineraryStop(
                    stop_id="anchor-b", place_id="poi-b", day_index=0, order_index=1,
                    category="attraction",
                ),
            ]),
            ItineraryDay(day_index=1, date=dates.end, stops=[]),
        ],
        change_summary={"map_stop_projections": {}},
        created_by="user-suggest",
    ))
    workspace = TripWorkspace(
        workspace_id="workspace-suggest",
        room_id="room-suggest",
        city="北京",
        trip_date_range=dates,
        current_itinerary_revision=1,
        created_by="user-suggest",
    )
    itineraries = InMemoryItineraryRepository()
    await itineraries.create_workspace(workspace, revision)
    repository = InMemorySuggestionRepository(itineraries)
    return itineraries, repository, SuggestionSetService(repository, itineraries)


def _candidate(
    candidate_id: str = "candidate-c",
    *,
    place_id: str = "poi-c",
    hard_passed: bool = True,
    freshness: FreshnessStatus = FreshnessStatus.FRESH,
    route_status: str = "AVAILABLE",
) -> SuggestionCandidateDraft:
    coords = Coordinates(lng=116.397, lat=39.918)
    receipt = ResolvedPlaceReceipt(
        canonical_place_id=place_id,
        provider="amap",
        provider_place_id=f"amap-{place_id}",
        name="景山公园",
        city="北京",
        district="东城区",
        address="景山西街44号",
        category="attraction",
        longitude=coords.lng,
        latitude=coords.lat,
        request_hash="a" * 64,
        response_hash="b" * 64,
        observed_at=NOW,
        execution_mode=RetrievalExecutionMode.FIXTURE,
        source_url="https://example.test/amap/poi-c",
    )
    return SuggestionCandidateDraft(
        candidate_id=candidate_id,
        canonical_place=FrozenCanonicalPlace(
            place_id=place_id,
            name=receipt.name,
            city=receipt.city,
            district=receipt.district,
            address=receipt.address,
            category="attraction",
            coords=coords,
        ),
        provider_receipt=receipt,
        provider_receipt_id=f"receipt-{candidate_id}",
        rank_position=1,
        classification=(
            SuggestionClassification.ON_ROUTE
            if hard_passed
            else SuggestionClassification.INFEASIBLE
        ),
        source_prior_refs=["official-route:beijing-classic:v1", "ugc-snapshot:note-12"],
        score_components={"route": 0.9, "official_prior": 0.4, "content": 0.7},
        total_score=0.81,
        hard_gate=HardGate(
            passed=hard_passed,
            reason_codes=[] if hard_passed else ["MEMBER_HARD_CONSTRAINT"],
        ),
        route_delta=RouteDelta(
            status=route_status,
            delta_route_minutes=-4 if route_status == "AVAILABLE" else None,
            previous_to_candidate_minutes=8 if route_status == "AVAILABLE" else None,
            candidate_to_next_minutes=6 if route_status == "AVAILABLE" else None,
            previous_to_next_minutes=18 if route_status == "AVAILABLE" else None,
            route_receipts=(
                _route_receipts(place_id, coords) if route_status == "AVAILABLE" else ()
            ),
            reason_code=None if route_status == "AVAILABLE" else "ROUTE_PROVIDER_TIMEOUT",
        ),
        evidence_freshness=EvidenceFreshness(
            status=freshness,
            observed_at=NOW,
            max_age_seconds=3600,
            reason_code=None if freshness is FreshnessStatus.FRESH else "POI_FACT_STALE",
        ),
        explanation_codes=["NEARBY", "OFFICIAL_ROUTE_PRIOR", "LOWER_ROUTE_COST"],
    )


def _input(candidate: SuggestionCandidateDraft | None = None) -> SuggestionSetCreateInput:
    return SuggestionSetCreateInput(
        suggestion_set_id="set-1",
        workspace_id="workspace-suggest",
        base_revision=1,
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
        intents=[SuggestionIntent.NEARBY, SuggestionIntent.FUN],
        context_hash="c" * 64,
        policy_version="ranker-v1.0.0",
        provider_snapshot_id="amap-fixture-snapshot-20260821-a1",
        expires_at=NOW + timedelta(hours=1),
        candidates=[candidate or _candidate()],
        session_id="session-suggest",
        created_by="user-suggest",
    )


@pytest.mark.asyncio
async def test_create_freezes_ranked_candidates_and_writes_exposure_event():
    _, repository, service = await _seed()
    result = await service.create_from_ranked(_input())

    assert result.base_revision == 1
    assert result.candidates[0].route_delta.delta_route_minutes == -4
    assert result.candidates[0].provider_receipt.request_hash == "a" * 64
    assert result.candidates[0].source_prior_refs == [
        "official-route:beijing-classic:v1",
        "ugc-snapshot:note-12",
    ]
    events = await repository.list_events("workspace-suggest")
    assert [event.event_type for event in events] == [RecommendationEventType.SUGGESTIONS_SHOWN]
    assert events[0].payload["candidate_ids"] == ["candidate-c"]
    assert events[0].payload["rank_positions"] == {"candidate-c": 1}
    assert events[0].payload["source_prior_refs"] == {
        "candidate-c": [
            "official-route:beijing-classic:v1",
            "ugc-snapshot:note-12",
        ]
    }


@pytest.mark.asyncio
async def test_accept_is_atomic_and_new_place_is_a_canonical_next_anchor():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())

    accepted = await service.accept(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        if_match_revision=1,
        idempotency_key="accept-1",
        actor_user_id="user-suggest",
    )

    assert accepted.new_revision == 2
    assert [stop.place_id for stop in accepted.revision.days[0].stops] == ["poi-a", "poi-c", "poi-b"]
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 2
    record = repository.place_records["workspace-suggest"]["poi-c"]
    assert record["coords"] == {"lng": 116.397, "lat": 39.918}
    assert record["resolved_place_receipt"]["response_hash"] == "b" * 64
    assert repository.receipts[("workspace-suggest", 2, accepted.stop_id)]["provider_place_id"] == "amap-poi-c"
    frozen_routes = accepted.revision.change_summary["route_delta"]["route_receipts"]
    assert [item["leg"] for item in frozen_routes] == [
        "PREVIOUS_TO_CANDIDATE",
        "CANDIDATE_TO_NEXT",
        "PREVIOUS_TO_NEXT",
    ]
    assert all(item["request_hash"] and item["response_hash"] for item in frozen_routes)
    projection = build_map_projection(accepted.revision, lineage=[accepted.revision])
    assert projection.stops[0].place_id == "poi-c"
    assert projection.stops[0].coords == Coordinates(lng=116.397, lat=39.918)
    assert [event.event_type for event in await repository.list_events("workspace-suggest")] == [
        RecommendationEventType.SUGGESTIONS_SHOWN,
        RecommendationEventType.CANDIDATE_ACCEPTED,
    ]
    accepted_event = (await repository.list_events("workspace-suggest"))[1]
    assert accepted_event.payload["source_prior_refs"] == [
        "official-route:beijing-classic:v1",
        "ugc-snapshot:note-12",
    ]


@pytest.mark.asyncio
async def test_accept_replays_same_command_without_duplicate_revision_or_event():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())
    kwargs = dict(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        if_match_revision=1,
        idempotency_key="accept-replay",
        actor_user_id="user-suggest",
    )
    first = await service.accept(**kwargs)
    replay = await service.accept(**kwargs)
    assert first.new_revision == replay.new_revision == 2
    assert replay.idempotent_replay is True
    assert len(await itineraries.list_revisions("workspace-suggest")) == 2
    assert len(await repository.list_events("workspace-suggest")) == 2

    with pytest.raises(IdempotencyKeyReusedError):
        await service.accept(**{**kwargs, "actor_user_id": "other-user"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "error_type"),
    [
        (_candidate(hard_passed=False), SuggestionCandidateBlockedError),
        (_candidate(freshness=FreshnessStatus.STALE), SuggestionCandidateEvidenceUnavailableError),
        (_candidate(route_status="UNAVAILABLE"), SuggestionCandidateEvidenceUnavailableError),
    ],
)
async def test_accept_fails_closed_without_revision_for_blocked_or_unknown_facts(candidate, error_type):
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input(candidate))
    with pytest.raises(error_type):
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key="blocked",
            actor_user_id="user-suggest",
        )
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 1
    assert len(await itineraries.list_revisions("workspace-suggest")) == 1
    assert repository.place_records == {}


@pytest.mark.asyncio
async def test_accept_rechecks_canonical_duplicate_at_the_mutation_boundary():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input(_candidate(place_id="poi-a")))

    with pytest.raises(SuggestionCandidateBlockedError) as exc_info:
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key="duplicate-place",
            actor_user_id="user-suggest",
        )

    assert exc_info.value.context["reason_code"] == "DUPLICATE_CANONICAL_PLACE"
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 1
    assert len(await itineraries.list_revisions("workspace-suggest")) == 1
    assert repository.place_records == {}


@pytest.mark.asyncio
async def test_expired_set_is_rejected_without_materialization():
    itineraries, repository, _ = await _seed()
    clock = [NOW]
    service = SuggestionSetService(repository, itineraries, clock=lambda: clock[0])
    await service.create_from_ranked(
        _input().model_copy(update={"expires_at": NOW + timedelta(seconds=1)})
    )
    clock[0] = NOW + timedelta(seconds=2)
    with pytest.raises(SuggestionSetExpiredError):
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key="expired",
            actor_user_id="user-suggest",
        )
    assert len(await itineraries.list_revisions("workspace-suggest")) == 1


@pytest.mark.asyncio
async def test_controlled_failure_rolls_back_revision_place_receipt_pointer_event_and_command():
    itineraries, repository, _ = await _seed()
    repository.fail_at = "after_workspace_pointer"
    service = SuggestionSetService(repository, itineraries)
    await service.create_from_ranked(_input())
    with pytest.raises(RuntimeError, match="after_workspace_pointer"):
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key="rollback",
            actor_user_id="user-suggest",
        )
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 1
    assert len(await itineraries.list_revisions("workspace-suggest")) == 1
    assert repository.place_records["workspace-suggest"] == {}
    assert repository.receipts == {}
    assert repository.commands == {}
    assert [event.event_type for event in repository.events] == [RecommendationEventType.SUGGESTIONS_SHOWN]


@pytest.mark.asyncio
async def test_concurrent_accept_has_one_commit_and_persists_revision_conflict_event():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())

    async def accept(key: str):
        return await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key=key,
            actor_user_id="user-suggest",
        )

    outcomes = await asyncio.gather(accept("race-a"), accept("race-b"), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
    assert len(await itineraries.list_revisions("workspace-suggest")) == 2
    assert [event.event_type for event in repository.events].count(
        RecommendationEventType.REVISION_CONFLICT
    ) == 1


@pytest.mark.asyncio
async def test_suggestion_accept_and_ordinary_edit_share_one_workspace_mutation_lock():
    itineraries, repository, _ = await _seed()
    suggestion_service = SuggestionSetService(repository, itineraries)
    await suggestion_service.create_from_ranked(_input())
    command_service = RevisionCommandService(itineraries)
    edit = ItineraryEditCommand(
        command_id="lock-anchor-race",
        workspace_id="workspace-suggest",
        base_revision=1,
        actor_user_id="user-suggest",
        operation=EditOperation.LOCK_STOP,
        payload={"stop_id": "anchor-a"},
    )

    accept = suggestion_service.accept(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        if_match_revision=1,
        idempotency_key="suggestion-vs-edit",
        actor_user_id="user-suggest",
    )
    ordinary_edit = command_service.apply(
        edit,
        if_match_revision=1,
        idempotency_key="ordinary-vs-suggestion",
    )
    outcomes = await asyncio.gather(accept, ordinary_edit, return_exceptions=True)

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 2
    assert len(await itineraries.list_revisions("workspace-suggest")) == 2


@pytest.mark.asyncio
async def test_accept_recomputes_poi_and_route_freshness_with_injected_clock():
    itineraries, repository, _ = await _seed()
    clock = [NOW]
    service = SuggestionSetService(repository, itineraries, clock=lambda: clock[0])
    create_input = _input().model_copy(update={"expires_at": NOW + timedelta(hours=3)})
    await service.create_from_ranked(create_input)
    clock[0] = NOW + timedelta(seconds=3601)

    with pytest.raises(SuggestionCandidateEvidenceUnavailableError) as exc_info:
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key="poi-expired-at-accept",
            actor_user_id="user-suggest",
        )

    assert exc_info.value.context["reason"] == "POI_RECEIPT_STALE_AT_ACCEPT"
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("clock_offset", "route_max_age", "expected_reason"),
    [
        (-1, 3600, "POI_RECEIPT_OBSERVED_IN_FUTURE"),
        (61, 60, "ROUTE_RECEIPT_STALE_AT_ACCEPT"),
    ],
)
async def test_accept_fails_closed_on_clock_anomaly_or_expired_route_receipt(
    clock_offset: int,
    route_max_age: int,
    expected_reason: str,
):
    itineraries, repository, _ = await _seed()
    candidate = _candidate()
    route_receipts = tuple(
        receipt.model_copy(update={"max_age_seconds": route_max_age})
        for receipt in candidate.route_delta.route_receipts
    )
    candidate = candidate.model_copy(update={
        "route_delta": candidate.route_delta.model_copy(update={"route_receipts": route_receipts}),
    })
    clock = [NOW]
    service = SuggestionSetService(repository, itineraries, clock=lambda: clock[0])
    await service.create_from_ranked(
        _input(candidate).model_copy(update={"expires_at": NOW + timedelta(hours=3)})
    )
    clock[0] = NOW + timedelta(seconds=clock_offset)

    with pytest.raises(SuggestionCandidateEvidenceUnavailableError) as exc_info:
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key=f"clock-route-{clock_offset}",
            actor_user_id="user-suggest",
        )
    assert exc_info.value.context["reason"] == expected_reason


@pytest.mark.asyncio
async def test_accept_rejects_route_receipts_for_another_frozen_edge():
    itineraries, repository, _ = await _seed()
    candidate = _candidate()
    receipts = tuple(
        receipt.model_copy(update={"origin_place_id": "foreign-anchor"})
        if receipt.leg in {
            RouteReceiptLeg.PREVIOUS_TO_CANDIDATE,
            RouteReceiptLeg.PREVIOUS_TO_NEXT,
        }
        else receipt
        for receipt in candidate.route_delta.route_receipts
    )
    candidate = candidate.model_copy(update={
        "route_delta": candidate.route_delta.model_copy(update={"route_receipts": receipts}),
    })
    service = SuggestionSetService(repository, itineraries)
    await service.create_from_ranked(_input(candidate))

    with pytest.raises(SuggestionCandidateEvidenceUnavailableError) as exc_info:
        await service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            if_match_revision=1,
            idempotency_key="foreign-route-edge",
            actor_user_id="user-suggest",
        )
    assert exc_info.value.context["reason"] == "ROUTE_RECEIPT_ENDPOINT_MISMATCH"


def test_route_delta_requires_complete_receipts_and_exact_arithmetic():
    receipts = _route_receipts("poi-c", Coordinates(lng=116.397, lat=39.918))
    with pytest.raises(ValueError, match="arithmetic"):
        RouteDelta(
            status="AVAILABLE",
            delta_route_minutes=-3,
            previous_to_candidate_minutes=8,
            candidate_to_next_minutes=6,
            previous_to_next_minutes=18,
            route_receipts=receipts,
        )
    with pytest.raises(ValueError, match="exactly one receipt"):
        RouteDelta(
            status="AVAILABLE",
            delta_route_minutes=-4,
            previous_to_candidate_minutes=8,
            candidate_to_next_minutes=6,
            previous_to_next_minutes=18,
            route_receipts=receipts[:2],
        )


@pytest.mark.asyncio
async def test_create_uses_injected_clock_and_repository_defensively_copies_frozen_set():
    itineraries, repository, _ = await _seed()
    service = SuggestionSetService(repository, itineraries, clock=lambda: NOW)
    expired = _input().model_copy(update={"expires_at": NOW})
    with pytest.raises(SuggestionSetExpiredError):
        await service.create_from_ranked(expired)

    create_input = _input()
    created = await service.create_from_ranked(create_input)
    create_input.candidates[0].score_components["route"] = -999
    created.candidates[0].score_components["route"] = -888
    created.candidates.clear()
    readback = await repository.get_set("workspace-suggest", "set-1")
    assert readback is not None
    assert len(readback.candidates) == 1
    assert readback.candidates[0].score_components["route"] == 0.9


def test_set_rejects_duplicate_canonical_or_receipt_identity_and_address_mismatch():
    first = _candidate(candidate_id="candidate-one", place_id="poi-one")
    duplicate_place = _candidate(candidate_id="candidate-two", place_id="poi-one").model_copy(
        update={"rank_position": 2, "provider_receipt_id": "receipt-two"}
    )
    with pytest.raises(ValueError, match="canonical place"):
        SuggestionSetCreateInput(**{
            **_input(first).model_dump(exclude={"candidates"}),
            "candidates": [first, duplicate_place],
        })

    duplicate_receipt_id = _candidate(candidate_id="candidate-two", place_id="poi-two").model_copy(
        update={"rank_position": 2, "provider_receipt_id": first.provider_receipt_id}
    )
    with pytest.raises(ValueError, match="provider receipt id"):
        SuggestionSetCreateInput(**{
            **_input(first).model_dump(exclude={"candidates"}),
            "candidates": [first, duplicate_receipt_id],
        })

    with pytest.raises(ValueError, match="canonical address"):
        SuggestionCandidateDraft(**{
            **first.model_dump(exclude={"canonical_place"}),
            "canonical_place": first.canonical_place.model_copy(update={"address": None}),
        })
@pytest.mark.asyncio
async def test_interaction_commands_derive_frozen_context_and_replay_exactly_once():
    itineraries, repository, service = await _seed()
    frozen = await service.create_from_ranked(_input())
    before = datetime.now(timezone.utc)

    preview = await service.record_candidate_previewed(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        actor_user_id="user-suggest",
        idempotency_key="preview-1",
    )
    replay = await service.record_candidate_previewed(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        actor_user_id="user-suggest",
        idempotency_key="preview-1",
    )

    assert preview.event.occurred_at >= before
    assert replay.idempotent_replay is True
    assert replay.event == preview.event
    assert preview.event.session_id == frozen.session_id
    assert preview.event.context_hash == frozen.context_hash
    assert preview.event.policy_version == frozen.policy_version
    assert preview.event.provider_snapshot_id == frozen.provider_snapshot_id
    assert preview.event.rank_position == frozen.candidates[0].rank_position
    assert preview.event.revision_before == (
        await itineraries.get_workspace("workspace-suggest")
    ).current_itinerary_revision
    assert [event.event_type for event in repository.events] == [
        RecommendationEventType.SUGGESTIONS_SHOWN,
        RecommendationEventType.CANDIDATE_PREVIEWED,
    ]

    with pytest.raises(IdempotencyKeyReusedError):
        await service.record_candidate_dismissed(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            actor_user_id="user-suggest",
            idempotency_key="preview-1",
            reason_code="TOO_FAR",
        )


@pytest.mark.asyncio
async def test_dismiss_and_line_completed_have_type_specific_scope():
    _, repository, service = await _seed()
    await service.create_from_ranked(_input())

    dismissed = await service.record_candidate_dismissed(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        actor_user_id="user-suggest",
        idempotency_key="dismiss-1",
        reason_code="NOT_INTERESTED",
    )
    completed = await service.record_line_completed(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        actor_user_id="user-suggest",
        idempotency_key="line-1",
    )

    assert dismissed.event.reason_code == "NOT_INTERESTED"
    assert dismissed.event.candidate_id == "candidate-c"
    assert dismissed.event.rank_position == 1
    assert completed.event.candidate_id is None
    assert completed.event.rank_position is None
    assert completed.event.suggestion_set_id == "set-1"
    assert [item.event_type for item in repository.events[-2:]] == [
        RecommendationEventType.CANDIDATE_DISMISSED,
        RecommendationEventType.LINE_COMPLETED,
    ]


@pytest.mark.asyncio
async def test_interaction_scope_and_transaction_failure_are_fail_closed():
    _, repository, service = await _seed()
    await service.create_from_ranked(_input())

    with pytest.raises(ResourceNotFound):
        await service.record_candidate_previewed(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="foreign-candidate",
            actor_user_id="user-suggest",
            idempotency_key="foreign-candidate",
        )

    repository.fail_at = "after_interaction_event"
    with pytest.raises(RuntimeError, match="after_interaction_event"):
        await service.record_candidate_previewed(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-1",
            candidate_id="candidate-c",
            actor_user_id="user-suggest",
            idempotency_key="rollback-preview",
        )
    assert ("workspace-suggest", "rollback-preview") not in repository.event_commands
    assert all(
        event.event_type is not RecommendationEventType.CANDIDATE_PREVIEWED
        for event in repository.events
    )


@pytest.mark.asyncio
async def test_legacy_stop_undone_service_hook_remains_retry_safe():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())
    workspace = await itineraries.get_workspace("workspace-suggest")
    itineraries.workspaces["workspace-suggest"] = workspace.model_copy(
        update={"current_itinerary_revision": 3}
    )

    result = await service.record_stop_undone(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        actor_user_id="user-suggest",
        idempotency_key="undo-event-1",
        revision_before=2,
        revision_after=3,
    )
    replay = await service.record_stop_undone(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        actor_user_id="user-suggest",
        idempotency_key="undo-event-1",
        revision_before=2,
        revision_after=3,
    )

    assert result.event.event_type is RecommendationEventType.STOP_UNDONE
    assert result.event.revision_before == 2
    assert result.event.revision_after == 3
    assert replay.idempotent_replay is True
    assert sum(
        event.event_type is RecommendationEventType.STOP_UNDONE for event in repository.events
    ) == 1


def _undo_command(*, command_id: str = "undo-suggestion-1", actor: str = "user-suggest"):
    return ItineraryEditCommand(
        command_id=command_id,
        workspace_id="workspace-suggest",
        base_revision=2,
        actor_user_id=actor,
        operation=EditOperation.UNDO,
        payload={"target_revision": 1},
    )


@pytest.mark.asyncio
async def test_atomic_suggestion_undo_freezes_acceptance_lineage_and_exactly_replays():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())
    accepted = await service.accept(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        if_match_revision=1,
        idempotency_key="accept-before-undo",
        actor_user_id="user-suggest",
    )
    accepted_event = accepted.event
    atomic = AtomicSuggestionUndoService(repository)
    command = _undo_command()

    result = await atomic.apply_if_accepted_suggestion(
        command,
        if_match_revision=2,
        idempotency_key="undo-idempotency-1",
    )
    replay = await atomic.apply_if_accepted_suggestion(
        command,
        if_match_revision=2,
        idempotency_key="undo-idempotency-1",
    )

    assert result is not None and result.new_revision == 3
    assert replay is not None and replay.new_revision == 3 and replay.idempotent_replay is True
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 3
    restored = await itineraries.get_revision("workspace-suggest", 3)
    assert [stop.place_id for stop in restored.days[0].stops] == ["poi-a", "poi-b"]
    events = await repository.list_events("workspace-suggest")
    undone = events[-1]
    assert undone.event_type is RecommendationEventType.STOP_UNDONE
    assert undone.revision_before == 2 and undone.revision_after == 3
    assert undone.suggestion_set_id == accepted_event.suggestion_set_id
    assert undone.candidate_id == accepted_event.candidate_id
    assert undone.context_hash == accepted_event.context_hash
    assert undone.policy_version == accepted_event.policy_version
    assert undone.provider_snapshot_id == accepted_event.provider_snapshot_id
    assert undone.rank_position == accepted_event.rank_position
    assert undone.payload == {
        "source_accept_event_id": accepted_event.event_id,
        "source_accept_revision": 2,
        "target_revision": 1,
        "stop_id": accepted.stop_id,
        "canonical_place_id": "poi-c",
    }
    assert repository.undo_links[command.command_id]["source_accept_event_id"] == accepted_event.event_id
    assert len(await itineraries.list_revisions("workspace-suggest")) == 3
    assert sum(event.event_type is RecommendationEventType.STOP_UNDONE for event in events) == 1

    with pytest.raises(IdempotencyKeyReusedError):
        await atomic.apply_if_accepted_suggestion(
            _undo_command(command_id="different-command", actor="another-user"),
            if_match_revision=2,
            idempotency_key="undo-idempotency-1",
        )
    with pytest.raises(RevisionConflictError):
        await atomic.apply_if_accepted_suggestion(
            _undo_command(command_id="duplicate-undo"),
            if_match_revision=2,
            idempotency_key="duplicate-undo-new-key",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fail_at",
    [
        "after_undo_revision",
        "after_undo_workspace_pointer",
        "after_undo_command",
        "after_undo_event",
        "after_undo_link",
    ],
)
async def test_atomic_suggestion_undo_rolls_back_every_write_stage(fail_at):
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())
    await service.accept(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        if_match_revision=1,
        idempotency_key="accept-before-rollback",
        actor_user_id="user-suggest",
    )
    repository.fail_at = fail_at

    with pytest.raises(RuntimeError, match="controlled suggestion repository failure"):
        await AtomicSuggestionUndoService(repository).apply_if_accepted_suggestion(
            _undo_command(command_id=f"undo-{fail_at}"),
            if_match_revision=2,
            idempotency_key=f"undo-key-{fail_at}",
        )

    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 2
    assert await itineraries.get_revision("workspace-suggest", 3) is None
    assert ("workspace-suggest", f"undo-key-{fail_at}") not in itineraries.commands
    assert f"undo-{fail_at}" not in repository.undo_links
    events = await repository.list_events("workspace-suggest")
    assert sum(event.event_type is RecommendationEventType.STOP_UNDONE for event in events) == 0


@pytest.mark.asyncio
async def test_concurrent_accept_and_suggestion_undo_serialize_on_one_workspace_lock():
    itineraries, repository, service = await _seed()
    await service.create_from_ranked(_input())
    first = await service.accept(
        workspace_id="workspace-suggest",
        suggestion_set_id="set-1",
        candidate_id="candidate-c",
        if_match_revision=1,
        idempotency_key="accept-first",
        actor_user_id="user-suggest",
    )
    second_candidate = _candidate(candidate_id="candidate-d", place_id="poi-d")
    second_routes = list(second_candidate.route_delta.route_receipts)
    for index in (1, 2):
        second_routes[index] = second_routes[index].model_copy(update={
            "destination_place_id": "poi-c",
            "destination_coords": Coordinates(lng=116.397, lat=39.918),
        })
    second_candidate = second_candidate.model_copy(update={
        "route_delta": second_candidate.route_delta.model_copy(update={
            "route_receipts": tuple(second_routes),
        }),
    })
    await service.create_from_ranked(_input(second_candidate).model_copy(update={
        "suggestion_set_id": "set-2",
        "base_revision": 2,
        "insert_before_stop_id": first.stop_id,
    }))

    outcomes = await asyncio.gather(
        service.accept(
            workspace_id="workspace-suggest",
            suggestion_set_id="set-2",
            candidate_id="candidate-d",
            if_match_revision=2,
            idempotency_key="concurrent-accept",
            actor_user_id="user-suggest",
        ),
        AtomicSuggestionUndoService(repository).apply_if_accepted_suggestion(
            _undo_command(command_id="concurrent-undo"),
            if_match_revision=2,
            idempotency_key="concurrent-undo",
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(item, RevisionConflictError) for item in outcomes) == 1
    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert (await itineraries.get_workspace("workspace-suggest")).current_itinerary_revision == 3
    terminal_events = [
        event
        for event in await repository.list_events("workspace-suggest")
        if event.revision_after == 3
        and event.event_type in {
            RecommendationEventType.CANDIDATE_ACCEPTED,
            RecommendationEventType.STOP_UNDONE,
        }
    ]
    assert len(terminal_events) == 1


@pytest.mark.asyncio
async def test_setless_suggestion_failure_is_persisted_or_fails_closed_without_partial_event():
    itineraries, repository, service = await _seed()
    failed = await service.record_suggestion_failed(
        workspace_id="workspace-suggest",
        session_id="failed-session",
        actor_user_id="user-suggest",
        revision_before=1,
        reason_code="PROVIDER_TIMEOUT",
        request_context={"day_index": 0, "intents": ["FUN"]},
    )
    assert failed.event_type is RecommendationEventType.SUGGESTION_FAILED
    assert failed.suggestion_set_id is None
    assert failed.payload["request_context"] == {"day_index": 0, "intents": ["FUN"]}

    rollback_repository = InMemorySuggestionRepository(
        itineraries,
        fail_at="after_system_event",
    )
    rollback_service = SuggestionSetService(rollback_repository, itineraries)
    with pytest.raises(RuntimeError, match="after_system_event"):
        await rollback_service.record_suggestion_failed(
            workspace_id="workspace-suggest",
            session_id="failed-session-rollback",
            actor_user_id="user-suggest",
            revision_before=1,
            reason_code="PROVIDER_TIMEOUT",
            request_context={"day_index": 0},
        )
    assert rollback_repository.events == []
