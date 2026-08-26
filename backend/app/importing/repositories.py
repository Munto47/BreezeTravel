from __future__ import annotations

import json
from asyncio import Lock
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.db.connection import get_pool
from app.importing.models import (
    ImportApplyResult,
    ImportStatus,
    ItineraryImport,
    PlaceCandidate,
    RawStop,
    ResolvedPlaceReceipt,
    ResolvedStop,
    resolution_set_is_ready,
)
from app.importing.errors import DraftAmbiguousError, ImportStateConflictError, InvalidImportStateError
from app.itineraries.errors import (
    IdempotencyKeyReusedError,
    InvalidEditCommandError,
    ResourceNotFound,
    RevisionConflictError,
)
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.models import ItineraryRevision, ResolutionStatus
from app.itineraries.repositories import ItineraryRepository, insert_revision_record


class ImportRepository(Protocol):
    async def create_import(self, itinerary_import: ItineraryImport) -> ItineraryImport: ...

    async def create_import_bundle(
        self,
        itinerary_import: ItineraryImport,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> ItineraryImport: ...

    async def get_import(self, import_id: str) -> ItineraryImport | None: ...

    async def list_imports(
        self,
        workspace_id: str,
        *,
        limit: int,
        unfinished_only: bool = False,
    ) -> list[ItineraryImport]: ...

    async def save_resolutions(self, import_id: str, resolutions: list[ResolvedStop]) -> ItineraryImport: ...

    async def save_resolution(
        self,
        import_id: str,
        resolution: ResolvedStop,
        *,
        expected_state_version: int | None = None,
    ) -> ItineraryImport: ...

    async def confirm_resolution(
        self,
        import_id: str,
        raw_stop_id: str,
        place_id: str,
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport: ...

    async def confirm_resolutions(
        self,
        import_id: str,
        confirmations: dict[str, str],
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport: ...

    async def apply_ready_import(
        self,
        import_id: str,
        revision: ItineraryRevision,
        *,
        actor_user_id: str,
        expected_state_version: int,
        idempotency_key: str,
        place_records: dict[str, dict[str, Any]],
        place_records_by_stop: dict[str, dict[str, Any]],
        resolved_place_receipts: list[ResolvedPlaceReceipt],
        resolved_place_receipts_by_stop: dict[str, ResolvedPlaceReceipt],
    ) -> ImportApplyResult: ...


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _parsed_payload(itinerary_import: ItineraryImport) -> dict[str, Any]:
    return {
        "raw_stops": [stop.model_dump(mode="json") for stop in itinerary_import.raw_stops],
        "member_summary": itinerary_import.member_summary,
        "parse_errors": itinerary_import.parse_errors,
    }


def _resolution_candidates_payload(resolution: ResolvedStop) -> dict[str, Any]:
    """Serialize offered and hard-rejected candidates in one versioned JSONB value.

    ``candidates_json`` historically contained a bare list of offered candidates.
    Keeping both collections in the existing column avoids a schema migration while
    preserving the important boundary: only ``candidates`` can be confirmed.
    """

    return {
        "schema_version": 2,
        "candidates": [candidate.model_dump(mode="json") for candidate in resolution.candidates],
        "rejected_candidates": [candidate.model_dump(mode="json") for candidate in resolution.rejected_candidates],
    }


def _resolution_candidate_lists(value: Any) -> tuple[list[Any], list[Any]]:
    payload = _json_value(value)
    if payload is None:
        return [], []
    if isinstance(payload, list):
        # Backwards compatibility for rows written before rejected receipts were
        # persisted.  An absent receipt remains absent; it is never fabricated.
        return payload, []
    if not isinstance(payload, dict):
        raise ValueError("resolution candidates_json must be a list or object")
    candidates = payload.get("candidates", [])
    rejected = payload.get("rejected_candidates", [])
    if not isinstance(candidates, list) or not isinstance(rejected, list):
        raise ValueError("resolution candidate collections must be lists")
    return candidates, rejected


def _resolution_from_row(row: Any) -> ResolvedStop:
    candidates, rejected_candidates = _resolution_candidate_lists(row["candidates_json"])
    return ResolvedStop(
        raw_stop_id=row["raw_stop_id"],
        canonical_place_id=row["canonical_place_id"],
        candidates=candidates,
        rejected_candidates=rejected_candidates,
        confidence=row["confidence"],
        resolution_status=row["resolution_status"],
        resolution_version=row["resolution_version"],
        confirmed_by=row["confirmed_by"],
        confirmed_at=row["confirmed_at"],
    )


def _import_from_row(row: Any, resolution_rows: list[Any]) -> ItineraryImport:
    parsed = _json_value(row["parsed_json"]) or {}
    return ItineraryImport(
        import_id=row["import_id"],
        workspace_id=row["workspace_id"],
        source_type=row["source_type"],
        raw_text=row["raw_text"],
        parse_version=row["parse_version"],
        status=row["status"],
        raw_stops=[RawStop.model_validate(item) for item in parsed.get("raw_stops", [])],
        resolutions=[_resolution_from_row(item) for item in resolution_rows],
        member_summary=list(parsed.get("member_summary", [])),
        parse_errors=list(parsed.get("parse_errors", [])),
        state_version=row["state_version"],
        applied_revision=row["applied_revision"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _assert_draft_mutable(status: ImportStatus | str) -> None:
    value = status.value if isinstance(status, ImportStatus) else status
    if value in {ImportStatus.APPLIED.value, ImportStatus.FAILED.value}:
        raise InvalidImportStateError(
            "terminal imports cannot mutate draft resolutions",
            context={"import_status": value},
        )


def _assert_state_version(actual: int, expected: int | None) -> None:
    if expected is not None and actual != expected:
        raise ImportStateConflictError(
            "import state version is stale",
            context={"expected_state_version": expected, "actual_state_version": actual},
        )


class PostgresImportRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def create_import(self, itinerary_import: ItineraryImport) -> ItineraryImport:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO itinerary_imports (
                    import_id, workspace_id, source_type, raw_text, parse_version,
                    status, parsed_json, created_by, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
                """,
                itinerary_import.import_id,
                itinerary_import.workspace_id,
                itinerary_import.source_type.value,
                itinerary_import.raw_text,
                itinerary_import.parse_version,
                itinerary_import.status.value,
                json.dumps(_parsed_payload(itinerary_import), ensure_ascii=False),
                itinerary_import.created_by,
                itinerary_import.created_at,
            )
        return itinerary_import

    async def create_import_bundle(
        self,
        itinerary_import: ItineraryImport,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> ItineraryImport:
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as acquired, acquired.transaction():
                return await self.create_import_bundle(
                    itinerary_import,
                    basis=basis,
                    conn=acquired,
                )
        workspace = await conn.fetchrow(
            "SELECT current_import_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
            itinerary_import.workspace_id,
        )
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        actual_basis = {"current_import_id": workspace["current_import_id"]}
        if actual_basis != basis:
            raise ImportStateConflictError(
                "workspace import pointer changed after idempotent import claim",
                context={"expected_basis": basis, "actual_basis": actual_basis},
            )
        await conn.execute(
            """
            INSERT INTO itinerary_imports (
                import_id, workspace_id, source_type, raw_text, parse_version,
                status, parsed_json, state_version, applied_revision,
                created_by, created_at, updated_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11, $12)
            """,
            itinerary_import.import_id,
            itinerary_import.workspace_id,
            itinerary_import.source_type.value,
            itinerary_import.raw_text,
            itinerary_import.parse_version,
            itinerary_import.status.value,
            json.dumps(_parsed_payload(itinerary_import), ensure_ascii=False),
            itinerary_import.state_version,
            itinerary_import.applied_revision,
            itinerary_import.created_by,
            itinerary_import.created_at,
            itinerary_import.updated_at,
        )
        raw_by_id = {item.raw_stop_id: item for item in itinerary_import.raw_stops}
        for resolution in itinerary_import.resolutions:
            raw_stop = raw_by_id[resolution.raw_stop_id]
            await conn.execute(
                """
                INSERT INTO itinerary_stop_resolutions (
                    raw_stop_id, import_id, day_index, raw_name, raw_time, source_span,
                    source_sentence, fixed_commitment, canonical_place_id, candidates_json,
                    confidence, resolution_status, resolution_version, confirmed_by, confirmed_at
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $15)
                """,
                raw_stop.raw_stop_id,
                itinerary_import.import_id,
                raw_stop.day_index,
                raw_stop.raw_name,
                raw_stop.raw_time,
                json.dumps(raw_stop.source_span.model_dump(mode="json")),
                raw_stop.source_sentence,
                raw_stop.fixed_commitment,
                resolution.canonical_place_id,
                json.dumps(_resolution_candidates_payload(resolution), ensure_ascii=False),
                resolution.confidence,
                resolution.resolution_status.value,
                resolution.resolution_version,
                resolution.confirmed_by,
                resolution.confirmed_at,
            )
        await conn.execute(
            """
            UPDATE trip_workspaces
            SET current_import_id = $2, updated_at = NOW()
            WHERE workspace_id = $1
            """,
            itinerary_import.workspace_id,
            itinerary_import.import_id,
        )
        return itinerary_import

    async def get_import(self, import_id: str) -> ItineraryImport | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM itinerary_imports WHERE import_id = $1", import_id)
            if row is None:
                return None
            resolution_rows = await conn.fetch(
                "SELECT * FROM itinerary_stop_resolutions WHERE import_id = $1 ORDER BY day_index, raw_stop_id",
                import_id,
            )
        return _import_from_row(row, list(resolution_rows))

    async def list_imports(
        self,
        workspace_id: str,
        *,
        limit: int,
        unfinished_only: bool = False,
    ) -> list[ItineraryImport]:
        pool = await self._get_pool()
        status_clause = "AND status <> 'APPLIED'" if unfinished_only else ""
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT import_id FROM itinerary_imports
                WHERE workspace_id = $1 {status_clause}
                ORDER BY updated_at DESC, import_id DESC
                LIMIT $2
                """,
                workspace_id,
                limit,
            )
        results: list[ItineraryImport] = []
        for row in rows:
            item = await self.get_import(row["import_id"])
            if item is not None:
                results.append(item)
        return results

    async def save_resolutions(self, import_id: str, resolutions: list[ResolvedStop]) -> ItineraryImport:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            import_row = await conn.fetchrow(
                "SELECT * FROM itinerary_imports WHERE import_id = $1 FOR UPDATE",
                import_id,
            )
            if import_row is None:
                raise ResourceNotFound("import does not exist")
            _assert_draft_mutable(import_row["status"])
            parsed = _json_value(import_row["parsed_json"]) or {}
            raw_by_id = {
                item.raw_stop_id: item for item in (RawStop.model_validate(raw) for raw in parsed.get("raw_stops", []))
            }
            if {item.raw_stop_id for item in resolutions} != set(raw_by_id):
                raise InvalidEditCommandError("resolution set must match every parsed raw stop")
            for resolution in resolutions:
                raw_stop = raw_by_id[resolution.raw_stop_id]
                existing_version = await conn.fetchval(
                    "SELECT resolution_version FROM itinerary_stop_resolutions WHERE raw_stop_id = $1",
                    resolution.raw_stop_id,
                )
                version = (existing_version or 0) + 1
                await conn.execute(
                    """
                    INSERT INTO itinerary_stop_resolutions (
                        raw_stop_id, import_id, day_index, raw_name, raw_time, source_span,
                        source_sentence, fixed_commitment, canonical_place_id, candidates_json,
                        confidence, resolution_status, resolution_version, confirmed_by, confirmed_at
                    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $15)
                    ON CONFLICT (raw_stop_id) DO UPDATE SET
                        canonical_place_id = EXCLUDED.canonical_place_id,
                        candidates_json = EXCLUDED.candidates_json,
                        confidence = EXCLUDED.confidence,
                        resolution_status = EXCLUDED.resolution_status,
                        resolution_version = EXCLUDED.resolution_version,
                        confirmed_by = EXCLUDED.confirmed_by,
                        confirmed_at = EXCLUDED.confirmed_at
                    """,
                    raw_stop.raw_stop_id,
                    import_id,
                    raw_stop.day_index,
                    raw_stop.raw_name,
                    raw_stop.raw_time,
                    json.dumps(raw_stop.source_span.model_dump(mode="json")),
                    raw_stop.source_sentence,
                    raw_stop.fixed_commitment,
                    resolution.canonical_place_id,
                    json.dumps(_resolution_candidates_payload(resolution), ensure_ascii=False),
                    resolution.confidence,
                    resolution.resolution_status.value,
                    version,
                    resolution.confirmed_by,
                    resolution.confirmed_at,
                )
            ready = resolution_set_is_ready(list(raw_by_id.values()), resolutions)
            await conn.execute(
                """
                UPDATE itinerary_imports
                SET status = $2, state_version = state_version + 1, updated_at = NOW()
                WHERE import_id = $1
                """,
                import_id,
                ImportStatus.READY.value if ready else ImportStatus.NEEDS_RESOLUTION.value,
            )
        result = await self.get_import(import_id)
        if result is None:
            raise RuntimeError("import disappeared after saving resolutions")
        return result

    async def save_resolution(
        self,
        import_id: str,
        resolution: ResolvedStop,
        *,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        """Replace candidates for one raw stop without mutating unrelated resolution versions."""

        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            import_row = await conn.fetchrow(
                "SELECT parsed_json, status, state_version FROM itinerary_imports WHERE import_id = $1 FOR UPDATE",
                import_id,
            )
            if import_row is None:
                raise ResourceNotFound("import does not exist")
            _assert_draft_mutable(import_row["status"])
            _assert_state_version(import_row["state_version"], expected_state_version)
            parsed = _json_value(import_row["parsed_json"]) or {}
            raw_stop = next(
                (
                    RawStop.model_validate(item)
                    for item in parsed.get("raw_stops", [])
                    if item.get("raw_stop_id") == resolution.raw_stop_id
                ),
                None,
            )
            if raw_stop is None:
                raise ResourceNotFound("raw stop does not belong to import")
            existing_version = await conn.fetchval(
                "SELECT resolution_version FROM itinerary_stop_resolutions WHERE import_id = $1 AND raw_stop_id = $2",
                import_id,
                resolution.raw_stop_id,
            )
            version = (existing_version or 0) + 1
            await conn.execute(
                """
                INSERT INTO itinerary_stop_resolutions (
                    raw_stop_id, import_id, day_index, raw_name, raw_time, source_span,
                    source_sentence, fixed_commitment, canonical_place_id, candidates_json,
                    confidence, resolution_status, resolution_version, confirmed_by, confirmed_at
                ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb, $11, $12, $13, $14, $15)
                ON CONFLICT (raw_stop_id) DO UPDATE SET
                    canonical_place_id = EXCLUDED.canonical_place_id,
                    candidates_json = EXCLUDED.candidates_json,
                    confidence = EXCLUDED.confidence,
                    resolution_status = EXCLUDED.resolution_status,
                    resolution_version = EXCLUDED.resolution_version,
                    confirmed_by = EXCLUDED.confirmed_by,
                    confirmed_at = EXCLUDED.confirmed_at
                """,
                raw_stop.raw_stop_id,
                import_id,
                raw_stop.day_index,
                raw_stop.raw_name,
                raw_stop.raw_time,
                json.dumps(raw_stop.source_span.model_dump(mode="json")),
                raw_stop.source_sentence,
                raw_stop.fixed_commitment,
                resolution.canonical_place_id,
                json.dumps(_resolution_candidates_payload(resolution), ensure_ascii=False),
                resolution.confidence,
                resolution.resolution_status.value,
                version,
                resolution.confirmed_by,
                resolution.confirmed_at,
            )
            current_rows = await conn.fetch(
                "SELECT * FROM itinerary_stop_resolutions WHERE import_id = $1 ORDER BY day_index, raw_stop_id",
                import_id,
            )
            current_resolutions = [_resolution_from_row(row) for row in current_rows]
            raw_stops = [RawStop.model_validate(item) for item in parsed.get("raw_stops", [])]
            next_status = (
                ImportStatus.READY
                if resolution_set_is_ready(raw_stops, current_resolutions)
                else ImportStatus.NEEDS_RESOLUTION
            )
            await conn.execute(
                """
                UPDATE itinerary_imports
                SET status = $2, state_version = state_version + 1, updated_at = NOW()
                WHERE import_id = $1
                """,
                import_id,
                next_status.value,
            )
        result = await self.get_import(import_id)
        if result is None:
            raise RuntimeError("import disappeared after saving one resolution")
        return result

    async def confirm_resolution(
        self,
        import_id: str,
        raw_stop_id: str,
        place_id: str,
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        return await self.confirm_resolutions(
            import_id,
            {raw_stop_id: place_id},
            actor_user_id,
            expected_state_version,
        )

    async def confirm_resolutions(
        self,
        import_id: str,
        confirmations: dict[str, str],
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        if not confirmations:
            raise InvalidEditCommandError("at least one resolution confirmation is required")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            import_row = await conn.fetchrow(
                "SELECT status, state_version, parsed_json FROM itinerary_imports WHERE import_id = $1 FOR UPDATE",
                import_id,
            )
            if import_row is None:
                raise ResourceNotFound("import does not exist")
            _assert_draft_mutable(import_row["status"])
            _assert_state_version(import_row["state_version"], expected_state_version)
            rows = await conn.fetch(
                """
                SELECT raw_stop_id, candidates_json FROM itinerary_stop_resolutions
                WHERE import_id = $1 AND raw_stop_id = ANY($2::text[]) FOR UPDATE
                """,
                import_id,
                list(confirmations),
            )
            rows_by_id = {row["raw_stop_id"]: row for row in rows}
            if set(rows_by_id) != set(confirmations):
                raise ResourceNotFound("one or more raw stop resolutions do not exist")
            for raw_stop_id, place_id in confirmations.items():
                raw_candidates, _ = _resolution_candidate_lists(rows_by_id[raw_stop_id]["candidates_json"])
                selected = next(
                    (
                        PlaceCandidate.model_validate(candidate)
                        for candidate in raw_candidates
                        if str(candidate.get("place_id")) == place_id
                    ),
                    None,
                )
                if selected is None:
                    raise InvalidEditCommandError(
                        "selected place is not an offered resolution candidate",
                        context={"raw_stop_id": raw_stop_id, "place_id": place_id},
                    )
                if selected.resolved_place_receipt is None:
                    raise InvalidEditCommandError(
                        "selected place candidate lacks authoritative provider facts",
                        context={
                            "reason": "CANDIDATE_FACTS_INCOMPLETE",
                            "raw_stop_id": raw_stop_id,
                            "place_id": place_id,
                        },
                    )
            for raw_stop_id, place_id in confirmations.items():
                await conn.execute(
                    """
                    UPDATE itinerary_stop_resolutions
                    SET canonical_place_id = $3, resolution_status = 'USER_CONFIRMED',
                        resolution_version = resolution_version + 1,
                        confirmed_by = $4, confirmed_at = NOW()
                    WHERE import_id = $1 AND raw_stop_id = $2
                    """,
                    import_id,
                    raw_stop_id,
                    place_id,
                    actor_user_id,
                )
            current_rows = await conn.fetch(
                "SELECT * FROM itinerary_stop_resolutions WHERE import_id = $1 ORDER BY day_index, raw_stop_id",
                import_id,
            )
            current_resolutions = [_resolution_from_row(row) for row in current_rows]
            parsed = _json_value(import_row["parsed_json"]) or {}
            raw_stops = [RawStop.model_validate(item) for item in parsed.get("raw_stops", [])]
            status = (
                ImportStatus.READY
                if resolution_set_is_ready(raw_stops, current_resolutions)
                else ImportStatus.NEEDS_RESOLUTION
            )
            await conn.execute(
                """
                UPDATE itinerary_imports
                SET status = $2, state_version = state_version + 1, updated_at = NOW()
                WHERE import_id = $1
                """,
                import_id,
                status.value,
            )
        result = await self.get_import(import_id)
        if result is None:
            raise RuntimeError("import disappeared after confirmation")
        return result

    async def apply_ready_import(
        self,
        import_id: str,
        revision: ItineraryRevision,
        *,
        actor_user_id: str,
        expected_state_version: int,
        idempotency_key: str,
        place_records: dict[str, dict[str, Any]],
        place_records_by_stop: dict[str, dict[str, Any]],
        resolved_place_receipts: list[ResolvedPlaceReceipt],
        resolved_place_receipts_by_stop: dict[str, ResolvedPlaceReceipt],
    ) -> ImportApplyResult:
        pool = await self._get_pool()
        request_hash = sha256_canonical(
            {
                "operation": "APPLY_IMPORT",
                "import_id": import_id,
                "expected_state_version": expected_state_version,
                "actor_user_id": actor_user_id,
            }
        )
        async with pool.acquire() as conn, conn.transaction():
            import_row = await conn.fetchrow(
                "SELECT * FROM itinerary_imports WHERE import_id = $1 FOR UPDATE",
                import_id,
            )
            if import_row is None:
                raise ResourceNotFound("import does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM itinerary_import_commands
                WHERE import_id = $1 AND idempotency_key = $2
                """,
                import_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different import request")
                replay = ImportApplyResult.model_validate(_json_value(existing["response_json"]))
                return replay.model_copy(update={"idempotent_replay": True})
            if import_row["status"] != ImportStatus.READY.value:
                if import_row["status"] in {ImportStatus.APPLIED.value, ImportStatus.FAILED.value}:
                    raise InvalidImportStateError(
                        "import is not in an applicable draft state",
                        context={"import_status": import_row["status"]},
                    )
                raise DraftAmbiguousError("all ambiguous or missing places must be resolved before apply")
            _assert_state_version(import_row["state_version"], expected_state_version)
            workspace = await conn.fetchrow(
                "SELECT room_id, current_itinerary_revision FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                import_row["workspace_id"],
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            if workspace["current_itinerary_revision"] is not None:
                raise RevisionConflictError(
                    "workspace already has an itinerary revision",
                    context={"actual_revision": workspace["current_itinerary_revision"]},
                )
            await insert_revision_record(conn, revision)
            for place_id, place_data in place_records.items():
                await conn.execute(
                    """
                    INSERT INTO room_places (room_id, place_id, place_data, voted_by, added_at, updated_at)
                    VALUES ($1, $2, $3::jsonb, '{}'::text[], NOW(), NOW())
                    ON CONFLICT (room_id, place_id) DO UPDATE
                    SET place_data = EXCLUDED.place_data, updated_at = NOW()
                    """,
                    workspace["room_id"],
                    place_id,
                    json.dumps(place_data, ensure_ascii=False),
                )
            for stop_id, receipt in resolved_place_receipts_by_stop.items():
                receipt_json = receipt.model_dump(mode="json")
                await conn.execute(
                    """
                    INSERT INTO itinerary_place_receipts (
                        workspace_id, itinerary_revision, stop_id, place_id,
                        receipt_hash, receipt_json, place_data_json
                    ) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)
                    """,
                    revision.workspace_id,
                    revision.revision,
                    stop_id,
                    receipt.canonical_place_id,
                    sha256_canonical(receipt_json),
                    json.dumps(receipt_json, ensure_ascii=False),
                    json.dumps(place_records_by_stop[stop_id], ensure_ascii=False),
                )
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision = 1, current_report_id = NULL,
                    status = 'DRAFT', updated_at = NOW()
                WHERE workspace_id = $1
                """,
                revision.workspace_id,
            )
            await conn.execute(
                """
                UPDATE itinerary_imports
                SET status = 'APPLIED', applied_revision = $2,
                    state_version = state_version + 1, updated_at = NOW()
                WHERE import_id = $1
                """,
                import_id,
                revision.revision,
            )
            stored_row = await conn.fetchrow("SELECT * FROM itinerary_imports WHERE import_id = $1", import_id)
            resolution_rows = await conn.fetch(
                "SELECT * FROM itinerary_stop_resolutions WHERE import_id = $1 ORDER BY day_index, raw_stop_id",
                import_id,
            )
            stored_import = _import_from_row(stored_row, list(resolution_rows))
            result = ImportApplyResult(
                itinerary_import=stored_import,
                revision=revision,
                resolved_place_receipts=resolved_place_receipts,
            )
            await conn.execute(
                """
                INSERT INTO itinerary_import_commands (
                    command_id, import_id, workspace_id, actor_user_id, operation,
                    request_hash, idempotency_key, response_json
                ) VALUES ($1, $2, $3, $4, 'APPLY_IMPORT', $5, $6, $7::jsonb)
                """,
                f"apply-import:{import_id}:{idempotency_key}",
                import_id,
                revision.workspace_id,
                actor_user_id,
                request_hash,
                idempotency_key,
                json.dumps(result.model_dump(mode="json"), ensure_ascii=False),
            )
            return result


class InMemoryImportRepository:
    def __init__(
        self,
        itinerary_repository: ItineraryRepository | None = None,
        *,
        place_record_store: dict[str, dict[str, dict[str, Any]]] | None = None,
        apply_fault_hook: Callable[[str], None] | None = None,
    ):
        self.imports: dict[str, ItineraryImport] = {}
        self.workspaces_current_revision: dict[str, int | None] = {}
        self.applied_revisions: dict[str, ItineraryRevision] = {}
        self.materialized_place_records = place_record_store if place_record_store is not None else {}
        self.materialized_place_receipts: dict[str, dict[str, ResolvedPlaceReceipt]] = {}
        self.apply_commands: dict[tuple[str, str], tuple[str, ImportApplyResult]] = {}
        self._import_locks: defaultdict[str, Lock] = defaultdict(Lock)
        self.itinerary_repository = itinerary_repository
        self.apply_fault_hook = apply_fault_hook

    async def create_import(self, itinerary_import: ItineraryImport) -> ItineraryImport:
        self.imports[itinerary_import.import_id] = itinerary_import
        return itinerary_import

    async def create_import_bundle(
        self,
        itinerary_import: ItineraryImport,
        *,
        basis: dict[str, Any],
        conn: Any | None = None,
    ) -> ItineraryImport:
        workspace = (
            await self.itinerary_repository.get_workspace(itinerary_import.workspace_id)
            if self.itinerary_repository is not None
            else None
        )
        actual_basis = {"current_import_id": workspace.current_import_id if workspace else None}
        if actual_basis != basis:
            raise ImportStateConflictError(
                "workspace import pointer changed after idempotent import claim",
                context={"expected_basis": basis, "actual_basis": actual_basis},
            )
        self.imports[itinerary_import.import_id] = itinerary_import
        if workspace is not None:
            self.itinerary_repository.workspaces[itinerary_import.workspace_id] = workspace.model_copy(
                update={"current_import_id": itinerary_import.import_id}
            )
        return itinerary_import

    async def get_import(self, import_id: str) -> ItineraryImport | None:
        return self.imports.get(import_id)

    async def list_imports(
        self,
        workspace_id: str,
        *,
        limit: int,
        unfinished_only: bool = False,
    ) -> list[ItineraryImport]:
        items = [
            item
            for item in self.imports.values()
            if item.workspace_id == workspace_id and (not unfinished_only or item.status != ImportStatus.APPLIED)
        ]
        return sorted(items, key=lambda item: (item.updated_at, item.import_id), reverse=True)[:limit]

    async def save_resolutions(self, import_id: str, resolutions: list[ResolvedStop]) -> ItineraryImport:
        itinerary_import = self.imports.get(import_id)
        if itinerary_import is None:
            raise ResourceNotFound("import does not exist")
        _assert_draft_mutable(itinerary_import.status)
        if {item.raw_stop_id for item in resolutions} != {item.raw_stop_id for item in itinerary_import.raw_stops}:
            raise InvalidEditCommandError("resolution set must match every parsed raw stop")
        previous = {item.raw_stop_id: item for item in itinerary_import.resolutions}
        versioned = [
            item.model_copy(update={"resolution_version": previous.get(item.raw_stop_id).resolution_version + 1})
            if item.raw_stop_id in previous
            else item
            for item in resolutions
        ]
        ready = resolution_set_is_ready(itinerary_import.raw_stops, versioned)
        stored = itinerary_import.model_copy(
            update={
                "resolutions": versioned,
                "status": ImportStatus.READY if ready else ImportStatus.NEEDS_RESOLUTION,
                "state_version": itinerary_import.state_version + 1,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.imports[import_id] = stored
        return stored

    async def save_resolution(
        self,
        import_id: str,
        resolution: ResolvedStop,
        *,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        async with self._import_locks[import_id]:
            itinerary_import = self.imports.get(import_id)
            if itinerary_import is None:
                raise ResourceNotFound("import does not exist")
            _assert_draft_mutable(itinerary_import.status)
            _assert_state_version(itinerary_import.state_version, expected_state_version)
            if resolution.raw_stop_id not in {item.raw_stop_id for item in itinerary_import.raw_stops}:
                raise ResourceNotFound("raw stop does not belong to import")
            previous = {item.raw_stop_id: item for item in itinerary_import.resolutions}
            prior = previous.get(resolution.raw_stop_id)
            replacement = resolution.model_copy(
                update={
                    "resolution_version": prior.resolution_version + 1 if prior else 1,
                }
            )
            resolutions = [
                replacement if item.raw_stop_id == resolution.raw_stop_id else item
                for item in itinerary_import.resolutions
            ]
            if prior is None:
                resolutions.append(replacement)
            ready = resolution_set_is_ready(itinerary_import.raw_stops, resolutions)
            stored = itinerary_import.model_copy(
                update={
                    "resolutions": resolutions,
                    "status": ImportStatus.READY if ready else ImportStatus.NEEDS_RESOLUTION,
                    "state_version": itinerary_import.state_version + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self.imports[import_id] = stored
            return stored

    async def confirm_resolution(
        self,
        import_id: str,
        raw_stop_id: str,
        place_id: str,
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        return await self.confirm_resolutions(
            import_id,
            {raw_stop_id: place_id},
            actor_user_id,
            expected_state_version,
        )

    async def confirm_resolutions(
        self,
        import_id: str,
        confirmations: dict[str, str],
        actor_user_id: str,
        expected_state_version: int | None = None,
    ) -> ItineraryImport:
        async with self._import_locks[import_id]:
            itinerary_import = self.imports.get(import_id)
            if itinerary_import is None:
                raise ResourceNotFound("import does not exist")
            _assert_draft_mutable(itinerary_import.status)
            _assert_state_version(itinerary_import.state_version, expected_state_version)
            if not confirmations:
                raise InvalidEditCommandError("at least one resolution confirmation is required")
            existing_by_id = {item.raw_stop_id: item for item in itinerary_import.resolutions}
            if not set(confirmations).issubset(existing_by_id):
                raise ResourceNotFound("one or more raw stop resolutions do not exist")
            for raw_stop_id, place_id in confirmations.items():
                resolution = existing_by_id[raw_stop_id]
                selected = next(
                    (candidate for candidate in resolution.candidates if candidate.place_id == place_id),
                    None,
                )
                if selected is None:
                    raise InvalidEditCommandError(
                        "selected place is not an offered resolution candidate",
                        context={"raw_stop_id": raw_stop_id, "place_id": place_id},
                    )
                if selected.resolved_place_receipt is None:
                    raise InvalidEditCommandError(
                        "selected place candidate lacks authoritative provider facts",
                        context={
                            "reason": "CANDIDATE_FACTS_INCOMPLETE",
                            "raw_stop_id": raw_stop_id,
                            "place_id": place_id,
                        },
                    )
            resolutions = []
            for resolution in itinerary_import.resolutions:
                place_id = confirmations.get(resolution.raw_stop_id)
                if place_id is None:
                    resolutions.append(resolution)
                    continue
                resolutions.append(
                    resolution.model_copy(
                        update={
                            "canonical_place_id": place_id,
                            "resolution_status": ResolutionStatus.USER_CONFIRMED,
                            "resolution_version": resolution.resolution_version + 1,
                            "confirmed_by": actor_user_id,
                            "confirmed_at": datetime.now(timezone.utc),
                        }
                    )
                )
            ready = resolution_set_is_ready(itinerary_import.raw_stops, resolutions)
            stored = itinerary_import.model_copy(
                update={
                    "resolutions": resolutions,
                    "status": ImportStatus.READY if ready else ImportStatus.NEEDS_RESOLUTION,
                    "state_version": itinerary_import.state_version + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            self.imports[import_id] = stored
            return stored

    async def apply_ready_import(
        self,
        import_id: str,
        revision: ItineraryRevision,
        *,
        actor_user_id: str,
        expected_state_version: int,
        idempotency_key: str,
        place_records: dict[str, dict[str, Any]],
        place_records_by_stop: dict[str, dict[str, Any]],
        resolved_place_receipts: list[ResolvedPlaceReceipt],
        resolved_place_receipts_by_stop: dict[str, ResolvedPlaceReceipt],
    ) -> ImportApplyResult:
        request_hash = sha256_canonical(
            {
                "operation": "APPLY_IMPORT",
                "import_id": import_id,
                "expected_state_version": expected_state_version,
                "actor_user_id": actor_user_id,
            }
        )
        async with self._import_locks[import_id]:
            command_key = (import_id, idempotency_key)
            existing = self.apply_commands.get(command_key)
            if existing is not None:
                existing_hash, result = existing
                if existing_hash != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different import request")
                return result.model_copy(update={"idempotent_replay": True})
            itinerary_import = self.imports.get(import_id)
            if itinerary_import is None:
                raise ResourceNotFound("import does not exist")
            if itinerary_import.status != ImportStatus.READY:
                if itinerary_import.status in {ImportStatus.APPLIED, ImportStatus.FAILED}:
                    raise InvalidImportStateError(
                        "import is not in an applicable draft state",
                        context={"import_status": itinerary_import.status.value},
                    )
                raise DraftAmbiguousError("all ambiguous or missing places must be resolved before apply")
            _assert_state_version(itinerary_import.state_version, expected_state_version)
            stored = itinerary_import.model_copy(
                update={
                    "status": ImportStatus.APPLIED,
                    "applied_revision": revision.revision,
                    "state_version": itinerary_import.state_version + 1,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            result = ImportApplyResult(
                itinerary_import=stored,
                revision=revision,
                resolved_place_receipts=resolved_place_receipts,
            )
            missing = object()
            workspace_id = revision.workspace_id
            own_snapshots = {
                "current": self.workspaces_current_revision.get(workspace_id, missing),
                "revision": self.applied_revisions.get(workspace_id, missing),
                "places": self.materialized_place_records.get(workspace_id, missing),
                "receipts": self.materialized_place_receipts.get(workspace_id, missing),
                "import": self.imports.get(import_id, missing),
                "command": self.apply_commands.get(command_key, missing),
            }
            itinerary_workspaces = getattr(self.itinerary_repository, "workspaces", None)
            itinerary_revisions = getattr(self.itinerary_repository, "revisions", None)
            upstream_workspace = (
                itinerary_workspaces.get(workspace_id, missing) if isinstance(itinerary_workspaces, dict) else missing
            )
            upstream_revision = (
                itinerary_revisions.get((workspace_id, 1), missing)
                if isinstance(itinerary_revisions, dict)
                else missing
            )

            def restore(mapping: dict, key: Any, value: Any) -> None:
                if value is missing:
                    mapping.pop(key, None)
                else:
                    mapping[key] = value

            try:
                if self.itinerary_repository is not None:
                    await self.itinerary_repository.attach_initial_revision(workspace_id, revision)
                elif self.workspaces_current_revision.get(workspace_id) is not None:
                    raise RevisionConflictError("workspace already has an itinerary revision")
                if self.apply_fault_hook is not None:
                    self.apply_fault_hook("after_revision")
                self.workspaces_current_revision[workspace_id] = revision.revision
                self.applied_revisions[workspace_id] = revision
                self.materialized_place_records[workspace_id] = {
                    place_id: dict(record) for place_id, record in place_records.items()
                }
                self.materialized_place_receipts[workspace_id] = dict(resolved_place_receipts_by_stop)
                if self.apply_fault_hook is not None:
                    self.apply_fault_hook("after_places")
                self.imports[import_id] = stored
                self.apply_commands[command_key] = (request_hash, result)
                return result
            except Exception:
                restore(self.workspaces_current_revision, workspace_id, own_snapshots["current"])
                restore(self.applied_revisions, workspace_id, own_snapshots["revision"])
                restore(self.materialized_place_records, workspace_id, own_snapshots["places"])
                restore(self.materialized_place_receipts, workspace_id, own_snapshots["receipts"])
                restore(self.imports, import_id, own_snapshots["import"])
                restore(self.apply_commands, command_key, own_snapshots["command"])
                if isinstance(itinerary_workspaces, dict):
                    restore(itinerary_workspaces, workspace_id, upstream_workspace)
                if isinstance(itinerary_revisions, dict):
                    restore(itinerary_revisions, (workspace_id, 1), upstream_revision)
                raise
