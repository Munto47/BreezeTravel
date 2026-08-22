from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from app.audit.evidence_service import EvidenceService
from app.audit.engine import AuditEngine
from app.audit.models import AuditRunInput, AuditStatus
from app.audit.repositories import InMemoryAuditRepository
from app.audit.suggestion_gate import SLOT_POLICY_VERSION, SuggestionAuditGate, verify_frozen_gate_inputs
from app.api import suggestions as suggestions_api
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    CommitmentKind,
    ItineraryRevisionContent,
)
from app.members.repositories import InMemoryMemberConstraintRepository
from app.members.models import (
    ConstraintConfirmationStatus,
    ConstraintHardness,
    ConstraintSource,
    MemberConstraint,
)
from app.schemas.place import Coordinates, PlaceCategory, RetrievalExecutionMode
from app.schemas.task_spec import DateRange, HardConstraint, TripTaskSpec
from app.suggestions.errors import SuggestionSetStaleError
from app.suggestions.models import (
    CandidateCurrentFact,
    RouteDelta,
    SuggestionIntent,
    SuggestionSetCreateInput,
)
from app.suggestions.providers import (
    ControlledCandidateFact,
    ControlledRouteSource,
    ProviderCandidateQuery,
    RouteTimes,
    _amap_candidate,
)

from tests.test_ranked_suggestions_api_integration import AvailableRoutes, RecordingSource, _repositories
from tests.test_suggestion_sets import NOW, _candidate, _input, _seed


async def _gate_fixture():
    itineraries, suggestions, service = await _seed()
    workspace = itineraries.workspaces["workspace-suggest"].model_copy(update={
        "current_task_spec_revision": 1,
    })
    itineraries.workspaces[workspace.workspace_id] = workspace
    base = itineraries.revisions[(workspace.workspace_id, 1)]
    first_day = base.days[0]
    timed = first_day.model_copy(update={"stops": [
        first_day.stops[0].model_copy(update={
            "start_time": "09:00", "end_time": "10:00", "raw_name": "故宫",
        }),
        first_day.stops[1].model_copy(update={
            "start_time": "13:00", "end_time": "14:00", "raw_name": "北海公园",
        }),
    ]})
    base = with_content_hash(ItineraryRevisionContent.model_validate({
        **base.model_dump(mode="python", exclude={"content_hash"}),
        "days": [timed, base.days[1]],
    }))
    itineraries.revisions[(workspace.workspace_id, 1)] = base
    suggestions.place_records[workspace.workspace_id] = {
        "poi-a": {
            "name": "故宫", "city": "北京", "category": "attraction",
            "opening_hours": "08:00-18:00", "provider": "controlled_snapshot",
            "retrieval_observed_at": NOW.isoformat(),
        },
        "poi-b": {
            "name": "北海公园", "city": "北京", "category": "attraction",
            "opening_hours": "08:00-18:00", "provider": "controlled_snapshot",
            "retrieval_observed_at": NOW.isoformat(),
        },
    }
    audit = InMemoryAuditRepository(
        itineraries.workspaces,
        place_records=suggestions.place_records,
    )
    audit.task_specs[workspace.workspace_id] = TripTaskSpec(
        task_id="task-suggestion-gate",
        room_id=workspace.room_id,
        task_revision=1,
        city=workspace.city,
        date_range=DateRange(start=workspace.trip_date_range.start, days=2),
    )
    members = InMemoryMemberConstraintRepository(itineraries.workspaces)
    gate = SuggestionAuditGate(audit, members, clock=lambda: NOW)
    return itineraries, suggestions, service, audit, members, gate, workspace, base


def _opening_fact() -> CandidateCurrentFact:
    return CandidateCurrentFact(
        fact_type="OPENING_HOURS",
        value="08:00-18:00",
        provider="controlled_operational_snapshot",
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
        request_hash="d" * 64,
        response_hash="e" * 64,
        execution_mode=RetrievalExecutionMode.FIXTURE,
        source_url="fixture://operational/opening",
    )


