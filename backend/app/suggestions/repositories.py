from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from app.db.connection import get_pool
from app.itineraries.errors import (
    IdempotencyKeyReusedError,
    InvalidEditCommandError,
    ResourceNotFound,
    RevisionConflictError,
)
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import (
    ItineraryEditCommand,
    ItineraryPatchResult,
    ItineraryRevision,
    TripWorkspace,
    WorkspaceStatus,
)
from app.itineraries.repositories import (
    InMemoryItineraryRepository,
    _revision_from_row,
    _workspace_from_row,
    insert_revision_record,
)
from app.suggestions.models import (
    AcceptSuggestionResult,
    RecommendationEvent,
    RecommendationEventCommandResult,
    SuggestionCandidate,
    SuggestionSet,
)


AcceptBuilder = Callable[
    [SuggestionSet, SuggestionCandidate, TripWorkspace, ItineraryRevision],
    AcceptSuggestionResult,
]
EventBuilder = Callable[
    [SuggestionSet, SuggestionCandidate | None, TripWorkspace],
    RecommendationEvent,
]
SuggestionUndoBuilder = Callable[
    [TripWorkspace, ItineraryRevision, ItineraryRevision],
    tuple[ItineraryRevision, ItineraryPatchResult, WorkspaceStatus],
]
SuggestionUndoEventBuilder = Callable[
    [
        SuggestionSet,
        SuggestionCandidate,
        TripWorkspace,
        ItineraryRevision,
        ItineraryRevision,
        ItineraryRevision,
        RecommendationEvent,
    ],
    RecommendationEvent,
]


class SuggestionRepository(Protocol):
    async def create_set(self, suggestion_set: SuggestionSet, shown_event: RecommendationEvent) -> SuggestionSet: ...

    async def get_set(self, workspace_id: str, suggestion_set_id: str) -> SuggestionSet | None: ...

    async def list_events(self, workspace_id: str) -> list[RecommendationEvent]: ...

    async def append_event(self, event: RecommendationEvent) -> RecommendationEvent: ...

    async def append_event_command(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str | None,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        builder: EventBuilder,
    ) -> RecommendationEventCommandResult: ...

    async def accept_candidate(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        base_revision: int,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        builder: AcceptBuilder,
    ) -> AcceptSuggestionResult: ...

    async def undo_accepted_candidate(
        self,
        *,
        command: ItineraryEditCommand,
        target_revision: int,
        idempotency_key: str,
        request_hash: str,
        revision_builder: SuggestionUndoBuilder,
        event_builder: SuggestionUndoEventBuilder,
    ) -> ItineraryPatchResult | None: ...


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _set_from_rows(row: Any, candidate_rows: list[Any]) -> SuggestionSet:
    return SuggestionSet(
        suggestion_set_id=row["suggestion_set_id"],
        workspace_id=row["workspace_id"],
        base_revision=row["base_revision"],
        day_index=row["day_index"],
        insert_after_stop_id=row["insert_after_stop_id"],
        insert_before_stop_id=row["insert_before_stop_id"],
        intents=_json(row["intents_json"]),
        context_hash=row["context_hash"].strip(),
        policy_version=row["policy_version"],
        provider_snapshot_id=row["provider_snapshot_id"],
        expires_at=row["expires_at"],
        session_id=row["session_id"],
        candidates=[SuggestionCandidate.model_validate(_json(item["candidate_json"])) for item in candidate_rows],
        created_by=row["created_by"],
        created_at=row["created_at"],
        result_status=row["result_status"],
        shortage_reason_codes=_json(row["shortage_reason_codes_json"]),
        excluded_counts=_json(row["excluded_counts_json"]),
    )


def _event_from_row(row: Any) -> RecommendationEvent:
    return RecommendationEvent.model_validate(_json(row["event_json"]))


def _validate_frozen_event_context(
    event: RecommendationEvent,
    suggestion_set: SuggestionSet,
    candidate: SuggestionCandidate | None,
    workspace: TripWorkspace,
    actor_user_id: str,
) -> None:
    """Reject accidental or malicious authority injection at the write seam."""
    expected = {
        "workspace_id": workspace.workspace_id,
        "session_id": suggestion_set.session_id,
        "actor_id": actor_user_id,
        "suggestion_set_id": suggestion_set.suggestion_set_id,
        "context_hash": suggestion_set.context_hash,
        "policy_version": suggestion_set.policy_version,
        "provider_snapshot_id": suggestion_set.provider_snapshot_id,
        "candidate_id": candidate.candidate_id if candidate is not None else None,
        "rank_position": candidate.rank_position if candidate is not None else None,
    }
    mismatches = [field for field, value in expected.items() if getattr(event, field) != value]
    if mismatches:
        raise ValueError(f"recommendation event differs from frozen authority: {', '.join(mismatches)}")
    writable = {
        "candidate_previewed",
        "candidate_dismissed",
        "stop_undone",
        "line_completed",
    }
    if event.event_type.value not in writable:
        raise ValueError("event type is not writable through recommendation event commands")
    requires_candidate = event.event_type.value != "line_completed"
    if requires_candidate != (candidate is not None):
        raise ValueError("recommendation event candidate scope does not match its type")


