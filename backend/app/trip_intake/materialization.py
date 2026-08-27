from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.db.connection import get_pool
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.importing.parser import ItineraryTextParser
from app.importing.repositories import PostgresImportRepository
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound, RevisionConflictError
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripDateRange, TripWorkspace
from app.trip_check.briefs import PostgresTripBriefRepository, with_brief_content_hash
from app.trip_check.models import (
    NO_PREFERENCE,
    UNSPECIFIED,
    AccommodationBrief,
    ArrivalDeparture,
    BriefFieldConfirmation,
    BriefFieldOrigin,
    BriefFieldProvenance,
    BriefHardness,
    BriefRequirement,
    BriefSourceSpan,
    TripBriefRevision,
    TripBriefStatus,
)
from app.trip_intake.models import (
    ConfirmedField,
    IntakeStatus,
    LocationMention,
    PreferencePolarity,
    PreferenceStatus,
    TripIntakeRevision,
)
from app.trip_intake.repository import TripIntakeRepository


class TripIntakeMaterialization(BaseModel):
    model_config = ConfigDict(frozen=True)

    materialization_id: str = Field(min_length=1)
    intake_id: str = Field(min_length=1)
    intake_revision: int = Field(gt=0)
    workspace: TripWorkspace
    brief: TripBriefRevision
    itinerary_import: ItineraryImport
    created_at: datetime


class MaterializationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    materialization: TripIntakeMaterialization
    idempotent_replay: bool = False
    resolution_dispatch: str = Field(pattern=r"^(NOT_CONFIGURED|SUCCEEDED|FAILED_RETRYABLE)$")


class TripIntakeMaterializationRepository(Protocol):
    async def materialize(
        self,
        *,
        intake: TripIntakeRevision,
        workspace: TripWorkspace,
        brief: TripBriefRevision,
        itinerary_import: ItineraryImport,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripIntakeMaterialization, bool]: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresTripIntakeMaterializationRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def materialize(
        self,
        *,
        intake: TripIntakeRevision,
        workspace: TripWorkspace,
        brief: TripBriefRevision,
        itinerary_import: ItineraryImport,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripIntakeMaterialization, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM trip_intake_materializations
                WHERE intake_id = $1 AND idempotency_key = $2
                """,
                intake.intake_id,
                idempotency_key,
            )
            if existing:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different materialization request"
                    )
                return TripIntakeMaterialization.model_validate(
                    _json_value(existing["response_json"])
                ), True

            locked = await conn.fetchrow(
                """
                SELECT revision, status FROM trip_intake_revisions
                WHERE intake_id = $1 ORDER BY revision DESC LIMIT 1 FOR UPDATE
                """,
                intake.intake_id,
            )
            if locked is None:
                raise ResourceNotFound("trip intake does not exist")
            if locked["revision"] != intake.revision:
                raise RevisionConflictError(
                    "trip intake revision is stale",
                    context={
                        "expected_revision": intake.revision,
                        "actual_revision": locked["revision"],
                    },
                )
            if locked["status"] != IntakeStatus.READY.value:
                raise ValueError("trip intake must be READY before materialization")

            prior = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM trip_intake_materializations
                WHERE intake_id = $1 AND intake_revision = $2
                """,
                intake.intake_id,
                intake.revision,
            )
            if prior:
                if prior["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError(
                        "intake revision was already materialized with a different request"
                    )
                return TripIntakeMaterialization.model_validate(_json_value(prior["response_json"])), True

            await conn.execute(
                """
                INSERT INTO trip_workspaces (
                    workspace_id, room_id, city, trip_start_date, trip_end_date,
                    current_itinerary_revision, current_task_spec_revision,
                    current_member_constraint_revision, current_report_id,
                    current_import_id, status, created_by, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, NULL, NULL, NULL, NULL, NULL, $6, $7, $8, $9)
                """,
                workspace.workspace_id,
                workspace.room_id,
                workspace.city,
                workspace.trip_date_range.start,
                workspace.trip_date_range.end,
                workspace.status.value,
                workspace.created_by,
                workspace.created_at,
                workspace.updated_at,
            )
            await PostgresImportRepository(pool).create_import_bundle(
                itinerary_import,
                basis={"current_import_id": None},
                conn=conn,
            )
            await PostgresTripBriefRepository(pool).save_import_brief(brief, conn=conn)
            materialized_workspace = workspace.model_copy(
                update={
                    "current_import_id": itinerary_import.import_id,
                    "current_brief_id": brief.brief_id,
                    "current_trip_brief_revision": brief.revision,
                }
            )
            receipt = TripIntakeMaterialization(
                materialization_id=str(uuid4()),
                intake_id=intake.intake_id,
                intake_revision=intake.revision,
                workspace=materialized_workspace,
                brief=brief,
                itinerary_import=itinerary_import,
                created_at=datetime.now(timezone.utc),
            )
            await conn.execute(
                """
                INSERT INTO trip_intake_materializations (
                    materialization_id, intake_id, intake_revision, actor_user_id,
                    idempotency_key, request_hash, workspace_id, brief_id,
                    brief_revision, import_id, response_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11::jsonb, $12)
                """,
                receipt.materialization_id,
                intake.intake_id,
                intake.revision,
                actor_user_id,
                idempotency_key,
                request_hash,
                workspace.workspace_id,
                brief.brief_id,
                brief.revision,
                itinerary_import.import_id,
                json.dumps(receipt.model_dump(mode="json"), ensure_ascii=False),
                receipt.created_at,
            )
        return receipt, False


