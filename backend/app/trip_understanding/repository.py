from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.db.connection import get_pool
from app.trip_understanding.demo import DEMO_SOURCE_SHA256
from app.trip_understanding.errors import (
    CapabilityExpiredError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    JobLeaseLostError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
)
from app.trip_understanding.models import (
    CreateOutcome,
    PipelineOutput,
    PublicEventPayload,
    PublicEventRecord,
    PublicResourceRecord,
    StoredResult,
    TripUnderstandingAcceptedView,
    TripUnderstandingJobRecord,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _accepted(public_resource_id: str) -> TripUnderstandingAcceptedView:
    base = f"/api/v3/trip-understandings/{public_resource_id}"
    return TripUnderstandingAcceptedView(
        public_resource_id=public_resource_id,
        result_url=f"{base}/result",
        events_url=f"{base}/events",
    )


class TripUnderstandingRepository(Protocol):
    async def create_demo(
        self,
        *,
        capability_hash: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        ttl_hours: int,
    ) -> CreateOutcome: ...

    async def authorize(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None = None,
        now: datetime,
    ) -> PublicResourceRecord: ...

    async def get_result(self, resource: PublicResourceRecord) -> StoredResult | None: ...

    async def list_events(
        self,
        resource: PublicResourceRecord,
        *,
        after_event_id: int,
    ) -> list[PublicEventRecord]: ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> TripUnderstandingJobRecord | None: ...

    async def complete_job(
        self,
        job: TripUnderstandingJobRecord,
        output: PipelineOutput,
        *,
        now: datetime,
    ) -> bool: ...

    async def fail_job(
        self,
        job: TripUnderstandingJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None: ...


class PostgresTripUnderstandingRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    async def create_demo(
        self,
        *,
        capability_hash: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        ttl_hours: int,
    ) -> CreateOutcome:
        if len(idempotency_key) > 200:
            raise ValueError("idempotency key is too long")
        expires_at = now + timedelta(hours=ttl_hours)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            session = await conn.fetchrow(
                """
                SELECT * FROM trip_understanding_anonymous_sessions
                WHERE capability_hash = $1 FOR UPDATE
                """,
                capability_hash,
            )
            if session is not None and (
                session["revoked_at"] is not None or session["expires_at"] <= now
            ):
                raise CapabilityExpiredError("anonymous capability is no longer active")
            if session is None:
                session_id = str(uuid4())
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_anonymous_sessions (
                        session_id, capability_hash, expires_at, created_at, last_seen_at
                    ) VALUES ($1, $2, $3, $4, $4)
                    """,
                    session_id,
                    capability_hash,
                    expires_at,
                    now,
                )
            else:
                session_id = session["session_id"]
                await conn.execute(
                    """
                    UPDATE trip_understanding_anonymous_sessions
                    SET last_seen_at = $2
                    WHERE session_id = $1
                    """,
                    session_id,
                    now,
                )

            scope = f"anonymous:{session_id}:create"
            key_hash = _sha256_text(idempotency_key)
            claimed = await conn.fetchval(
                """
                INSERT INTO trip_understanding_idempotency_records (
                    scope, key_hash, request_hash, state, lease_until, created_at
                ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
                ON CONFLICT (scope, key_hash) DO NOTHING
                RETURNING scope
                """,
                scope,
                key_hash,
                request_hash,
                now + timedelta(seconds=30),
                now,
            )
            if claimed is None:
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, state, response_json
                    FROM trip_understanding_idempotency_records
                    WHERE scope = $1 AND key_hash = $2
                    """,
                    scope,
                    key_hash,
                )
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different request"
                    )
                if existing["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("matching create request is still in progress")
                return CreateOutcome(
                    accepted=TripUnderstandingAcceptedView.model_validate(
                        _json_value(existing["response_json"])
                    ),
                    replayed=True,
                )

            understanding_id = str(uuid4())
            public_resource_id = secrets.token_urlsafe(24)
            source_id = str(uuid4())
            job_id = str(uuid4())
            draft_payload = {
                "source_hash": DEMO_SOURCE_SHA256,
                "destination": {"status": "PENDING"},
                "assumptions": [],
                "proposal": {},
                "inference_binding": {"status": "NOT_RUN"},
                "compiler_receipt": {"status": "NOT_RUN"},
            }
            await conn.execute(
                """
                INSERT INTO trip_understandings (
                    understanding_id, public_resource_id, anonymous_session_id,
                    state, current_revision, etag_nonce, source_expires_at,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, 'PROCESSING', 1, $4, $5, $6, $6)
                """,
                understanding_id,
                public_resource_id,
                session_id,
                secrets.token_hex(32),
                expires_at,
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_sources (
                    source_id, understanding_id, source_type, content_hash,
                    retention_until, created_at
                ) VALUES ($1, $2, 'FIXED_DEMO', $3, $4, $5)
                """,
                source_id,
                understanding_id,
                DEMO_SOURCE_SHA256,
                expires_at,
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_revisions (
                    understanding_id, revision, parent_revision, source_id, status,
                    content_hash, destination_json, assumptions_json, proposal_json,
                    inference_binding_json, compiler_receipt_json, created_at
                ) VALUES (
                    $1, 1, NULL, $2, 'PROCESSING', $3, $4::jsonb, $5::jsonb,
                    $6::jsonb, $7::jsonb, $8::jsonb, $9
                )
                """,
                understanding_id,
                source_id,
                canonical_sha256(draft_payload),
                json.dumps(draft_payload["destination"], ensure_ascii=False),
                json.dumps(draft_payload["assumptions"], ensure_ascii=False),
                json.dumps(draft_payload["proposal"], ensure_ascii=False),
                json.dumps(draft_payload["inference_binding"], ensure_ascii=False),
                json.dumps(draft_payload["compiler_receipt"], ensure_ascii=False),
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_jobs (
                    job_id, understanding_id, revision, job_type, status,
                    input_hash, available_at, created_at, updated_at
                ) VALUES ($1, $2, 1, 'UNDERSTAND', 'QUEUED', $3, $4, $4, $4)
                """,
                job_id,
                understanding_id,
                DEMO_SOURCE_SHA256,
                now,
            )
            event_payload = PublicEventPayload(
                status="PROCESSING",
                message="正在整理每天行程",
            ).model_dump(mode="json")
            await conn.execute(
                """
                INSERT INTO trip_understanding_events (
                    understanding_id, event_key, event_type, public_payload_json, created_at
                ) VALUES ($1, 'created', 'progress', $2::jsonb, $3)
                """,
                understanding_id,
                json.dumps(event_payload, ensure_ascii=False),
                now,
            )
            accepted = _accepted(public_resource_id)
            response_json = accepted.model_dump(mode="json")
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 202,
                    response_json = $3::jsonb, response_headers_json = '{}'::jsonb,
                    lease_until = NULL, completed_at = $4
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                json.dumps(response_json, ensure_ascii=False),
                now,
            )
        return CreateOutcome(accepted=accepted)

    async def authorize(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None = None,
        now: datetime,
    ) -> PublicResourceRecord:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            tombstone = await conn.fetchval(
                "SELECT reason FROM trip_understanding_resource_tombstones WHERE public_resource_id = $1",
                public_resource_id,
            )
            if tombstone is not None:
                raise ResourceGoneError("trip resource is no longer available")
            row = await conn.fetchrow(
                """
                SELECT u.*, s.capability_hash, s.expires_at, s.revoked_at
                FROM trip_understandings u
                LEFT JOIN trip_understanding_anonymous_sessions s
                  ON s.session_id = u.anonymous_session_id
                WHERE u.public_resource_id = $1
                """,
                public_resource_id,
            )
        if row is None:
            raise ResourceNotFoundError("trip resource does not exist")
        if row["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        if row["owner_user_id"] is not None:
            if user_id != row["owner_user_id"]:
                raise ResourceAccessDeniedError("trip resource is not available to this session")
        else:
            stored_hash = (row["capability_hash"] or "").strip()
            if (
                not capability_hash
                or not hmac.compare_digest(stored_hash, capability_hash)
                or row["revoked_at"] is not None
                or row["expires_at"] <= now
            ):
                raise ResourceAccessDeniedError("trip resource is not available to this session")
        return PublicResourceRecord(
            understanding_id=row["understanding_id"],
            public_resource_id=row["public_resource_id"],
            state=row["state"],
            current_result_id=row["current_result_id"],
        )

    async def get_result(self, resource: PublicResourceRecord) -> StoredResult | None:
        if resource.current_result_id is None:
            return None
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT public_json, opaque_etag FROM trip_understanding_results WHERE result_id = $1",
                resource.current_result_id,
            )
        if row is None:
            return None
        return StoredResult(
            result=UserFacingTripResult.model_validate(_json_value(row["public_json"])),
            opaque_etag=row["opaque_etag"],
        )

    async def list_events(
        self,
        resource: PublicResourceRecord,
        *,
        after_event_id: int,
    ) -> list[PublicEventRecord]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT event_id, event_type, public_payload_json
                FROM trip_understanding_events
                WHERE understanding_id = $1 AND event_id > $2
                ORDER BY event_id
                """,
                resource.understanding_id,
                after_event_id,
            )
        return [
            PublicEventRecord(
                event_id=row["event_id"],
                event_type=row["event_type"],
                payload=PublicEventPayload.model_validate(_json_value(row["public_payload_json"])),
            )
            for row in rows
        ]

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> TripUnderstandingJobRecord | None:
        lease_until = now + timedelta(seconds=lease_seconds)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                WITH candidate AS (
                    SELECT job_id
                    FROM trip_understanding_jobs
                    WHERE (
                        (status = 'QUEUED' AND available_at <= $1)
                        OR (status = 'RUNNING' AND lease_until <= $1)
                    )
                      AND attempt < max_attempts
                    ORDER BY available_at, created_at
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE trip_understanding_jobs j
                SET status = 'RUNNING', lease_owner = $2, lease_until = $3,
                    attempt = j.attempt + 1,
                    started_at = COALESCE(j.started_at, $1),
                    updated_at = $1, last_error_category = NULL
                FROM candidate
                WHERE j.job_id = candidate.job_id
                RETURNING j.*
                """,
                now,
                worker_id,
                lease_until,
            )
            if row is None:
                return None
            payload = PublicEventPayload(
                status="PROCESSING",
                message="正在核对地点",
            ).model_dump(mode="json")
            await conn.execute(
                """
                INSERT INTO trip_understanding_events (
                    understanding_id, event_key, event_type, public_payload_json, created_at
                ) VALUES ($1, $2, 'progress', $3::jsonb, $4)
                ON CONFLICT (understanding_id, event_key) DO NOTHING
                """,
                row["understanding_id"],
                f"job:{row['job_id']}:place-check",
                json.dumps(payload, ensure_ascii=False),
                now,
            )
        return TripUnderstandingJobRecord(
            job_id=row["job_id"],
            understanding_id=row["understanding_id"],
            revision=row["revision"],
            status="RUNNING",
            lease_owner=row["lease_owner"],
            lease_until=row["lease_until"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            input_hash=row["input_hash"].strip(),
        )

    async def complete_job(
        self,
        job: TripUnderstandingJobRecord,
        output: PipelineOutput,
        *,
        now: datetime,
    ) -> bool:
        if output.source_hash != job.input_hash:
            raise ValueError("worker output is not bound to the claimed source")
        effect_key = f"trip-understanding:{job.understanding_id}:r{job.revision}:fixture-pipeline-v1"
        public_payload = output.public_result.model_dump(mode="json")
        public_hash = canonical_sha256(public_payload)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            current_job = await conn.fetchrow(
                "SELECT * FROM trip_understanding_jobs WHERE job_id = $1 FOR UPDATE",
                job.job_id,
            )
            if current_job is None:
                raise ResourceNotFoundError("understanding job does not exist")
            existing_receipt = await conn.fetchrow(
                """
                SELECT request_hash, response_hash
                FROM trip_understanding_side_effect_receipts WHERE effect_key = $1
                """,
                effect_key,
            )
            if existing_receipt is not None:
                if (
                    existing_receipt["request_hash"].strip() != job.input_hash
                    or existing_receipt["response_hash"].strip() != public_hash
                ):
                    raise IdempotencyConflictError("side effect receipt binding mismatch")
                return True
            if (
                current_job["status"] != "RUNNING"
                or current_job["lease_owner"] != job.lease_owner
                or current_job["lease_until"] <= now
            ):
                raise JobLeaseLostError("understanding job lease was lost before completion")
            aggregate = await conn.fetchrow(
                "SELECT * FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                job.understanding_id,
            )
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource was deleted during processing")
            source_id = await conn.fetchval(
                "SELECT source_id FROM trip_understanding_sources WHERE understanding_id = $1",
                job.understanding_id,
            )
            result_revision = int(aggregate["current_revision"]) + 1
            await conn.execute(
                """
                INSERT INTO trip_understanding_revisions (
                    understanding_id, revision, parent_revision, source_id, status,
                    content_hash, destination_json, assumptions_json, proposal_json,
                    inference_binding_json, compiler_receipt_json, created_at
                ) VALUES (
                    $1, $2, $3, $4, 'READY', $5, $6::jsonb, $7::jsonb,
                    $8::jsonb, $9::jsonb, $10::jsonb, $11
                )
                """,
                job.understanding_id,
                result_revision,
                job.revision,
                source_id,
                output.content_hash,
                json.dumps(output.destination, ensure_ascii=False),
                json.dumps(output.assumptions, ensure_ascii=False),
                json.dumps(output.proposal.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(output.inference_binding, ensure_ascii=False),
                json.dumps(output.compiler_receipt, ensure_ascii=False),
                now,
            )
            for activity in output.activities:
                mention = activity.compiled.mention
                place = activity.place
                resolver_receipt = (
                    place.provider_binding
                    if place
                    else {
                        "status": activity.resolution_status.value,
                        "external_calls": 0,
                    }
                )
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_activities (
                        activity_id, understanding_id, revision, public_activity_token,
                        day_index, sequence_index, role, mention_text, atomic_place_name,
                        category_hint, time_hint, eligible_for_place_search,
                        resolution_status, canonical_place_id, resolver_receipt_json, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                        $13, $14, $15::jsonb, $16
                    )
                    """,
                    activity.compiled.activity_id,
                    job.understanding_id,
                    result_revision,
                    activity.compiled.public_activity_token,
                    mention.day_index,
                    mention.sequence_index,
                    mention.role.value,
                    mention.raw_text,
                    mention.atomic_place_name,
                    mention.category_hint,
                    mention.time_hint,
                    activity.compiled.eligible_for_place_search,
                    activity.resolution_status.value,
                    place.canonical_place_id if place else None,
                    json.dumps(resolver_receipt, ensure_ascii=False),
                    now,
                )
            for claim in output.claims:
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_source_claims (
                        claim_id, understanding_id, revision, source_id, activity_id,
                        claim_type, span_start, span_end, quote, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    """,
                    claim.claim_id,
                    job.understanding_id,
                    result_revision,
                    source_id,
                    claim.activity_id,
                    claim.claim_type,
                    claim.span_start,
                    claim.span_end,
                    claim.quote,
                    now,
                )
            result_id = str(uuid4())
            opaque_etag = f"tu3_{secrets.token_urlsafe(32)}"
            await conn.execute(
                """
                INSERT INTO trip_understanding_results (
                    result_id, understanding_id, revision, public_json,
                    public_sha256, opaque_etag, created_at
                ) VALUES ($1, $2, $3, $4::jsonb, $5, $6, $7)
                """,
                result_id,
                job.understanding_id,
                result_revision,
                json.dumps(public_payload, ensure_ascii=False),
                public_hash,
                opaque_etag,
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_side_effect_receipts (
                    receipt_id, job_id, effect_key, effect_type, request_hash,
                    response_hash, provider_binding_json, created_at
                ) VALUES ($1, $2, $3, 'FIXTURE_INFERENCE_RESOLUTION_PROJECTION',
                    $4, $5, $6::jsonb, $7)
                """,
                str(uuid4()),
                job.job_id,
                effect_key,
                job.input_hash,
                public_hash,
                json.dumps(
                    {
                        "inference": output.inference_binding,
                        "place_resolver": "frozen_beijing_fixture",
                        "external_calls": 0,
                    },
                    ensure_ascii=False,
                ),
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understandings
                SET state = 'READY', current_revision = $2, result_revision = $2,
                    current_result_id = $3, updated_at = $4
                WHERE understanding_id = $1
                """,
                job.understanding_id,
                result_revision,
                result_id,
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understanding_jobs
                SET status = 'SUCCEEDED', lease_owner = NULL, lease_until = NULL,
                    finished_at = $2, updated_at = $2
                WHERE job_id = $1
                """,
                job.job_id,
                now,
            )
            event_payload = PublicEventPayload(
                status="READY",
                message="卡片已可用",
            ).model_dump(mode="json")
            await conn.execute(
                """
                INSERT INTO trip_understanding_events (
                    understanding_id, event_key, event_type, public_payload_json, created_at
                ) VALUES ($1, $2, 'result_available', $3::jsonb, $4)
                ON CONFLICT (understanding_id, event_key) DO NOTHING
                """,
                job.understanding_id,
                f"job:{job.job_id}:result",
                json.dumps(event_payload, ensure_ascii=False),
                now,
            )
        return False

    async def fail_job(
        self,
        job: TripUnderstandingJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                "SELECT status, lease_owner, attempt, max_attempts FROM trip_understanding_jobs WHERE job_id = $1 FOR UPDATE",
                job.job_id,
            )
            if row is None or row["status"] != "RUNNING" or row["lease_owner"] != job.lease_owner:
                return
            retryable = row["attempt"] < row["max_attempts"]
            await conn.execute(
                """
                UPDATE trip_understanding_jobs
                SET status = $2, lease_owner = NULL, lease_until = NULL,
                    last_error_category = $3, available_at = $4,
                    finished_at = CASE WHEN $2 = 'FAILED' THEN $5 ELSE NULL END,
                    updated_at = $5
                WHERE job_id = $1
                """,
                job.job_id,
                "QUEUED" if retryable else "FAILED",
                category,
                now + timedelta(seconds=2),
                now,
            )
            if not retryable:
                await conn.execute(
                    """
                    UPDATE trip_understandings SET state = 'FAILED', updated_at = $2
                    WHERE understanding_id = $1 AND state = 'PROCESSING'
                    """,
                    job.understanding_id,
                    now,
                )


class InMemoryTripUnderstandingRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.resources: dict[str, dict[str, Any]] = {}
        self.resources_by_understanding: dict[str, str] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, TripUnderstandingAcceptedView]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[PublicEventRecord]] = {}
        self.results: dict[str, StoredResult] = {}
        self.side_effects: dict[str, tuple[str, str]] = {}

    async def create_demo(
        self,
        *,
        capability_hash: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        ttl_hours: int,
    ) -> CreateOutcome:
        session = self.sessions.get(capability_hash)
        if session and session["expires_at"] <= now:
            raise CapabilityExpiredError("anonymous capability is no longer active")
        if session is None:
            session = {
                "session_id": str(uuid4()),
                "expires_at": now + timedelta(hours=ttl_hours),
            }
            self.sessions[capability_hash] = session
        scope = f"anonymous:{session['session_id']}:create"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different request"
                )
            return CreateOutcome(accepted=existing[1], replayed=True)
        understanding_id = str(uuid4())
        public_resource_id = secrets.token_urlsafe(24)
        result_id = None
        accepted = _accepted(public_resource_id)
        self.idempotency[key] = (request_hash, accepted)
        self.resources[public_resource_id] = {
            "understanding_id": understanding_id,
            "public_resource_id": public_resource_id,
            "state": "PROCESSING",
            "current_result_id": result_id,
            "capability_hash": capability_hash,
            "expires_at": session["expires_at"],
            "current_revision": 1,
        }
        self.resources_by_understanding[understanding_id] = public_resource_id
        job_id = str(uuid4())
        self.jobs[job_id] = {
            "job_id": job_id,
            "understanding_id": understanding_id,
            "revision": 1,
            "status": "QUEUED",
            "lease_owner": None,
            "lease_until": None,
            "attempt": 0,
            "max_attempts": 3,
            "input_hash": DEMO_SOURCE_SHA256,
            "available_at": now,
        }
        self.events[understanding_id] = [
            PublicEventRecord(
                event_id=1,
                event_type="progress",
                payload=PublicEventPayload(status="PROCESSING", message="正在整理每天行程"),
            )
        ]
        return CreateOutcome(accepted=accepted)

    async def authorize(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None = None,
        now: datetime,
    ) -> PublicResourceRecord:
        del user_id
        row = self.resources.get(public_resource_id)
        if row is None:
            raise ResourceNotFoundError("trip resource does not exist")
        if row["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        if (
            not capability_hash
            or not hmac.compare_digest(row["capability_hash"], capability_hash)
            or row["expires_at"] <= now
        ):
            raise ResourceAccessDeniedError("trip resource is not available to this session")
        return PublicResourceRecord.model_validate(
            {key: row[key] for key in PublicResourceRecord.model_fields}
        )

    async def get_result(self, resource: PublicResourceRecord) -> StoredResult | None:
        return self.results.get(resource.current_result_id or "")

    async def list_events(
        self,
        resource: PublicResourceRecord,
        *,
        after_event_id: int,
    ) -> list[PublicEventRecord]:
        return [
            event
            for event in self.events.get(resource.understanding_id, [])
            if event.event_id > after_event_id
        ]

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> TripUnderstandingJobRecord | None:
        eligible = [
            item
            for item in self.jobs.values()
            if item["attempt"] < item["max_attempts"]
            and (
                (item["status"] == "QUEUED" and item["available_at"] <= now)
                or (
                    item["status"] == "RUNNING"
                    and item["lease_until"] is not None
                    and item["lease_until"] <= now
                )
            )
        ]
        if not eligible:
            return None
        item = sorted(eligible, key=lambda value: value["available_at"])[0]
        item.update(
            {
                "status": "RUNNING",
                "lease_owner": worker_id,
                "lease_until": now + timedelta(seconds=lease_seconds),
                "attempt": item["attempt"] + 1,
            }
        )
        event_list = self.events[item["understanding_id"]]
        if not any(event.payload.message == "正在核对地点" for event in event_list):
            event_list.append(
                PublicEventRecord(
                    event_id=len(event_list) + 1,
                    event_type="progress",
                    payload=PublicEventPayload(status="PROCESSING", message="正在核对地点"),
                )
            )
        return TripUnderstandingJobRecord.model_validate(
            {key: item[key] for key in TripUnderstandingJobRecord.model_fields}
        )

    async def complete_job(
        self,
        job: TripUnderstandingJobRecord,
        output: PipelineOutput,
        *,
        now: datetime,
    ) -> bool:
        item = self.jobs[job.job_id]
        public_payload = output.public_result.model_dump(mode="json")
        public_hash = canonical_sha256(public_payload)
        effect_key = f"trip-understanding:{job.understanding_id}:r{job.revision}:fixture-pipeline-v1"
        existing = self.side_effects.get(effect_key)
        if existing:
            if existing != (job.input_hash, public_hash):
                raise IdempotencyConflictError("side effect receipt binding mismatch")
            return True
        if (
            item["status"] != "RUNNING"
            or item["lease_owner"] != job.lease_owner
            or item["lease_until"] <= now
        ):
            raise JobLeaseLostError("understanding job lease was lost before completion")
        result_id = str(uuid4())
        self.results[result_id] = StoredResult(
            result=output.public_result,
            opaque_etag=f"tu3_{secrets.token_urlsafe(32)}",
        )
        public_id = self.resources_by_understanding[job.understanding_id]
        self.resources[public_id].update(
            {
                "state": "READY",
                "current_result_id": result_id,
                "current_revision": 2,
            }
        )
        item.update({"status": "SUCCEEDED", "lease_owner": None, "lease_until": None})
        self.side_effects[effect_key] = (job.input_hash, public_hash)
        event_list = self.events[job.understanding_id]
        event_list.append(
            PublicEventRecord(
                event_id=len(event_list) + 1,
                event_type="result_available",
                payload=PublicEventPayload(status="READY", message="卡片已可用"),
            )
        )
        return False

    async def fail_job(
        self,
        job: TripUnderstandingJobRecord,
        *,
        category: str,
        now: datetime,
    ) -> None:
        item = self.jobs.get(job.job_id)
        if item is None or item["status"] != "RUNNING" or item["lease_owner"] != job.lease_owner:
            return
        retryable = item["attempt"] < item["max_attempts"]
        item.update(
            {
                "status": "QUEUED" if retryable else "FAILED",
                "lease_owner": None,
                "lease_until": None,
                "available_at": now + timedelta(seconds=2),
                "last_error_category": category,
            }
        )

    @property
    def side_effect_count(self) -> int:
        return len(self.side_effects)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
