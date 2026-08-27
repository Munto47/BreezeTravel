from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import base64
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from app.config import get_settings
from app.db.connection import get_pool
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.demo import DEMO_SOURCE_SHA256, DEMO_SOURCE_TEXT
from app.trip_understanding.errors import (
    CapabilityExpiredError,
    ConcurrentJobLimitError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    JobLeaseLostError,
    ResourceNotReadyError,
    ResourceAccessDeniedError,
    ResourceGoneError,
    ResourceNotFoundError,
    RevisionConflictError,
    SourceUnavailableError,
)
from app.trip_understanding.map_repository import (
    InMemoryMapRenderRepositoryMixin,
    MapRenderRepository,
    PostgresMapRenderRepositoryMixin,
)
from app.trip_understanding.models import (
    ActivityTextEditCommand,
    ClaimOutcome,
    ClaimedTripView,
    CommandAppliedView,
    CommandOutcome,
    CreateOutcome,
    DeletionOutcome,
    PipelineOutput,
    PublicEventPayload,
    PublicEventRecord,
    PublicResourceRecord,
    StoredResult,
    TripUnderstandingAcceptedView,
    TripUnderstandingJobRecord,
    TripUnderstandingCommand,
    TripUnderstandingSourcePayload,
    TravelDataDeletionOutcome,
    TravelDataDeletionStatusView,
    UserFacingTripResult,
)
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.source_crypto import SourceCipher