def _operational_fact(fact_type: str, value, char: str) -> CandidateCurrentFact:
    return CandidateCurrentFact(
        fact_type=fact_type,
        value=value,
        provider="controlled_operational_snapshot",
        observed_at=NOW,
        valid_until=NOW + timedelta(hours=1),
        request_hash=char * 64,
        response_hash=chr(ord(char) + 1) * 64,
        execution_mode=RetrievalExecutionMode.FIXTURE,
        source_url=f"fixture://operational/{fact_type.casefold()}",
    )


def test_amap_nonempty_business_opening_is_bound_to_the_same_provider_receipt():
    query = ProviderCandidateQuery(
        city="北京",
        intents=(SuggestionIntent.NEARBY,),
        typecodes=("110000",),
        radius_m=1000,
        anchor_name="故宫",
        anchor_place_id="poi-a",
        anchor_coords=Coordinates(lng=116.391, lat=39.916),
    )
    raw = {
        "id": "poi-operational",
        "name": "运营事实地点",
        "location": "116.397,39.918",
        "cityname": "北京市",
        "typecode": "110100",
        "type": "风景名胜",
        "business": {"opentime_today": "08:30-17:30"},
    }
    candidate = _amap_candidate(raw, query, "1" * 64, "2" * 64, NOW)

    assert candidate is not None
    opening = candidate.current_facts[0]
    assert opening.fact_type == "OPENING_HOURS"
    assert opening.value == "08:30-17:30"
    assert opening.request_hash == candidate.provider_receipt.request_hash
    assert opening.response_hash == candidate.provider_receipt.response_hash


def test_three_audited_candidates_remain_visible_as_typed_partial_without_fabrication():
    itineraries, suggestion_repository = _repositories("北京")
    workspace = itineraries.workspaces["workspace-北京"].model_copy(update={
        "current_task_spec_revision": 1,
    })
    itineraries.workspaces[workspace.workspace_id] = workspace
    base = itineraries.revisions[(workspace.workspace_id, 1)]
    first = base.days[0]
    timed = first.model_copy(update={"stops": [
        first.stops[0].model_copy(update={"start_time": "09:00", "end_time": "10:00"}),
    ]})
    base = with_content_hash(ItineraryRevisionContent.model_validate({
        **base.model_dump(mode="python", exclude={"content_hash"}),
        "days": [timed, base.days[1]],
    }))
    itineraries.revisions[(workspace.workspace_id, 1)] = base
    audit = InMemoryAuditRepository(
        itineraries.workspaces,
        place_records=suggestion_repository.place_records,
    )
    audit.task_specs[workspace.workspace_id] = TripTaskSpec(
        task_id="task-partial",
        room_id=workspace.room_id,
        task_revision=1,
        city=workspace.city,
        date_range=DateRange(start=workspace.trip_date_range.start, days=2),
    )
    facts = [
        ControlledCandidateFact(
            place_id=f"partial-{index}",
            name=f"受控候选{index}",
            city="北京",
            category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lng=116.392 + index * 0.001, lat=39.917),
            current_facts=(_opening_fact().model_copy(update={
                "request_hash": f"{index}" * 64,
                "response_hash": f"{index + 3}" * 64,
            }),),
        )
        for index in range(1, 4)
    ]
    source = RecordingSource(facts, NOW)
    provider = suggestions_api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: source,
        route_source_factory=AvailableRoutes,
        audit_gate=SuggestionAuditGate(
            audit,
            InMemoryMemberConstraintRepository(itineraries.workspaces),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )
    result = asyncio.run(provider.rank(
        workspace_id=workspace.workspace_id,
        request=suggestions_api.CreateSuggestionSetRequest(
            base_revision=1,
            day_index=0,
            insert_after_stop_id="anchor-a",
            intents=[SuggestionIntent.NEARBY],
            session_id="partial-session",
        ),
        actor_user_id="user-ranked-api",
    ))

    assert len(result.candidates) == 3
    assert result.result_status == "PARTIAL"
    assert "RESULTS_BELOW_MINIMUM" in result.shortage_reason_codes
    assert [item.rank_position for item in result.candidates] == [1, 2, 3]
    assert all(item.audit_gate is not None for item in result.candidates)
    assert "VISIBLE_RESULTS_BELOW_MINIMUM" in result.shortage_reason_codes
    assert "AUDIT_SATISFIED_OPTIONS_BELOW_TOP3" not in result.shortage_reason_codes


