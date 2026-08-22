from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.db.connection import get_pool
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound, RevisionConflictError
from app.itineraries.models import (
    ItineraryEditCommand,
    ItineraryPatchResult,
    ItineraryRevision,
    TripDateRange,
    TripWorkspace,
    WorkspaceStatus,
)


CommandBuilder = Callable[
    [TripWorkspace, ItineraryRevision],
    tuple[ItineraryRevision, ItineraryPatchResult, WorkspaceStatus],
]
CommandPrecondition = Callable[[Any | None, TripWorkspace, ItineraryRevision], Awaitable[None]]


class ItineraryRepository(Protocol):
    async def create_workspace(
        self,
        workspace: TripWorkspace,
        initial_revision: ItineraryRevision | None = None,
    ) -> TripWorkspace: ...

    async def get_workspace(self, workspace_id: str) -> TripWorkspace | None: ...

    async def attach_initial_revision(self, workspace_id: str, revision: ItineraryRevision) -> TripWorkspace: ...

    async def attach_initial_revision_in_transaction(
        self, conn: Any | None, workspace_id: str, revision: ItineraryRevision
    ) -> TripWorkspace: ...

    async def get_revision(self, workspace_id: str, revision: int) -> ItineraryRevision | None: ...

    async def list_revisions(self, workspace_id: str) -> list[ItineraryRevision]: ...

    async def execute_command(
        self,
        command: ItineraryEditCommand,
        *,
        idempotency_key: str,
        request_hash: str,
        builder: CommandBuilder,
        precondition: CommandPrecondition | None = None,
    ) -> ItineraryPatchResult: ...


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _workspace_from_row(row: Any) -> TripWorkspace:
    return TripWorkspace(
        workspace_id=row["workspace_id"],
        room_id=row["room_id"],
        city=row["city"],
        trip_date_range=TripDateRange(start=row["trip_start_date"], end=row["trip_end_date"]),
        current_itinerary_revision=row["current_itinerary_revision"],
        current_task_spec_revision=row["current_task_spec_revision"],
        current_member_constraint_revision=row["current_member_constraint_revision"],
        current_report_id=row["current_report_id"],
        current_import_id=row.get("current_import_id"),
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _revision_from_row(row: Any) -> ItineraryRevision:
    return ItineraryRevision(
        itinerary_id=row["itinerary_id"],
        workspace_id=row["workspace_id"],
        revision=row["revision"],
        parent_revision=row["parent_revision"],
        source_type=row["source_type"],
        city=row["city"],
        date_range=TripDateRange(start=row["trip_start_date"], end=row["trip_end_date"]),
        days=_json_value(row["days_json"]),
        locked_commitments=_json_value(row["locked_commitments_json"]),
        change_summary=_json_value(row["change_summary_json"]),
        content_hash=row["content_hash"].strip(),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


async def insert_revision_record(conn: Any, revision: ItineraryRevision) -> None:
    await conn.execute(
        """
        INSERT INTO itinerary_revisions (
            itinerary_id, workspace_id, revision, parent_revision, source_type,
            city, trip_start_date, trip_end_date, days_json, locked_commitments_json,
            change_summary_json, content_hash, created_by, created_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, $11::jsonb, $12, $13, $14)
        """,
        revision.itinerary_id,
        revision.workspace_id,
        revision.revision,
        revision.parent_revision,
        revision.source_type.value,
        revision.city,
        revision.date_range.start,
        revision.date_range.end,
        json.dumps([day.model_dump(mode="json") for day in revision.days], ensure_ascii=False),
        json.dumps(revision.locked_commitments, ensure_ascii=False),
        json.dumps(revision.change_summary, ensure_ascii=False),
        revision.content_hash,
        revision.created_by,
        revision.created_at,
    )


class PostgresItineraryRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def create_workspace(
        self,
        workspace: TripWorkspace,
        initial_revision: ItineraryRevision | None = None,
    ) -> TripWorkspace:
        if initial_revision and initial_revision.workspace_id != workspace.workspace_id:
            raise ValueError("initial revision belongs to another workspace")
        if initial_revision and initial_revision.revision != 1:
            raise ValueError("initial revision must be revision 1")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO trip_workspaces (
                    workspace_id, room_id, city, trip_start_date, trip_end_date,
                    current_itinerary_revision, current_task_spec_revision,
                    current_member_constraint_revision, current_report_id,
                    current_import_id, status, created_by, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                workspace.workspace_id,
                workspace.room_id,
                workspace.city,
                workspace.trip_date_range.start,
                workspace.trip_date_range.end,
                initial_revision.revision if initial_revision else workspace.current_itinerary_revision,
                workspace.current_task_spec_revision,
                workspace.current_member_constraint_revision,
                workspace.current_report_id,
                workspace.current_import_id,
                workspace.status.value,
                workspace.created_by,
                workspace.created_at,
                workspace.updated_at,
            )
            if initial_revision:
                await insert_revision_record(conn, initial_revision)
        created = await self.get_workspace(workspace.workspace_id)
        if created is None:
            raise RuntimeError("workspace insert did not persist")
        return created

    async def get_workspace(self, workspace_id: str) -> TripWorkspace | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trip_workspaces WHERE workspace_id = $1", workspace_id)
        return _workspace_from_row(row) if row else None

    async def attach_initial_revision(self, workspace_id: str, revision: ItineraryRevision) -> TripWorkspace:
        if revision.workspace_id != workspace_id or revision.revision != 1:
            raise ValueError("initial revision must be revision 1 for the target workspace")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                workspace_id,
            )
            if row is None:
                raise ResourceNotFound("workspace does not exist")
            if row["current_itinerary_revision"] is not None:
                raise RevisionConflictError(
                    "workspace already has an itinerary revision",
                    context={"actual_revision": row["current_itinerary_revision"]},
                )
            await insert_revision_record(conn, revision)
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision = 1, current_report_id = NULL,
                    status = 'DRAFT', updated_at = NOW()
                WHERE workspace_id = $1
                """,
                workspace_id,
            )
        result = await self.get_workspace(workspace_id)
        if result is None:
            raise RuntimeError("workspace disappeared after attaching initial revision")
        return result

    async def attach_initial_revision_in_transaction(
        self, conn: Any | None, workspace_id: str, revision: ItineraryRevision
    ) -> TripWorkspace:
        """Attach revision one using the caller's command-ledger transaction."""
        if conn is None:
            return await self.attach_initial_revision(workspace_id, revision)
        if revision.workspace_id != workspace_id or revision.revision != 1:
            raise ValueError("initial revision must be revision 1 for the target workspace")
        current = await conn.fetchrow(
            "SELECT current_itinerary_revision FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
            workspace_id,
        )
        if current is None:
            raise ResourceNotFound("workspace does not exist")
        if current["current_itinerary_revision"] is not None:
            raise RevisionConflictError(
                "workspace already has an itinerary revision",
                context={"actual_revision": current["current_itinerary_revision"]},
            )
        await insert_revision_record(conn, revision)
        row = await conn.fetchrow(
            """
            UPDATE trip_workspaces
            SET current_itinerary_revision = 1, current_report_id = NULL,
                status = 'DRAFT', updated_at = NOW()
            WHERE workspace_id = $1
            RETURNING *
            """,
            workspace_id,
        )
        if row is None:
            raise RuntimeError("workspace disappeared after attaching initial revision")
        return _workspace_from_row(row)

    async def get_revision(self, workspace_id: str, revision: int) -> ItineraryRevision | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM itinerary_revisions WHERE workspace_id = $1 AND revision = $2",
                workspace_id,
                revision,
            )
        return _revision_from_row(row) if row else None

    async def list_revisions(self, workspace_id: str) -> list[ItineraryRevision]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM itinerary_revisions WHERE workspace_id = $1 ORDER BY revision",
                workspace_id,
            )
        return [_revision_from_row(row) for row in rows]

    async def get_legacy_verification_report(self, workspace_id: str, report_id: str) -> dict[str, Any] | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT vr.report_json
                FROM verification_reports vr
                JOIN trip_workspaces tw ON tw.room_id = vr.room_id
                WHERE tw.workspace_id = $1 AND vr.report_id = $2
                """,
                workspace_id,
                report_id,
            )
        return _json_value(row["report_json"]) if row else None

    async def execute_command(
        self,
        command: ItineraryEditCommand,
        *,
        idempotency_key: str,
        request_hash: str,
        builder: CommandBuilder,
        precondition: CommandPrecondition | None = None,
    ) -> ItineraryPatchResult:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace_row = await conn.fetchrow(
                "SELECT * FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                command.workspace_id,
            )
            if workspace_row is None:
                raise ResourceNotFound("workspace does not exist", context={"workspace_id": command.workspace_id})

            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json
                FROM itinerary_edit_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                command.workspace_id,
                idempotency_key,
            )
            if existing:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
                result = ItineraryPatchResult.model_validate(_json_value(existing["response_json"]))
                return result.model_copy(update={"idempotent_replay": True})

            workspace = _workspace_from_row(workspace_row)
            actual_revision = workspace.current_itinerary_revision
            if actual_revision != command.base_revision:
                raise RevisionConflictError(
                    "base revision is stale",
                    context={"expected_revision": command.base_revision, "actual_revision": actual_revision},
                )
            revision_row = await conn.fetchrow(
                "SELECT * FROM itinerary_revisions WHERE workspace_id = $1 AND revision = $2",
                command.workspace_id,
                command.base_revision,
            )
            if revision_row is None:
                raise ResourceNotFound("base revision does not exist")
            base_revision = _revision_from_row(revision_row)
            if precondition is not None:
                await precondition(conn, workspace, base_revision)
            new_revision, result, next_status = builder(workspace, base_revision)
            await insert_revision_record(conn, new_revision)
            response_json = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
            await conn.execute(
                """
                INSERT INTO itinerary_edit_commands (
                    command_id, workspace_id, base_revision, result_revision, actor_user_id,
                    operation, payload_json, request_hash, idempotency_key, client_timestamp,
                    response_json
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11::jsonb)
                """,
                command.command_id,
                command.workspace_id,
                command.base_revision,
                new_revision.revision,
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
                SET current_itinerary_revision = $2,
                    current_report_id = NULL,
                    status = $3,
                    updated_at = NOW()
                WHERE workspace_id = $1
                """,
                command.workspace_id,
                new_revision.revision,
                next_status.value,
            )
            return result


class InMemoryItineraryRepository:
    """Deterministic repository for unit tests; mirrors the production write contract."""

    def __init__(self):
        self.workspaces: dict[str, TripWorkspace] = {}
        self.revisions: dict[tuple[str, int], ItineraryRevision] = {}
        self.commands: dict[tuple[str, str], tuple[str, ItineraryPatchResult]] = {}
        self._lock = asyncio.Lock()

    async def create_workspace(
        self,
        workspace: TripWorkspace,
        initial_revision: ItineraryRevision | None = None,
    ) -> TripWorkspace:
        async with self._lock:
            if workspace.workspace_id in self.workspaces:
                raise ValueError("workspace already exists")
            current = initial_revision.revision if initial_revision else workspace.current_itinerary_revision
            stored = workspace.model_copy(update={"current_itinerary_revision": current})
            self.workspaces[workspace.workspace_id] = stored
            if initial_revision:
                self.revisions[(workspace.workspace_id, initial_revision.revision)] = initial_revision
            return stored

    async def get_workspace(self, workspace_id: str) -> TripWorkspace | None:
        return self.workspaces.get(workspace_id)

    async def attach_initial_revision(self, workspace_id: str, revision: ItineraryRevision) -> TripWorkspace:
        async with self._lock:
            workspace = self.workspaces.get(workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            if workspace.current_itinerary_revision is not None:
                raise RevisionConflictError(
                    "workspace already has an itinerary revision",
                    context={"actual_revision": workspace.current_itinerary_revision},
                )
            if revision.workspace_id != workspace_id or revision.revision != 1:
                raise ValueError("initial revision must be revision 1 for the target workspace")
            self.revisions[(workspace_id, 1)] = revision
            stored = workspace.model_copy(update={
                "current_itinerary_revision": 1,
                "current_report_id": None,
                "status": WorkspaceStatus.DRAFT,
            })
            self.workspaces[workspace_id] = stored
            return stored

    async def attach_initial_revision_in_transaction(
        self, conn: Any | None, workspace_id: str, revision: ItineraryRevision
    ) -> TripWorkspace:
        return await self.attach_initial_revision(workspace_id, revision)

    async def get_revision(self, workspace_id: str, revision: int) -> ItineraryRevision | None:
        return self.revisions.get((workspace_id, revision))

    async def list_revisions(self, workspace_id: str) -> list[ItineraryRevision]:
        return [
            revision
            for (candidate_workspace, _), revision in sorted(self.revisions.items(), key=lambda item: item[0][1])
            if candidate_workspace == workspace_id
        ]

    async def execute_command(
        self,
        command: ItineraryEditCommand,
        *,
        idempotency_key: str,
        request_hash: str,
        builder: CommandBuilder,
        precondition: CommandPrecondition | None = None,
    ) -> ItineraryPatchResult:
        async with self._lock:
            workspace = self.workspaces.get(command.workspace_id)
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            command_key = (command.workspace_id, idempotency_key)
            existing = self.commands.get(command_key)
            if existing:
                existing_hash, result = existing
                if existing_hash != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
                return result.model_copy(update={"idempotent_replay": True})
            if workspace.current_itinerary_revision != command.base_revision:
                raise RevisionConflictError(
                    "base revision is stale",
                    context={
                        "expected_revision": command.base_revision,
                        "actual_revision": workspace.current_itinerary_revision,
                    },
                )
            base = self.revisions.get((command.workspace_id, command.base_revision))
            if base is None:
                raise ResourceNotFound("base revision does not exist")
            if precondition is not None:
                await precondition(None, workspace, base)
            new_revision, result, next_status = builder(workspace, base)
            self.revisions[(command.workspace_id, new_revision.revision)] = new_revision
            self.workspaces[command.workspace_id] = workspace.model_copy(update={
                "current_itinerary_revision": new_revision.revision,
                "current_report_id": None,
                "status": next_status,
            })
            self.commands[command_key] = (request_hash, result)
            return result