def _json_value(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _account_subject_hash(user_id: str) -> str:
    """Return a stable, non-reversible lookup key for account deletion state."""
    settings = get_settings()
    secret = (
        settings.trip_understanding_cookie_signing_key
        or settings.trip_understanding_source_encryption_key
        or settings.jwt_secret_key
    )
    return hmac.new(
        secret.encode("utf-8"),
        f"trip-understanding-account:{user_id}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def _delete_understanding_business_rows(conn: Any, understanding_id: str) -> None:
    """Delete one v3 aggregate in FK-safe order inside the caller transaction."""
    await conn.execute(
        "UPDATE trip_understandings SET current_result_id = NULL WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_source_claims WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_activities WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_results WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_events WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_deletion_jobs WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_jobs WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_claim_commands WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_idempotency_records WHERE scope = $1",
        f"understanding:{understanding_id}:map-renders",
    )
    await conn.execute(
        "DELETE FROM trip_understanding_revisions WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understanding_sources WHERE understanding_id = $1",
        understanding_id,
    )
    await conn.execute(
        "DELETE FROM trip_understandings WHERE understanding_id = $1",
        understanding_id,
    )


def _accepted(public_resource_id: str) -> TripUnderstandingAcceptedView:
    base = f"/api/v3/trip-understandings/{public_resource_id}"
    return TripUnderstandingAcceptedView(
        public_resource_id=public_resource_id,
        result_url=f"{base}/result",
        events_url=f"{base}/events",
    )


def _persisted_proposal(output: PipelineOutput) -> dict[str, object]:
    """Persist structural semantics without duplicating verbatim source quotes."""
    return {
        "schema_version": output.proposal.schema_version,
        "source_hash": output.proposal.source_hash,
        "destination_name": output.proposal.destination_name,
        "mention_count": len(output.proposal.mentions),
        "binding": output.proposal.binding,
        "verbatim_quotes": "ENCRYPTED_IN_SOURCE_CLAIMS",
    }


class TripUnderstandingRepository(MapRenderRepository, Protocol):
    async def create_demo(
        self,
        *,
        capability_hash: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        ttl_hours: int,
    ) -> CreateOutcome: ...

    async def create_full(
        self,
        *,
        owner_user_id: str,
        source_text: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
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

    async def apply_command(
        self,
        resource: PublicResourceRecord,
        command: TripUnderstandingCommand,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> CommandOutcome: ...

    async def claim_demo(
        self,
        public_resource_id: str,
        *,
        capability_hash: str,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> ClaimOutcome: ...

    async def delete_source(
        self,
        resource: PublicResourceRecord,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> DeletionOutcome: ...

    async def delete_trip(
        self,
        resource: PublicResourceRecord,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> DeletionOutcome: ...

    async def replay_trip_deletion(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        request_hash: str,
    ) -> bool: ...

    async def tombstone_reason(self, public_resource_id: str) -> str | None: ...

    async def delete_account_travel_data(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> TravelDataDeletionOutcome: ...

    async def get_account_travel_data_deletion(
        self,
        *,
        user_id: str,
    ) -> TravelDataDeletionStatusView: ...

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

    async def load_source(
        self,
        job: TripUnderstandingJobRecord,
        *,
        now: datetime,
    ) -> TripUnderstandingSourcePayload: ...

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


class PostgresTripUnderstandingRepository(PostgresMapRenderRepositoryMixin):
    def __init__(self, pool: Any | None = None, source_cipher: SourceCipher | None = None):
        self._pool = pool
        self._source_cipher = source_cipher

    async def _get_pool(self):
        return self._pool or await get_pool()

    def _get_source_cipher(self) -> SourceCipher:
        if self._source_cipher is None:
            settings = get_settings()
            secret = (
                settings.trip_understanding_source_encryption_key
                or settings.trip_understanding_cookie_signing_key
                or settings.jwt_secret_key
            )
            self._source_cipher = SourceCipher(secret)
        return self._source_cipher

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

    async def create_full(
        self,
        *,
        owner_user_id: str,
        source_text: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> CreateOutcome:
        if len(idempotency_key) > 200:
            raise ValueError("idempotency key is too long")
        if not source_text.strip() or len(source_text) > 50_000:
            raise ValueError("text source is outside the supported size")
        expires_at = now + timedelta(days=retention_days)
        content_hash = _sha256_text(source_text)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{owner_user_id}",
            )
            scope = f"user:{owner_user_id}:create"
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

            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-create:{owner_user_id}",
            )
            active_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understandings
                WHERE owner_user_id = $1 AND state = 'PROCESSING'
                """,
                owner_user_id,
            )
            if active_count >= 2:
                raise ConcurrentJobLimitError("user already has two understanding jobs in progress")

            understanding_id = str(uuid4())
            public_resource_id = secrets.token_urlsafe(24)
            source_id = str(uuid4())
            job_id = str(uuid4())
            cipher = self._get_source_cipher()
            encrypted_content = cipher.encrypt(
                source_text,
                source_id=source_id,
                content_hash=content_hash,
            )
            draft_payload = {
                "source_hash": content_hash,
                "destination": {"status": "PENDING"},
                "assumptions": [],
                "proposal": {},
                "inference_binding": {"status": "NOT_RUN"},
                "compiler_receipt": {"status": "NOT_RUN"},
            }
            await conn.execute(
                """
                INSERT INTO trip_understandings (
                    understanding_id, public_resource_id, owner_user_id,
                    state, current_revision, etag_nonce, source_expires_at,
                    created_at, updated_at
                ) VALUES ($1, $2, $3, 'PROCESSING', 1, $4, $5, $6, $6)
                """,
                understanding_id,
                public_resource_id,
                owner_user_id,
                secrets.token_hex(32),
                expires_at,
                now,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_sources (
                    source_id, understanding_id, source_type, content_hash,
                    encrypted_content, encryption_key_ref, retention_until, created_at
                ) VALUES ($1, $2, 'TEXT', $3, $4, $5, $6, $7)
                """,
                source_id,
                understanding_id,
                content_hash,
                encrypted_content,
                cipher.key_ref,
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
                content_hash,
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
                "SELECT revision, public_json, opaque_etag FROM trip_understanding_results WHERE result_id = $1",
                resource.current_result_id,
            )
            if row is None:
                return None
            result = UserFacingTripResult.model_validate(_json_value(row["public_json"]))
            readiness = await self._project_map_readiness(
                conn,
                resource.understanding_id,
                int(row["revision"]),
            )
        return StoredResult(
            result=result.model_copy(update={"map": readiness}),
            opaque_etag=row["opaque_etag"],
        )

    async def apply_command(
        self,
        resource: PublicResourceRecord,
        command: TripUnderstandingCommand,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> CommandOutcome:
        if len(idempotency_key) > 200:
            raise ValueError("idempotency key is too long")
        scope = f"understanding:{resource.understanding_id}:command"
        key_hash = _sha256_text(idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
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
                    SELECT request_hash, state, response_json, response_headers_json
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
                    raise IdempotencyInProgressError("matching command is still in progress")
                headers = _json_value(existing["response_headers_json"])
                return CommandOutcome(
                    applied=CommandAppliedView.model_validate(_json_value(existing["response_json"])),
                    opaque_etag=str(headers["ETag"]).strip('"'),
                    replayed=True,
                )

            aggregate = await conn.fetchrow(
                "SELECT * FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            if aggregate["current_result_id"] is None:
                raise ResourceNotReadyError("trip cards are not ready for editing")
            current = await conn.fetchrow(
                """
                SELECT r.source_id, r.destination_json, r.assumptions_json,
                       result.public_json, result.opaque_etag
                FROM trip_understanding_revisions r
                JOIN trip_understanding_results result
                  ON result.understanding_id = r.understanding_id
                 AND result.revision = r.revision
                WHERE r.understanding_id = $1 AND r.revision = $2
                """,
                resource.understanding_id,
                aggregate["current_revision"],
            )
            if current is None:
                raise ResourceNotReadyError("current trip result is unavailable")
            if not hmac.compare_digest(current["opaque_etag"], expected_etag):
                raise RevisionConflictError("command precondition does not match current result")

            current_result = UserFacingTripResult.model_validate(_json_value(current["public_json"]))
            mutation = apply_public_command(current_result, command)
            public_payload = mutation.result.model_dump(mode="json")
            public_hash = canonical_sha256(public_payload)
            parent_revision = int(aggregate["current_revision"])
            result_revision = parent_revision + 1
            terminal_state = "READY" if mutation.result.status == "READY" else "PARTIAL"
            destination = _json_value(current["destination_json"])
            assumptions = _json_value(current["assumptions_json"])
            if command.command_type == "ASSUMPTION_SET":
                assumptions = [item for item in assumptions if item.get("key") != command.key]
                assumptions.append(
                    {"key": command.key, "value": command.value, "source": "USER_EDIT"}
                )
                if command.key == "destination":
                    destination = {"name": command.value, "status": "USER_EDITED"}
            revision_content = {
                "parent_revision": parent_revision,
                "command_hash": request_hash,
                "public_hash": public_hash,
            }
            await conn.execute(
                """
                INSERT INTO trip_understanding_revisions (
                    understanding_id, revision, parent_revision, source_id, status,
                    content_hash, destination_json, assumptions_json, proposal_json,
                    inference_binding_json, compiler_receipt_json, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9::jsonb,
                    $10::jsonb, $11::jsonb, $12
                )
                """,
                resource.understanding_id,
                result_revision,
                parent_revision,
                current["source_id"],
                terminal_state,
                canonical_sha256(revision_content),
                json.dumps(destination, ensure_ascii=False),
                json.dumps(assumptions, ensure_ascii=False),
                json.dumps(
                    {
                        "kind": "USER_EDIT",
                        "command_type": command.command_type,
                        "source_quotes": "PARENT_REVISION_ONLY",
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"provider_calls": 0, "route_provider_calls": 0},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"kind": "USER_EDIT", "source_claims_copied": 0},
                    ensure_ascii=False,
                ),
                now,
            )

            current_activities = await conn.fetch(
                """
                SELECT * FROM trip_understanding_activities
                WHERE understanding_id = $1 AND revision = $2
                ORDER BY day_index NULLS LAST, sequence_index, activity_id
                """,
                resource.understanding_id,
                parent_revision,
            )
            old_by_token = {row["public_activity_token"]: row for row in current_activities}
            old_token_by_new = {new: old for old, new in mutation.token_map.items()}
            invalidated_token = None
            if command.command_type == "PLACE_REPLACE":
                invalidated_token = command.activity_token
            elif isinstance(command, ActivityTextEditCommand) and command.name is not None:
                invalidated_token = command.activity_token
            for day_index, day in enumerate(mutation.result.days, start=1):
                for sequence_index, card in enumerate(day.activities):
                    old_token = old_token_by_new.get(card.activity_token)
                    old = old_by_token.get(old_token) if old_token else None
                    preserve_resolution = old is not None and old_token != invalidated_token
                    resolver_receipt = (
                        _json_value(old["resolver_receipt_json"])
                        if preserve_resolution
                        else {
                            "status": "USER_EDITED_NEEDS_CONFIRMATION",
                            "category": card.category,
                            "area_or_address": card.area_or_address,
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
                            $1, $2, $3, $4, $5, $6, 'PLANNED', $7, $7, $8, $9,
                            $10, $11, $12, $13::jsonb, $14
                        )
                        """,
                        str(uuid4()),
                        resource.understanding_id,
                        result_revision,
                        card.activity_token,
                        day_index,
                        sequence_index,
                        card.name,
                        card.category,
                        card.time_hint,
                        bool(old["eligible_for_place_search"]) if preserve_resolution else False,
                        old["resolution_status"] if preserve_resolution else "NEEDS_CONFIRMATION",
                        old["canonical_place_id"] if preserve_resolution else None,
                        json.dumps(resolver_receipt, ensure_ascii=False),
                        now,
                    )
            for old in current_activities:
                if old["role"] == "PLANNED":
                    continue
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_activities (
                        activity_id, understanding_id, revision, public_activity_token,
                        day_index, sequence_index, role, mention_text, atomic_place_name,
                        category_hint, time_hint, eligible_for_place_search,
                        resolution_status, canonical_place_id, resolver_receipt_json, created_at
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                        $12, $13, $14, $15::jsonb, $16
                    )
                    """,
                    str(uuid4()),
                    resource.understanding_id,
                    result_revision,
                    secrets.token_urlsafe(24),
                    old["day_index"],
                    old["sequence_index"],
                    old["role"],
                    old["mention_text"],
                    old["atomic_place_name"],
                    old["category_hint"],
                    old["time_hint"],
                    old["eligible_for_place_search"],
                    old["resolution_status"],
                    old["canonical_place_id"],
                    json.dumps(_json_value(old["resolver_receipt_json"]), ensure_ascii=False),
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
                resource.understanding_id,
                result_revision,
                json.dumps(public_payload, ensure_ascii=False),
                public_hash,
                opaque_etag,
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understandings
                SET state = $2, current_revision = $3, result_revision = $3,
                    current_result_id = $4, updated_at = $5
                WHERE understanding_id = $1
                """,
                resource.understanding_id,
                terminal_state,
                result_revision,
                result_id,
                now,
            )
            applied = CommandAppliedView(changed_days=mutation.changed_days)
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 200,
                    response_json = $3::jsonb, response_headers_json = $4::jsonb,
                    lease_until = NULL, completed_at = $5
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                json.dumps(applied.model_dump(mode="json"), ensure_ascii=False),
                json.dumps({"ETag": f'"{opaque_etag}"'}, ensure_ascii=False),
                now,
            )
        return CommandOutcome(applied=applied, opaque_etag=opaque_etag)

    async def claim_demo(
        self,
        public_resource_id: str,
        *,
        capability_hash: str,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> ClaimOutcome:
        if len(idempotency_key) > 200:
            raise ValueError("idempotency key is too long")
        key_hash = _sha256_text(idempotency_key)
        expires_at = now + timedelta(days=retention_days)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{user_id}",
            )
            tombstone = await conn.fetchrow(
                """
                SELECT reason, replacement_public_resource_id
                FROM trip_understanding_resource_tombstones
                WHERE public_resource_id = $1
                """,
                public_resource_id,
            )
            if tombstone is not None:
                if tombstone["reason"] != "CLAIMED":
                    raise ResourceGoneError("trip resource is no longer available")
                existing = await conn.fetchrow(
                    """
                    SELECT request_hash, response_json
                    FROM trip_understanding_claim_commands
                    WHERE old_public_resource_id = $1 AND actor_user_id = $2
                      AND idempotency_key_hash = $3
                    """,
                    public_resource_id,
                    user_id,
                    key_hash,
                )
                if existing is None:
                    raise ResourceGoneError("anonymous trip was already claimed")
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different claim"
                    )
                current_etag = await conn.fetchval(
                    """
                    SELECT r.opaque_etag FROM trip_understandings u
                    JOIN trip_understanding_results r ON r.result_id = u.current_result_id
                    WHERE u.public_resource_id = $1
                    """,
                    tombstone["replacement_public_resource_id"],
                )
                if current_etag is None:
                    raise ResourceNotReadyError("claimed trip result is unavailable")
                return ClaimOutcome(
                    claimed=ClaimedTripView.model_validate(_json_value(existing["response_json"])),
                    opaque_etag=current_etag,
                    replayed=True,
                )

            row = await conn.fetchrow(
                """
                SELECT u.*, s.capability_hash, s.expires_at, s.revoked_at
                FROM trip_understandings u
                JOIN trip_understanding_anonymous_sessions s
                  ON s.session_id = u.anonymous_session_id
                WHERE u.public_resource_id = $1
                FOR UPDATE OF u, s
                """,
                public_resource_id,
            )
            if row is None:
                raise ResourceNotFoundError("anonymous trip does not exist")
            if (
                not hmac.compare_digest(row["capability_hash"].strip(), capability_hash)
                or row["revoked_at"] is not None
                or row["expires_at"] <= now
            ):
                raise ResourceAccessDeniedError("anonymous trip is not available to this session")
            if row["current_result_id"] is None:
                raise ResourceNotReadyError("trip cards are not ready to claim")
            existing = await conn.fetchrow(
                """
                SELECT request_hash, response_json, new_public_resource_id
                FROM trip_understanding_claim_commands
                WHERE understanding_id = $1 AND idempotency_key_hash = $2
                """,
                row["understanding_id"],
                key_hash,
            )
            if existing is not None:
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used with a different claim"
                    )
                current_etag = await conn.fetchval(
                    "SELECT opaque_etag FROM trip_understanding_results WHERE result_id = $1",
                    row["current_result_id"],
                )
                return ClaimOutcome(
                    claimed=ClaimedTripView.model_validate(_json_value(existing["response_json"])),
                    opaque_etag=current_etag,
                    replayed=True,
                )

            new_public_resource_id = secrets.token_urlsafe(24)
            current_etag = await conn.fetchval(
                "SELECT opaque_etag FROM trip_understanding_results WHERE result_id = $1",
                row["current_result_id"],
            )
            await conn.execute(
                """
                UPDATE trip_understanding_anonymous_sessions
                SET claimed_by = $2, revoked_at = $3, last_seen_at = $3
                WHERE session_id = $1
                """,
                row["anonymous_session_id"],
                user_id,
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understandings
                SET public_resource_id = $2, owner_user_id = $3,
                    anonymous_session_id = NULL, etag_nonce = $4,
                    source_expires_at = $5, updated_at = $6
                WHERE understanding_id = $1
                """,
                row["understanding_id"],
                new_public_resource_id,
                user_id,
                secrets.token_hex(32),
                expires_at,
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understanding_sources
                SET retention_until = GREATEST(retention_until, $2)
                WHERE understanding_id = $1 AND deleted_at IS NULL
                """,
                row["understanding_id"],
                expires_at,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_resource_tombstones (
                    public_resource_id, reason, replacement_public_resource_id, created_at
                ) VALUES ($1, 'CLAIMED', $2, $3)
                """,
                public_resource_id,
                new_public_resource_id,
                now,
            )
            claimed = ClaimedTripView(public_resource_id=new_public_resource_id)
            await conn.execute(
                """
                INSERT INTO trip_understanding_claim_commands (
                    command_id, understanding_id, actor_user_id, idempotency_key_hash,
                    request_hash, old_public_resource_id, new_public_resource_id,
                    response_json, created_at
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9)
                """,
                str(uuid4()),
                row["understanding_id"],
                user_id,
                key_hash,
                request_hash,
                public_resource_id,
                new_public_resource_id,
                json.dumps(claimed.model_dump(mode="json"), ensure_ascii=False),
                now,
            )
        return ClaimOutcome(claimed=claimed, opaque_etag=current_etag)

    async def delete_source(
        self,
        resource: PublicResourceRecord,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> DeletionOutcome:
        scope = f"understanding:{resource.understanding_id}:delete-source"
        key_hash = _sha256_text(idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{user_id}",
            )
            claimed = await conn.fetchval(
                """
                INSERT INTO trip_understanding_idempotency_records (
                    scope, key_hash, request_hash, state, lease_until, created_at
                ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
                ON CONFLICT (scope, key_hash) DO NOTHING RETURNING scope
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
                    SELECT request_hash, state FROM trip_understanding_idempotency_records
                    WHERE scope = $1 AND key_hash = $2
                    """,
                    scope,
                    key_hash,
                )
                if existing["request_hash"].strip() != request_hash:
                    raise IdempotencyConflictError("source deletion idempotency key was reused")
                if existing["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("source deletion is still in progress")
                return DeletionOutcome(replayed=True)
            aggregate = await conn.fetchrow(
                "SELECT owner_user_id FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["owner_user_id"] != user_id:
                raise ResourceAccessDeniedError("only the signed-in owner can delete source text")
            sources = await conn.fetch(
                """
                SELECT source_id, content_hash, deleted_at
                FROM trip_understanding_sources
                WHERE understanding_id = $1 FOR UPDATE
                """,
                resource.understanding_id,
            )
            active_sources = [source for source in sources if source["deleted_at"] is None]
            if active_sources:
                receipt_hash = canonical_sha256(
                    {
                        "scope": "SOURCE",
                        "understanding_id": resource.understanding_id,
                        "content_hashes": sorted(source["content_hash"].strip() for source in sources),
                    }
                )
                await conn.execute(
                    "DELETE FROM trip_understanding_source_claims WHERE understanding_id = $1",
                    resource.understanding_id,
                )
                await conn.execute(
                    """
                    DELETE FROM trip_understanding_activities
                    WHERE understanding_id = $1 AND role <> 'PLANNED'
                    """,
                    resource.understanding_id,
                )
                await conn.execute(
                    """
                    UPDATE trip_understanding_sources
                    SET encrypted_content = NULL, encryption_key_ref = NULL,
                        deleted_at = $2, deletion_receipt_hash = $3
                    WHERE understanding_id = $1 AND deleted_at IS NULL
                    """,
                    resource.understanding_id,
                    now,
                    receipt_hash,
                )
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_deletion_jobs (
                        deletion_job_id, scope, understanding_id, owner_user_id,
                        status, request_hash, receipt_hash, created_at, updated_at
                    ) VALUES ($1, 'SOURCE', $2, $3, 'COMPLETED', $4, $5, $6, $6)
                    """,
                    str(uuid4()),
                    resource.understanding_id,
                    user_id,
                    request_hash,
                    receipt_hash,
                    now,
                )
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 204,
                    response_json = '{}'::jsonb, response_headers_json = '{}'::jsonb,
                    lease_until = NULL, completed_at = $3
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                now,
            )
        return DeletionOutcome()

    async def delete_trip(
        self,
        resource: PublicResourceRecord,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> DeletionOutcome:
        public_id_hash = _sha256_text(resource.public_resource_id)
        scope = f"deleted-resource:{public_id_hash}:delete-trip"
        key_hash = _sha256_text(idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            if user_id is not None:
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                    f"trip-understanding-user:{user_id}",
                )
            claimed = await conn.fetchval(
                """
                INSERT INTO trip_understanding_idempotency_records (
                    scope, key_hash, request_hash, state, lease_until, created_at
                ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
                ON CONFLICT (scope, key_hash) DO NOTHING RETURNING scope
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
                    raise IdempotencyConflictError("trip deletion idempotency key was reused")
                if existing["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("trip deletion is still in progress")
                replay = _json_value(existing["response_json"])
                if replay.get("authorization_kind") == "USER":
                    valid_actor = bool(user_id) and hmac.compare_digest(
                        replay.get("authorization_hash", ""),
                        _account_subject_hash(user_id or ""),
                    )
                else:
                    valid_actor = bool(capability_hash) and hmac.compare_digest(
                        replay.get("authorization_hash", ""),
                        capability_hash or "",
                    )
                if not valid_actor:
                    raise ResourceAccessDeniedError("trip deletion replay is not authorized")
                return DeletionOutcome(replayed=True)
            row = await conn.fetchrow(
                """
                SELECT u.public_resource_id, u.owner_user_id, u.anonymous_session_id,
                       s.capability_hash
                FROM trip_understandings u
                LEFT JOIN trip_understanding_anonymous_sessions s
                  ON s.session_id = u.anonymous_session_id
                WHERE u.understanding_id = $1
                FOR UPDATE OF u
                """,
                resource.understanding_id,
            )
            if row is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if row["public_resource_id"] != resource.public_resource_id:
                raise ResourceAccessDeniedError("trip resource binding changed")
            if row["owner_user_id"] is not None:
                if user_id != row["owner_user_id"]:
                    raise ResourceAccessDeniedError("trip deletion is not authorized")
                authorization_kind = "USER"
                authorization_hash = _account_subject_hash(user_id or "")
            else:
                stored_capability = (row["capability_hash"] or "").strip()
                if not capability_hash or not hmac.compare_digest(
                    stored_capability, capability_hash
                ):
                    raise ResourceAccessDeniedError("trip deletion is not authorized")
                authorization_kind = "ANONYMOUS"
                authorization_hash = capability_hash
            receipt_hash = canonical_sha256(
                {
                    "scope": "TRIP",
                    "public_resource_id_hash": public_id_hash,
                    "request_hash": request_hash,
                }
            )
            await conn.execute(
                """
                DELETE FROM trip_understanding_idempotency_records
                WHERE scope <> $1
                  AND (
                    scope IN ($2, $3)
                    OR response_json ->> 'public_resource_id' = $4
                    OR response_json ->> 'public_resource_id' IN (
                      SELECT public_resource_id
                      FROM trip_understanding_resource_tombstones
                      WHERE reason = 'CLAIMED'
                        AND replacement_public_resource_id = $4
                    )
                  )
                """,
                scope,
                f"understanding:{resource.understanding_id}:command",
                f"understanding:{resource.understanding_id}:delete-source",
                resource.public_resource_id,
            )
            await conn.execute(
                """
                UPDATE trip_understanding_resource_tombstones
                SET reason = 'DELETED', replacement_public_resource_id = NULL
                WHERE reason = 'CLAIMED' AND replacement_public_resource_id = $1
                """,
                row["public_resource_id"],
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_resource_tombstones (
                    public_resource_id, reason, created_at
                ) VALUES ($1, 'DELETED', $2)
                """,
                row["public_resource_id"],
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 204,
                    response_json = $3::jsonb, response_headers_json = $4::jsonb,
                    lease_until = NULL, completed_at = $5
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                json.dumps(
                    {
                        "authorization_kind": authorization_kind,
                        "authorization_hash": authorization_hash,
                    },
                    ensure_ascii=False,
                ),
                json.dumps({"receipt_hash": receipt_hash}, ensure_ascii=False),
                now,
            )
            await _delete_understanding_business_rows(conn, resource.understanding_id)
            if row["owner_user_id"] is not None:
                await conn.execute(
                    """
                    UPDATE trip_understanding_anonymous_sessions
                    SET claimed_by = NULL
                    WHERE claimed_by = $1
                    """,
                    row["owner_user_id"],
                )
            if row["anonymous_session_id"] is not None:
                await conn.execute(
                    """
                    DELETE FROM trip_understanding_anonymous_sessions s
                    WHERE s.session_id = $1
                      AND NOT EXISTS (
                        SELECT 1 FROM trip_understandings u
                        WHERE u.anonymous_session_id = s.session_id
                      )
                    """,
                    row["anonymous_session_id"],
                )
        return DeletionOutcome()

    async def replay_trip_deletion(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        request_hash: str,
    ) -> bool:
        scope = f"deleted-resource:{_sha256_text(public_resource_id)}:delete-trip"
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT request_hash, state, response_json
                FROM trip_understanding_idempotency_records
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                _sha256_text(idempotency_key),
            )
        if row is None or row["state"] != "COMPLETED":
            return False
        if row["request_hash"].strip() != request_hash:
            raise IdempotencyConflictError("trip deletion idempotency key was reused")
        replay = _json_value(row["response_json"])
        if replay.get("authorization_kind") == "USER":
            return bool(user_id) and hmac.compare_digest(
                replay.get("authorization_hash", ""),
                _account_subject_hash(user_id or ""),
            )
        return bool(capability_hash) and hmac.compare_digest(
            replay.get("authorization_hash", ""),
            capability_hash or "",
        )

    async def tombstone_reason(self, public_resource_id: str) -> str | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(
                "SELECT reason FROM trip_understanding_resource_tombstones WHERE public_resource_id = $1",
                public_resource_id,
            )

    async def delete_account_travel_data(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> TravelDataDeletionOutcome:
        subject_hash = _account_subject_hash(user_id)
        scope = f"account-travel-delete:{subject_hash}"
        key_hash = _sha256_text(idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{user_id}",
            )
            claimed = await conn.fetchval(
                """
                INSERT INTO trip_understanding_idempotency_records (
                    scope, key_hash, request_hash, state, lease_until, created_at
                ) VALUES ($1, $2, $3, 'IN_PROGRESS', $4, $5)
                ON CONFLICT (scope, key_hash) DO NOTHING RETURNING scope
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
                    raise IdempotencyConflictError("account deletion idempotency key was reused")
                if existing["state"] != "COMPLETED":
                    raise IdempotencyInProgressError("account travel deletion is still in progress")
                return TravelDataDeletionOutcome(
                    view=TravelDataDeletionStatusView.model_validate(
                        _json_value(existing["response_json"])
                    ),
                    replayed=True,
                )
            rows = await conn.fetch(
                """
                SELECT understanding_id, public_resource_id
                FROM trip_understandings WHERE owner_user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            claimed_session_ids = await conn.fetch(
                """
                SELECT session_id FROM trip_understanding_anonymous_sessions
                WHERE claimed_by = $1
                """,
                user_id,
            )
            for row in rows:
                await conn.execute(
                    """
                    UPDATE trip_understanding_resource_tombstones
                    SET reason = 'DELETED', replacement_public_resource_id = NULL
                    WHERE reason = 'CLAIMED' AND replacement_public_resource_id = $1
                    """,
                    row["public_resource_id"],
                )
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_resource_tombstones (
                        public_resource_id, reason, created_at
                    ) VALUES ($1, 'DELETED', $2)
                    ON CONFLICT (public_resource_id) DO NOTHING
                    """,
                    row["public_resource_id"],
                    now,
                )
                await conn.execute(
                    """
                    DELETE FROM trip_understanding_idempotency_records
                    WHERE scope IN ($1, $2)
                       OR response_json ->> 'public_resource_id' = $3
                    """,
                    f"understanding:{row['understanding_id']}:command",
                    f"understanding:{row['understanding_id']}:delete-source",
                    row["public_resource_id"],
                )
            for session in claimed_session_ids:
                await conn.execute(
                    "DELETE FROM trip_understanding_idempotency_records WHERE scope = $1",
                    f"anonymous:{session['session_id']}:create",
                )
            await conn.execute(
                """
                DELETE FROM trip_understanding_idempotency_records
                WHERE scope IN ($1, $2)
                   OR (
                     response_json ->> 'authorization_kind' = 'USER'
                     AND response_json ->> 'authorization_hash' = $3
                   )
                """,
                f"user:{user_id}:account-travel-delete",
                f"user:{user_id}:create",
                subject_hash,
            )
            await conn.execute(
                "DELETE FROM trip_understanding_deletion_jobs WHERE owner_user_id = $1",
                user_id,
            )
            for row in rows:
                await _delete_understanding_business_rows(conn, row["understanding_id"])
            await conn.execute(
                """
                UPDATE trip_understanding_anonymous_sessions
                SET claimed_by = NULL
                WHERE claimed_by = $1
                """,
                user_id,
            )
            receipt_hash = canonical_sha256(
                {
                    "scope": "ACCOUNT_TRAVEL_DATA",
                    "deleted_resource_count": len(rows),
                    "deleted_resource_ids": sorted(row["understanding_id"] for row in rows),
                }
            )
            view = TravelDataDeletionStatusView(
                status="COMPLETED",
                message="旅行数据已清空",
                next_action="NONE",
            )
            await conn.execute(
                """
                UPDATE trip_understanding_idempotency_records
                SET state = 'COMPLETED', response_status = 202,
                    response_json = $3::jsonb, response_headers_json = $4::jsonb,
                    lease_until = NULL, completed_at = $5
                WHERE scope = $1 AND key_hash = $2
                """,
                scope,
                key_hash,
                json.dumps(view.model_dump(mode="json"), ensure_ascii=False),
                json.dumps({"receipt_hash": receipt_hash}, ensure_ascii=False),
                now,
            )
        return TravelDataDeletionOutcome(view=view)

    async def get_account_travel_data_deletion(
        self,
        *,
        user_id: str,
    ) -> TravelDataDeletionStatusView:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT state, response_json
                FROM trip_understanding_idempotency_records
                WHERE scope = $1
                ORDER BY created_at DESC LIMIT 1
                """,
                f"account-travel-delete:{_account_subject_hash(user_id)}",
            )
        if row is None:
            return TravelDataDeletionStatusView(
                status="COMPLETED",
                message="当前没有进行中的删除请求",
                next_action="NONE",
            )
        if row["state"] == "IN_PROGRESS":
            return TravelDataDeletionStatusView(
                status="IN_PROGRESS",
                message="正在清理旅行数据",
                next_action="NONE",
            )
        return TravelDataDeletionStatusView.model_validate(_json_value(row["response_json"]))

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

    async def load_source(
        self,
        job: TripUnderstandingJobRecord,
        *,
        now: datetime,
    ) -> TripUnderstandingSourcePayload:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT s.source_id, s.source_type, s.content_hash, s.encrypted_content,
                       s.encryption_key_ref, s.retention_until, s.deleted_at
                FROM trip_understanding_jobs j
                JOIN trip_understanding_revisions r
                  ON r.understanding_id = j.understanding_id AND r.revision = j.revision
                JOIN trip_understanding_sources s ON s.source_id = r.source_id
                WHERE j.job_id = $1
                """,
                job.job_id,
            )
        if row is None:
            raise ResourceNotFoundError("understanding source does not exist")
        content_hash = row["content_hash"].strip()
        if content_hash != job.input_hash:
            raise ValueError("claimed job is not bound to its source")
        if row["deleted_at"] is not None or row["retention_until"] <= now:
            raise SourceUnavailableError("understanding source is no longer available")
        if row["source_type"] == "FIXED_DEMO":
            if content_hash != DEMO_SOURCE_SHA256:
                raise ValueError("fixed demo source hash is invalid")
            return TripUnderstandingSourcePayload(source_type="FIXED_DEMO", text=DEMO_SOURCE_TEXT)
        if row["source_type"] != "TEXT" or row["encrypted_content"] is None:
            raise SourceUnavailableError("recoverable text source is unavailable")
        cipher = self._get_source_cipher()
        if row["encryption_key_ref"] != cipher.key_ref:
            raise SourceUnavailableError("source encryption key is unavailable")
        text = cipher.decrypt(
            bytes(row["encrypted_content"]),
            source_id=row["source_id"],
            content_hash=content_hash,
        )
        if _sha256_text(text) != content_hash:
            raise ValueError("decrypted source hash mismatch")
        return TripUnderstandingSourcePayload(source_type="TEXT", text=text)

    async def complete_job(
        self,
        job: TripUnderstandingJobRecord,
        output: PipelineOutput,
        *,
        now: datetime,
    ) -> bool:
        if output.source_hash != job.input_hash:
            raise ValueError("worker output is not bound to the claimed source")
        effect_key = f"trip-understanding:{job.understanding_id}:r{job.revision}:pipeline-v1"
        public_payload = output.public_result.model_dump(mode="json")
        public_hash = canonical_sha256(public_payload)
        terminal_state = "READY" if output.public_result.status == "READY" else "PARTIAL"
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
            source_row = await conn.fetchrow(
                """
                SELECT s.source_id, s.source_type, s.content_hash
                FROM trip_understanding_revisions r
                JOIN trip_understanding_sources s ON s.source_id = r.source_id
                WHERE r.understanding_id = $1 AND r.revision = $2
                """,
                job.understanding_id,
                job.revision,
            )
            if source_row is None:
                raise ResourceNotFoundError("understanding source does not exist")
            source_id = source_row["source_id"]
            source_type = source_row["source_type"]
            source_hash = source_row["content_hash"].strip()
            result_revision = int(aggregate["current_revision"]) + 1
            await conn.execute(
                """
                INSERT INTO trip_understanding_revisions (
                    understanding_id, revision, parent_revision, source_id, status,
                    content_hash, destination_json, assumptions_json, proposal_json,
                    inference_binding_json, compiler_receipt_json, created_at
                ) VALUES (
                    $1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb,
                    $9::jsonb, $10::jsonb, $11::jsonb, $12
                )
                """,
                job.understanding_id,
                result_revision,
                job.revision,
                source_id,
                terminal_state,
                output.content_hash,
                json.dumps(output.destination, ensure_ascii=False),
                json.dumps(output.assumptions, ensure_ascii=False),
                json.dumps(_persisted_proposal(output), ensure_ascii=False),
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
                stored_quote = claim.quote
                if source_type == "TEXT":
                    quote_envelope = self._get_source_cipher().encrypt(
                        claim.quote,
                        source_id=source_id,
                        content_hash=source_hash,
                        purpose=f"claim:{claim.claim_id}",
                    )
                    stored_quote = "enc:v1:" + base64.urlsafe_b64encode(quote_envelope).decode("ascii")
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
                    stored_quote,
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
                        "place_resolution": "controlled_fixture_snapshot",
                        "external_calls": 0,
                    },
                    ensure_ascii=False,
                ),
                now,
            )
            await conn.execute(
                """
                UPDATE trip_understandings
                SET state = $2, current_revision = $3, result_revision = $3,
                    current_result_id = $4, updated_at = $5
                WHERE understanding_id = $1
                """,
                job.understanding_id,
                terminal_state,
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
            await self._enqueue_initial_map_job(
                conn,
                job.understanding_id,
                result_revision,
                now=now,
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


class InMemoryTripUnderstandingRepository(InMemoryMapRenderRepositoryMixin):
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.resources: dict[str, dict[str, Any]] = {}
        self.resources_by_understanding: dict[str, str] = {}
        self.idempotency: dict[tuple[str, str], tuple[str, TripUnderstandingAcceptedView]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[PublicEventRecord]] = {}
        self.results: dict[str, StoredResult] = {}
        self.result_owners: dict[str, str] = {}
        self.result_revisions: dict[str, int] = {}
        self.side_effects: dict[str, tuple[str, str]] = {}
        self.sources: dict[str, TripUnderstandingSourcePayload] = {}
        self.command_idempotency: dict[tuple[str, str], tuple[str, CommandOutcome]] = {}
        self.claim_idempotency: dict[tuple[str, str, str], tuple[str, ClaimOutcome]] = {}
        self.privacy_idempotency: dict[tuple[str, str], str] = {}
        self.trip_deletion_idempotency: dict[
            tuple[str, str], tuple[str, str, str]
        ] = {}
        self.tombstones: dict[str, dict[str, str | None]] = {}
        self.account_deletion_status: dict[str, TravelDataDeletionStatusView] = {}
        self._init_map_store()

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
            "owner_user_id": None,
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
        self.sources[job_id] = TripUnderstandingSourcePayload(
            source_type="FIXED_DEMO",
            text=DEMO_SOURCE_TEXT,
        )
        self.events[understanding_id] = [
            PublicEventRecord(
                event_id=1,
                event_type="progress",
                payload=PublicEventPayload(status="PROCESSING", message="正在整理每天行程"),
            )
        ]
        return CreateOutcome(accepted=accepted)

    async def create_full(
        self,
        *,
        owner_user_id: str,
        source_text: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> CreateOutcome:
        if not source_text.strip() or len(source_text) > 50_000:
            raise ValueError("text source is outside the supported size")
        scope = f"user:{owner_user_id}:create"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different request"
                )
            return CreateOutcome(accepted=existing[1], replayed=True)
        active_count = sum(
            row["owner_user_id"] == owner_user_id and row["state"] == "PROCESSING"
            for row in self.resources.values()
        )
        if active_count >= 2:
            raise ConcurrentJobLimitError("user already has two understanding jobs in progress")
        understanding_id = str(uuid4())
        public_resource_id = secrets.token_urlsafe(24)
        accepted = _accepted(public_resource_id)
        self.idempotency[key] = (request_hash, accepted)
        self.resources[public_resource_id] = {
            "understanding_id": understanding_id,
            "public_resource_id": public_resource_id,
            "state": "PROCESSING",
            "current_result_id": None,
            "capability_hash": None,
            "owner_user_id": owner_user_id,
            "expires_at": now + timedelta(days=retention_days),
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
            "input_hash": _sha256_text(source_text),
            "available_at": now,
        }
        self.sources[job_id] = TripUnderstandingSourcePayload(
            source_type="TEXT",
            text=source_text,
        )
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
        if public_resource_id in self.tombstones:
            raise ResourceGoneError("trip resource is no longer available")
        row = self.resources.get(public_resource_id)
        if row is None:
            raise ResourceNotFoundError("trip resource does not exist")
        if row["state"] == "DELETED":
            raise ResourceGoneError("trip resource is no longer available")
        if row["owner_user_id"] is not None:
            if user_id != row["owner_user_id"]:
                raise ResourceAccessDeniedError("trip resource is not available to this session")
        elif (
            not capability_hash
            or not hmac.compare_digest(row["capability_hash"], capability_hash)
            or row["expires_at"] <= now
        ):
            raise ResourceAccessDeniedError("trip resource is not available to this session")
        return PublicResourceRecord.model_validate(
            {key: row[key] for key in PublicResourceRecord.model_fields}
        )

    async def get_result(self, resource: PublicResourceRecord) -> StoredResult | None:
        stored = self.results.get(resource.current_result_id or "")
        if stored is None:
            return None
        public_id = self.resources_by_understanding[resource.understanding_id]
        aggregate = self.resources[public_id]
        readiness = self._project_map_readiness_memory(
            resource.understanding_id,
            int(aggregate["current_revision"]),
        )
        return stored.model_copy(
            update={"result": stored.result.model_copy(update={"map": readiness})}
        )

    async def apply_command(
        self,
        resource: PublicResourceRecord,
        command: TripUnderstandingCommand,
        *,
        expected_etag: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> CommandOutcome:
        del now
        scope = f"understanding:{resource.understanding_id}:command"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.command_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different request"
                )
            return existing[1].model_copy(update={"replayed": True})
        public_id = self.resources_by_understanding[resource.understanding_id]
        aggregate = self.resources[public_id]
        stored = self.results.get(aggregate["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready for editing")
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("command precondition does not match current result")
        mutation = apply_public_command(stored.result, command)
        result_id = str(uuid4())
        opaque_etag = f"tu3_{secrets.token_urlsafe(32)}"
        self.results[result_id] = StoredResult(
            result=mutation.result,
            opaque_etag=opaque_etag,
        )
        self.result_owners[result_id] = resource.understanding_id
        self.result_revisions[result_id] = int(aggregate["current_revision"]) + 1
        aggregate.update(
            {
                "state": "READY" if mutation.result.status == "READY" else "PARTIAL",
                "current_result_id": result_id,
                "current_revision": aggregate["current_revision"] + 1,
            }
        )
        outcome = CommandOutcome(
            applied=CommandAppliedView(changed_days=mutation.changed_days),
            opaque_etag=opaque_etag,
        )
        self.command_idempotency[key] = (request_hash, outcome)
        return outcome

    async def claim_demo(
        self,
        public_resource_id: str,
        *,
        capability_hash: str,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> ClaimOutcome:
        key = (public_resource_id, user_id, _sha256_text(idempotency_key))
        existing = self.claim_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError("claim idempotency key was reused")
            return existing[1].model_copy(update={"replayed": True})
        tombstone = self.tombstones.get(public_resource_id)
        if tombstone is not None:
            raise ResourceGoneError("anonymous trip was already claimed or deleted")
        row = self.resources.get(public_resource_id)
        if row is None:
            raise ResourceNotFoundError("anonymous trip does not exist")
        if row["owner_user_id"] is not None:
            raise ResourceAccessDeniedError("trip is already owned")
        if (
            not hmac.compare_digest(row["capability_hash"], capability_hash)
            or row["expires_at"] <= now
        ):
            raise ResourceAccessDeniedError("anonymous trip is not available to this session")
        stored = self.results.get(row["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready to claim")
        new_public_id = secrets.token_urlsafe(24)
        self.resources.pop(public_resource_id)
        row.update(
            {
                "public_resource_id": new_public_id,
                "owner_user_id": user_id,
                "capability_hash": None,
                "expires_at": now + timedelta(days=retention_days),
            }
        )
        self.resources[new_public_id] = row
        self.resources_by_understanding[row["understanding_id"]] = new_public_id
        self.tombstones[public_resource_id] = {
            "reason": "CLAIMED",
            "replacement_public_resource_id": new_public_id,
        }
        outcome = ClaimOutcome(
            claimed=ClaimedTripView(public_resource_id=new_public_id),
            opaque_etag=stored.opaque_etag,
        )
        self.claim_idempotency[key] = (request_hash, outcome)
        return outcome

    async def delete_source(
        self,
        resource: PublicResourceRecord,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> DeletionOutcome:
        del now
        public_id = self.resources_by_understanding[resource.understanding_id]
        row = self.resources[public_id]
        if row["owner_user_id"] != user_id:
            raise ResourceAccessDeniedError("only the signed-in owner can delete source text")
        scope = f"understanding:{resource.understanding_id}:delete-source"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.privacy_idempotency.get(key)
        if existing:
            if existing != request_hash:
                raise IdempotencyConflictError("source deletion idempotency key was reused")
            return DeletionOutcome(replayed=True)
        for job_id, job in list(self.jobs.items()):
            if job["understanding_id"] == resource.understanding_id:
                self.sources.pop(job_id, None)
        self.privacy_idempotency[key] = request_hash
        return DeletionOutcome()

    async def delete_trip(
        self,
        resource: PublicResourceRecord,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> DeletionOutcome:
        del now
        public_id_hash = _sha256_text(resource.public_resource_id)
        key = (public_id_hash, _sha256_text(idempotency_key))
        existing = self.trip_deletion_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError("trip deletion idempotency key was reused")
            if existing[1] == "USER":
                valid_actor = bool(user_id) and hmac.compare_digest(
                    existing[2], _account_subject_hash(user_id or "")
                )
            else:
                valid_actor = bool(capability_hash) and hmac.compare_digest(
                    existing[2], capability_hash or ""
                )
            if not valid_actor:
                raise ResourceAccessDeniedError("trip deletion replay is not authorized")
            return DeletionOutcome(replayed=True)
        public_id = self.resources_by_understanding[resource.understanding_id]
        row = self.resources[public_id]
        if row["owner_user_id"] is not None:
            if user_id != row["owner_user_id"]:
                raise ResourceAccessDeniedError("trip deletion is not authorized")
            authorization_kind = "USER"
            authorization_hash = _account_subject_hash(user_id or "")
        else:
            if not capability_hash or not hmac.compare_digest(
                row["capability_hash"], capability_hash
            ):
                raise ResourceAccessDeniedError("trip deletion is not authorized")
            authorization_kind = "ANONYMOUS"
            authorization_hash = capability_hash
        self._delete_map_memory(resource.understanding_id)
        for result_id, understanding_id in list(self.result_owners.items()):
            if understanding_id == resource.understanding_id:
                self.results.pop(result_id, None)
                self.result_owners.pop(result_id, None)
                self.result_revisions.pop(result_id, None)
        for job_id, job in list(self.jobs.items()):
            if job["understanding_id"] == resource.understanding_id:
                self.jobs.pop(job_id, None)
                self.sources.pop(job_id, None)
        self.events.pop(resource.understanding_id, None)
        for effect_key in list(self.side_effects):
            if effect_key.startswith(f"trip-understanding:{resource.understanding_id}:"):
                self.side_effects.pop(effect_key, None)
        previous_public_ids = {
            old_public_id
            for old_public_id, tombstone in self.tombstones.items()
            if tombstone.get("replacement_public_resource_id") == public_id
        }
        for create_key, (_, accepted) in list(self.idempotency.items()):
            if accepted.public_resource_id in {public_id, *previous_public_ids}:
                self.idempotency.pop(create_key, None)
        command_scope = f"understanding:{resource.understanding_id}:command"
        for command_key in list(self.command_idempotency):
            if command_key[0] == command_scope:
                self.command_idempotency.pop(command_key, None)
        source_scope = f"understanding:{resource.understanding_id}:delete-source"
        for privacy_key in list(self.privacy_idempotency):
            if privacy_key[0] == source_scope:
                self.privacy_idempotency.pop(privacy_key, None)
        for claim_key, (_, outcome) in list(self.claim_idempotency.items()):
            if outcome.claimed.public_resource_id == public_id:
                self.claim_idempotency.pop(claim_key, None)
        for tombstone in self.tombstones.values():
            if tombstone.get("replacement_public_resource_id") == public_id:
                tombstone.update(
                    {"reason": "DELETED", "replacement_public_resource_id": None}
                )
        self.resources.pop(public_id, None)
        self.resources_by_understanding.pop(resource.understanding_id, None)
        self.tombstones[public_id] = {
            "reason": "DELETED",
            "replacement_public_resource_id": None,
        }
        self.trip_deletion_idempotency[key] = (
            request_hash,
            authorization_kind,
            authorization_hash,
        )
        return DeletionOutcome()

    async def replay_trip_deletion(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        request_hash: str,
    ) -> bool:
        existing = self.trip_deletion_idempotency.get(
            (_sha256_text(public_resource_id), _sha256_text(idempotency_key))
        )
        if existing is None:
            return False
        if existing[0] != request_hash:
            raise IdempotencyConflictError("trip deletion idempotency key was reused")
        if existing[1] == "USER":
            return bool(user_id) and hmac.compare_digest(
                existing[2], _account_subject_hash(user_id or "")
            )
        return bool(capability_hash) and hmac.compare_digest(
            existing[2], capability_hash or ""
        )

    async def tombstone_reason(self, public_resource_id: str) -> str | None:
        tombstone = self.tombstones.get(public_resource_id)
        return str(tombstone["reason"]) if tombstone else None

    async def delete_account_travel_data(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
    ) -> TravelDataDeletionOutcome:
        subject_hash = _account_subject_hash(user_id)
        scope = f"account-travel-delete:{subject_hash}"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.privacy_idempotency.get(key)
        if existing:
            if existing != request_hash:
                raise IdempotencyConflictError("account deletion idempotency key was reused")
            return TravelDataDeletionOutcome(
                view=self.account_deletion_status[subject_hash],
                replayed=True,
            )
        owned_resources = [
            PublicResourceRecord.model_validate(
                {field: row[field] for field in PublicResourceRecord.model_fields}
            )
            for row in self.resources.values()
            if row["owner_user_id"] == user_id and row["state"] != "DELETED"
        ]
        for index, owned in enumerate(owned_resources):
            await self.delete_trip(
                owned,
                capability_hash=None,
                user_id=user_id,
                idempotency_key=f"account-cascade-{index}-{idempotency_key}",
                request_hash=request_hash,
                now=now,
            )
        for deletion_key, deletion_record in list(
            self.trip_deletion_idempotency.items()
        ):
            if deletion_record[1:] == ("USER", subject_hash):
                self.trip_deletion_idempotency.pop(deletion_key, None)
        view = TravelDataDeletionStatusView(
            status="COMPLETED",
            message="旅行数据已清空",
            next_action="NONE",
        )
        self.account_deletion_status[subject_hash] = view
        self.privacy_idempotency[key] = request_hash
        return TravelDataDeletionOutcome(view=view)

    async def get_account_travel_data_deletion(
        self,
        *,
        user_id: str,
    ) -> TravelDataDeletionStatusView:
        return self.account_deletion_status.get(
            _account_subject_hash(user_id),
            TravelDataDeletionStatusView(
                status="COMPLETED",
                message="当前没有进行中的删除请求",
                next_action="NONE",
            ),
        )

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

    async def load_source(
        self,
        job: TripUnderstandingJobRecord,
        *,
        now: datetime,
    ) -> TripUnderstandingSourcePayload:
        del now
        source = self.sources.get(job.job_id)
        if source is None or _sha256_text(source.text) != job.input_hash:
            raise SourceUnavailableError("understanding source is unavailable")
        return source

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
        effect_key = f"trip-understanding:{job.understanding_id}:r{job.revision}:pipeline-v1"
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
        self.result_owners[result_id] = job.understanding_id
        self.result_revisions[result_id] = 2
        public_id = self.resources_by_understanding[job.understanding_id]
        self.resources[public_id].update(
            {
                "state": "READY" if output.public_result.status == "READY" else "PARTIAL",
                "current_result_id": result_id,
                "current_revision": 2,
            }
        )
        self._enqueue_initial_map_job_memory(
            job.understanding_id,
            2,
            now=now,
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