def test_authoritative_gate_promotes_directly_acceptable_candidates_above_infeasible_provider_rank():
    itineraries, suggestion_repository = _repositories("北京")
    workspace = itineraries.workspaces["workspace-北京"].model_copy(update={
        "current_task_spec_revision": 1,
    })
    itineraries.workspaces[workspace.workspace_id] = workspace
    base = itineraries.revisions[(workspace.workspace_id, 1)]
    first = base.days[0]
    timed = first.model_copy(update={"stops": [
        first.stops[0].model_copy(update={"start_time": "09:00", "end_time": "10:00"}),
    ]})
    base = with_content_hash(ItineraryRevisionContent.model_validate({
        **base.model_dump(mode="python", exclude={"content_hash"}),
        "days": [timed, base.days[1]],
    }))
    itineraries.revisions[(workspace.workspace_id, 1)] = base
    audit = InMemoryAuditRepository(
        itineraries.workspaces,
        place_records=suggestion_repository.place_records,
    )
    audit.task_specs[workspace.workspace_id] = TripTaskSpec(
        task_id="task-authoritative-rerank",
        room_id=workspace.room_id,
        task_revision=1,
        city=workspace.city,
        date_range=DateRange(start=workspace.trip_date_range.start, days=2),
    )
    facts = []
    for index in range(5):
        opening = _opening_fact().model_copy(update={
            "value": "12:00-13:00" if index in {0, 1} else "08:00-18:00",
            "request_hash": f"{index + 1}" * 64,
            "response_hash": f"{index + 5}" * 64,
        })
        facts.append(ControlledCandidateFact(
            place_id=f"gate-rerank-{index}",
            name=f"权威重排候选{index}",
            city="北京",
            category=PlaceCategory.ATTRACTION,
            coords=Coordinates(lng=116.392 + index * 0.001, lat=39.917),
            popularity=1.0 if index in {0, 1} else 0.0,
            current_facts=(opening,),
        ))

    class MixedRoutes(AvailableRoutes):
        async def route_times(self, query, candidate):
            if candidate.canonical_place.place_id != "gate-rerank-4":
                return await super().route_times(query, candidate)
            return await ControlledRouteSource({
                "gate-rerank-4": RouteTimes(
                    status="AVAILABLE",
                    previous_to_candidate_minutes=45,
                )
            }).route_times(query, candidate)

    provider = suggestions_api.DefaultRankedSuggestionProvider(
        itineraries,
        candidate_source_factory=lambda _city, _now: RecordingSource(facts, NOW),
        route_source_factory=MixedRoutes,
        audit_gate=SuggestionAuditGate(
            audit,
            InMemoryMemberConstraintRepository(itineraries.workspaces),
            clock=lambda: NOW,
        ),
        clock=lambda: NOW,
    )

    result = asyncio.run(provider.rank(
        workspace_id=workspace.workspace_id,
        request=suggestions_api.CreateSuggestionSetRequest(
            base_revision=1,
            day_index=0,
            insert_after_stop_id="anchor-a",
            intents=[SuggestionIntent.NEARBY],
            session_id="authoritative-rerank-session",
        ),
        actor_user_id="user-ranked-api",
    ))

    assert {
        candidate.canonical_place.place_id for candidate in result.candidates[:3]
    } == {"gate-rerank-2", "gate-rerank-3", "gate-rerank-4"}
    assert all(candidate.hard_gate.passed for candidate in result.candidates[:3])
    assert result.candidates[2].canonical_place.place_id == "gate-rerank-4"
    assert result.candidates[2].classification.value == "DEFER_TO_OTHER_DAY"
    assert {
        candidate.canonical_place.place_id for candidate in result.candidates[3:]
    } == {"gate-rerank-0", "gate-rerank-1"}
    assert all(candidate.hard_gate.passed is False for candidate in result.candidates[3:])
    assert all(
        "OUTSIDE_OPENING_HOURS" in candidate.hard_gate.reason_codes
        and any(
            finding.reason_code == "OUTSIDE_OPENING_HOURS"
            and finding.status is AuditStatus.VIOLATED
            and finding.severity.value == "HIGH"
            for finding in candidate.audit_gate.findings
        )
        for candidate in result.candidates[3:]
        if candidate.audit_gate is not None
    )
    assert [candidate.rank_position for candidate in result.candidates] == [1, 2, 3, 4, 5]
    assert all(
        verify_frozen_gate_inputs(workspace=workspace, base=base, candidate=candidate)
        for candidate in result.candidates
    )


