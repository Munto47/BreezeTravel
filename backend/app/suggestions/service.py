from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from app.itineraries.errors import InvalidEditCommandError, ResourceNotFound
from app.itineraries.command_service import build_revision_command_outcome
from app.itineraries.hash_service import compute_command_request_hash, sha256_canonical, with_content_hash
from app.itineraries.models import (
    EditOperation,
    ItineraryDay,
    ItineraryEditCommand,
    ItineraryPatchResult,
    ItineraryRevision,
    ItineraryRevisionContent,
    ItineraryStop,
    RevisionTransport,
    RevisionSource,
    TripWorkspace,
)
from app.itineraries.repositories import ItineraryRepository
from app.audit.models import AuditStatus
from app.audit.evidence_policy import EvidencePolicy
from app.audit.suggestion_gate import candidate_slot_times, verify_frozen_gate_inputs
from app.suggestions.errors import (
    SuggestionCandidateBlockedError,
    SuggestionCandidateEvidenceUnavailableError,
    SuggestionEdgeConflictError,
    SuggestionSetExpiredError,
    SuggestionSetStaleError,
)
from app.suggestions.models import (
    AcceptSuggestionResult,
    FreshnessStatus,
    RecommendationEvent,
    RecommendationEventCommandResult,
    RecommendationEventType,
    RouteReceiptLeg,
    SuggestionCandidate,
    SuggestionSet,
    SuggestionSetCreateInput,
)
from app.suggestions.repositories import SuggestionRepository


def _event_id() -> str:
    return f"re-{uuid4()}"


def _validate_edge(suggestion_set: SuggestionSet, revision: ItineraryRevision) -> tuple[int, ItineraryDay]:
    if suggestion_set.day_index >= len(revision.days):
        raise SuggestionEdgeConflictError(
            "suggestion day is outside the itinerary",
            context={"day_index": suggestion_set.day_index},
        )
    day = revision.days[suggestion_set.day_index]
    ids = [stop.stop_id for stop in day.stops]
    after = suggestion_set.insert_after_stop_id
    before = suggestion_set.insert_before_stop_id
    if after is not None and after not in ids:
        raise SuggestionEdgeConflictError(
            "insert-after anchor is absent from the frozen base revision",
            context={"insert_after_stop_id": after},
        )
    if before is not None and before not in ids:
        raise SuggestionEdgeConflictError(
            "insert-before anchor is absent from the frozen base revision",
            context={"insert_before_stop_id": before},
        )
    if after is not None and before is not None:
        after_index, before_index = ids.index(after), ids.index(before)
        if before_index != after_index + 1:
            raise SuggestionEdgeConflictError(
                "frozen insertion anchors no longer form one route edge",
                context={"insert_after_stop_id": after, "insert_before_stop_id": before},
            )
        return before_index, day
    if before is not None:
        return ids.index(before), day
    if after is not None:
        return ids.index(after) + 1, day
    return len(ids), day


def _validate_age(
    *,
    now: datetime,
    observed_at: datetime,
    max_age_seconds: int | None,
    future_reason: str,
    stale_reason: str,
) -> None:
    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < 0:
        raise SuggestionCandidateEvidenceUnavailableError(
            "evidence observation is in the future",
            context={"reason": future_reason},
        )
    if max_age_seconds is None:
        raise SuggestionCandidateEvidenceUnavailableError(
            "evidence freshness has no bounded lifetime",
            context={"reason": "EVIDENCE_MAX_AGE_UNKNOWN"},
        )
    if age_seconds > max_age_seconds:
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate evidence expired before acceptance",
            context={"reason": stale_reason, "age_seconds": age_seconds},
        )


