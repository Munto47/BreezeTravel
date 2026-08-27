from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.db.connection import get_pool
from app.importing.models import ItineraryImport
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import TripWorkspace
from app.trip_check.errors import TripBriefAlreadyConfirmedError, TripBriefRevisionConflictError
from app.trip_check.models import (
    NO_PREFERENCE,
    UNSPECIFIED,
    AccommodationBrief,
    ArrivalDeparture,
    BriefFieldConfirmation,
    BriefFieldOrigin,
    BriefFieldProvenance,
    BriefHardness,
    BriefSourceSpan,
    TransportMode,
    TripBriefRevision,
    TripBriefStatus,
)


class TripBriefRepository(Protocol):
    async def save_import_brief(
        self,
        brief: TripBriefRevision,
        *,
        conn: Any | None = None,
    ) -> TripBriefRevision: ...

    async def get_brief(self, workspace_id: str, revision: int) -> TripBriefRevision | None: ...

    async def get_latest_brief(self, workspace_id: str) -> TripBriefRevision | None: ...

    async def save_command_revision(
        self,
        brief: TripBriefRevision,
        *,
        expected_revision: int,
        operation: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripBriefRevision, bool]: ...


def _brief_semantic_payload(brief: TripBriefRevision) -> dict[str, Any]:
    payload = brief.model_dump(mode="json")
    for key in (
        "brief_id",
        "workspace_id",
        "revision",
        "parent_revision",
        "content_hash",
        "created_by",
        "created_at",
        "confirmed_by",
        "confirmed_at",
    ):
        payload.pop(key, None)
    return payload


def with_brief_content_hash(brief: TripBriefRevision) -> TripBriefRevision:
    return brief.model_copy(update={"content_hash": sha256_canonical(_brief_semantic_payload(brief))})


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