@pytest.mark.asyncio
async def test_missing_opening_fact_stays_unknown_and_route_priors_cannot_promote_it():
    *_, gate, workspace, base = await _gate_fixture()
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate(),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.UNKNOWN
    assert candidate.hard_gate.passed is False
    assert "OPENING_HOURS_CURRENT_FACT_UNKNOWN" in candidate.hard_gate.reason_codes
    assert candidate.source_prior_refs  # priors remain display/ranking provenance only


@pytest.mark.asyncio
async def test_controlled_opening_route_and_slot_are_satisfied_and_hash_verifies():
    *_, gate, workspace, base = await _gate_fixture()
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.SATISFIED
    assert candidate.audit_gate.slot_policy_version == SLOT_POLICY_VERSION
    assert candidate.hard_gate.passed is True
    assert verify_frozen_gate_inputs(workspace=workspace, base=base, candidate=candidate)


@pytest.mark.asyncio
async def test_medium_adjacent_category_warning_is_not_promoted_to_a_hard_block():
    *_, gate, workspace, base = await _gate_fixture()
    original = _candidate()
    previous_leg = original.route_delta.route_receipts[0].model_copy(update={
        "origin_place_id": "poi-b",
    })
    candidate = original.model_copy(update={
        "canonical_place": original.canonical_place.model_copy(update={
            "name": "北海公园东门",
        }),
        "provider_receipt": original.provider_receipt.model_copy(update={
            "name": "北海公园东门",
        }),
        "current_facts": (_opening_fact(),),
        "route_delta": RouteDelta(
            status="AVAILABLE",
            previous_to_candidate_minutes=8,
            delta_route_minutes=8,
            route_receipts=(previous_leg,),
        ),
    })

    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=candidate,
        day_index=0,
        insert_after_stop_id="anchor-b",
        insert_before_stop_id=None,
    )

    assert candidate.audit_gate is not None
    repeated = [
        finding
        for finding in candidate.audit_gate.findings
        if finding.reason_code == "ADJACENT_CATEGORY_REPEATED"
    ]
    assert len(repeated) == 1
    assert repeated[0].status is AuditStatus.VIOLATED
    assert repeated[0].severity.value == "MEDIUM"
    assert candidate.audit_gate.status is AuditStatus.SATISFIED
    assert candidate.hard_gate.passed is True
    assert candidate.classification.value != "INFEASIBLE"
    assert "AUDIT_GATE_NONBLOCKING_WARNING" in candidate.explanation_codes
    assert verify_frozen_gate_inputs(workspace=workspace, base=base, candidate=candidate)


@pytest.mark.asyncio
async def test_provider_24_hour_opening_is_usable_by_the_authoritative_suggestion_gate():
    *_, gate, workspace, base = await _gate_fixture()
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(
            update={"current_facts": (_opening_fact().model_copy(update={"value": "24小时营业"}),)}
        ),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.SATISFIED
    assert candidate.hard_gate.passed is True
    assert "OPENING_HOURS_UNPARSEABLE" not in candidate.hard_gate.reason_codes


@pytest.mark.asyncio
async def test_progressive_builder_does_not_treat_missing_daily_hotel_as_candidate_hard_failure():
    *_, gate, workspace, base = await _gate_fixture()
    base = with_content_hash(ItineraryRevisionContent.model_validate({
        **base.model_dump(mode="python", exclude={"content_hash"}),
        "days": [base.days[0].model_copy(update={"stops": [base.days[0].stops[0]]}), base.days[1]],
    }))

    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id=None,
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.SATISFIED
    assert candidate.hard_gate.passed is True
    assert all(finding.rule_id != "constraint.daily_hotel" for finding in candidate.audit_gate.findings)


