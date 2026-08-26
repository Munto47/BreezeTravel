from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel

from app.db.connection import get_pool
from app.itineraries.errors import IdempotencyKeyReusedError, RevisionConflictError
from app.trip_intake.models import EvidenceSpan, TripIntakeRevision


class TripIntakeRepository(Protocol):
    async def save_initial(self, intake: TripIntakeRevision) -> TripIntakeRevision: ...

    async def get_revision(self, intake_id: str, revision: int) -> TripIntakeRevision | None: ...

    async def get_latest(self, intake_id: str) -> TripIntakeRevision | None: ...

    async def get_latest_for_room(self, room_id: str) -> TripIntakeRevision | None: ...

    async def save_command_revision(
        self,
        intake: TripIntakeRevision,
        *,
        expected_revision: int,
        operation: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripIntakeRevision, bool]: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _evidence_records(value: Any, path: str = "") -> Iterable[tuple[str, int, EvidenceSpan]]:
    if isinstance(value, BaseModel):
        yield from _evidence_records(value.model_dump(mode="python"), path)
        return
    if isinstance(value, dict):
        if {"source_id", "start", "end", "quote"}.issubset(value):
            yield path, 0, EvidenceSpan.model_validate(value)
            return
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield from _evidence_records(child, child_path)
        return
    if isinstance(value, list):
        if value and all(
            isinstance(item, dict) and {"source_id", "start", "end", "quote"}.issubset(item)
            for item in value
        ):
            for index, item in enumerate(value):
                yield path, index, EvidenceSpan.model_validate(item)
            return
        for index, child in enumerate(value):
            yield from _evidence_records(child, f"{path}[{index}]")


async def insert_intake_revision(conn: Any, intake: TripIntakeRevision) -> None:
    payload = intake.model_dump(mode="json")
    await conn.execute(
        """
        INSERT INTO trip_intake_revisions (
            intake_id, room_id, revision, parent_revision, schema_version,
            source_type, raw_text, raw_text_sha256, sources_json,
            parser_binding_json, extraction_json, confirmed_fields_json,
            status, content_json, content_hash, created_by, created_at,
            confirmed_by, confirmed_at
        ) VALUES (
            $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb,
            $11::jsonb, $12::jsonb, $13, $14::jsonb, $15, $16, $17, $18, $19
        )
        """,
        intake.intake_id,
        intake.room_id,
        intake.revision,
        intake.parent_revision,
        intake.schema_version,
        intake.source_type.value,
        intake.raw_text,
        intake.raw_text_sha256,
        json.dumps(payload["sources"], ensure_ascii=False),
        json.dumps(payload["parser_binding"], ensure_ascii=False),
        json.dumps(payload["extraction"], ensure_ascii=False),
        json.dumps(sorted(payload["confirmed_fields"]), ensure_ascii=False),
        intake.status.value,
        json.dumps(payload, ensure_ascii=False),
        intake.content_hash,
        intake.created_by,
        intake.created_at,
        intake.confirmed_by,
        intake.confirmed_at,
    )
    for field_path, source_index, span in _evidence_records(intake.extraction):
        await conn.execute(
            """
            INSERT INTO trip_intake_field_sources (
                intake_id, intake_revision, field_path, source_index,
                source_id, span_start, span_end, quote, confidence
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, NULL)
            """,
            intake.intake_id,
            intake.revision,
            field_path,
            source_index,
            span.source_id,
            span.start,
            span.end,
            span.quote,
        )


class PostgresTripIntakeRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def save_initial(self, intake: TripIntakeRevision) -> TripIntakeRevision:
        if intake.revision != 1 or intake.parent_revision is not None:
            raise ValueError("initial intake must be revision 1")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await insert_intake_revision(conn, intake)
        return intake

    async def get_revision(self, intake_id: str, revision: int) -> TripIntakeRevision | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT content_json FROM trip_intake_revisions WHERE intake_id = $1 AND revision = $2",
                intake_id,
                revision,
            )
        return TripIntakeRevision.model_validate(_json_value(row["content_json"])) if row else None

    async def get_latest(self, intake_id: str) -> TripIntakeRevision | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content_json FROM trip_intake_revisions
                WHERE intake_id = $1 ORDER BY revision DESC LIMIT 1
                """,
                intake_id,
            )
        return TripIntakeRevision.model_validate(_json_value(row["content_json"])) if row else None

    async def get_latest_for_room(self, room_id: str) -> TripIntakeRevision | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content_json FROM trip_intake_revisions
                WHERE room_id = $1 ORDER BY created_at DESC, revision DESC LIMIT 1
                """,
                room_id,
            )
        return TripIntakeRevision.model_validate(_json_value(row["content_json"])) if row else None

    async def save_command_revision(
        self,
        intake: TripIntakeRevision,
        *,
        expected_revision: int,
        operation: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripIntakeRevision, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM trip_intake_commands
                WHERE intake_id = $1 AND idempotency_key = $2
                """,
                intake.intake_id,
                idempotency_key,
            )
            if existing:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError(
                        "idempotency key was already used with a different intake request"
                    )
                return TripIntakeRevision.model_validate(_json_value(existing["response_json"])), True
            latest = await conn.fetchrow(
                """
                SELECT revision FROM trip_intake_revisions
                WHERE intake_id = $1 ORDER BY revision DESC LIMIT 1 FOR UPDATE
                """,
                intake.intake_id,
            )
            actual_revision = latest["revision"] if latest else None
            if actual_revision != expected_revision:
                raise RevisionConflictError(
                    "trip intake revision is stale",
                    context={"expected_revision": expected_revision, "actual_revision": actual_revision},
                )
            await insert_intake_revision(conn, intake)
            await conn.execute(
                """
                INSERT INTO trip_intake_commands (
                    command_id, intake_id, base_revision, result_revision,
                    operation, actor_user_id, idempotency_key, request_hash, response_json
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                """,
                str(uuid4()),
                intake.intake_id,
                expected_revision,
                intake.revision,
                operation,
                actor_user_id,
                idempotency_key,
                request_hash,
                json.dumps(intake.model_dump(mode="json"), ensure_ascii=False),
            )
        return intake, False


class InMemoryTripIntakeRepository:
    def __init__(self):
        self.revisions: dict[tuple[str, int], TripIntakeRevision] = {}
        self.commands: dict[tuple[str, str], tuple[str, TripIntakeRevision]] = {}

    async def save_initial(self, intake: TripIntakeRevision) -> TripIntakeRevision:
        if intake.revision != 1 or intake.parent_revision is not None:
            raise ValueError("initial intake must be revision 1")
        self.revisions[(intake.intake_id, 1)] = intake
        return intake

    async def get_revision(self, intake_id: str, revision: int) -> TripIntakeRevision | None:
        return self.revisions.get((intake_id, revision))

    async def get_latest(self, intake_id: str) -> TripIntakeRevision | None:
        matches = [item for (stored_id, _), item in self.revisions.items() if stored_id == intake_id]
        return max(matches, key=lambda item: item.revision) if matches else None

    async def get_latest_for_room(self, room_id: str) -> TripIntakeRevision | None:
        matches = [item for item in self.revisions.values() if item.room_id == room_id]
        return max(matches, key=lambda item: (item.created_at, item.revision)) if matches else None

    async def save_command_revision(
        self,
        intake: TripIntakeRevision,
        *,
        expected_revision: int,
        operation: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripIntakeRevision, bool]:
        del operation, actor_user_id
        command_key = (intake.intake_id, idempotency_key)
        existing = self.commands.get(command_key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError(
                    "idempotency key was already used with a different intake request"
                )
            return existing[1], True
        latest = await self.get_latest(intake.intake_id)
        actual_revision = latest.revision if latest else None
        if actual_revision != expected_revision:
            raise RevisionConflictError(
                "trip intake revision is stale",
                context={"expected_revision": expected_revision, "actual_revision": actual_revision},
            )
        self.revisions[(intake.intake_id, intake.revision)] = intake
        self.commands[command_key] = (request_hash, intake)
        return intake, False