def _validate_system_event(event: RecommendationEvent) -> None:
    if (
        event.event_type.value != "suggestion_failed"
        or event.suggestion_set_id is not None
        or event.candidate_id is not None
    ):
        raise ValueError("set-less event seam only accepts suggestion_failed")


def _accepted_suggestion_ids(base: ItineraryRevision, target_revision: int) -> tuple[str, str] | None:
    summary = base.change_summary
    if summary.get("operation") != "ACCEPT_SUGGESTION_CANDIDATE":
        return None
    suggestion_set_id = summary.get("suggestion_set_id")
    candidate_id = summary.get("candidate_id")
    if not isinstance(suggestion_set_id, str) or not suggestion_set_id:
        raise InvalidEditCommandError("accepted suggestion revision has no frozen suggestion_set_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise InvalidEditCommandError("accepted suggestion revision has no frozen candidate_id")
    if base.parent_revision != target_revision:
        raise InvalidEditCommandError(
            "suggestion Undo must target the accepted revision's direct parent",
            context={"expected_target_revision": base.parent_revision, "target_revision": target_revision},
        )
    return suggestion_set_id, candidate_id


def _validate_acceptance_lineage(
    *,
    accepted_event: RecommendationEvent,
    suggestion_set: SuggestionSet,
    candidate: SuggestionCandidate,
    base: ItineraryRevision,
    target: ItineraryRevision,
) -> str:
    expected = {
        "workspace_id": base.workspace_id,
        "suggestion_set_id": suggestion_set.suggestion_set_id,
        "candidate_id": candidate.candidate_id,
        "session_id": suggestion_set.session_id,
        "context_hash": suggestion_set.context_hash,
        "policy_version": suggestion_set.policy_version,
        "provider_snapshot_id": suggestion_set.provider_snapshot_id,
        "rank_position": candidate.rank_position,
        "revision_before": target.revision,
        "revision_after": base.revision,
    }
    mismatches = [field for field, value in expected.items() if getattr(accepted_event, field) != value]
    if accepted_event.event_type.value != "candidate_accepted" or mismatches:
        raise InvalidEditCommandError(
            "accepted suggestion lineage does not match its frozen event",
            context={"mismatched_fields": mismatches},
        )
    stop_id = accepted_event.payload.get("stop_id")
    if not isinstance(stop_id, str) or not stop_id:
        raise InvalidEditCommandError("accepted suggestion event has no frozen stop_id")
    base_stop_ids = {stop.stop_id for day in base.days for stop in day.stops}
    target_stop_ids = {stop.stop_id for day in target.days for stop in day.stops}
    if stop_id not in base_stop_ids or stop_id in target_stop_ids:
        raise InvalidEditCommandError(
            "accepted suggestion stop does not match the revision delta",
            context={"stop_id": stop_id},
        )
    return stop_id


def _place_record(candidate: SuggestionCandidate) -> dict[str, Any]:
    receipt = candidate.provider_receipt
    place = candidate.canonical_place
    current = {fact.fact_type: fact for fact in candidate.current_facts}
    record = {
        "place_id": place.place_id,
        "provider_place_id": receipt.provider_place_id,
        "name": place.name,
        "city": place.city,
        "district": place.district,
        "address": place.address or "",
        "category": place.category,
        "coords": place.coords.model_dump(mode="json"),
        "provider": receipt.provider,
        "source": "amap_poi" if receipt.provider == "amap" else receipt.provider,
        "execution_mode": receipt.execution_mode.value,
        "retrieval_provider": receipt.provider,
        "retrieval_request_hash": receipt.request_hash,
        "retrieval_response_hash": receipt.response_hash,
        "retrieval_observed_at": receipt.observed_at.isoformat(),
        "source_url": receipt.source_url,
        "resolved_place_receipt": receipt.model_dump(mode="json"),
        "suggestion_provenance": {
            "suggestion_set_id": candidate.suggestion_set_id,
            "candidate_id": candidate.candidate_id,
            "rank_position": candidate.rank_position,
            "source_prior_refs": candidate.source_prior_refs,
        },
    }
    fact_fields = {
        "OPENING_HOURS": "opening_hours",
        "RESERVATION_POLICY": "reservation_policy",
        "ACCESSIBILITY_POLICY": "accessibility_policy",
        "DIETARY_SUPPORT": "dietary_support",
    }
    for fact_type, field in fact_fields.items():
        fact = current.get(fact_type)
        if fact is not None:
            record[field] = fact.value
            record[f"{field}_fact"] = fact.model_dump(mode="json")
    return record


class PostgresSuggestionRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def create_set(self, suggestion_set: SuggestionSet, shown_event: RecommendationEvent) -> SuggestionSet:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                suggestion_set.workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            if workspace["current_itinerary_revision"] != suggestion_set.base_revision:
                raise RevisionConflictError(
                    "suggestion set base revision is stale",
                    context={
                        "expected_revision": suggestion_set.base_revision,
                        "actual_revision": workspace["current_itinerary_revision"],
                    },
                )
            await conn.execute(
                """
                INSERT INTO suggestion_sets (
                    suggestion_set_id, workspace_id, base_revision, day_index,
                    insert_after_stop_id, insert_before_stop_id, intents_json,
                    context_hash, policy_version, provider_snapshot_id, expires_at,
                    session_id, created_by, created_at, result_status,
                    shortage_reason_codes_json, excluded_counts_json
                ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11,$12,$13,$14,$15,$16::jsonb,$17::jsonb)
                """,
                suggestion_set.suggestion_set_id,
                suggestion_set.workspace_id,
                suggestion_set.base_revision,
                suggestion_set.day_index,
                suggestion_set.insert_after_stop_id,
                suggestion_set.insert_before_stop_id,
                json.dumps([intent.value for intent in suggestion_set.intents]),
                suggestion_set.context_hash,
                suggestion_set.policy_version,
                suggestion_set.provider_snapshot_id,
                suggestion_set.expires_at,
                shown_event.session_id,
                suggestion_set.created_by,
                suggestion_set.created_at,
                suggestion_set.result_status,
                json.dumps(suggestion_set.shortage_reason_codes, ensure_ascii=False),
                json.dumps(suggestion_set.excluded_counts, ensure_ascii=False),
            )
            for candidate in suggestion_set.candidates:
                await conn.execute(
                    """
                    INSERT INTO suggestion_candidates (
                        workspace_id, suggestion_set_id, candidate_id, canonical_place_id,
                        provider_receipt_id, rank_position, hard_gate_passed, candidate_json
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb)
                    """,
                    suggestion_set.workspace_id,
                    suggestion_set.suggestion_set_id,
                    candidate.candidate_id,
                    candidate.canonical_place.place_id,
                    candidate.provider_receipt_id,
                    candidate.rank_position,
                    candidate.hard_gate.passed,
                    json.dumps(candidate.model_dump(mode="json"), ensure_ascii=False),
                )
            await self._insert_event(conn, shown_event)
        return suggestion_set

    async def _load_set(self, conn: Any, workspace_id: str, suggestion_set_id: str) -> SuggestionSet | None:
        row = await conn.fetchrow(
            "SELECT * FROM suggestion_sets WHERE workspace_id = $1 AND suggestion_set_id = $2",
            workspace_id,
            suggestion_set_id,
        )
        if row is None:
            return None
        candidates = await conn.fetch(
            "SELECT candidate_json FROM suggestion_candidates WHERE suggestion_set_id = $1 ORDER BY rank_position",
            suggestion_set_id,
        )
        return _set_from_rows(row, list(candidates))

    async def get_set(self, workspace_id: str, suggestion_set_id: str) -> SuggestionSet | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await self._load_set(conn, workspace_id, suggestion_set_id)

    async def list_events(self, workspace_id: str) -> list[RecommendationEvent]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT event_json FROM recommendation_events WHERE workspace_id = $1 ORDER BY occurred_at, event_id",
                workspace_id,
            )
        return [_event_from_row(row) for row in rows]

    async def append_event(self, event: RecommendationEvent) -> RecommendationEvent:
        """Append a server-authored event in one transaction.

        This path is used for provider failures where no SuggestionSet exists.
        The workspace lock makes deletion/races fail closed and prevents an
        orphan event from being acknowledged.
        """
        _validate_system_event(event)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT workspace_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                event.workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            await self._insert_event(conn, event)
        return event

    async def _insert_event(self, conn: Any, event: RecommendationEvent) -> None:
        await conn.execute(
            """
            INSERT INTO recommendation_events (
                event_id, session_id, workspace_id, actor_id, event_type,
                suggestion_set_id, candidate_id, revision_before, revision_after,
                context_hash, policy_version, provider_snapshot_id, rank_position,
                latency_ms, reason_code, occurred_at, event_json
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17::jsonb)
            """,
            event.event_id,
            event.session_id,
            event.workspace_id,
            event.actor_id,
            event.event_type.value,
            event.suggestion_set_id,
            event.candidate_id,
            event.revision_before,
            event.revision_after,
            event.context_hash,
            event.policy_version,
            event.provider_snapshot_id,
            event.rank_position,
            event.latency_ms,
            event.reason_code,
            event.occurred_at,
            json.dumps(event.model_dump(mode="json"), ensure_ascii=False),
        )

    async def append_event_command(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str | None,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        builder: EventBuilder,
    ) -> RecommendationEventCommandResult:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace_row = await conn.fetchrow(
                "SELECT * FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                workspace_id,
            )
            if workspace_row is None:
                raise ResourceNotFound("workspace does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM recommendation_event_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                workspace_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different recommendation event request"
                    )
                replay = RecommendationEventCommandResult.model_validate(_json(existing["response_json"]))
                return replay.model_copy(update={"idempotent_replay": True})

            suggestion_set = await self._load_set(conn, workspace_id, suggestion_set_id)
            if suggestion_set is None:
                raise ResourceNotFound("suggestion set does not exist")
            candidate = None
            if candidate_id is not None:
                candidate = next(
                    (item for item in suggestion_set.candidates if item.candidate_id == candidate_id),
                    None,
                )
                if candidate is None:
                    raise ResourceNotFound("suggestion candidate does not exist")
            workspace = _workspace_from_row(workspace_row)
            event = builder(suggestion_set, candidate, workspace)
            _validate_frozen_event_context(event, suggestion_set, candidate, workspace, actor_user_id)
            result = RecommendationEventCommandResult(event=event)
            await self._insert_event(conn, event)
            await conn.execute(
                """
                INSERT INTO recommendation_event_commands (
                    command_id, workspace_id, suggestion_set_id, candidate_id,
                    event_type, actor_user_id, request_hash, idempotency_key,
                    response_json, created_at
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,NOW())
                """,
                event.event_id,
                workspace_id,
                suggestion_set_id,
                candidate_id,
                event.event_type.value,
                actor_user_id,
                request_hash,
                idempotency_key,
                json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
            )
            return result

    async def accept_candidate(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        base_revision: int,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        builder: AcceptBuilder,
    ) -> AcceptSuggestionResult:
        pool = await self._get_pool()
        conflict: RevisionConflictError | None = None
        result: AcceptSuggestionResult | None = None
        async with pool.acquire() as conn:
            async with conn.transaction():
                workspace_row = await conn.fetchrow(
                    "SELECT * FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                    workspace_id,
                )
                if workspace_row is None:
                    raise ResourceNotFound("workspace does not exist")
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, response_json FROM suggestion_accept_commands
                    WHERE workspace_id = $1 AND idempotency_key = $2
                    """,
                    workspace_id,
                    idempotency_key,
                )
                if existing is not None:
                    if existing["request_hash"].strip() != request_hash:
                        raise IdempotencyKeyReusedError(
                            "idempotency key was already used with a different suggestion accept request"
                        )
                    replay = AcceptSuggestionResult.model_validate(_json(existing["response_json"]))
                    return replay.model_copy(update={"idempotent_replay": True})

                suggestion_set = await self._load_set(conn, workspace_id, suggestion_set_id)
                if suggestion_set is None:
                    raise ResourceNotFound("suggestion set does not exist")
                candidate = next((item for item in suggestion_set.candidates if item.candidate_id == candidate_id), None)
                if candidate is None:
                    raise ResourceNotFound("suggestion candidate does not exist")
                workspace = _workspace_from_row(workspace_row)
                if workspace.current_itinerary_revision != base_revision:
                    from app.suggestions.service import revision_conflict_event

                    conflict_event = revision_conflict_event(
                        suggestion_set,
                        candidate,
                        actor_user_id=actor_user_id,
                        expected_revision=base_revision,
                        actual_revision=workspace.current_itinerary_revision,
                    )
                    await self._insert_event(conn, conflict_event)
                    conflict = RevisionConflictError(
                        "suggestion accept base revision is stale",
                        context={
                            "expected_revision": base_revision,
                            "actual_revision": workspace.current_itinerary_revision,
                        },
                    )
                else:
                    revision_row = await conn.fetchrow(
                        "SELECT * FROM itinerary_revisions WHERE workspace_id = $1 AND revision = $2",
                        workspace_id,
                        base_revision,
                    )
                    if revision_row is None:
                        raise ResourceNotFound("base revision does not exist")
                    base = _revision_from_row(revision_row)
                    result = builder(suggestion_set, candidate, workspace, base)
                    await insert_revision_record(conn, result.revision)
                    place_data = _place_record(candidate)
                    await conn.execute(
                        """
                        INSERT INTO room_places (room_id, place_id, place_data, voted_by, added_at, updated_at)
                        VALUES ($1,$2,$3::jsonb,'{}'::text[],NOW(),NOW())
                        ON CONFLICT (room_id, place_id) DO UPDATE
                        SET place_data = EXCLUDED.place_data, updated_at = NOW()
                        """,
                        workspace.room_id,
                        candidate.canonical_place.place_id,
                        json.dumps(place_data, ensure_ascii=False),
                    )
                    receipt_json = candidate.provider_receipt.model_dump(mode="json")
                    await conn.execute(
                        """
                        INSERT INTO itinerary_place_receipts (
                            workspace_id, itinerary_revision, stop_id, place_id,
                            receipt_hash, receipt_json, place_data_json
                        ) VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb)
                        """,
                        workspace_id,
                        result.new_revision,
                        result.stop_id,
                        candidate.canonical_place.place_id,
                        sha256_canonical(receipt_json),
                        json.dumps(receipt_json, ensure_ascii=False),
                        json.dumps(place_data, ensure_ascii=False),
                    )
                    await conn.execute(
                        """
                        UPDATE trip_workspaces SET current_itinerary_revision=$2,
                            current_report_id=NULL, status='DRAFT', updated_at=NOW()
                        WHERE workspace_id=$1
                        """,
                        workspace_id,
                        result.new_revision,
                    )
                    await self._insert_event(conn, result.event)
                    await conn.execute(
                        """
                        INSERT INTO suggestion_accept_commands (
                            command_id, workspace_id, suggestion_set_id, candidate_id,
                            base_revision, result_revision, actor_user_id, request_hash,
                            idempotency_key, response_json
                        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10::jsonb)
                        """,
                        result.event.event_id,
                        workspace_id,
                        suggestion_set_id,
                        candidate_id,
                        base_revision,
                        result.new_revision,
                        actor_user_id,
                        request_hash,
                        idempotency_key,
                        json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
                    )
            if conflict is not None:
                raise conflict
        if result is None:
            raise RuntimeError("suggestion accept completed without a result")
        return result

    async def undo_accepted_candidate(
        self,
        *,
        command: ItineraryEditCommand,
        target_revision: int,
        idempotency_key: str,
        request_hash: str,
        revision_builder: SuggestionUndoBuilder,
        event_builder: SuggestionUndoEventBuilder,
    ) -> ItineraryPatchResult | None:
        """Atomically Undo one accepted candidate and append its frozen event."""
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace_row = await conn.fetchrow(
                "SELECT * FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                command.workspace_id,
            )
            if workspace_row is None:
                raise ResourceNotFound("workspace does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM itinerary_edit_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                command.workspace_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different itinerary request"
                    )
                replay = ItineraryPatchResult.model_validate(_json(existing["response_json"]))
                return replay.model_copy(update={"idempotent_replay": True})

            workspace = _workspace_from_row(workspace_row)
            if workspace.current_itinerary_revision != command.base_revision:
                raise RevisionConflictError(
                    "base revision is stale",
                    context={
                        "expected_revision": command.base_revision,
                        "actual_revision": workspace.current_itinerary_revision,
                    },
                )
            base_row = await conn.fetchrow(
                "SELECT * FROM itinerary_revisions WHERE workspace_id = $1 AND revision = $2",
                command.workspace_id,
                command.base_revision,
            )
            if base_row is None:
                raise ResourceNotFound("base revision does not exist")
            base = _revision_from_row(base_row)
            accepted_ids = _accepted_suggestion_ids(base, target_revision)
            if accepted_ids is None:
                return None
            suggestion_set_id, candidate_id = accepted_ids
            target_row = await conn.fetchrow(
                "SELECT * FROM itinerary_revisions WHERE workspace_id = $1 AND revision = $2",
                command.workspace_id,
                target_revision,
            )
            if target_row is None:
                raise InvalidEditCommandError("UNDO target revision does not exist")
            target = _revision_from_row(target_row)
            suggestion_set = await self._load_set(conn, command.workspace_id, suggestion_set_id)
            if suggestion_set is None:
                raise InvalidEditCommandError("accepted suggestion set is absent from its workspace lineage")
            candidate = next(
                (item for item in suggestion_set.candidates if item.candidate_id == candidate_id),
                None,
            )
            if candidate is None:
                raise InvalidEditCommandError("accepted suggestion candidate is absent from its frozen set")
            accepted_rows = await conn.fetch(
                """
                SELECT event_json FROM recommendation_events
                WHERE workspace_id = $1 AND suggestion_set_id = $2 AND candidate_id = $3
                  AND event_type = 'candidate_accepted' AND revision_after = $4
                ORDER BY occurred_at, event_id
                LIMIT 2
                """,
                command.workspace_id,
                suggestion_set_id,
                candidate_id,
                base.revision,
            )
            if len(accepted_rows) != 1:
                raise InvalidEditCommandError(
                    "accepted suggestion revision must have exactly one source acceptance event",
                    context={"event_count": len(accepted_rows)},
                )
            accepted_event = _event_from_row(accepted_rows[0])
            stop_id = _validate_acceptance_lineage(
                accepted_event=accepted_event,
                suggestion_set=suggestion_set,
                candidate=candidate,
                base=base,
                target=target,
            )
            revision, result, next_status = revision_builder(workspace, base, target)
            event = event_builder(
                suggestion_set,
                candidate,
                workspace,
                base,
                target,
                revision,
                accepted_event,
            )
            _validate_frozen_event_context(event, suggestion_set, candidate, workspace, command.actor_user_id)
            if (
                event.revision_before != base.revision
                or event.revision_after != revision.revision
                or event.payload.get("source_accept_event_id") != accepted_event.event_id
                or event.payload.get("stop_id") != stop_id
            ):
                raise ValueError("stop_undone event differs from accepted revision lineage")

            await insert_revision_record(conn, revision)
            response_json = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
            await conn.execute(
                """
                INSERT INTO itinerary_edit_commands (
                    command_id, workspace_id, base_revision, result_revision, actor_user_id,
                    operation, payload_json, request_hash, idempotency_key, client_timestamp,
                    response_json
                ) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb,$8,$9,$10,$11::jsonb)
                """,
                command.command_id,
                command.workspace_id,
                command.base_revision,
                revision.revision,
                command.actor_user_id,
                command.operation.value,
                json.dumps(command.payload, ensure_ascii=False),
                request_hash,
                idempotency_key,
                command.client_timestamp,
                response_json,
            )
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision=$2, current_report_id=NULL,
                    status=$3, updated_at=NOW()
                WHERE workspace_id=$1
                """,
                command.workspace_id,
                revision.revision,
                next_status.value,
            )
            await self._insert_event(conn, event)
            await conn.execute(
                """
                INSERT INTO suggestion_undo_links (
                    undo_command_id, event_id, source_accept_event_id, workspace_id,
                    suggestion_set_id, candidate_id, accepted_revision,
                    target_revision, result_revision
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                """,
                command.command_id,
                event.event_id,
                accepted_event.event_id,
                command.workspace_id,
                suggestion_set_id,
                candidate_id,
                base.revision,
                target.revision,
                revision.revision,
            )
            return result