@pytest.mark.asyncio
async def test_return_commitment_requires_real_route_plus_buffer():
    *_, gate, workspace, base = await _gate_fixture()
    day = base.days[0]
    return_stop = day.stops[1].model_copy(update={
        "start_time": "13:00",
        "end_time": "13:30",
        "raw_name": "北京南站返程",
        "commitment_kind": CommitmentKind.RETURN_DEPARTURE,
        "fixed_commitment": True,
        "locked": True,
    })
    base = with_content_hash(ItineraryRevisionContent.model_validate({
        **base.model_dump(mode="python", exclude={"content_hash"}),
        "days": [day.model_copy(update={"stops": [day.stops[0], return_stop]}), base.days[1]],
    }))
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.VIOLATED
    assert "RETURN_DEPARTURE_CONFLICT" in candidate.hard_gate.reason_codes


@pytest.mark.asyncio
async def test_locked_edge_without_a_declared_edit_rule_is_explicit_unknown():
    *_, gate, workspace, base = await _gate_fixture()
    day = base.days[0]
    locked_anchor = day.stops[0].model_copy(update={"locked": True})
    base = with_content_hash(ItineraryRevisionContent.model_validate({
        **base.model_dump(mode="python", exclude={"content_hash"}),
        "days": [day.model_copy(update={"stops": [locked_anchor, day.stops[1]]}), base.days[1]],
    }))
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.UNKNOWN
    assert "LOCKED_EDGE_POLICY_NOT_IMPLEMENTED" in candidate.hard_gate.reason_codes


@pytest.mark.asyncio
async def test_applicable_reservation_policy_requires_an_operational_fact():
    _, _, _, audit, _, gate, workspace, base = await _gate_fixture()
    audit.task_specs[workspace.workspace_id] = audit.task_specs[workspace.workspace_id].model_copy(update={
        "hard_constraints": [HardConstraint(
            id="booking-required",
            type="reservation_required",
            value=True,
        )],
    })
    missing = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )
    controlled = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={
            "current_facts": (
                _opening_fact(),
                _operational_fact("RESERVATION_POLICY", {"required": True, "status": "AVAILABLE"}, "7"),
            ),
        }),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert "RESERVATION_POLICY_CURRENT_FACT_UNKNOWN" in missing.hard_gate.reason_codes
    assert controlled.audit_gate is not None
    assert any(
        finding.reason_code == "RESERVATION_POLICY_CURRENT"
        for finding in controlled.audit_gate.findings
    )


@pytest.mark.asyncio
async def test_wheelchair_hard_input_remains_unknown_until_a_real_rule_exists():
    itineraries, _, _, audit, members, _, workspace, base = await _gate_fixture()
    workspace = workspace.model_copy(update={"current_member_constraint_revision": 1})
    itineraries.workspaces[workspace.workspace_id] = workspace
    members.constraints.append(MemberConstraint(
        constraint_id="wheelchair-a",
        owner_member_id="member-a",
        type="wheelchair_accessibility",
        operator="eq",
        value=True,
        hardness=ConstraintHardness.HARD,
        source=ConstraintSource.MEMBER_EXPLICIT,
        confirmation_status=ConstraintConfirmationStatus.CONFIRMED,
        workspace_id=workspace.workspace_id,
        revision=1,
    ))
    gate = SuggestionAuditGate(audit, members, clock=lambda: NOW)
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={
            "current_facts": (
                _opening_fact(),
                _operational_fact("ACCESSIBILITY_POLICY", {"wheelchair_accessible": True}, "8"),
            ),
        }),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.status is AuditStatus.UNKNOWN
    assert "MEMBER_CONSTRAINT_TYPE_UNSUPPORTED" in candidate.hard_gate.reason_codes