def _validate_candidate(
    candidate: SuggestionCandidate,
    workspace: TripWorkspace,
    *,
    now: datetime,
) -> None:
    receipt = candidate.provider_receipt
    place = candidate.canonical_place
    if candidate.audit_gate is not None:
        if candidate.audit_gate.status is not AuditStatus.SATISFIED:
            raise SuggestionCandidateBlockedError(
                "candidate did not satisfy the authoritative audit gate",
                context={
                    "reason_codes": [
                        item.reason_code
                        for item in candidate.audit_gate.findings
                        if item.status is not AuditStatus.SATISFIED
                    ],
                },
            )
    if not candidate.hard_gate.passed:
        raise SuggestionCandidateBlockedError(
            "candidate is blocked by a frozen hard gate",
            context={"reason_codes": candidate.hard_gate.reason_codes},
        )
    if candidate.evidence_freshness.status is not FreshnessStatus.FRESH:
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate evidence is not fresh enough to accept",
            context={"reason": candidate.evidence_freshness.reason_code or "EVIDENCE_NOT_FRESH"},
        )
    if candidate.route_delta.status != "AVAILABLE":
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate route delta is unavailable",
            context={"reason": candidate.route_delta.reason_code or "ROUTE_DELTA_UNAVAILABLE"},
        )
    if place.city.strip().removesuffix("市") != workspace.city.strip().removesuffix("市"):
        raise SuggestionCandidateBlockedError(
            "candidate belongs to another city",
            context={"workspace_city": workspace.city, "candidate_city": place.city},
        )
    # Reassert the materialization boundary at mutation time instead of relying
    # on an earlier ranking object having passed Pydantic validation.
    if (
        receipt.canonical_place_id != place.place_id
        or receipt.provider_place_id.strip() == ""
        or receipt.request_hash.strip() == ""
        or receipt.response_hash.strip() == ""
        or receipt.observed_at.tzinfo is None
        or receipt.observed_at.utcoffset() is None
    ):
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate provider receipt is not materializable",
            context={"reason": "PROVIDER_RECEIPT_INCOMPLETE"},
        )
    if candidate.evidence_freshness.observed_at != receipt.observed_at:
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate freshness is not bound to its POI receipt",
            context={"reason": "POI_FRESHNESS_RECEIPT_MISMATCH"},
        )
    _validate_age(
        now=now,
        observed_at=receipt.observed_at,
        max_age_seconds=candidate.evidence_freshness.max_age_seconds,
        future_reason="POI_RECEIPT_OBSERVED_IN_FUTURE",
        stale_reason="POI_RECEIPT_STALE_AT_ACCEPT",
    )
    for route_receipt in candidate.route_delta.route_receipts:
        _validate_age(
            now=now,
            observed_at=route_receipt.observed_at,
            max_age_seconds=route_receipt.max_age_seconds,
            future_reason="ROUTE_RECEIPT_OBSERVED_IN_FUTURE",
            stale_reason="ROUTE_RECEIPT_STALE_AT_ACCEPT",
        )
    fact_policy = EvidencePolicy()
    for current_fact in candidate.current_facts:
        if current_fact.observed_at > now:
            raise SuggestionCandidateEvidenceUnavailableError(
                "candidate current fact observation is in the future",
                context={"reason": f"{current_fact.fact_type}_OBSERVED_IN_FUTURE"},
            )
        valid_until = current_fact.valid_until
        if valid_until is None:
            ttl = fact_policy.ttl_for(current_fact.fact_type)
            valid_until = current_fact.observed_at + ttl if ttl is not None else None
        if valid_until is None or valid_until < now:
            raise SuggestionCandidateEvidenceUnavailableError(
                "candidate current fact expired before acceptance",
                context={"reason": f"{current_fact.fact_type}_STALE_AT_ACCEPT"},
            )