class InMemoryTripIntakeMaterializationRepository:
    def __init__(self):
        self.by_command: dict[tuple[str, str], tuple[str, TripIntakeMaterialization]] = {}
        self.by_revision: dict[tuple[str, int], tuple[str, TripIntakeMaterialization]] = {}

    async def materialize(
        self,
        *,
        intake: TripIntakeRevision,
        workspace: TripWorkspace,
        brief: TripBriefRevision,
        itinerary_import: ItineraryImport,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripIntakeMaterialization, bool]:
        del actor_user_id
        command_key = (intake.intake_id, idempotency_key)
        existing = self.by_command.get(command_key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError(
                    "idempotency key was already used with a different materialization request"
                )
            return existing[1], True
        revision_key = (intake.intake_id, intake.revision)
        existing = self.by_revision.get(revision_key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError(
                    "intake revision was already materialized with a different request"
                )
            return existing[1], True
        materialized_workspace = workspace.model_copy(
            update={
                "current_import_id": itinerary_import.import_id,
                "current_brief_id": brief.brief_id,
                "current_trip_brief_revision": brief.revision,
            }
        )
        receipt = TripIntakeMaterialization(
            materialization_id=str(uuid4()),
            intake_id=intake.intake_id,
            intake_revision=intake.revision,
            workspace=materialized_workspace,
            brief=brief,
            itinerary_import=itinerary_import,
            created_at=datetime.now(timezone.utc),
        )
        self.by_command[command_key] = (request_hash, receipt)
        self.by_revision[revision_key] = (request_hash, receipt)
        return receipt, False


def _source_spans(spans: list[Any]) -> list[BriefSourceSpan]:
    return [BriefSourceSpan(source_id=span.source_id, start=span.start, end=span.end) for span in spans]


def _primary_location(intake: TripIntakeRevision) -> LocationMention:
    mention = next(
        (
            item
            for item in intake.extraction.locations.mentions
            if item.mention_id == intake.extraction.locations.primary_mention_id
        ),
        None,
    )
    if mention is None or not mention.normalized_name or mention.country_code != "CN":
        raise ValueError("confirmed intake lacks a domestic primary city")
    return mention


def _build_brief(
    intake: TripIntakeRevision,
    workspace: TripWorkspace,
    *,
    actor_user_id: str,
    now: datetime,
) -> TripBriefRevision:
    location = _primary_location(intake)
    date_range = intake.extraction.temporal.date_range
    party = intake.extraction.party_size.total
    if date_range is None or date_range.start.year is None or date_range.end.year is None:
        raise ValueError("confirmed intake lacks a complete date range")
    if party.min is None or party.min != party.max or party.min <= 0:
        raise ValueError("confirmed intake lacks an exact positive party size")

    confirmed = BriefFieldConfirmation.CONFIRMED
    core: dict[str, BriefFieldProvenance] = {
        "city": BriefFieldProvenance(
            source_spans=_source_spans(location.evidence),
            confidence=1,
            origin=BriefFieldOrigin.USER_CONFIRMED,
            confirmation=confirmed,
            hardness=BriefHardness.HARD,
        ),
        "date_range": BriefFieldProvenance(
            source_spans=_source_spans(date_range.evidence),
            confidence=1,
            origin=BriefFieldOrigin.USER_CONFIRMED,
            confirmation=confirmed,
            hardness=BriefHardness.HARD,
        ),
        "traveler_count": BriefFieldProvenance(
            source_spans=_source_spans(party.evidence),
            confidence=1,
            origin=BriefFieldOrigin.USER_CONFIRMED,
            confirmation=confirmed,
            hardness=BriefHardness.HARD,
        ),
    }
    for field_name in (
        "arrival",
        "departure",
        "accommodation",
        "transport_modes",
        "transport_restrictions",
        "budget",
        "dining_style",
        "lodging_style",
        "dietary_restrictions",
        "daily_pace",
        "activity_intensity",
    ):
        core[field_name] = BriefFieldProvenance(
            confidence=1,
            origin=BriefFieldOrigin.UNSPECIFIED,
            confirmation=BriefFieldConfirmation.UNCONFIRMED,
            hardness=BriefHardness.SOFT,
        )

    preferences = intake.extraction.preferences
    explicit_no_preference = (
        ConfirmedField.PREFERENCES in intake.confirmed_fields
        and preferences.status == PreferenceStatus.NO_PREFERENCE
    )
    optional_value = NO_PREFERENCE if explicit_no_preference else UNSPECIFIED
    if explicit_no_preference:
        for field_name in (
            "transport_restrictions",
            "budget",
            "dining_style",
            "lodging_style",
            "dietary_restrictions",
            "daily_pace",
            "activity_intensity",
        ):
            core[field_name] = BriefFieldProvenance(
                source_spans=_source_spans(preferences.no_preference_evidence),
                confidence=1,
                origin=BriefFieldOrigin.USER_CONFIRMED,
                confirmation=confirmed,
                hardness=BriefHardness.NO_PREFERENCE,
            )

    requirements = [
        BriefRequirement(
            category=item.category,
            operator=item.operator.value,
            value=item.value,
            unit=item.unit,
            currency=item.currency,
            applies_to=item.applies_to,
            source_spans=_source_spans(item.evidence),
        )
        for item in preferences.items
        if item.polarity == PreferencePolarity.REQUIREMENT and item.operator is not None
    ]
    brief = TripBriefRevision(
        brief_id=str(uuid4()),
        workspace_id=workspace.workspace_id,
        revision=1,
        content_hash="0" * 64,
        city=location.normalized_name,
        date_range=workspace.trip_date_range,
        traveler_count=party.min,
        arrival=ArrivalDeparture(
            location=(intake.extraction.temporal.arrival.location_text if intake.extraction.temporal.arrival else None),
            notes=(intake.extraction.temporal.arrival.at_text if intake.extraction.temporal.arrival else None),
        ),
        departure=ArrivalDeparture(
            location=(
                intake.extraction.temporal.departure.location_text
                if intake.extraction.temporal.departure
                else None
            ),
            notes=(
                intake.extraction.temporal.departure.at_text
                if intake.extraction.temporal.departure
                else None
            ),
        ),
        accommodation=AccommodationBrief(),
        transport_modes=[],
        transport_restrictions=optional_value,
        budget=optional_value,
        dining_style=optional_value,
        lodging_style=optional_value,
        dietary_restrictions=optional_value,
        daily_pace=optional_value,
        activity_intensity=optional_value,
        requirements=requirements,
        source_intake_id=intake.intake_id,
        source_intake_revision=intake.revision,
        field_provenance=core,
        status=TripBriefStatus.CONFIRMED,
        created_by=actor_user_id,
        created_at=now,
        confirmed_by=actor_user_id,
        confirmed_at=now,
    )
    return with_brief_content_hash(brief)


class TripIntakeMaterializationService:
    def __init__(
        self,
        *,
        intake_repository: TripIntakeRepository,
        materialization_repository: TripIntakeMaterializationRepository,
        parser: ItineraryTextParser | None = None,
        provider_resolution_trigger: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.intake_repository = intake_repository
        self.materialization_repository = materialization_repository
        self.parser = parser or ItineraryTextParser()
        self.provider_resolution_trigger = provider_resolution_trigger

    async def materialize(
        self,
        *,
        intake_id: str,
        revision: int,
        actor_user_id: str,
        idempotency_key: str,
    ) -> MaterializationResult:
        intake = await self.intake_repository.get_revision(intake_id, revision)
        if intake is None:
            raise ResourceNotFound("trip intake revision does not exist")
        if intake.status != IntakeStatus.READY:
            raise ValueError("trip intake must be READY before materialization")
        primary = _primary_location(intake)
        date_range = intake.extraction.temporal.date_range
        if date_range is None or date_range.start.year is None or date_range.end.year is None:
            raise ValueError("trip intake date range is incomplete")
        now = datetime.now(timezone.utc)
        workspace = TripWorkspace(
            workspace_id=str(uuid4()),
            room_id=intake.room_id,
            city=primary.normalized_name or primary.raw_text,
            trip_date_range=TripDateRange(
                start=date(date_range.start.year, date_range.start.month, date_range.start.day),
                end=date(date_range.end.year, date_range.end.month, date_range.end.day),
            ),
            created_by=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        import_id = str(uuid4())
        draft = self.parser.parse(intake.raw_text, import_id=import_id)
        source_type = ImportSourceType(intake.source_type.value)
        itinerary_import = ItineraryImport(
            import_id=import_id,
            workspace_id=workspace.workspace_id,
            source_type=source_type,
            raw_text=intake.raw_text,
            parse_version=self.parser.version,
            status=ImportStatus.PARSED if draft.raw_stops else ImportStatus.FAILED,
            raw_stops=draft.raw_stops,
            member_summary=draft.member_summary,
            parse_errors=draft.errors,
            created_by=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        brief = _build_brief(intake, workspace, actor_user_id=actor_user_id, now=now)
        request_hash = sha256_canonical(
            {
                "operation": "MATERIALIZE",
                "intake_id": intake_id,
                "revision": revision,
                "actor_user_id": actor_user_id,
                "content_hash": intake.content_hash,
            }
        )
        receipt, replayed = await self.materialization_repository.materialize(
            intake=intake,
            workspace=workspace,
            brief=brief,
            itinerary_import=itinerary_import,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
        dispatch = "NOT_CONFIGURED"
        if self.provider_resolution_trigger is not None and receipt.itinerary_import.raw_stops:
            try:
                await self.provider_resolution_trigger(receipt.itinerary_import.import_id)
                dispatch = "SUCCEEDED"
            except Exception:
                dispatch = "FAILED_RETRYABLE"
        return MaterializationResult(
            materialization=receipt,
            idempotent_replay=replayed,
            resolution_dispatch=dispatch,
        )