@pytest.mark.asyncio
async def test_confirmed_member_walking_limit_is_authoritative_not_a_score_signal():
    itineraries, _, _, audit, members, _, workspace, base = await _gate_fixture()
    workspace = workspace.model_copy(update={"current_member_constraint_revision": 1})
    itineraries.workspaces[workspace.workspace_id] = workspace
    members.constraints.append(MemberConstraint(
        constraint_id="walk-five",
        owner_member_id="member-a",
        type="walking_limit_minutes",
        operator="lte",
        value=5,
        hardness=ConstraintHardness.HARD,
        source=ConstraintSource.MEMBER_EXPLICIT,
        confirmation_status=ConstraintConfirmationStatus.CONFIRMED,
        workspace_id=workspace.workspace_id,
        revision=1,
    ))
    gate = SuggestionAuditGate(audit, members, clock=lambda: NOW)
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )

    assert candidate.audit_gate is not None
    assert candidate.audit_gate.member_constraint_revision_set == {"walk-five": 1}
    assert candidate.audit_gate.status is AuditStatus.VIOLATED
    assert "WALKING_LIMIT_EXCEEDED" in candidate.hard_gate.reason_codes


@pytest.mark.asyncio
async def test_accept_rejects_changed_task_token_inside_repository_lock():
    itineraries, _, service, _, _, gate, workspace, base = await _gate_fixture()
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )
    create_input = SuggestionSetCreateInput.model_validate({
        **_input(candidate).model_dump(mode="python"),
        "candidates": [candidate],
    })
    await service.create_from_ranked(create_input)
    itineraries.workspaces[workspace.workspace_id] = workspace.model_copy(update={
        "current_task_spec_revision": 2,
    })

    with pytest.raises(SuggestionSetStaleError):
        await service.accept(
            workspace_id=workspace.workspace_id,
            suggestion_set_id="set-1",
            candidate_id=candidate.candidate_id,
            if_match_revision=1,
            idempotency_key="gate-token-stale",
            actor_user_id="user-suggest",
        )


@pytest.mark.asyncio
async def test_accept_route_receipts_feed_ordinary_audit_observations():
    itineraries, suggestions, service, audit, _, gate, workspace, base = await _gate_fixture()
    candidate = await gate.evaluate_candidate(
        workspace=workspace,
        base=base,
        candidate=_candidate().model_copy(update={"current_facts": (_opening_fact(),)}),
        day_index=0,
        insert_after_stop_id="anchor-a",
        insert_before_stop_id="anchor-b",
    )
    await service.create_from_ranked(SuggestionSetCreateInput.model_validate({
        **_input(candidate).model_dump(mode="python"),
        "candidates": [candidate],
    }))
    accepted = await service.accept(
        workspace_id=workspace.workspace_id,
        suggestion_set_id="set-1",
        candidate_id=candidate.candidate_id,
        if_match_revision=1,
        idempotency_key="gate-route-readback",
        actor_user_id="user-suggest",
    )
    records = suggestions.place_records[workspace.workspace_id]
    observations = EvidenceService().observations_from_revision(
        accepted.revision,
        records,
        now=NOW,
        target_itinerary_revision=accepted.new_revision,
    )
    route = [item for item in observations if item.fact_type == "ROUTE_TIME"]

    assert {item.subject_id for item in route} == {
        f"anchor-a->{accepted.stop_id}",
        f"{accepted.stop_id}->anchor-b",
    }
    assert {item.value["duration_minutes"] for item in route} == {8, 6}
    assert accepted.revision.days[0].stops[0].transport_to_next.mode == "walking"
    snapshot = EvidenceService().create_snapshot(
        workspace_id=workspace.workspace_id,
        itinerary_revision=accepted.new_revision,
        observations=observations,
        now=NOW,
    )
    task = audit.task_specs[workspace.workspace_id]
    report = AuditEngine().run(
        run_input=AuditRunInput(
            workspace_id=workspace.workspace_id,
            itinerary_revision=accepted.new_revision,
            task_id=task.task_id,
            task_revision=task.task_revision,
        ),
        revision=accepted.revision,
        task_spec=task,
        evidence_snapshot=snapshot,
        now=NOW,
    )
    route_findings = [item for item in report.findings if item.rule_id == "audit.route_gap"]
    assert route_findings
    assert all(item.status is AuditStatus.SATISFIED for item in route_findings)