def _validate_route_receipt_edge(
    suggestion_set: SuggestionSet,
    candidate: SuggestionCandidate,
    base: ItineraryRevision,
) -> None:
    day = base.days[suggestion_set.day_index]
    by_stop_id = {stop.stop_id: stop for stop in day.stops}
    after = by_stop_id.get(suggestion_set.insert_after_stop_id or "")
    before = by_stop_id.get(suggestion_set.insert_before_stop_id or "")
    candidate_id = candidate.canonical_place.place_id
    receipt_by_leg = {receipt.leg: receipt for receipt in candidate.route_delta.route_receipts}
    if after is not None and before is not None:
        expected = {
            RouteReceiptLeg.PREVIOUS_TO_CANDIDATE: (after.place_id, candidate_id),
            RouteReceiptLeg.CANDIDATE_TO_NEXT: (candidate_id, before.place_id),
            RouteReceiptLeg.PREVIOUS_TO_NEXT: (after.place_id, before.place_id),
        }
    elif after is not None:
        expected = {RouteReceiptLeg.PREVIOUS_TO_CANDIDATE: (after.place_id, candidate_id)}
    elif before is not None:
        expected = {RouteReceiptLeg.CANDIDATE_TO_NEXT: (candidate_id, before.place_id)}
    else:
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate route evidence has no insertion anchor",
            context={"reason": "ROUTE_ANCHOR_UNKNOWN"},
        )
    if set(receipt_by_leg) != set(expected):
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate route evidence does not cover the exact insertion edge",
            context={"reason": "ROUTE_RECEIPT_LEGS_MISMATCH"},
        )
    for leg, endpoints in expected.items():
        receipt = receipt_by_leg[leg]
        if (receipt.origin_place_id, receipt.destination_place_id) != endpoints:
            raise SuggestionCandidateEvidenceUnavailableError(
                "candidate route receipt endpoints differ from the base revision",
                context={"reason": "ROUTE_RECEIPT_ENDPOINT_MISMATCH", "leg": leg.value},
            )
    previous_candidate = receipt_by_leg.get(RouteReceiptLeg.PREVIOUS_TO_CANDIDATE)
    candidate_next = receipt_by_leg.get(RouteReceiptLeg.CANDIDATE_TO_NEXT)
    if (
        previous_candidate is not None
        and previous_candidate.destination_coords != candidate.canonical_place.coords
    ) or (
        candidate_next is not None
        and candidate_next.origin_coords != candidate.canonical_place.coords
    ):
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate route receipt coordinates differ from the canonical POI receipt",
            context={"reason": "ROUTE_RECEIPT_CANDIDATE_COORDINATE_MISMATCH"},
        )
    modes = {receipt.transport_mode for receipt in receipt_by_leg.values()}
    providers = {receipt.provider for receipt in receipt_by_leg.values()}
    executions = {receipt.execution_mode for receipt in receipt_by_leg.values()}
    if len(modes) != 1 or len(providers) != 1 or len(executions) != 1:
        raise SuggestionCandidateEvidenceUnavailableError(
            "candidate route receipts do not share one comparable route policy",
            context={"reason": "ROUTE_RECEIPT_POLICY_MISMATCH"},
        )


def revision_conflict_event(
    suggestion_set: SuggestionSet,
    candidate: SuggestionCandidate,
    *,
    actor_user_id: str,
    expected_revision: int,
    actual_revision: int | None,
) -> RecommendationEvent:
    return RecommendationEvent(
        event_id=_event_id(),
        session_id=suggestion_set.session_id,
        workspace_id=suggestion_set.workspace_id,
        actor_id=actor_user_id,
        event_type=RecommendationEventType.REVISION_CONFLICT,
        revision_before=expected_revision,
        suggestion_set_id=suggestion_set.suggestion_set_id,
        candidate_id=candidate.candidate_id,
        context_hash=suggestion_set.context_hash,
        policy_version=suggestion_set.policy_version,
        provider_snapshot_id=suggestion_set.provider_snapshot_id,
        rank_position=candidate.rank_position,
        reason_code="ITINERARY_REVISION_CONFLICT",
        payload={"expected_revision": expected_revision, "actual_revision": actual_revision},
    )