class InMemorySuggestionRepository:
    """Atomic deterministic contract sharing the authoritative itinerary store."""

    def __init__(
        self,
        itinerary_repository: InMemoryItineraryRepository,
        *,
        place_records: dict[str, dict[str, dict[str, Any]]] | None = None,
        fail_at: str | None = None,
    ):
        self.itineraries = itinerary_repository
        self.sets: dict[tuple[str, str], SuggestionSet] = {}
        self.events: list[RecommendationEvent] = []
        self.commands: dict[tuple[str, str], tuple[str, AcceptSuggestionResult]] = {}
        self.event_commands: dict[
            tuple[str, str], tuple[str, RecommendationEventCommandResult]
        ] = {}
        self.place_records = place_records if place_records is not None else {}
        self.receipts: dict[tuple[str, int, str], dict[str, Any]] = {}
        self.undo_links: dict[str, dict[str, Any]] = {}
        self.fail_at = fail_at
        # Suggestion acceptance and ordinary itinerary edits mutate the same
        # workspace/revision pointer.  They must therefore share one lock,
        # mirroring PostgreSQL's trip_workspaces FOR UPDATE transaction.
        self._lock = itinerary_repository._lock

    @staticmethod
    def _clone_set(value: SuggestionSet) -> SuggestionSet:
        return SuggestionSet.model_validate(value.model_dump(mode="python"))

    @staticmethod
    def _clone_event(value: RecommendationEvent) -> RecommendationEvent:
        return RecommendationEvent.model_validate(value.model_dump(mode="python"))

    @staticmethod
    def _clone_event_result(
        value: RecommendationEventCommandResult,
    ) -> RecommendationEventCommandResult:
        return RecommendationEventCommandResult.model_validate(value.model_dump(mode="python"))

    @staticmethod
    def _clone_accept_result(value: AcceptSuggestionResult) -> AcceptSuggestionResult:
        return AcceptSuggestionResult.model_validate(value.model_dump(mode="python"))

    def _maybe_fail(self, stage: str) -> None:
        if self.fail_at == stage:
            raise RuntimeError(f"controlled suggestion repository failure at {stage}")

    async def create_set(self, suggestion_set: SuggestionSet, shown_event: RecommendationEvent) -> SuggestionSet:
        async with self._lock:
            workspace = self.itineraries.workspaces.get(suggestion_set.workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            if workspace.current_itinerary_revision != suggestion_set.base_revision:
                raise RevisionConflictError(
                    "suggestion set base revision is stale",
                    context={
                        "expected_revision": suggestion_set.base_revision,
                        "actual_revision": workspace.current_itinerary_revision,
                    },
                )
            key = (suggestion_set.workspace_id, suggestion_set.suggestion_set_id)
            if key in self.sets:
                raise ValueError("suggestion set already exists")
            stored_set = self._clone_set(suggestion_set)
            stored_event = self._clone_event(shown_event)
            self.sets[key] = stored_set
            self.events.append(stored_event)
            return self._clone_set(stored_set)

    async def get_set(self, workspace_id: str, suggestion_set_id: str) -> SuggestionSet | None:
        value = self.sets.get((workspace_id, suggestion_set_id))
        return self._clone_set(value) if value is not None else None

    async def list_events(self, workspace_id: str) -> list[RecommendationEvent]:
        return [
            self._clone_event(event)
            for event in self.events
            if event.workspace_id == workspace_id
        ]

    async def append_event(self, event: RecommendationEvent) -> RecommendationEvent:
        async with self._lock:
            _validate_system_event(event)
            if event.workspace_id not in self.itineraries.workspaces:
                raise ResourceNotFound("workspace does not exist")
            event_count = len(self.events)
            try:
                self.events.append(self._clone_event(event))
                self._maybe_fail("after_system_event")
            except Exception:
                del self.events[event_count:]
                raise
            return self._clone_event(event)

    async def append_event_command(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str | None,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        builder: EventBuilder,
    ) -> RecommendationEventCommandResult:
        async with self._lock:
            command_key = (workspace_id, idempotency_key)
            existing = self.event_commands.get(command_key)
            if existing is not None:
                existing_hash, replay = existing
                if existing_hash != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different recommendation event request"
                    )
                return self._clone_event_result(replay).model_copy(update={"idempotent_replay": True})
            workspace = self.itineraries.workspaces.get(workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            suggestion_set = self.sets.get((workspace_id, suggestion_set_id))
            if suggestion_set is None:
                raise ResourceNotFound("suggestion set does not exist")
            candidate = None
            if candidate_id is not None:
                candidate = next(
                    (item for item in suggestion_set.candidates if item.candidate_id == candidate_id),
                    None,
                )
                if candidate is None:
                    raise ResourceNotFound("suggestion candidate does not exist")
            event = builder(suggestion_set, candidate, workspace)
            _validate_frozen_event_context(event, suggestion_set, candidate, workspace, actor_user_id)
            result = RecommendationEventCommandResult(event=event)
            event_count = len(self.events)
            try:
                self.events.append(self._clone_event(event))
                self._maybe_fail("after_interaction_event")
                self.event_commands[command_key] = (request_hash, self._clone_event_result(result))
                self._maybe_fail("after_interaction_command")
            except Exception:
                del self.events[event_count:]
                self.event_commands.pop(command_key, None)
                raise
            return self._clone_event_result(result)

    async def accept_candidate(
        self,
        *,
        workspace_id: str,
        suggestion_set_id: str,
        candidate_id: str,
        base_revision: int,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        builder: AcceptBuilder,
    ) -> AcceptSuggestionResult:
        async with self._lock:
            command_key = (workspace_id, idempotency_key)
            existing = self.commands.get(command_key)
            if existing is not None:
                existing_hash, replay = existing
                if existing_hash != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different suggestion accept request"
                    )
                return self._clone_accept_result(replay).model_copy(update={"idempotent_replay": True})
            suggestion_set = self.sets.get((workspace_id, suggestion_set_id))
            if suggestion_set is None:
                raise ResourceNotFound("suggestion set does not exist")
            candidate = next((item for item in suggestion_set.candidates if item.candidate_id == candidate_id), None)
            if candidate is None:
                raise ResourceNotFound("suggestion candidate does not exist")
            workspace = self.itineraries.workspaces.get(workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            if workspace.current_itinerary_revision != base_revision:
                from app.suggestions.service import revision_conflict_event

                self.events.append(revision_conflict_event(
                    suggestion_set,
                    candidate,
                    actor_user_id=actor_user_id,
                    expected_revision=base_revision,
                    actual_revision=workspace.current_itinerary_revision,
                ))
                raise RevisionConflictError(
                    "suggestion accept base revision is stale",
                    context={
                        "expected_revision": base_revision,
                        "actual_revision": workspace.current_itinerary_revision,
                    },
                )
            base = self.itineraries.revisions.get((workspace_id, base_revision))
            if base is None:
                raise ResourceNotFound("base revision does not exist")
            result = builder(suggestion_set, candidate, workspace, base)
            stored_result = self._clone_accept_result(result)

            old_workspace = workspace
            revision_key = (workspace_id, result.new_revision)
            old_revision = self.itineraries.revisions.get(revision_key)
            old_places = dict(self.place_records.get(workspace_id, {}))
            receipt_key = (workspace_id, result.new_revision, result.stop_id)
            old_receipt = self.receipts.get(receipt_key)
            event_count = len(self.events)
            try:
                self.itineraries.revisions[revision_key] = stored_result.revision
                self._maybe_fail("after_revision")
                self.place_records.setdefault(workspace_id, {})[candidate.canonical_place.place_id] = _place_record(candidate)
                self._maybe_fail("after_place_projection")
                self.receipts[receipt_key] = candidate.provider_receipt.model_dump(mode="json")
                self._maybe_fail("after_receipt")
                self.itineraries.workspaces[workspace_id] = workspace.model_copy(update={
                    "current_itinerary_revision": result.new_revision,
                    "current_report_id": None,
                    "status": WorkspaceStatus.DRAFT,
                })
                self._maybe_fail("after_workspace_pointer")
                self.events.append(self._clone_event(stored_result.event))
                self._maybe_fail("after_event")
                self.commands[command_key] = (request_hash, stored_result)
                self._maybe_fail("after_command")
            except Exception:
                if old_revision is None:
                    self.itineraries.revisions.pop(revision_key, None)
                else:
                    self.itineraries.revisions[revision_key] = old_revision
                self.itineraries.workspaces[workspace_id] = old_workspace
                self.place_records[workspace_id] = old_places
                if old_receipt is None:
                    self.receipts.pop(receipt_key, None)
                else:
                    self.receipts[receipt_key] = old_receipt
                del self.events[event_count:]
                self.commands.pop(command_key, None)
                raise
            return self._clone_accept_result(stored_result)

    async def undo_accepted_candidate(
        self,
        *,
        command: ItineraryEditCommand,
        target_revision: int,
        idempotency_key: str,
        request_hash: str,
        revision_builder: SuggestionUndoBuilder,
        event_builder: SuggestionUndoEventBuilder,
    ) -> ItineraryPatchResult | None:
        async with self._lock:
            command_key = (command.workspace_id, idempotency_key)
            existing = self.itineraries.commands.get(command_key)
            if existing is not None:
                existing_hash, replay = existing
                if existing_hash != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different itinerary request"
                    )
                return replay.model_copy(deep=True, update={"idempotent_replay": True})
            workspace = self.itineraries.workspaces.get(command.workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            if workspace.current_itinerary_revision != command.base_revision:
                raise RevisionConflictError(
                    "base revision is stale",
                    context={
                        "expected_revision": command.base_revision,
                        "actual_revision": workspace.current_itinerary_revision,
                    },
                )
            base = self.itineraries.revisions.get((command.workspace_id, command.base_revision))
            if base is None:
                raise ResourceNotFound("base revision does not exist")
            accepted_ids = _accepted_suggestion_ids(base, target_revision)
            if accepted_ids is None:
                return None
            suggestion_set_id, candidate_id = accepted_ids
            target = self.itineraries.revisions.get((command.workspace_id, target_revision))
            if target is None:
                raise InvalidEditCommandError("UNDO target revision does not exist")
            suggestion_set = self.sets.get((command.workspace_id, suggestion_set_id))
            if suggestion_set is None:
                raise InvalidEditCommandError("accepted suggestion set is absent from its workspace lineage")
            candidate = next(
                (item for item in suggestion_set.candidates if item.candidate_id == candidate_id),
                None,
            )
            if candidate is None:
                raise InvalidEditCommandError("accepted suggestion candidate is absent from its frozen set")
            accepted_events = [
                event
                for event in self.events
                if event.workspace_id == command.workspace_id
                and event.suggestion_set_id == suggestion_set_id
                and event.candidate_id == candidate_id
                and event.event_type.value == "candidate_accepted"
                and event.revision_after == base.revision
            ]
            if len(accepted_events) != 1:
                raise InvalidEditCommandError(
                    "accepted suggestion revision must have exactly one source acceptance event",
                    context={"event_count": len(accepted_events)},
                )
            accepted_event = accepted_events[0]
            stop_id = _validate_acceptance_lineage(
                accepted_event=accepted_event,
                suggestion_set=suggestion_set,
                candidate=candidate,
                base=base,
                target=target,
            )
            revision, result, next_status = revision_builder(workspace, base, target)
            event = event_builder(
                suggestion_set,
                candidate,
                workspace,
                base,
                target,
                revision,
                accepted_event,
            )
            _validate_frozen_event_context(event, suggestion_set, candidate, workspace, command.actor_user_id)
            if (
                event.revision_before != base.revision
                or event.revision_after != revision.revision
                or event.payload.get("source_accept_event_id") != accepted_event.event_id
                or event.payload.get("stop_id") != stop_id
            ):
                raise ValueError("stop_undone event differs from accepted revision lineage")

            revision_key = (command.workspace_id, revision.revision)
            old_revision = self.itineraries.revisions.get(revision_key)
            old_workspace = workspace
            event_count = len(self.events)
            old_link = self.undo_links.get(command.command_id)
            try:
                self.itineraries.revisions[revision_key] = revision
                self._maybe_fail("after_undo_revision")
                self.itineraries.workspaces[command.workspace_id] = workspace.model_copy(update={
                    "current_itinerary_revision": revision.revision,
                    "current_report_id": None,
                    "status": next_status,
                })
                self._maybe_fail("after_undo_workspace_pointer")
                stored_result = result.model_copy(deep=True)
                self.itineraries.commands[command_key] = (request_hash, stored_result)
                self._maybe_fail("after_undo_command")
                self.events.append(self._clone_event(event))
                self._maybe_fail("after_undo_event")
                self.undo_links[command.command_id] = {
                    "event_id": event.event_id,
                    "source_accept_event_id": accepted_event.event_id,
                    "workspace_id": command.workspace_id,
                    "suggestion_set_id": suggestion_set_id,
                    "candidate_id": candidate_id,
                    "accepted_revision": base.revision,
                    "target_revision": target.revision,
                    "result_revision": revision.revision,
                }
                self._maybe_fail("after_undo_link")
            except Exception:
                if old_revision is None:
                    self.itineraries.revisions.pop(revision_key, None)
                else:
                    self.itineraries.revisions[revision_key] = old_revision
                self.itineraries.workspaces[command.workspace_id] = old_workspace
                self.itineraries.commands.pop(command_key, None)
                del self.events[event_count:]
                if old_link is None:
                    self.undo_links.pop(command.command_id, None)
                else:
                    self.undo_links[command.command_id] = old_link
                raise
            return result.model_copy(deep=True)
