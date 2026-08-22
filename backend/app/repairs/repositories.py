from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Protocol

from app.audit.repositories import InMemoryAuditRepository
from app.db.connection import get_pool
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound, RevisionConflictError
from app.itineraries.hash_service import compute_command_request_hash
from app.itineraries.models import WorkspaceStatus
from app.itineraries.repositories import InMemoryItineraryRepository, insert_revision_record
from app.repairs.errors import InvalidRepairDecisionError, RepairStaleError
from app.repairs.models import RepairApplyResult, RepairOperation, RepairOption, RepairStatus
from app.repairs.objective import repair_option_sort_key


class RepairRepository(Protocol):
    async def save_option(self, option: RepairOption, *, conn: Any | None = None) -> RepairOption: ...

    async def get_option(self, repair_id: str) -> RepairOption | None: ...

    async def list_options(self, source_report_id: str) -> list[RepairOption]: ...

    async def reject_option(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        reason: str,
    ) -> RepairOption: ...

    async def apply_option(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        if_match_revision: int,
        idempotency_key: str,
    ) -> RepairApplyResult: ...


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


class PostgresRepairRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def save_option(self, option: RepairOption, *, conn: Any | None = None) -> RepairOption:
        if conn is None:
            pool = await self._get_pool()
            async with pool.acquire() as acquired, acquired.transaction():
                return await self.save_option(option, conn=acquired)
        if True:  # use the caller-owned transaction when one is supplied
            await conn.execute(
                """
                INSERT INTO repair_options (
                    repair_id, source_report_id, base_itinerary_revision,
                    targeted_finding_ids, edit_cost, risk_cost, route_cost_delta,
                    new_unknown_count, tradeoffs_json, affected_member_ids,
                    result_preview_json, postcheck_report_id, status, decided_by,
                    decision_reason, decided_at, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10, $11::jsonb, $12, $13, $14, $15, $16, $17)
                """,
                option.repair_id,
                option.source_report_id,
                option.base_itinerary_revision,
                option.targeted_finding_ids,
                option.edit_cost,
                option.risk_cost,
                option.route_cost_delta,
                option.new_unknown_count,
                json.dumps(option.tradeoffs, ensure_ascii=False),
                option.affected_member_ids,
                json.dumps(option.result_preview.model_dump(mode="json"), ensure_ascii=False),
                option.postcheck_report_id,
                option.status.value,
                option.decided_by,
                option.decision_reason,
                option.decided_at,
                option.created_at,
            )
            for index, operation in enumerate(option.operations):
                await conn.execute(
                    """
                    INSERT INTO repair_operations(repair_id, operation_index, operation, payload_json)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    option.repair_id,
                    index,
                    operation.operation.value,
                    json.dumps(
                        {
                            "payload": operation.payload,
                            "rationale": operation.rationale,
                        },
                        ensure_ascii=False,
                    ),
                )
        return option

    async def _get_with_conn(self, conn: Any, repair_id: str) -> RepairOption | None:
        row = await conn.fetchrow("SELECT * FROM repair_options WHERE repair_id = $1", repair_id)
        if row is None:
            return None
        operation_rows = await conn.fetch(
            "SELECT * FROM repair_operations WHERE repair_id = $1 ORDER BY operation_index",
            repair_id,
        )
        operations = []
        for operation_row in operation_rows:
            data = _json_value(operation_row["payload_json"])
            operations.append(
                RepairOperation(
                    operation=operation_row["operation"],
                    payload=data.get("payload", {}),
                    rationale=data.get("rationale", ""),
                )
            )
        return RepairOption(
            repair_id=row["repair_id"],
            source_report_id=row["source_report_id"],
            base_itinerary_revision=row["base_itinerary_revision"],
            operations=operations,
            targeted_finding_ids=list(row["targeted_finding_ids"] or []),
            edit_cost=row["edit_cost"],
            risk_cost=row["risk_cost"],
            route_cost_delta=row["route_cost_delta"],
            new_unknown_count=row["new_unknown_count"],
            tradeoffs=list(_json_value(row["tradeoffs_json"]) or []),
            affected_member_ids=list(row["affected_member_ids"] or []),
            result_preview=_json_value(row["result_preview_json"]),
            postcheck_report_id=row["postcheck_report_id"],
            status=row["status"],
            decided_by=row["decided_by"],
            decision_reason=row["decision_reason"],
            decided_at=row["decided_at"],
            created_at=row["created_at"],
        )

    async def get_option(self, repair_id: str) -> RepairOption | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await self._get_with_conn(conn, repair_id)

    async def list_options(self, source_report_id: str) -> list[RepairOption]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            ids = await conn.fetch(
                "SELECT repair_id FROM repair_options WHERE source_report_id = $1 ORDER BY repair_id",
                source_report_id,
            )
            options = [
                option for row in ids if (option := await self._get_with_conn(conn, row["repair_id"])) is not None
            ]
            return sorted(options, key=repair_option_sort_key)

    async def reject_option(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        reason: str,
    ) -> RepairOption:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise InvalidRepairDecisionError("repair rejection reason is required")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            locked = await conn.fetchrow(
                """
                SELECT status, decided_by, decision_reason
                FROM repair_options WHERE repair_id = $1 FOR UPDATE
                """,
                repair_id,
            )
            if locked is None:
                raise ResourceNotFound("repair option does not exist")
            if (
                locked["status"] == RepairStatus.REJECTED.value
                and locked["decided_by"] == actor_user_id
                and locked["decision_reason"] == normalized_reason
            ):
                rejected = await self._get_with_conn(conn, repair_id)
                if rejected is None:
                    raise RuntimeError("repair option disappeared during reject replay")
                return rejected
            if locked["status"] != RepairStatus.PROPOSED.value:
                raise RepairStaleError("repair option is no longer proposed")
            decided_at = datetime.now(timezone.utc)
            update_status = await conn.execute(
                """
                UPDATE repair_options
                SET status = 'REJECTED', decided_by = $2, decision_reason = $3, decided_at = $4
                WHERE repair_id = $1 AND status = 'PROPOSED'
                """,
                repair_id,
                actor_user_id,
                normalized_reason,
                decided_at,
            )
            if update_status != "UPDATE 1":
                raise RepairStaleError("repair option is no longer proposed")
            rejected = await self._get_with_conn(conn, repair_id)
            if rejected is None:
                raise RuntimeError("repair option disappeared during reject")
            return rejected

    async def apply_option(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        if_match_revision: int,
        idempotency_key: str,
    ) -> RepairApplyResult:
        pool = await self._get_pool()
        request_hash = compute_command_request_hash(
            {
                "repair_id": repair_id,
                "actor_user_id": actor_user_id,
                "base_revision": if_match_revision,
            }
        )
        async with pool.acquire() as conn, conn.transaction():
            identity = await conn.fetchrow(
                """
                SELECT ro.source_report_id, ar.workspace_id
                FROM repair_options ro
                JOIN audit_reports ar ON ar.report_id = ro.source_report_id
                WHERE ro.repair_id = $1
                """,
                repair_id,
            )
            if identity is None:
                raise ResourceNotFound("repair option does not exist")

            # Every workspace mutation locks the workspace first. Locking all
            # sibling options in a deterministic order afterwards prevents two
            # concurrent applies from holding different option rows while each
            # waits on the other's workspace/sibling lock.
            workspace = await conn.fetchrow(
                "SELECT current_itinerary_revision, current_report_id "
                "FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                identity["workspace_id"],
            )
            await conn.fetch(
                """
                SELECT repair_id
                FROM repair_options
                WHERE source_report_id = $1
                ORDER BY repair_id
                FOR UPDATE
                """,
                identity["source_report_id"],
            )
            row = await conn.fetchrow(
                """
                SELECT ro.*, ar.workspace_id,
                       pr.workspace_id AS postcheck_workspace_id,
                       pr.itinerary_revision AS postcheck_revision
                FROM repair_options ro
                JOIN audit_reports ar ON ar.report_id = ro.source_report_id
                JOIN audit_reports pr ON pr.report_id = ro.postcheck_report_id
                WHERE ro.repair_id = $1
                """,
                repair_id,
            )
            if row is None:
                raise ResourceNotFound("repair option does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM itinerary_edit_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                row["workspace_id"],
                idempotency_key,
            )
            if existing:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was used for another request")
                replay = RepairApplyResult.model_validate(_json_value(existing["response_json"]))
                return replay.model_copy(update={"idempotent_replay": True})
            if row["status"] != RepairStatus.PROPOSED.value:
                raise RepairStaleError("repair option is no longer proposed")
            actual_revision = workspace["current_itinerary_revision"] if workspace else None
            current_report_id = workspace["current_report_id"] if workspace else None
            if (
                actual_revision != if_match_revision
                or row["base_itinerary_revision"] != if_match_revision
                or current_report_id != row["source_report_id"]
            ):
                raise RevisionConflictError(
                    "repair base revision is stale",
                    context={
                        "expected_revision": if_match_revision,
                        "actual_revision": actual_revision,
                        "expected_report_id": row["source_report_id"],
                        "actual_report_id": current_report_id,
                    },
                )
            if not row["postcheck_report_id"]:
                raise RepairStaleError("repair option has no postcheck report")
            if (
                row["postcheck_workspace_id"] != row["workspace_id"]
                or row["postcheck_revision"] != if_match_revision + 1
            ):
                raise RepairStaleError("repair postcheck is not bound to the preview revision")
            revision_data = _json_value(row["result_preview_json"])
            revision_data["created_by"] = actor_user_id
            revision_data["created_at"] = datetime.now(timezone.utc).isoformat()
            from app.itineraries.models import ItineraryRevision

            revision = ItineraryRevision.model_validate(revision_data)
            if revision.revision != if_match_revision + 1:
                raise RepairStaleError("repair preview revision is inconsistent")
            await insert_revision_record(conn, revision)
            option = await self._get_with_conn(conn, repair_id)
            if option is None:
                raise RuntimeError("repair option disappeared during apply")
            decided_at = datetime.now(timezone.utc)
            applied_option = option.model_copy(
                update={
                    "status": RepairStatus.APPLIED,
                    "decided_by": actor_user_id,
                    "decided_at": decided_at,
                }
            )
            result = RepairApplyResult(
                repair=applied_option,
                new_revision=revision.revision,
                postcheck_report_id=row["postcheck_report_id"],
            )
            response_json = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
            await conn.execute(
                """
                INSERT INTO itinerary_edit_commands (
                    command_id, workspace_id, base_revision, result_revision, actor_user_id,
                    operation, payload_json, request_hash, idempotency_key, response_json
                ) VALUES ($1, $2, $3, $4, $5, 'APPLY_REPAIR', $6::jsonb, $7, $8, $9::jsonb)
                """,
                f"repair:{repair_id}",
                row["workspace_id"],
                if_match_revision,
                revision.revision,
                actor_user_id,
                json.dumps({"repair_id": repair_id}),
                request_hash,
                idempotency_key,
                response_json,
            )
            await conn.execute(
                """
                UPDATE trip_workspaces
                SET current_itinerary_revision = $2, current_report_id = $3,
                    status = 'NEEDS_CONFIRMATION', updated_at = NOW()
                WHERE workspace_id = $1
                """,
                row["workspace_id"],
                revision.revision,
                row["postcheck_report_id"],
            )
            update_status = await conn.execute(
                """
                UPDATE repair_options
                SET status = 'APPLIED', decided_by = $2, decided_at = $3
                WHERE repair_id = $1 AND status = 'PROPOSED'
                """,
                repair_id,
                actor_user_id,
                decided_at,
            )
            if update_status != "UPDATE 1":
                raise RepairStaleError("repair option is no longer proposed")
            await conn.execute(
                """
                UPDATE repair_options
                SET status = 'STALE', decided_at = $3
                WHERE source_report_id = $1 AND repair_id <> $2 AND status = 'PROPOSED'
                """,
                row["source_report_id"],
                repair_id,
                decided_at,
            )
            return result


class InMemoryRepairRepository:
    def __init__(
        self,
        itinerary_repository: InMemoryItineraryRepository,
        audit_repository: InMemoryAuditRepository,
    ):
        self.options: dict[str, RepairOption] = {}
        self.itinerary_repository = itinerary_repository
        self.audit_repository = audit_repository
        self.idempotency: dict[tuple[str, str], tuple[str, RepairApplyResult]] = {}
        self._decision_locks: dict[str, asyncio.Lock] = {}

    def _decision_lock(self, repair_id: str) -> asyncio.Lock:
        lock = self._decision_locks.get(repair_id)
        if lock is None:
            lock = asyncio.Lock()
            self._decision_locks[repair_id] = lock
        return lock

    async def save_option(self, option: RepairOption, *, conn: Any | None = None) -> RepairOption:
        self.options[option.repair_id] = option
        return option

    async def get_option(self, repair_id: str) -> RepairOption | None:
        return self.options.get(repair_id)

    async def list_options(self, source_report_id: str) -> list[RepairOption]:
        return sorted(
            [item for item in self.options.values() if item.source_report_id == source_report_id],
            key=repair_option_sort_key,
        )

    async def reject_option(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        reason: str,
    ) -> RepairOption:
        async with self._decision_lock(repair_id):
            return await self._reject_option_locked(
                repair_id,
                actor_user_id=actor_user_id,
                reason=reason,
            )

    async def _reject_option_locked(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        reason: str,
    ) -> RepairOption:
        option = self.options.get(repair_id)
        if option is None:
            raise ResourceNotFound("repair option does not exist")
        normalized_reason = reason.strip()
        if (
            option.status == RepairStatus.REJECTED
            and option.decided_by == actor_user_id
            and option.decision_reason == normalized_reason
        ):
            return option
        if option.status != RepairStatus.PROPOSED:
            raise RepairStaleError("repair option is no longer proposed")
        if not normalized_reason:
            raise InvalidRepairDecisionError("repair rejection reason is required")
        rejected = option.model_copy(
            update={
                "status": RepairStatus.REJECTED,
                "decided_by": actor_user_id,
                "decision_reason": normalized_reason,
                "decided_at": datetime.now(timezone.utc),
            }
        )
        self.options[repair_id] = rejected
        return rejected

    async def apply_option(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        if_match_revision: int,
        idempotency_key: str,
    ) -> RepairApplyResult:
        async with self._decision_lock(repair_id):
            return await self._apply_option_locked(
                repair_id,
                actor_user_id=actor_user_id,
                if_match_revision=if_match_revision,
                idempotency_key=idempotency_key,
            )

    async def _apply_option_locked(
        self,
        repair_id: str,
        *,
        actor_user_id: str,
        if_match_revision: int,
        idempotency_key: str,
    ) -> RepairApplyResult:
        option = self.options.get(repair_id)
        if option is None:
            raise ResourceNotFound("repair option does not exist")
        request_hash = compute_command_request_hash(
            {
                "repair_id": repair_id,
                "actor_user_id": actor_user_id,
                "base_revision": if_match_revision,
            }
        )
        key = (option.result_preview.workspace_id, idempotency_key)
        existing = self.idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError("idempotency key was used for another request")
            return existing[1].model_copy(update={"idempotent_replay": True})
        workspace = await self.itinerary_repository.get_workspace(option.result_preview.workspace_id)
        actual = workspace.current_itinerary_revision if workspace else None
        current_report_id = workspace.current_report_id if workspace else None
        if (
            option.status != RepairStatus.PROPOSED
            or actual != if_match_revision
            or option.base_itinerary_revision != if_match_revision
            or current_report_id != option.source_report_id
        ):
            raise RevisionConflictError(
                "repair base revision is stale",
                context={
                    "expected_revision": if_match_revision,
                    "actual_revision": actual,
                    "expected_report_id": option.source_report_id,
                    "actual_report_id": current_report_id,
                },
            )
        postcheck = await self.audit_repository.get_report(option.postcheck_report_id)
        if (
            postcheck is None
            or postcheck.workspace_id != option.result_preview.workspace_id
            or postcheck.itinerary_revision != if_match_revision + 1
        ):
            raise RepairStaleError("repair postcheck is not bound to the preview revision")
        revision = option.result_preview.model_copy(
            update={
                "created_by": actor_user_id,
                "created_at": datetime.now(timezone.utc),
            }
        )
        self.itinerary_repository.revisions[(revision.workspace_id, revision.revision)] = revision
        self.itinerary_repository.workspaces[revision.workspace_id] = workspace.model_copy(
            update={
                "current_itinerary_revision": revision.revision,
                "current_report_id": option.postcheck_report_id,
                "status": WorkspaceStatus.NEEDS_CONFIRMATION,
            }
        )
        applied = option.model_copy(
            update={
                "status": RepairStatus.APPLIED,
                "decided_by": actor_user_id,
                "decided_at": datetime.now(timezone.utc),
            }
        )
        self.options[repair_id] = applied
        for sibling_id, sibling in list(self.options.items()):
            if (
                sibling_id != repair_id
                and sibling.source_report_id == option.source_report_id
                and sibling.status == RepairStatus.PROPOSED
            ):
                self.options[sibling_id] = sibling.model_copy(
                    update={
                        "status": RepairStatus.STALE,
                        "decided_at": applied.decided_at,
                    }
                )
        self.audit_repository.current_revisions[revision.workspace_id] = revision.revision
        self.audit_repository.current_reports[revision.workspace_id] = option.postcheck_report_id
        result = RepairApplyResult(
            repair=applied,
            new_revision=revision.revision,
            postcheck_report_id=option.postcheck_report_id,
        )
        self.idempotency[key] = (request_hash, result)
        return result