class AtomicSuggestionUndoService:
    """Connect public itinerary Undo to a frozen recommendation event atomically."""

    def __init__(self, repository: SuggestionRepository):
        self.repository = repository

    async def apply_if_accepted_suggestion(
        self,
        command: ItineraryEditCommand,
        *,
        if_match_revision: int,
        idempotency_key: str,
    ) -> ItineraryPatchResult | None:
        if command.operation is not EditOperation.UNDO:
            raise InvalidEditCommandError("atomic suggestion Undo only accepts UNDO commands")
        if if_match_revision != command.base_revision:
            raise InvalidEditCommandError("If-Match and command base_revision must match")
        if not idempotency_key or len(idempotency_key) > 200:
            raise InvalidEditCommandError("Idempotency-Key must contain 1 to 200 characters")
        target_revision = command.payload.get("target_revision")
        if isinstance(target_revision, bool) or not isinstance(target_revision, int):
            raise InvalidEditCommandError("target_revision must be an integer")
        if target_revision <= 0 or target_revision >= command.base_revision:
            raise InvalidEditCommandError("UNDO target_revision must be older than base_revision")
        request_hash = compute_command_request_hash(command.model_dump(mode="json"))

        def revision_builder(workspace, base, target):
            return build_revision_command_outcome(
                workspace,
                base,
                command,
                undo_target=target,
            )

        def event_builder(
            frozen_set,
            frozen_candidate,
            workspace,
            base,
            target,
            revision,
            accepted_event,
        ):
            return RecommendationEvent(
                event_id=_event_id(),
                session_id=accepted_event.session_id,
                workspace_id=workspace.workspace_id,
                actor_id=command.actor_user_id,
                event_type=RecommendationEventType.STOP_UNDONE,
                revision_before=base.revision,
                revision_after=revision.revision,
                suggestion_set_id=accepted_event.suggestion_set_id,
                candidate_id=accepted_event.candidate_id,
                context_hash=accepted_event.context_hash,
                policy_version=accepted_event.policy_version,
                provider_snapshot_id=accepted_event.provider_snapshot_id,
                rank_position=accepted_event.rank_position,
                reason_code="UNDO_ACCEPTED_SUGGESTION",
                payload={
                    "source_accept_event_id": accepted_event.event_id,
                    "source_accept_revision": base.revision,
                    "target_revision": target.revision,
                    "stop_id": accepted_event.payload.get("stop_id"),
                    "canonical_place_id": accepted_event.payload.get("canonical_place_id"),
                },
            )

        return await self.repository.undo_accepted_candidate(
            command=command,
            target_revision=target_revision,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            revision_builder=revision_builder,
            event_builder=event_builder,
        )