async def _insert_brief(conn: Any, brief: TripBriefRevision) -> None:
    payload = brief.model_dump(mode="json")
    await conn.execute(
        """
        INSERT INTO trip_brief_revisions (
            brief_id, workspace_id, revision, parent_revision, status, city,
            trip_start_date, trip_end_date, traveler_count, content_json,
            content_hash, created_by, created_at, confirmed_by, confirmed_at,
            source_intake_id, source_intake_revision
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb,
            $11, $12, $13, $14, $15, $16, $17
        )
        """,
        brief.brief_id,
        brief.workspace_id,
        brief.revision,
        brief.parent_revision,
        brief.status.value,
        brief.city,
        brief.date_range.start,
        brief.date_range.end,
        brief.traveler_count,
        json.dumps(payload, ensure_ascii=False),
        brief.content_hash,
        brief.created_by,
        brief.created_at,
        brief.confirmed_by,
        brief.confirmed_at,
        brief.source_intake_id,
        brief.source_intake_revision,
    )
    for field_path, provenance in brief.field_provenance.items():
        spans = provenance.source_spans or [None]
        for source_index, span in enumerate(spans):
            await conn.execute(
                """
                INSERT INTO trip_brief_field_sources (
                    brief_id, brief_revision, field_path, source_index,
                    source_id, span_start, span_end, confidence, origin,
                    confirmation_status, hardness
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                brief.brief_id,
                brief.revision,
                field_path,
                source_index,
                span.source_id if span else None,
                span.start if span else None,
                span.end if span else None,
                provenance.confidence,
                provenance.origin.value,
                provenance.confirmation.value,
                provenance.hardness.value,
            )


class PostgresTripBriefRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def save_import_brief(
        self,
        brief: TripBriefRevision,
        *,
        conn: Any | None = None,
    ) -> TripBriefRevision:
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as owned_conn, owned_conn.transaction():
                return await self.save_import_brief(brief, conn=owned_conn)
        workspace = await conn.fetchrow(
            """
            SELECT current_brief_id, current_trip_brief_revision
            FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE
            """,
            brief.workspace_id,
        )
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        actual_id = workspace["current_brief_id"]
        actual_revision = workspace["current_trip_brief_revision"]
        if actual_id != (brief.brief_id if brief.parent_revision else None) or actual_revision != brief.parent_revision:
            raise TripBriefRevisionConflictError(
                "workspace brief pointer changed before import commit",
                context={"expected_revision": brief.parent_revision, "actual_revision": actual_revision},
            )
        await _insert_brief(conn, brief)
        await conn.execute(
            """
            UPDATE trip_workspaces
            SET current_brief_id = $2, current_trip_brief_revision = $3, updated_at = NOW()
            WHERE workspace_id = $1
            """,
            brief.workspace_id,
            brief.brief_id,
            brief.revision,
        )
        return brief

    async def get_brief(self, workspace_id: str, revision: int) -> TripBriefRevision | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content_json FROM trip_brief_revisions WHERE workspace_id = $1 AND revision = $2",
                workspace_id,
                revision,
            )
        return TripBriefRevision.model_validate(_json_value(row["content_json"])) if row else None

    async def get_latest_brief(self, workspace_id: str) -> TripBriefRevision | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT brief.content_json
                FROM trip_workspaces AS workspace
                JOIN trip_brief_revisions AS brief
                  ON brief.brief_id = workspace.current_brief_id
                 AND brief.revision = workspace.current_trip_brief_revision
                WHERE workspace.workspace_id = $1
                """,
                workspace_id,
            )
        return TripBriefRevision.model_validate(_json_value(row["content_json"])) if row else None

    async def save_command_revision(
        self,
        brief: TripBriefRevision,
        *,
        expected_revision: int,
        operation: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripBriefRevision, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                """
                SELECT current_brief_id, current_trip_brief_revision
                FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE
                """,
                brief.workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM trip_check_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                brief.workspace_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
                return TripBriefRevision.model_validate(_json_value(existing["response_json"])), True
            actual_revision = workspace["current_trip_brief_revision"]
            if actual_revision != expected_revision or workspace["current_brief_id"] != brief.brief_id:
                raise TripBriefRevisionConflictError(
                    "trip brief revision does not match current workspace state",
                    context={"expected_revision": expected_revision, "actual_revision": actual_revision},
                )
            await _insert_brief(conn, brief)
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_trip_brief_revision = $2, updated_at = NOW()
                WHERE workspace_id = $1
                """,
                brief.workspace_id,
                brief.revision,
            )
            response_json = brief.model_dump(mode="json")
            await conn.execute(
                """
                INSERT INTO trip_check_commands (
                    command_id, workspace_id, operation, actor_user_id,
                    idempotency_key, request_hash, response_status,
                    response_headers_json, response_json
                ) VALUES ($1, $2, $3, $4, $5, $6, 200, $7::jsonb, $8::jsonb)
                """,
                str(uuid4()),
                brief.workspace_id,
                operation,
                actor_user_id,
                idempotency_key,
                request_hash,
                json.dumps({"ETag": f'"{brief.revision}"'}, ensure_ascii=False),
                json.dumps(response_json, ensure_ascii=False),
            )
            return brief, False


class InMemoryTripBriefRepository:
    def __init__(self):
        self.briefs: dict[tuple[str, int], TripBriefRevision] = {}
        self.current: dict[str, tuple[str, int]] = {}
        self.commands: dict[tuple[str, str], tuple[str, TripBriefRevision]] = {}

    async def save_import_brief(
        self,
        brief: TripBriefRevision,
        *,
        conn: Any | None = None,
    ) -> TripBriefRevision:
        current = self.current.get(brief.workspace_id)
        expected = (brief.brief_id, brief.parent_revision) if brief.parent_revision else None
        if current != expected:
            raise TripBriefRevisionConflictError(
                "workspace brief pointer changed before import commit",
                context={"expected_revision": brief.parent_revision, "actual_revision": current[1] if current else None},
            )
        self.briefs[(brief.workspace_id, brief.revision)] = brief
        self.current[brief.workspace_id] = (brief.brief_id, brief.revision)
        return brief

    async def get_brief(self, workspace_id: str, revision: int) -> TripBriefRevision | None:
        return self.briefs.get((workspace_id, revision))

    async def get_latest_brief(self, workspace_id: str) -> TripBriefRevision | None:
        current = self.current.get(workspace_id)
        return self.briefs.get((workspace_id, current[1])) if current else None

    async def save_command_revision(
        self,
        brief: TripBriefRevision,
        *,
        expected_revision: int,
        operation: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripBriefRevision, bool]:
        command_key = (brief.workspace_id, idempotency_key)
        existing = self.commands.get(command_key)
        if existing is not None:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
            return existing[1], True
        current = self.current.get(brief.workspace_id)
        if current != (brief.brief_id, expected_revision):
            raise TripBriefRevisionConflictError(
                "trip brief revision does not match current workspace state",
                context={"expected_revision": expected_revision, "actual_revision": current[1] if current else None},
            )
        self.briefs[(brief.workspace_id, brief.revision)] = brief
        self.current[brief.workspace_id] = (brief.brief_id, brief.revision)
        self.commands[command_key] = (request_hash, brief)
        return brief, False


class TripBriefParser:
    version = "trip-brief-parser-v1"

    @staticmethod
    def _span(raw_text: str, pattern: str, source_id: str) -> list[BriefSourceSpan]:
        match = re.search(pattern, raw_text, re.I)
        return [BriefSourceSpan(source_id=source_id, start=match.start(), end=match.end())] if match else []

    def parse(
        self,
        *,
        workspace: TripWorkspace,
        itinerary_import: ItineraryImport,
        actor_user_id: str,
        source_confidence: float | None = None,
    ) -> TripBriefRevision | None:
        raw_text = itinerary_import.raw_text
        traveler_match = re.search(r"(?<!\d)([1-9]\d*)\s*(?:人|位)", raw_text)
        if traveler_match is None:
            return None
        traveler_count = int(traveler_match.group(1))
        transport_patterns = {
            TransportMode.WALKING: r"步行|走路",
            TransportMode.TRANSIT: r"公交|地铁|公共交通",
            TransportMode.BICYCLING: r"骑行|自行车",
            TransportMode.DRIVING: r"驾车|开车|打车|出租车",
        }
        transport_modes = [mode for mode, pattern in transport_patterns.items() if re.search(pattern, raw_text)]

        source_id = itinerary_import.import_id
        metadata: dict[str, BriefFieldProvenance] = {}
        text_patterns = {
            "city": re.escape(workspace.city),
            "date_range": r"(?:20\d{2}[年/-])?\d{1,2}[月/-]\d{1,2}日?",
            "traveler_count": r"(?<!\d)[1-9]\d*\s*(?:人|位)",
            "arrival": r"到达|抵达",
            "departure": r"返程|离开|航班|高铁|火车",
            "accommodation": r"酒店|住宿|民宿",
            "transport_modes": "|".join(transport_patterns.values()),
        }
        for field_name in (
            "city",
            "date_range",
            "traveler_count",
            "arrival",
            "departure",
            "accommodation",
            "transport_modes",
        ):
            spans = self._span(raw_text, text_patterns[field_name], source_id)
            metadata[field_name] = BriefFieldProvenance(
                source_spans=spans,
                confidence=min(0.85 if spans else 0.5, 1.0 if source_confidence is None else source_confidence),
                origin=BriefFieldOrigin.PARSER if spans else BriefFieldOrigin.INFERRED,
            )
        for field_name in (
            "transport_restrictions",
            "budget",
            "dining_style",
            "lodging_style",
            "dietary_restrictions",
            "daily_pace",
            "activity_intensity",
        ):
            metadata[field_name] = BriefFieldProvenance(
                confidence=1,
                origin=BriefFieldOrigin.UNSPECIFIED,
                hardness=BriefHardness.SOFT,
            )
        parent_revision = workspace.current_trip_brief_revision
        brief = TripBriefRevision(
            brief_id=workspace.current_brief_id or str(uuid4()),
            workspace_id=workspace.workspace_id,
            revision=(parent_revision or 0) + 1,
            parent_revision=parent_revision,
            content_hash="0" * 64,
            city=workspace.city,
            date_range=workspace.trip_date_range,
            traveler_count=traveler_count,
            arrival=ArrivalDeparture(),
            departure=ArrivalDeparture(),
            accommodation=AccommodationBrief(),
            transport_modes=transport_modes,
            transport_restrictions=UNSPECIFIED,
            budget=UNSPECIFIED,
            dining_style=UNSPECIFIED,
            lodging_style=UNSPECIFIED,
            dietary_restrictions=UNSPECIFIED,
            daily_pace=UNSPECIFIED,
            activity_intensity=UNSPECIFIED,
            field_provenance=metadata,
            status=TripBriefStatus.NEEDS_CONFIRMATION,
            created_by=actor_user_id,
        )
        return with_brief_content_hash(brief)


class TripBriefApplicationService:
    _PATCHABLE_FIELDS = frozenset(
        {
            "city",
            "date_range",
            "traveler_count",
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
        }
    )

    def __init__(self, repository: TripBriefRepository, parser: TripBriefParser | None = None):
        self.repository = repository
        self.parser = parser or TripBriefParser()

    async def create_for_import(
        self,
        *,
        workspace: TripWorkspace,
        itinerary_import: ItineraryImport,
        actor_user_id: str,
        conn: Any | None = None,
        source_confidence: float | None = None,
    ) -> TripBriefRevision | None:
        if workspace.current_trip_brief_revision is None:
            latest = await self.repository.get_latest_brief(workspace.workspace_id)
            if latest is not None:
                workspace = workspace.model_copy(
                    update={
                        "current_brief_id": latest.brief_id,
                        "current_trip_brief_revision": latest.revision,
                    }
                )
        brief = self.parser.parse(
            workspace=workspace,
            itinerary_import=itinerary_import,
            actor_user_id=actor_user_id,
            source_confidence=source_confidence,
        )
        if brief is None:
            return None
        return await self.repository.save_import_brief(brief, conn=conn)

    async def patch(
        self,
        *,
        workspace_id: str,
        revision: int,
        updates: dict[str, Any],
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripBriefRevision, bool]:
        unsupported = sorted(set(updates) - self._PATCHABLE_FIELDS)
        if unsupported:
            raise ValueError(f"unsupported brief fields: {unsupported}")
        base = await self.repository.get_brief(workspace_id, revision)
        if base is None:
            raise ResourceNotFound("trip brief revision does not exist")
        if base.status == TripBriefStatus.CONFIRMED:
            raise TripBriefAlreadyConfirmedError("confirmed trip brief revisions are read-only")
        provenance = dict(base.field_provenance)
        for field_name, value in updates.items():
            provenance[field_name] = BriefFieldProvenance(
                confidence=1,
                origin=BriefFieldOrigin.USER_CONFIRMED,
                confirmation=BriefFieldConfirmation.CONFIRMED,
                hardness=(
                    BriefHardness.NO_PREFERENCE
                    if value == NO_PREFERENCE
                    else BriefHardness.HARD
                    if field_name in {"city", "date_range", "traveler_count"}
                    else BriefHardness.SOFT
                ),
            )
        now = datetime.now(timezone.utc)
        candidate = base.model_copy(
            update={
                **updates,
                "revision": base.revision + 1,
                "parent_revision": base.revision,
                "field_provenance": provenance,
                "content_hash": "0" * 64,
                "created_by": actor_user_id,
                "created_at": now,
            }
        )
        candidate = TripBriefRevision.model_validate(candidate.model_dump())
        candidate = with_brief_content_hash(candidate)
        request_hash = sha256_canonical(
            {
                "operation": "PATCH_BRIEF",
                "workspace_id": workspace_id,
                "revision": revision,
                "actor_user_id": actor_user_id,
                "updates": updates,
            }
        )
        return await self.repository.save_command_revision(
            candidate,
            expected_revision=revision,
            operation="PATCH_BRIEF",
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def confirm(
        self,
        *,
        workspace_id: str,
        revision: int,
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripBriefRevision, bool]:
        base = await self.repository.get_brief(workspace_id, revision)
        if base is None:
            raise ResourceNotFound("trip brief revision does not exist")
        if base.status == TripBriefStatus.CONFIRMED:
            raise TripBriefAlreadyConfirmedError("confirmed trip brief revisions are read-only")
        provenance = {
            field_name: item.model_copy(update={"confirmation": BriefFieldConfirmation.CONFIRMED})
            for field_name, item in base.field_provenance.items()
        }
        now = datetime.now(timezone.utc)
        candidate = base.model_copy(
            update={
                "revision": base.revision + 1,
                "parent_revision": base.revision,
                "field_provenance": provenance,
                "status": TripBriefStatus.CONFIRMED,
                "content_hash": "0" * 64,
                "created_by": actor_user_id,
                "created_at": now,
                "confirmed_by": actor_user_id,
                "confirmed_at": now,
            }
        )
        candidate = TripBriefRevision.model_validate(candidate.model_dump())
        candidate = with_brief_content_hash(candidate)
        request_hash = sha256_canonical(
            {
                "operation": "CONFIRM_BRIEF",
                "workspace_id": workspace_id,
                "revision": revision,
                "actor_user_id": actor_user_id,
            }
        )
        return await self.repository.save_command_revision(
            candidate,
            expected_revision=revision,
            operation="CONFIRM_BRIEF",
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )
