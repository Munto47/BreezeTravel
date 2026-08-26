from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol
from uuid import uuid4

from app.db.connection import get_pool
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound
from app.operations.errors import IdempotencyLeaseLostError, IdempotencyRequestInProgressError
from app.operations.models import CreationCommandClaim, CreationCommandResponse, CreationOperation


Finalizer = Callable[[Any, dict[str, Any]], Awaitable[CreationCommandResponse]]


class CreationCommandRepository(Protocol):
    async def claim(
        self,
        *,
        workspace_id: str,
        operation: CreationOperation,
        target_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        basis: dict[str, Any],
    ) -> CreationCommandClaim: ...

    async def finalize(
        self,
        claim: CreationCommandClaim,
        finalizer: Finalizer,
    ) -> CreationCommandResponse: ...

    async def abandon(self, claim: CreationCommandClaim) -> None: ...


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


class PostgresCreationCommandRepository:
    def __init__(self, pool: Any | None = None, *, lease_seconds: int = 60):
        self._pool = pool
        self.lease_seconds = lease_seconds

    async def _get_pool(self):
        return self._pool or await get_pool()

    @staticmethod
    def _response_from_row(row: Any) -> CreationCommandResponse:
        return CreationCommandResponse(
            status_code=row["response_status"],
            body=_json_value(row["response_json"]),
            headers=dict(_json_value(row["response_headers_json"]) or {}),
        )

    async def claim(
        self,
        *,
        workspace_id: str,
        operation: CreationOperation,
        target_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        basis: dict[str, Any],
    ) -> CreationCommandClaim:
        pool = await self._get_pool()
        lease_owner = str(uuid4())
        command_id = str(uuid4())
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT workspace_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist", context={"workspace_id": workspace_id})
            await conn.execute(
                """
                INSERT INTO idempotent_creation_commands (
                    command_id, workspace_id, operation, target_id, actor_user_id,
                    idempotency_key, request_hash, basis_json, state,
                    lease_owner, lease_until
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8::jsonb, 'IN_PROGRESS',
                    $9, NOW() + ($10 * INTERVAL '1 second')
                )
                ON CONFLICT (workspace_id, idempotency_key) DO NOTHING
                """,
                command_id,
                workspace_id,
                operation.value,
                target_id,
                actor_user_id,
                idempotency_key,
                request_hash,
                json.dumps(basis, ensure_ascii=False),
                lease_owner,
                self.lease_seconds,
            )
            row = await conn.fetchrow(
                """
                SELECT command.*, NOW() AS command_clock_now
                FROM idempotent_creation_commands AS command
                WHERE workspace_id = $1 AND idempotency_key = $2
                FOR UPDATE
                """,
                workspace_id,
                idempotency_key,
            )
            if row is None:
                raise RuntimeError("creation command disappeared during claim")
            if row["request_hash"].strip() != request_hash:
                raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
            stored_basis = dict(_json_value(row["basis_json"]) or {})
            if row["state"] in {"SUCCEEDED", "FAILED_FINAL"}:
                return CreationCommandClaim(
                    command_id=row["command_id"],
                    workspace_id=workspace_id,
                    operation=CreationOperation(row["operation"]),
                    target_id=row["target_id"],
                    actor_user_id=row["actor_user_id"],
                    idempotency_key=idempotency_key,
                    request_hash=request_hash,
                    basis=stored_basis,
                    replay=self._response_from_row(row).as_replay(),
                )
            command_clock_now = row["command_clock_now"]
            if row["lease_owner"] != lease_owner and row["lease_until"] > command_clock_now:
                raise IdempotencyRequestInProgressError(
                    "an identical idempotent request is still in progress",
                    context={
                        "retry_after_seconds": max(
                            1,
                            int((row["lease_until"] - command_clock_now).total_seconds()),
                        )
                    },
                )
            if row["lease_owner"] != lease_owner:
                await conn.execute(
                    """
                    UPDATE idempotent_creation_commands
                    SET lease_owner = $3,
                        lease_until = NOW() + ($4 * INTERVAL '1 second'),
                        updated_at = NOW()
                    WHERE workspace_id = $1 AND idempotency_key = $2
                    """,
                    workspace_id,
                    idempotency_key,
                    lease_owner,
                    self.lease_seconds,
                )
            return CreationCommandClaim(
                command_id=row["command_id"],
                workspace_id=workspace_id,
                operation=CreationOperation(row["operation"]),
                target_id=row["target_id"],
                actor_user_id=row["actor_user_id"],
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                basis=stored_basis,
                lease_owner=lease_owner,
            )

    async def finalize(
        self,
        claim: CreationCommandClaim,
        finalizer: Finalizer,
    ) -> CreationCommandResponse:
        if claim.replay is not None or claim.lease_owner is None:
            raise ValueError("only an active command claim can be finalized")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT workspace_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                claim.workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            row = await conn.fetchrow(
                "SELECT * FROM idempotent_creation_commands WHERE command_id = $1 FOR UPDATE",
                claim.command_id,
            )
            if row is None or row["state"] != "IN_PROGRESS" or row["lease_owner"] != claim.lease_owner:
                raise IdempotencyLeaseLostError("creation command lease was lost before finalize")
            response = await finalizer(conn, claim.basis)
            await conn.execute(
                """
                UPDATE idempotent_creation_commands
                SET state = 'SUCCEEDED', lease_owner = NULL, lease_until = NULL,
                    response_status = $2, response_headers_json = $3::jsonb,
                    response_json = $4::jsonb, updated_at = NOW()
                WHERE command_id = $1
                """,
                claim.command_id,
                response.status_code,
                json.dumps(response.headers, ensure_ascii=False),
                json.dumps(response.body, ensure_ascii=False),
            )
            return response

    async def abandon(self, claim: CreationCommandClaim) -> None:
        if claim.replay is not None or claim.lease_owner is None:
            return
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                UPDATE idempotent_creation_commands
                SET lease_until = NOW(), updated_at = NOW()
                WHERE command_id = $1 AND state = 'IN_PROGRESS' AND lease_owner = $2
                """,
                claim.command_id,
                claim.lease_owner,
            )


class InMemoryCreationCommandRepository:
    def __init__(self):
        self.commands: dict[tuple[str, str], CreationCommandClaim] = {}
        self.responses: dict[tuple[str, str], CreationCommandResponse] = {}
        self.abandoned: set[tuple[str, str]] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, workspace_id: str) -> asyncio.Lock:
        return self._locks.setdefault(workspace_id, asyncio.Lock())

    async def claim(
        self,
        *,
        workspace_id: str,
        operation: CreationOperation,
        target_id: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        basis: dict[str, Any],
    ) -> CreationCommandClaim:
        async with self._lock(workspace_id):
            key = (workspace_id, idempotency_key)
            existing = self.commands.get(key)
            if existing is not None:
                if existing.request_hash != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
                response = self.responses.get(key)
                if response is not None:
                    return CreationCommandClaim(
                        **{
                            **existing.__dict__,
                            "lease_owner": None,
                            "replay": response.as_replay(),
                        }
                    )
                if key in self.abandoned:
                    replacement = CreationCommandClaim(
                        command_id=existing.command_id,
                        workspace_id=workspace_id,
                        operation=existing.operation,
                        target_id=existing.target_id,
                        actor_user_id=existing.actor_user_id,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        basis=existing.basis,
                        lease_owner=str(uuid4()),
                    )
                    self.commands[key] = replacement
                    self.abandoned.discard(key)
                    return replacement
                raise IdempotencyRequestInProgressError(
                    "an identical idempotent request is still in progress",
                    context={"retry_after_seconds": 1},
                )
            claim = CreationCommandClaim(
                command_id=str(uuid4()),
                workspace_id=workspace_id,
                operation=operation,
                target_id=target_id,
                actor_user_id=actor_user_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                basis=dict(basis),
                lease_owner=str(uuid4()),
            )
            self.commands[key] = claim
            return claim

    async def finalize(
        self,
        claim: CreationCommandClaim,
        finalizer: Finalizer,
    ) -> CreationCommandResponse:
        async with self._lock(claim.workspace_id):
            key = (claim.workspace_id, claim.idempotency_key)
            current = self.commands.get(key)
            if (
                current is None
                or current.command_id != claim.command_id
                or current.lease_owner != claim.lease_owner
                or claim.lease_owner is None
            ):
                raise IdempotencyLeaseLostError("creation command lease was lost before finalize")
            response = await finalizer(None, claim.basis)
            self.responses[key] = response
            self.abandoned.discard(key)
            return response

    async def abandon(self, claim: CreationCommandClaim) -> None:
        async with self._lock(claim.workspace_id):
            key = (claim.workspace_id, claim.idempotency_key)
            current = self.commands.get(key)
            if (
                key not in self.responses
                and current is not None
                and current.command_id == claim.command_id
                and current.lease_owner == claim.lease_owner
            ):
                self.abandoned.add(key)