class SuggestionSetService:
    def __init__(
        self,
        repository: SuggestionRepository,
        itinerary_repository: ItineraryRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.itinerary_repository = itinerary_repository
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise SuggestionCandidateEvidenceUnavailableError(
                "suggestion acceptance clock is not timezone-aware",
                context={"reason": "ACCEPT_CLOCK_INVALID"},
            )
        return now

    async def record_candidate_previewed(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> RecommendationEventCommandResult:
        return await self._record_interaction(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            event_type=RecommendationEventType.CANDIDATE_PREVIEWED,
        )

    async def record_candidate_dismissed(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        actor_user_id: str,
        idempotency_key: str,
        reason_code: str,
    ) -> RecommendationEventCommandResult:
        normalized_reason = reason_code.strip()
        if not normalized_reason or len(normalized_reason) > 100:
            raise InvalidEditCommandError("dismiss reason_code must contain 1 to 100 characters")
        return await self._record_interaction(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            event_type=RecommendationEventType.CANDIDATE_DISMISSED,
            reason_code=normalized_reason,
        )

    async def record_line_completed(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> RecommendationEventCommandResult:
        return await self._record_interaction(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=None,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            event_type=RecommendationEventType.LINE_COMPLETED,
        )

    async def record_stop_undone(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        actor_user_id: str,
        idempotency_key: str,
        revision_before: int,
        revision_after: int,
    ) -> RecommendationEventCommandResult:
        """Legacy explicit event hook; public Undo uses AtomicSuggestionUndoService."""
        if revision_before <= 0 or revision_after <= revision_before:
            raise InvalidEditCommandError("stop undo revisions must be positive and increasing")
        return await self._record_interaction(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            event_type=RecommendationEventType.STOP_UNDONE,
            revision_before=revision_before,
            revision_after=revision_after,
            reason_code="UNDO_ACCEPTED_SUGGESTION",
        )

    async def _record_interaction(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str | None,
        actor_user_id: str,
        idempotency_key: str,
        event_type: RecommendationEventType,
        reason_code: str | None = None,
        revision_before: int | None = None,
        revision_after: int | None = None,
    ) -> RecommendationEventCommandResult:
        allowed = {
            RecommendationEventType.CANDIDATE_PREVIEWED,
            RecommendationEventType.CANDIDATE_DISMISSED,
            RecommendationEventType.LINE_COMPLETED,
            RecommendationEventType.STOP_UNDONE,
        }
        if event_type not in allowed:
            raise InvalidEditCommandError("event type is not writable through the interaction command seam")
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise InvalidEditCommandError("idempotency key must contain 1 to 200 characters")
        request_hash = sha256_canonical({
            "operation": "APPEND_RECOMMENDATION_EVENT",
            "workspace_id": workspace_id,
            "suggestion_set_id": suggestion_set_id,
            "candidate_id": candidate_id,
            "actor_user_id": actor_user_id,
            "event_type": event_type.value,
            "reason_code": reason_code,
            "revision_before": revision_before,
            "revision_after": revision_after,
        })

        def builder(
            frozen_set: SuggestionSet,
            frozen_candidate: SuggestionCandidate | None,
            workspace: TripWorkspace,
        ) -> RecommendationEvent:
            if candidate_id is not None and frozen_candidate is None:
                raise ResourceNotFound("suggestion candidate does not exist")
            before = revision_before or workspace.current_itinerary_revision
            if event_type is RecommendationEventType.STOP_UNDONE and workspace.current_itinerary_revision != revision_after:
                from app.itineraries.errors import RevisionConflictError

                raise RevisionConflictError(
                    "stop undo event does not match the current itinerary revision",
                    context={
                        "expected_revision": revision_after,
                        "actual_revision": workspace.current_itinerary_revision,
                    },
                )
            return RecommendationEvent(
                event_id=_event_id(),
                session_id=frozen_set.session_id,
                workspace_id=workspace.workspace_id,
                actor_id=actor_user_id,
                event_type=event_type,
                revision_before=before,
                revision_after=revision_after,
                suggestion_set_id=frozen_set.suggestion_set_id,
                candidate_id=frozen_candidate.candidate_id if frozen_candidate is not None else None,
                context_hash=frozen_set.context_hash,
                policy_version=frozen_set.policy_version,
                provider_snapshot_id=frozen_set.provider_snapshot_id,
                rank_position=frozen_candidate.rank_position if frozen_candidate is not None else None,
                reason_code=reason_code,
            )

        return await self.repository.append_event_command(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            builder=builder,
        )

    async def record_suggestion_failed(
        self,
        *,
        workspace_id: str,
        session_id: str,
        actor_user_id: str,
        revision_before: int,
        reason_code: str,
        request_context: dict[str, object],
    ) -> RecommendationEvent:
        """Persist a provider failure even when no SuggestionSet was created."""
        event = RecommendationEvent(
            event_id=_event_id(),
            session_id=session_id,
            workspace_id=workspace_id,
            actor_id=actor_user_id,
            event_type=RecommendationEventType.SUGGESTION_FAILED,
            revision_before=revision_before,
            reason_code=reason_code,
            payload={"request_context": request_context},
        )
        return await self.repository.append_event(event)

    async def create_from_ranked(self, create_input: SuggestionSetCreateInput) -> SuggestionSet:
        """Persist trusted, already-ranked candidates as one immutable set.

        This seam intentionally does no Provider query and no ranking.  The
        future ranking layer must pass complete frozen receipts and scores.
        """
        created_at = self._now()
        if create_input.expires_at <= created_at:
            raise SuggestionSetExpiredError("suggestion set expiry must be after creation time")
        workspace = await self.itinerary_repository.get_workspace(create_input.workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.current_itinerary_revision != create_input.base_revision:
            from app.itineraries.errors import RevisionConflictError

            raise RevisionConflictError(
                "suggestion set base revision is stale",
                context={
                    "expected_revision": create_input.base_revision,
                    "actual_revision": workspace.current_itinerary_revision,
                },
            )
        base = await self.itinerary_repository.get_revision(create_input.workspace_id, create_input.base_revision)
        if base is None:
            raise ResourceNotFound("base revision does not exist")
        set_id = create_input.suggestion_set_id or f"ss-{uuid4()}"
        candidates = [
            SuggestionCandidate(
                **draft.model_copy(deep=True).model_dump(),
                suggestion_set_id=set_id,
            )
            for draft in create_input.candidates
        ]
        suggestion_set = SuggestionSet(
            suggestion_set_id=set_id,
            workspace_id=create_input.workspace_id,
            base_revision=create_input.base_revision,
            day_index=create_input.day_index,
            insert_after_stop_id=create_input.insert_after_stop_id,
            insert_before_stop_id=create_input.insert_before_stop_id,
            intents=create_input.intents,
            context_hash=create_input.context_hash,
            policy_version=create_input.policy_version,
            provider_snapshot_id=create_input.provider_snapshot_id,
            expires_at=create_input.expires_at,
            session_id=create_input.session_id,
            candidates=candidates,
            created_by=create_input.created_by,
            created_at=created_at,
            result_status=create_input.result_status,
            shortage_reason_codes=create_input.shortage_reason_codes,
            excluded_counts=create_input.excluded_counts,
        )
        _validate_edge(suggestion_set, base)
        for candidate in candidates:
            if candidate.canonical_place.city.strip().removesuffix("市") != workspace.city.strip().removesuffix("市"):
                raise SuggestionCandidateBlockedError(
                    "wrong-city candidate cannot enter a frozen suggestion set",
                    context={"candidate_id": candidate.candidate_id},
                )
        shown_event = RecommendationEvent(
            event_id=_event_id(),
            session_id=create_input.session_id,
            workspace_id=create_input.workspace_id,
            actor_id=create_input.created_by,
            event_type=RecommendationEventType.SUGGESTIONS_SHOWN,
            revision_before=create_input.base_revision,
            suggestion_set_id=set_id,
            context_hash=create_input.context_hash,
            policy_version=create_input.policy_version,
            provider_snapshot_id=create_input.provider_snapshot_id,
            payload={
                "candidate_ids": [candidate.candidate_id for candidate in candidates],
                "rank_positions": {candidate.candidate_id: candidate.rank_position for candidate in candidates},
                "source_prior_refs": {
                    candidate.candidate_id: list(candidate.source_prior_refs)
                    for candidate in candidates
                },
                "result_status": create_input.result_status,
                "shortage_reason_codes": create_input.shortage_reason_codes,
                "excluded_counts": create_input.excluded_counts,
            },
        )
        return await self.repository.create_set(suggestion_set, shown_event)

    async def accept(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        if_match_revision: int,
        idempotency_key: str,
        actor_user_id: str,
    ) -> AcceptSuggestionResult:
        if not idempotency_key.strip() or len(idempotency_key) > 200:
            raise InvalidEditCommandError("idempotency key must contain 1 to 200 characters")
        if if_match_revision <= 0:
            raise InvalidEditCommandError("If-Match revision must be positive")
        suggestion_set = await self.repository.get_set(workspace_id, suggestion_set_id)
        if suggestion_set is None:
            raise ResourceNotFound("suggestion set does not exist")
        if suggestion_set.base_revision != if_match_revision:
            raise SuggestionSetStaleError(
                "suggestion set is bound to another base revision",
                context={
                    "suggestion_base_revision": suggestion_set.base_revision,
                    "if_match_revision": if_match_revision,
                },
            )
        candidate = next((item for item in suggestion_set.candidates if item.candidate_id == candidate_id), None)
        if candidate is None:
            raise ResourceNotFound("suggestion candidate does not exist")
        request_hash = sha256_canonical({
            "operation": "ACCEPT_SUGGESTION_CANDIDATE",
            "workspace_id": workspace_id,
            "suggestion_set_id": suggestion_set_id,
            "candidate_id": candidate_id,
            "base_revision": if_match_revision,
            "actor_user_id": actor_user_id,
        })

        def builder(
            frozen_set: SuggestionSet,
            frozen_candidate: SuggestionCandidate,
            workspace: TripWorkspace,
            base: ItineraryRevision,
        ) -> AcceptSuggestionResult:
            accepted_at = self._now()
            if accepted_at >= frozen_set.expires_at:
                raise SuggestionSetExpiredError("suggestion set has expired")
            if frozen_set.base_revision != base.revision:
                raise SuggestionSetStaleError("suggestion set no longer matches the base revision")
            _validate_candidate(frozen_candidate, workspace, now=accepted_at)
            if frozen_candidate.audit_gate is not None and not verify_frozen_gate_inputs(
                workspace=workspace,
                base=base,
                candidate=frozen_candidate,
            ):
                raise SuggestionSetStaleError(
                    "authoritative suggestion audit inputs changed before acceptance",
                    context={"reason_code": "SUGGESTION_AUDIT_GATE_INPUT_STALE"},
                )
            if any(
                stop.place_id == frozen_candidate.canonical_place.place_id
                for day in base.days
                for stop in day.stops
            ):
                raise SuggestionCandidateBlockedError(
                    "candidate duplicates a canonical place already present in the itinerary",
                    context={
                        "reason_code": "DUPLICATE_CANONICAL_PLACE",
                        "canonical_place_id": frozen_candidate.canonical_place.place_id,
                    },
                )
            insert_index, target_day = _validate_edge(frozen_set, base)
            _validate_route_receipt_edge(frozen_set, frozen_candidate, base)
            stop_id = f"suggestion-stop-{frozen_set.suggestion_set_id}-{frozen_candidate.candidate_id}"
            if any(stop.stop_id == stop_id for day in base.days for stop in day.stops):
                raise InvalidEditCommandError("suggestion stop already exists in the base revision")
            receipt_by_leg = {item.leg: item for item in frozen_candidate.route_delta.route_receipts}
            slot_start, slot_end = candidate_slot_times(
                target_day.stops[insert_index - 1] if insert_index > 0 else None,
                target_day.stops[insert_index] if insert_index < len(target_day.stops) else None,
                receipt_by_leg,
            )
            inserted = ItineraryStop(
                stop_id=stop_id,
                place_id=frozen_candidate.canonical_place.place_id,
                day_index=frozen_set.day_index,
                order_index=insert_index,
                category=frozen_candidate.canonical_place.category,
                notes="",
                start_time=slot_start,
                end_time=slot_end,
                visit_duration_minutes=(
                    (
                        (int(slot_end[:2]) * 60 + int(slot_end[3:]))
                        - (int(slot_start[:2]) * 60 + int(slot_start[3:]))
                    )
                    if slot_start and slot_end
                    else None
                ),
                transport_to_next=(
                    RevisionTransport(
                        mode=next_receipt.transport_mode,
                        duration_minutes=next_receipt.duration_minutes,
                    )
                    if (
                        next_receipt := next(
                            (
                                item
                                for item in frozen_candidate.route_delta.route_receipts
                                if item.leg is RouteReceiptLeg.CANDIDATE_TO_NEXT
                            ),
                            None,
                        )
                    ) is not None
                    else None
                ),
            )
            new_days: list[ItineraryDay] = []
            for day in base.days:
                if day.day_index != frozen_set.day_index:
                    new_days.append(day)
                    continue
                stops = list(target_day.stops)
                previous_receipt = next(
                    (
                        item
                        for item in frozen_candidate.route_delta.route_receipts
                        if item.leg is RouteReceiptLeg.PREVIOUS_TO_CANDIDATE
                    ),
                    None,
                )
                if frozen_set.insert_after_stop_id is not None and previous_receipt is not None:
                    anchor_index = next(
                        index
                        for index, stop in enumerate(stops)
                        if stop.stop_id == frozen_set.insert_after_stop_id
                    )
                    stops[anchor_index] = stops[anchor_index].model_copy(update={
                        "transport_to_next": RevisionTransport(
                            mode=previous_receipt.transport_mode,
                            duration_minutes=previous_receipt.duration_minutes,
                        ),
                    })
                stops.insert(insert_index, inserted)
                normalized = [stop.model_copy(update={"order_index": index}) for index, stop in enumerate(stops)]
                new_days.append(day.model_copy(update={"stops": normalized}))
            receipt = frozen_candidate.provider_receipt
            receipt_hash = sha256_canonical(receipt.model_dump(mode="json"))
            map_projection = {
                "place_id": frozen_candidate.canonical_place.place_id,
                "canonical_name": frozen_candidate.canonical_place.name,
                "coords": frozen_candidate.canonical_place.coords.model_dump(mode="json"),
                "coordinate_role": "CANONICAL_PROVIDER_POI",
                "provenance": f"{receipt.provider}:{receipt.execution_mode.value}",
                "receipt_hash": receipt_hash,
            }
            revision = with_content_hash(ItineraryRevisionContent(
                itinerary_id=base.itinerary_id,
                workspace_id=workspace.workspace_id,
                revision=base.revision + 1,
                parent_revision=base.revision,
                source_type=RevisionSource.PLANNER,
                city=workspace.city,
                date_range=workspace.trip_date_range,
                days=new_days,
                locked_commitments=base.locked_commitments,
                change_summary={
                    "operation": "ACCEPT_SUGGESTION_CANDIDATE",
                    "suggestion_set_id": frozen_set.suggestion_set_id,
                    "candidate_id": frozen_candidate.candidate_id,
                    "context_hash": frozen_set.context_hash,
                    "policy_version": frozen_set.policy_version,
                    "provider_snapshot_id": frozen_set.provider_snapshot_id,
                    "rank_position": frozen_candidate.rank_position,
                    "source_prior_refs": frozen_candidate.source_prior_refs,
                    "route_delta": frozen_candidate.route_delta.model_dump(mode="json"),
                    "changed_days": [frozen_set.day_index],
                    "map_stop_projections": {stop_id: map_projection},
                },
                created_by=actor_user_id,
            ))
            event = RecommendationEvent(
                event_id=_event_id(),
                session_id=frozen_set.session_id,
                workspace_id=workspace.workspace_id,
                actor_id=actor_user_id,
                event_type=RecommendationEventType.CANDIDATE_ACCEPTED,
                revision_before=base.revision,
                revision_after=revision.revision,
                suggestion_set_id=frozen_set.suggestion_set_id,
                candidate_id=frozen_candidate.candidate_id,
                context_hash=frozen_set.context_hash,
                policy_version=frozen_set.policy_version,
                provider_snapshot_id=frozen_set.provider_snapshot_id,
                rank_position=frozen_candidate.rank_position,
                payload={
                    "stop_id": stop_id,
                    "canonical_place_id": frozen_candidate.canonical_place.place_id,
                    "receipt_hash": receipt_hash,
                    "source_prior_refs": list(frozen_candidate.source_prior_refs),
                },
            )
            return AcceptSuggestionResult(
                suggestion_set_id=frozen_set.suggestion_set_id,
                candidate_id=frozen_candidate.candidate_id,
                new_revision=revision.revision,
                stop_id=stop_id,
                revision=revision,
                event=event,
            )

        return await self.repository.accept_candidate(
            workspace_id=workspace_id,
            suggestion_set_id=suggestion_set_id,
            candidate_id=candidate_id,
            base_revision=if_match_revision,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            builder=builder,
        )
