from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.db.connection import get_pool
from app.itineraries.errors import IdempotencyKeyReusedError, ResourceNotFound
from app.itineraries.hash_service import sha256_canonical
from app.itineraries.repositories import ItineraryRepository
from app.trip_check.briefs import TripBriefRepository
from app.trip_check.errors import (
    RunConfigMismatchError,
    TripBriefConfirmationRequiredError,
    TripCheckRunConflictError,
    TripCheckRunNotResumableError,
)
from app.trip_check.models import (
    RunSpec,
    SideEffectReceipt,
    TripBriefStatus,
    TripCheckRun,
    TripCheckRunEvent,
    TripCheckRunStatus,
    TripCheckStage,
)


def run_config_hash(run_spec: RunSpec) -> str:
    return sha256_canonical(run_spec.model_dump(mode="json"))


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _run_from_row(row: Any) -> TripCheckRun:
    return TripCheckRun(
        run_id=row["run_id"],
        workspace_id=row["workspace_id"],
        itinerary_revision=row["itinerary_revision"],
        brief_id=row["brief_id"],
        brief_revision=row["brief_revision"],
        stage=row["stage"],
        stage_attempt=row["stage_attempt"],
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        run_spec=_json_value(row["run_spec_json"]),
        config_hash=row["config_hash"].strip(),
        completed_stages=_json_value(row["completed_stages_json"]),
        partial_failures=_json_value(row["partial_failures_json"]),
        status=row["status"],
        evidence_snapshot_id=row["evidence_snapshot_id"],
        report_id=row["report_id"],
        advice_bundle_id=row["advice_bundle_id"],
        version=row["version"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event_from_row(row: Any) -> TripCheckRunEvent:
    return TripCheckRunEvent(
        event_id=row["event_id"],
        run_id=row["run_id"],
        event_type=row["event_type"],
        stage=row["stage"],
        run_version=row["run_version"],
        payload=_json_value(row["payload_json"]),
        created_at=row["created_at"],
    )


def _receipt_from_row(row: Any) -> SideEffectReceipt:
    return SideEffectReceipt(
        receipt_id=row["receipt_id"],
        run_id=row["run_id"],
        stage=row["stage"],
        side_effect_key=row["side_effect_key"],
        effect_type=row["effect_type"],
        request_hash=row["request_hash"].strip(),
        response_hash=row["response_hash"].strip() if row["response_hash"] else None,
        provider=row["provider"],
        status=row["status"],
        receipt=_json_value(row["receipt_json"]),
        created_at=row["created_at"],
    )


class TripCheckRunRepository(Protocol):
    async def create_run(
        self,
        run: TripCheckRun,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripCheckRun, bool]: ...

    async def get_run(self, run_id: str) -> TripCheckRun | None: ...

    async def resume_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        config_hash: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> tuple[TripCheckRun, bool]: ...

    async def list_events(self, run_id: str, *, after_event_id: int = 0) -> list[TripCheckRunEvent]: ...

    async def save_receipt(self, receipt: SideEffectReceipt) -> tuple[SideEffectReceipt, bool]: ...

    async def get_receipt(self, run_id: str, side_effect_key: str) -> SideEffectReceipt | None: ...


class PostgresTripCheckRunRepository:
    def __init__(self, pool: Any | None = None, *, lease_seconds: int = 60):
        self._pool = pool
        self.lease_seconds = lease_seconds

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def create_run(
        self,
        run: TripCheckRun,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripCheckRun, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            workspace = await conn.fetchrow(
                "SELECT workspace_id FROM trip_workspaces WHERE workspace_id = $1 FOR UPDATE",
                run.workspace_id,
            )
            if workspace is None:
                raise ResourceNotFound("workspace does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json FROM trip_check_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                run.workspace_id,
                idempotency_key,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
                response = _json_value(existing["response_json"])
                row = await conn.fetchrow("SELECT * FROM trip_check_runs WHERE run_id = $1", response["run_id"])
                if row is None:
                    raise RuntimeError("idempotent run command points to a missing run")
                return _run_from_row(row), True
            await conn.execute(
                """
                INSERT INTO trip_check_runs (
                    run_id, workspace_id, itinerary_revision, brief_id, brief_revision,
                    stage, stage_attempt, status, lease_owner, lease_until,
                    run_spec_json, config_hash, completed_stages_json,
                    partial_failures_json, evidence_snapshot_id, report_id,
                    advice_bundle_id, version, created_by, created_at, updated_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                    $11::jsonb, $12, $13::jsonb, $14::jsonb, $15, $16,
                    $17, $18, $19, $20, $21
                )
                """,
                run.run_id,
                run.workspace_id,
                run.itinerary_revision,
                run.brief_id,
                run.brief_revision,
                run.stage.value,
                run.stage_attempt,
                run.status.value,
                run.lease_owner,
                run.lease_until,
                json.dumps(run.run_spec.model_dump(mode="json"), ensure_ascii=False),
                run.config_hash,
                json.dumps([item.value for item in run.completed_stages]),
                json.dumps([item.model_dump(mode="json") for item in run.partial_failures]),
                run.evidence_snapshot_id,
                run.report_id,
                run.advice_bundle_id,
                run.version,
                run.created_by,
                run.created_at,
                run.updated_at,
            )
            await conn.execute(
                "UPDATE trip_workspaces SET current_trip_check_run_id = $2, updated_at = NOW() WHERE workspace_id = $1",
                run.workspace_id,
                run.run_id,
            )
            await conn.execute(
                """
                INSERT INTO trip_check_commands (
                    command_id, workspace_id, run_id, operation, actor_user_id,
                    idempotency_key, request_hash, response_status,
                    response_headers_json, response_json
                ) VALUES ($1, $2, $3, 'CREATE_RUN', $4, $5, $6, 201, $7::jsonb, $8::jsonb)
                """,
                str(uuid4()),
                run.workspace_id,
                run.run_id,
                run.created_by,
                idempotency_key,
                request_hash,
                json.dumps({"ETag": f'"{run.version}"'}),
                json.dumps({"run_id": run.run_id}),
            )
            await conn.execute(
                """
                INSERT INTO trip_check_run_events (run_id, event_type, stage, run_version, payload_json)
                VALUES ($1, 'run_created', $2, $3, $4::jsonb)
                """,
                run.run_id,
                run.stage.value,
                run.version,
                json.dumps({"status": run.status.value}),
            )
        return run, False

    async def get_run(self, run_id: str) -> TripCheckRun | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM trip_check_runs WHERE run_id = $1", run_id)
        return _run_from_row(row) if row else None

    async def resume_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        config_hash: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> tuple[TripCheckRun, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow("SELECT * FROM trip_check_runs WHERE run_id = $1 FOR UPDATE", run_id)
            if row is None:
                raise ResourceNotFound("trip check run does not exist")
            existing = await conn.fetchrow(
                """
                SELECT request_hash FROM trip_check_commands
                WHERE workspace_id = $1 AND idempotency_key = $2
                """,
                row["workspace_id"],
                idempotency_key,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
                return _run_from_row(row), True
            current = _run_from_row(row)
            resumed = _resumed_run(
                current,
                expected_version=expected_version,
                config_hash=config_hash,
                now=now,
                lease_seconds=self.lease_seconds,
            )
            await conn.execute(
                """
                UPDATE trip_check_runs
                SET status = $2, stage_attempt = $3, lease_owner = $4,
                    lease_until = $5, version = $6, updated_at = $7
                WHERE run_id = $1
                """,
                run_id,
                resumed.status.value,
                resumed.stage_attempt,
                resumed.lease_owner,
                resumed.lease_until,
                resumed.version,
                resumed.updated_at,
            )
            await conn.execute(
                """
                INSERT INTO trip_check_run_events (run_id, event_type, stage, run_version, payload_json)
                VALUES ($1, 'run_resumed', $2, $3, $4::jsonb)
                """,
                run_id,
                resumed.stage.value,
                resumed.version,
                json.dumps({"status": resumed.status.value}),
            )
            await conn.execute(
                """
                INSERT INTO trip_check_commands (
                    command_id, workspace_id, run_id, operation, actor_user_id,
                    idempotency_key, request_hash, response_status,
                    response_headers_json, response_json
                ) VALUES ($1, $2, $3, 'RESUME_RUN', $4, $5, $6, 200, $7::jsonb, $8::jsonb)
                """,
                str(uuid4()),
                resumed.workspace_id,
                run_id,
                actor_user_id,
                idempotency_key,
                request_hash,
                json.dumps({"ETag": f'"{resumed.version}"'}),
                json.dumps({"run_id": run_id}),
            )
            return resumed, False

    async def list_events(self, run_id: str, *, after_event_id: int = 0) -> list[TripCheckRunEvent]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT * FROM trip_check_run_events
                WHERE run_id = $1 AND event_id > $2 ORDER BY event_id
                """,
                run_id,
                after_event_id,
            )
        return [_event_from_row(row) for row in rows]

    async def save_receipt(self, receipt: SideEffectReceipt) -> tuple[SideEffectReceipt, bool]:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO trip_check_side_effect_receipts (
                    receipt_id, run_id, stage, side_effect_key, effect_type,
                    request_hash, response_hash, provider, status, receipt_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb, $11)
                ON CONFLICT (run_id, side_effect_key) DO NOTHING
                """,
                receipt.receipt_id,
                receipt.run_id,
                receipt.stage.value,
                receipt.side_effect_key,
                receipt.effect_type,
                receipt.request_hash,
                receipt.response_hash,
                receipt.provider,
                receipt.status,
                json.dumps(receipt.receipt, ensure_ascii=False),
                receipt.created_at,
            )
            row = await conn.fetchrow(
                "SELECT * FROM trip_check_side_effect_receipts WHERE run_id = $1 AND side_effect_key = $2",
                receipt.run_id,
                receipt.side_effect_key,
            )
            stored = _receipt_from_row(row)
            if stored.request_hash != receipt.request_hash:
                raise IdempotencyKeyReusedError("side-effect key was already used with a different request")
            return stored, stored.receipt_id != receipt.receipt_id

    async def get_receipt(self, run_id: str, side_effect_key: str) -> SideEffectReceipt | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM trip_check_side_effect_receipts WHERE run_id = $1 AND side_effect_key = $2",
                run_id,
                side_effect_key,
            )
        return _receipt_from_row(row) if row else None


class InMemoryTripCheckRunRepository:
    def __init__(self, *, lease_seconds: int = 60):
        self.runs: dict[str, TripCheckRun] = {}
        self.commands: dict[tuple[str, str], tuple[str, str]] = {}
        self.events: dict[str, list[TripCheckRunEvent]] = {}
        self.receipts: dict[tuple[str, str], SideEffectReceipt] = {}
        self.lease_seconds = lease_seconds

    def _append_event(self, run: TripCheckRun, event_type: str) -> None:
        events = self.events.setdefault(run.run_id, [])
        events.append(
            TripCheckRunEvent(
                event_id=len(events) + 1,
                run_id=run.run_id,
                event_type=event_type,
                stage=run.stage,
                run_version=run.version,
                payload={"status": run.status.value},
            )
        )

    async def create_run(
        self,
        run: TripCheckRun,
        *,
        idempotency_key: str,
        request_hash: str,
    ) -> tuple[TripCheckRun, bool]:
        key = (run.workspace_id, idempotency_key)
        existing = self.commands.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
            return self.runs[existing[1]], True
        self.runs[run.run_id] = run
        self.commands[key] = (request_hash, run.run_id)
        self._append_event(run, "run_created")
        return run, False

    async def get_run(self, run_id: str) -> TripCheckRun | None:
        return self.runs.get(run_id)

    async def resume_run(
        self,
        run_id: str,
        *,
        expected_version: int,
        config_hash: str,
        actor_user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> tuple[TripCheckRun, bool]:
        current = self.runs.get(run_id)
        if current is None:
            raise ResourceNotFound("trip check run does not exist")
        key = (current.workspace_id, idempotency_key)
        existing = self.commands.get(key)
        if existing is not None:
            if existing[0] != request_hash:
                raise IdempotencyKeyReusedError("idempotency key was already used with a different request")
            return self.runs[run_id], True
        resumed = _resumed_run(
            current,
            expected_version=expected_version,
            config_hash=config_hash,
            now=now,
            lease_seconds=self.lease_seconds,
        )
        self.runs[run_id] = resumed
        self.commands[key] = (request_hash, run_id)
        self._append_event(resumed, "run_resumed")
        return resumed, False

    async def list_events(self, run_id: str, *, after_event_id: int = 0) -> list[TripCheckRunEvent]:
        return [item for item in self.events.get(run_id, []) if item.event_id > after_event_id]

    async def save_receipt(self, receipt: SideEffectReceipt) -> tuple[SideEffectReceipt, bool]:
        key = (receipt.run_id, receipt.side_effect_key)
        existing = self.receipts.get(key)
        if existing is not None:
            if existing.request_hash != receipt.request_hash:
                raise IdempotencyKeyReusedError("side-effect key was already used with a different request")
            return existing, True
        self.receipts[key] = receipt
        return receipt, False

    async def get_receipt(self, run_id: str, side_effect_key: str) -> SideEffectReceipt | None:
        return self.receipts.get((run_id, side_effect_key))


def _resumed_run(
    current: TripCheckRun,
    *,
    expected_version: int,
    config_hash: str,
    now: datetime,
    lease_seconds: int,
) -> TripCheckRun:
    if current.config_hash != config_hash:
        raise RunConfigMismatchError(
            "run config hash does not match the immutable RunSpec",
            context={"expected_config_hash": current.config_hash, "actual_config_hash": config_hash},
        )
    if current.version != expected_version:
        raise TripCheckRunConflictError(
            "trip check run version changed",
            context={"expected_version": expected_version, "actual_version": current.version},
        )
    lease_active = current.lease_until is not None and current.lease_until > now
    if current.status not in {
        TripCheckRunStatus.WAITING,
        TripCheckRunStatus.PARTIAL,
        TripCheckRunStatus.FAILED,
    } or lease_active:
        raise TripCheckRunNotResumableError(
            "trip check run is not retryable or its lease is still active",
            context={"run_status": current.status.value},
        )
    return current.model_copy(
        update={
            "status": TripCheckRunStatus.RUNNING,
            "stage_attempt": current.stage_attempt + 1,
            "lease_owner": f"worker:{uuid4()}",
            "lease_until": now + timedelta(seconds=lease_seconds),
            "version": current.version + 1,
            "updated_at": now,
        }
    )


class TripCheckRunService:
    def __init__(
        self,
        *,
        run_repository: TripCheckRunRepository,
        itinerary_repository: ItineraryRepository,
        brief_repository: TripBriefRepository,
    ):
        self.run_repository = run_repository
        self.itinerary_repository = itinerary_repository
        self.brief_repository = brief_repository

    async def create(
        self,
        *,
        workspace_id: str,
        itinerary_revision: int,
        brief_revision: int,
        run_spec: RunSpec,
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripCheckRun, bool]:
        workspace = await self.itinerary_repository.get_workspace(workspace_id)
        if workspace is None:
            raise ResourceNotFound("workspace does not exist")
        if workspace.current_itinerary_revision != itinerary_revision:
            raise TripCheckRunConflictError(
                "run must bind the current itinerary revision",
                context={
                    "expected_revision": itinerary_revision,
                    "actual_revision": workspace.current_itinerary_revision,
                },
            )
        brief = await self.brief_repository.get_brief(workspace_id, brief_revision)
        if brief is None:
            raise ResourceNotFound("trip brief revision does not exist")
        if brief.status != TripBriefStatus.CONFIRMED:
            raise TripBriefConfirmationRequiredError("trip check requires a confirmed TripBrief revision")
        latest = await self.brief_repository.get_latest_brief(workspace_id)
        if latest is None or latest.revision != brief_revision:
            raise TripCheckRunConflictError(
                "run must bind the current trip brief revision",
                context={"expected_revision": brief_revision, "actual_revision": latest.revision if latest else None},
            )
        request_hash = sha256_canonical(
            {
                "operation": "CREATE_RUN",
                "workspace_id": workspace_id,
                "itinerary_revision": itinerary_revision,
                "brief_revision": brief_revision,
                "run_spec": run_spec.model_dump(mode="json"),
                "actor_user_id": actor_user_id,
            }
        )
        now = datetime.now(timezone.utc)
        run = TripCheckRun(
            run_id=str(uuid4()),
            workspace_id=workspace_id,
            itinerary_revision=itinerary_revision,
            brief_id=brief.brief_id,
            brief_revision=brief_revision,
            stage=TripCheckStage.COLLECT_EVIDENCE,
            run_spec=run_spec,
            config_hash=run_config_hash(run_spec),
            completed_stages=[
                TripCheckStage.PARSE,
                TripCheckStage.WAIT_BRIEF_CONFIRMATION,
                TripCheckStage.RESOLVE_PLACES,
            ],
            status=TripCheckRunStatus.WAITING,
            created_by=actor_user_id,
            created_at=now,
            updated_at=now,
        )
        return await self.run_repository.create_run(
            run,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def resume(
        self,
        *,
        run_id: str,
        expected_version: int,
        config_hash: str,
        actor_user_id: str,
        idempotency_key: str,
    ) -> tuple[TripCheckRun, bool]:
        request_hash = sha256_canonical(
            {
                "operation": "RESUME_RUN",
                "run_id": run_id,
                "expected_version": expected_version,
                "config_hash": config_hash,
                "actor_user_id": actor_user_id,
            }
        )
        return await self.run_repository.resume_run(
            run_id,
            expected_version=expected_version,
            config_hash=config_hash,
            actor_user_id=actor_user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=datetime.now(timezone.utc),
        )
