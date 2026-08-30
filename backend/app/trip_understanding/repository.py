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
    ScreenshotBatchAlreadyUsedError,
    ScreenshotBatchExpiredError,
    ScreenshotBatchNotFoundError,
    ScreenshotBatchNotReadyError,
    ScreenshotBatchUnusableError,
    SourceUnavailableError,
)
from app.trip_understanding.g03_repository import (
    G03Repository,
    InMemoryG03RepositoryMixin,
    PostgresG03RepositoryMixin,
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
    ScreenshotBatchAcceptedView,
    ScreenshotBatchClaimInput,
    ScreenshotBatchCreateOutcome,
    ScreenshotBatchFailurePersistenceInput,
    ScreenshotBatchPersistenceInput,
    ScreenshotCleanupPersistenceInput,
    ScreenshotCleanupReceiptInput,
    ConfirmationSourceSpan,
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
from app.trip_understanding.screenshot_batch.models import (
    CleanupAttempt,
    LocalScreenshotRecoveryReport,
)
from app.trip_understanding.screenshot_ocr import ScreenshotSourceDocumentV1
from app.trip_understanding.route_geometry import (
    InMemoryRouteGeometryCache,
    RedisRouteGeometryCache,
)
from app.trip_understanding.source_crypto import SourceCipher
from app.trip_understanding.stay_repository import (
    InMemoryStayRecommendationRepositoryMixin,
    PostgresStayRecommendationRepositoryMixin,
    StayRecommendationRepository,
)


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
    internal_room_id = await conn.fetchval(
        """
        SELECT tw.room_id
        FROM trip_materialized_trips mt
        JOIN trip_workspaces tw ON tw.workspace_id = mt.workspace_id
        WHERE mt.understanding_id = $1
        """,
        understanding_id,
    )
    if internal_room_id is not None:
        # The room is an internal compatibility record.  Removing it first
        # cascades the workspace, immutable audit history and its plan pointer,
        # avoiding a restrictive cycle when the v3 aggregate is removed.
        await conn.execute("DELETE FROM rooms WHERE room_id = $1", internal_room_id)
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
        """
        DELETE FROM trip_understanding_idempotency_records
        WHERE scope = ANY($1::text[])
        """,
        [
            f"understanding:{understanding_id}:g03-materialize",
            f"understanding:{understanding_id}:g03-preview",
            f"understanding:{understanding_id}:g03-adopt",
        ],
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


def _screenshot_accepted(
    batch_ref: str,
    *,
    expires_at: datetime,
    outcome: str,
) -> ScreenshotBatchAcceptedView:
    return ScreenshotBatchAcceptedView(
        batch_ref=batch_ref,
        expires_at=expires_at,
        outcome=outcome,
        message=(
            "截图已读取，可以生成行程卡片"
            if outcome == "COMPLETE"
            else "部分截图未能读取，已保留其余内容"
        ),
    )


def _latest_cleanup_statuses(
    receipts: tuple[ScreenshotCleanupReceiptInput, ...],
) -> dict[int, str]:
    latest: dict[int, tuple[int, str]] = {}
    for receipt in receipts:
        if receipt.upload_position is None:
            continue
        previous = latest.get(receipt.upload_position)
        if previous is None or receipt.attempt_number >= previous[0]:
            latest[receipt.upload_position] = (
                receipt.attempt_number,
                receipt.cleanup_status,
            )
    return {position: value[1] for position, value in latest.items()}


def _local_recovery_status(attempt: CleanupAttempt, locator: str) -> str:
    if locator in attempt.deleted_locators:
        return "DELETED"
    if locator in attempt.already_absent_locators:
        return "ALREADY_ABSENT"
    return "DELETE_FAILED"


def _require_confirmed_asset_cleanup(
    assets: tuple[Any, ...],
    receipts: tuple[ScreenshotCleanupReceiptInput, ...],
) -> None:
    final_statuses = _latest_cleanup_statuses(receipts)
    for asset in assets:
        if final_statuses.get(asset.upload_position) not in {"DELETED", "ALREADY_ABSENT"}:
            raise ValueError("a consumable screenshot batch requires confirmed cleanup")


def _require_document_asset_binding(
    document: ScreenshotSourceDocumentV1,
    assets: tuple[Any, ...],
) -> None:
    documented = [
        (image.image_index, image.content_hash, image.status)
        for image in sorted(document.images, key=lambda item: item.image_index)
    ]
    persisted = [
        (asset.upload_position, asset.content_hash, asset.ocr_status)
        for asset in sorted(assets, key=lambda item: item.upload_position)
    ]
    if documented != persisted:
        raise ValueError("screenshot assets differ from the OCR document binding")


async def _insert_screenshot_cleanup_receipts(
    conn: Any,
    *,
    batch_id: str,
    assets_by_position: dict[int, tuple[str, str, str]],
    submitted_assets: tuple[Any, ...],
    receipts: tuple[ScreenshotCleanupReceiptInput, ...],
    now: datetime,
) -> None:
    submitted_bindings = {
        asset.upload_position: (asset.content_hash, asset.storage_locator)
        for asset in submitted_assets
    }
    for receipt in receipts:
        position = receipt.upload_position
        submitted = submitted_bindings.get(position) if position is not None else None
        submitted_hash = submitted[0] if submitted is not None else None
        stored = assets_by_position.get(position) if position is not None else None
        asset_id = (
            stored[0]
            if stored is not None and submitted == (stored[1], stored[2])
            else None
        )
        await conn.execute(
            """
            INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                receipt_id, batch_id, asset_id, upload_position,
                asset_content_hash, attempt_number, terminal_reason,
                cleanup_status, error_category, attempted_at, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            str(uuid4()),
            batch_id,
            asset_id,
            position,
            submitted_hash,
            receipt.attempt_number,
            receipt.terminal_reason,
            receipt.cleanup_status,
            receipt.error_category,
            receipt.attempted_at,
            now,
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


class TripUnderstandingRepository(
    MapRenderRepository,
    StayRecommendationRepository,
    G03Repository,
    Protocol,
):
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

    async def preflight_screenshot_batch(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        batch_ref: str,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome | None: ...

    async def claim_screenshot_batch(
        self,
        payload: ScreenshotBatchClaimInput,
        *,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome | None: ...

    async def record_screenshot_cleanup(
        self,
        payload: ScreenshotCleanupPersistenceInput,
        *,
        now: datetime,
    ) -> None: ...

    async def reconcile_local_screenshot_recovery(
        self,
        report: LocalScreenshotRecoveryReport,
        *,
        now: datetime,
    ) -> dict[str, int]: ...

    async def store_screenshot_batch(
        self,
        payload: ScreenshotBatchPersistenceInput,
        *,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome: ...

    async def create_full_from_screenshot(
        self,
        *,
        owner_user_id: str,
        batch_ref: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> CreateOutcome: ...

    async def store_screenshot_batch_failure(
        self,
        payload: ScreenshotBatchFailurePersistenceInput,
        *,
        now: datetime,
    ) -> None: ...

    async def purge_expired_private_data(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> dict[str, int]: ...

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

    async def renew_lease(
        self,
        job: TripUnderstandingJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool: ...

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


class PostgresTripUnderstandingRepository(
    PostgresG03RepositoryMixin,
    PostgresStayRecommendationRepositoryMixin,
    PostgresMapRenderRepositoryMixin,
):
    def __init__(
        self,
        pool: Any | None = None,
        source_cipher: SourceCipher | None = None,
        geometry_cache: Any | None = None,
    ):
        self._pool = pool
        self._source_cipher = source_cipher
        self._geometry_cache = geometry_cache or RedisRouteGeometryCache(
            get_settings().redis_url
        )

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

    async def preflight_screenshot_batch(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        batch_ref: str,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome | None:
        """Resolve an already-bound key without receiving screenshot bytes."""

        del now
        key_hash = _sha256_text(idempotency_key)
        ref_hash = _sha256_text(batch_ref)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT batch_ref_hash, status, expires_at,
                       image_count, successful_image_count
                FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1 AND idempotency_key_hash = $2
                """,
                owner_user_id,
                key_hash,
            )
        if existing is None:
            return None
        if existing["batch_ref_hash"].strip() != ref_hash:
            raise IdempotencyConflictError(
                "screenshot upload idempotency key binding is invalid"
            )
        if existing["status"] in {"READY", "PARTIAL"}:
            replay_outcome = (
                "COMPLETE"
                if existing["successful_image_count"] == existing["image_count"]
                else "PARTIAL"
            )
            return ScreenshotBatchCreateOutcome(
                accepted=_screenshot_accepted(
                    batch_ref,
                    expires_at=existing["expires_at"],
                    outcome=replay_outcome,
                ),
                replayed=True,
            )
        if existing["status"] == "PROCESSING":
            raise IdempotencyInProgressError(
                "matching screenshot upload is still in progress"
            )
        raise IdempotencyConflictError(
            "screenshot upload idempotency key belongs to a terminal request"
        )

    async def claim_screenshot_batch(
        self,
        payload: ScreenshotBatchClaimInput,
        *,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome | None:
        key_hash = _sha256_text(payload.idempotency_key)
        ref_hash = _sha256_text(payload.batch_ref)
        positions = {asset.upload_position for asset in payload.assets}
        if len(positions) != len(payload.assets):
            raise ValueError("screenshot upload positions must be unique")
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"trip-understanding-user:{payload.owner_user_id}",
            )
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"screenshot-upload:{payload.owner_user_id}:{key_hash}",
            )
            existing = await conn.fetchrow(
                """
                SELECT request_hash, batch_ref_hash, status, expires_at,
                       image_count, successful_image_count
                FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1 AND idempotency_key_hash = $2
                """,
                payload.owner_user_id,
                key_hash,
            )
            if existing is not None:
                if (
                    existing["request_hash"].strip() != payload.request_hash
                    or existing["batch_ref_hash"].strip() != ref_hash
                ):
                    raise IdempotencyConflictError(
                        "screenshot upload idempotency key was reused"
                    )
                if existing["status"] in {"READY", "PARTIAL"}:
                    replay_outcome = (
                        "COMPLETE"
                        if existing["successful_image_count"] == existing["image_count"]
                        else "PARTIAL"
                    )
                    return ScreenshotBatchCreateOutcome(
                        accepted=_screenshot_accepted(
                            payload.batch_ref,
                            expires_at=existing["expires_at"],
                            outcome=replay_outcome,
                        ),
                        replayed=True,
                    )
                if existing["status"] == "PROCESSING":
                    raise IdempotencyInProgressError(
                        "matching screenshot upload is still in progress"
                    )
                raise IdempotencyConflictError(
                    "screenshot upload idempotency key belongs to a terminal request"
                )

            batch_id = str(uuid4())
            await conn.execute(
                """
                INSERT INTO trip_understanding_screenshot_batches (
                    batch_id, owner_user_id, batch_ref_hash, idempotency_key_hash,
                    request_hash, status, image_count, successful_image_count,
                    expires_at, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5, 'PROCESSING', $6, 0, $7, $8, $8)
                """,
                batch_id,
                payload.owner_user_id,
                ref_hash,
                key_hash,
                payload.request_hash,
                len(payload.assets),
                payload.expires_at,
                now,
            )
            for asset in payload.assets:
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_screenshot_assets (
                        asset_id, batch_id, upload_position, content_hash,
                        media_type, byte_size, storage_locator, ocr_status,
                        cleanup_status, created_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'PENDING', 'PENDING', $8)
                    """,
                    str(uuid4()),
                    batch_id,
                    asset.upload_position,
                    asset.content_hash,
                    asset.media_type,
                    asset.byte_size,
                    asset.storage_locator,
                    now,
                )
        return None

    async def record_screenshot_cleanup(
        self,
        payload: ScreenshotCleanupPersistenceInput,
        *,
        now: datetime,
    ) -> None:
        key_hash = _sha256_text(payload.idempotency_key)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"screenshot-upload:{payload.owner_user_id}:{key_hash}",
            )
            batch = await conn.fetchrow(
                """
                SELECT batch_id, status
                FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1 AND idempotency_key_hash = $2
                FOR UPDATE
                """,
                payload.owner_user_id,
                key_hash,
            )
            if batch is None:
                return
            asset_rows = await conn.fetch(
                """
                SELECT asset_id, upload_position, content_hash, storage_locator
                FROM trip_understanding_screenshot_assets
                WHERE batch_id = $1
                """,
                batch["batch_id"],
            )
            assets_by_position = {
                int(row["upload_position"]): (
                    row["asset_id"],
                    row["content_hash"].strip(),
                    row["storage_locator"],
                )
                for row in asset_rows
            }
            await _insert_screenshot_cleanup_receipts(
                conn,
                batch_id=batch["batch_id"],
                assets_by_position=assets_by_position,
                submitted_assets=payload.assets,
                receipts=payload.cleanup_receipts,
                now=now,
            )
            submitted_bindings = {
                asset.upload_position: (asset.content_hash, asset.storage_locator)
                for asset in payload.assets
            }
            for position, final_status in _latest_cleanup_statuses(
                payload.cleanup_receipts
            ).items():
                submitted = submitted_bindings.get(position)
                if submitted is None:
                    continue
                await conn.execute(
                    """
                    UPDATE trip_understanding_screenshot_assets
                    SET cleanup_status = $5
                    WHERE batch_id = $1 AND upload_position = $2
                      AND content_hash = $3 AND storage_locator = $4
                    """,
                    batch["batch_id"],
                    position,
                    submitted[0],
                    submitted[1],
                    (
                        "CLEANED"
                        if final_status in {"DELETED", "ALREADY_ABSENT"}
                        else "CLEANUP_FAILED"
                    ),
                )
            if payload.privacy_blocked and batch["status"] != "CONSUMED":
                await conn.execute(
                    """
                    UPDATE trip_understanding_screenshot_batches
                    SET status = 'PRIVACY_BLOCKED', encrypted_source_document = NULL,
                        encryption_key_ref = NULL, source_document_hash = NULL,
                        semantic_text_hash = NULL, document_purged_at = $2,
                        last_error_category = 'SCREENSHOT_CLEANUP_FAILED', updated_at = $2
                    WHERE batch_id = $1
                    """,
                    batch["batch_id"],
                    now,
                )

    async def reconcile_local_screenshot_recovery(
        self,
        report: LocalScreenshotRecoveryReport,
        *,
        now: datetime,
    ) -> dict[str, int]:
        """Persist locator-bound crash cleanup after the database is available."""

        receipts_recorded = 0
        orphan_receipts = 0
        batches_finalized = 0
        observed_locators = {
            locator
            for recovered in report.batches
            for locator in recovered.asset_locators
        }

        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            rows = (
                await conn.fetch(
                    """
                    SELECT a.asset_id, a.batch_id, a.upload_position,
                           a.content_hash, a.storage_locator
                    FROM trip_understanding_screenshot_assets AS a
                    WHERE a.storage_locator = ANY($1::text[])
                    FOR UPDATE
                    """,
                    sorted(observed_locators),
                )
                if observed_locators
                else ()
            )
            by_locator = {str(row["storage_locator"]): row for row in rows}
            affected_batches: set[str] = set()
            cleanup_failed_batches: set[str] = set()
            for recovered in report.batches:
                batch_locator_hash = _sha256_text(recovered.batch_locator)
                for attempt in recovered.attempts:
                    attempt_locators = recovered.asset_locators or (None,)
                    for locator in attempt_locators:
                        row = by_locator.get(locator)
                        cleanup_status = (
                            _local_recovery_status(attempt, locator)
                            if locator is not None
                            else "DELETED"
                            if attempt.succeeded
                            else "DELETE_FAILED"
                        )
                        asset_locator_hash = (
                            _sha256_text(locator) if locator is not None else None
                        )
                        event_hash = canonical_sha256(
                            {
                                "batch_locator_hash": batch_locator_hash,
                                "asset_locator_hash": asset_locator_hash,
                                "attempt_number": attempt.attempt_number,
                                "terminal_reason": attempt.terminal_reason,
                                "cleanup_status": cleanup_status,
                                "error_categories": list(attempt.error_categories),
                                "attempted_at": attempt.attempted_at.isoformat(),
                            }
                        )
                        inserted = await conn.fetchval(
                            """
                            INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                                receipt_id, batch_id, asset_id, upload_position,
                                asset_content_hash, orphan_batch_locator_hash,
                                orphan_asset_locator_hash, recovery_event_hash,
                                attempt_number, terminal_reason, cleanup_status,
                                error_category, attempted_at, created_at
                            ) VALUES (
                                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                                $10, $11, $12, $13, $14
                            )
                            ON CONFLICT (recovery_event_hash) DO NOTHING
                            RETURNING receipt_id
                            """,
                            str(uuid4()),
                            row["batch_id"] if row is not None else None,
                            row["asset_id"] if row is not None else None,
                            row["upload_position"] if row is not None else None,
                            (
                                row["content_hash"].strip()
                                if row is not None
                                else None
                            ),
                            None if row is not None else batch_locator_hash,
                            None if row is not None else asset_locator_hash,
                            event_hash,
                            attempt.attempt_number,
                            attempt.terminal_reason,
                            cleanup_status,
                            (
                                attempt.error_categories[0]
                                if attempt.error_categories
                                else None
                            ),
                            attempt.attempted_at,
                            now,
                        )
                        if inserted is not None:
                            receipts_recorded += 1
                            if row is None:
                                orphan_receipts += 1
                        if row is None:
                            continue
                        await conn.execute(
                            """
                            UPDATE trip_understanding_screenshot_assets
                            SET cleanup_status = $2
                            WHERE asset_id = $1
                            """,
                            row["asset_id"],
                            (
                                "CLEANED"
                                if cleanup_status in {"DELETED", "ALREADY_ABSENT"}
                                else "CLEANUP_FAILED"
                            ),
                        )
                        affected_batches.add(str(row["batch_id"]))
                        if not attempt.succeeded or not attempt.directory_removed:
                            cleanup_failed_batches.add(str(row["batch_id"]))
            for issue in report.issues:
                batch_locator_hash = _sha256_text(issue.batch_locator)
                event_hash = canonical_sha256(
                    {
                        "batch_locator_hash": batch_locator_hash,
                        "asset_locator_hash": None,
                        "attempt_number": 1,
                        "cleanup_status": "DELETE_FAILED",
                        "error_categories": [issue.category],
                        "attempted_at": issue.observed_at.isoformat(),
                    }
                )
                inserted = await conn.fetchval(
                    """
                    INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                        receipt_id, orphan_batch_locator_hash,
                        recovery_event_hash, attempt_number, terminal_reason,
                        cleanup_status, error_category, attempted_at, created_at
                    ) VALUES (
                        $1, $2, $3, 1, 'CRASH_RECOVERY',
                        'DELETE_FAILED', $4, $5, $6
                    )
                    ON CONFLICT (recovery_event_hash) DO NOTHING
                    RETURNING receipt_id
                    """,
                    str(uuid4()),
                    batch_locator_hash,
                    event_hash,
                    issue.category,
                    issue.observed_at,
                    now,
                )
                if inserted is not None:
                    receipts_recorded += 1
                    orphan_receipts += 1
            for batch_id in affected_batches:
                batch_status = await conn.fetchval(
                    """
                    SELECT status
                    FROM trip_understanding_screenshot_batches
                    WHERE batch_id = $1
                    FOR UPDATE
                    """,
                    batch_id,
                )
                if batch_status == "CONSUMED":
                    continue
                state = await conn.fetchrow(
                    """
                    SELECT COUNT(*) AS asset_count,
                           COUNT(*) FILTER (
                               WHERE a.cleanup_status = 'CLEANED'
                           ) AS cleaned_count,
                           COUNT(*) FILTER (
                               WHERE a.cleanup_status = 'CLEANUP_FAILED'
                           ) AS failed_count
                    FROM trip_understanding_screenshot_batches AS b
                    JOIN trip_understanding_screenshot_assets AS a
                      ON a.batch_id = b.batch_id
                    WHERE b.batch_id = $1
                    """,
                    batch_id,
                )
                failed = (
                    int(state["failed_count"]) > 0
                    or batch_id in cleanup_failed_batches
                )
                all_cleaned = int(state["cleaned_count"]) == int(
                    state["asset_count"]
                )
                if failed:
                    final_status = "PRIVACY_BLOCKED"
                    error_category = "SCREENSHOT_CLEANUP_FAILED"
                elif batch_status == "PROCESSING" and all_cleaned:
                    final_status = "FAILED"
                    error_category = "CRASH_RECOVERED_NO_RESULT"
                else:
                    continue
                await conn.execute(
                    """
                    UPDATE trip_understanding_screenshot_batches
                    SET status = $2, encrypted_source_document = NULL,
                        encryption_key_ref = NULL, source_document_hash = NULL,
                        semantic_text_hash = NULL, document_purged_at = $3,
                        last_error_category = $4, updated_at = $3
                    WHERE batch_id = $1 AND status <> 'CONSUMED'
                    """,
                    batch_id,
                    final_status,
                    now,
                    error_category,
                )
                batches_finalized += 1

        return {
            "matched_assets": len(by_locator),
            "receipts_recorded": receipts_recorded,
            "orphan_receipts": orphan_receipts,
            "batches_finalized": batches_finalized,
            "unmatched_assets": len(observed_locators - set(by_locator)),
            "local_issues": len(report.issues),
        }

    async def store_screenshot_batch(
        self,
        payload: ScreenshotBatchPersistenceInput,
        *,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome:
        document = ScreenshotSourceDocumentV1.model_validate_json(
            payload.source_document_json
        )
        if document.document_hash != payload.source_document_hash:
            raise ValueError("screenshot source document hash binding differs")
        if _sha256_text(document.semantic_text) != payload.semantic_text_hash:
            raise ValueError("screenshot semantic text hash binding differs")
        if len(payload.assets) != len(document.images):
            raise ValueError("screenshot asset count differs from source document")
        _require_document_asset_binding(document, payload.assets)
        if len({asset.upload_position for asset in payload.assets}) != len(
            payload.assets
        ):
            raise ValueError("screenshot upload positions must be unique")
        _require_confirmed_asset_cleanup(payload.assets, payload.cleanup_receipts)

        key_hash = _sha256_text(payload.idempotency_key)
        ref_hash = _sha256_text(payload.batch_ref)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"screenshot-upload:{payload.owner_user_id}:{key_hash}",
            )
            existing = await conn.fetchrow(
                """
                SELECT batch_id, request_hash, batch_ref_hash, status, expires_at,
                       image_count, successful_image_count
                FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1 AND idempotency_key_hash = $2
                FOR UPDATE
                """,
                payload.owner_user_id,
                key_hash,
            )
            if existing is not None:
                if (
                    existing["request_hash"].strip() != payload.request_hash
                    or existing["batch_ref_hash"].strip() != ref_hash
                ):
                    raise IdempotencyConflictError(
                        "screenshot upload idempotency key was reused"
                    )
                if existing["status"] in {"READY", "PARTIAL"}:
                    replay_outcome = (
                        "COMPLETE"
                        if existing["successful_image_count"] == existing["image_count"]
                        else "PARTIAL"
                    )
                    return ScreenshotBatchCreateOutcome(
                        accepted=_screenshot_accepted(
                            payload.batch_ref,
                            expires_at=existing["expires_at"],
                            outcome=replay_outcome,
                        ),
                        replayed=True,
                    )
                if existing["status"] != "PROCESSING":
                    raise IdempotencyConflictError(
                        "screenshot upload idempotency key belongs to a terminal request"
                    )
                batch_id = existing["batch_id"]
                asset_rows = await conn.fetch(
                    """
                    SELECT asset_id, upload_position, content_hash, media_type,
                           byte_size, storage_locator
                    FROM trip_understanding_screenshot_assets
                    WHERE batch_id = $1
                    ORDER BY upload_position
                    """,
                    batch_id,
                )
                expected = [
                    (
                        asset.upload_position,
                        asset.content_hash,
                        asset.media_type,
                        asset.byte_size,
                        asset.storage_locator,
                    )
                    for asset in sorted(
                        payload.assets, key=lambda item: item.upload_position
                    )
                ]
                stored = [
                    (
                        int(row["upload_position"]),
                        row["content_hash"].strip(),
                        row["media_type"],
                        int(row["byte_size"]),
                        row["storage_locator"],
                    )
                    for row in asset_rows
                ]
                if stored != expected:
                    raise IdempotencyConflictError(
                        "claimed screenshot assets no longer match the request"
                    )
            else:
                batch_id = str(uuid4())
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_screenshot_batches (
                        batch_id, owner_user_id, batch_ref_hash, idempotency_key_hash,
                        request_hash, status, image_count, successful_image_count,
                        expires_at, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, 'PROCESSING', $6, 0, $7, $8, $8)
                    """,
                    batch_id,
                    payload.owner_user_id,
                    ref_hash,
                    key_hash,
                    payload.request_hash,
                    len(payload.assets),
                    payload.expires_at,
                    now,
                )
                for asset in payload.assets:
                    await conn.execute(
                        """
                        INSERT INTO trip_understanding_screenshot_assets (
                            asset_id, batch_id, upload_position, content_hash,
                            media_type, byte_size, storage_locator, ocr_status,
                            cleanup_status, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'PENDING', 'PENDING', $8)
                        """,
                        str(uuid4()),
                        batch_id,
                        asset.upload_position,
                        asset.content_hash,
                        asset.media_type,
                        asset.byte_size,
                        asset.storage_locator,
                        now,
                    )

            cipher = self._get_source_cipher()
            encrypted_document = cipher.encrypt(
                payload.source_document_json,
                source_id=batch_id,
                content_hash=payload.source_document_hash,
                purpose="screenshot-batch",
            )
            successful_count = sum(
                asset.ocr_status == "SUCCEEDED" for asset in payload.assets
            )
            updated = await conn.execute(
                """
                UPDATE trip_understanding_screenshot_batches
                SET status = $2, encrypted_source_document = $3,
                    encryption_key_ref = $4, source_document_hash = $5,
                    semantic_text_hash = $6, successful_image_count = $7,
                    expires_at = $8, document_purged_at = NULL,
                    last_error_category = NULL, updated_at = $9
                WHERE batch_id = $1 AND status = 'PROCESSING'
                """,
                batch_id,
                "READY" if payload.outcome == "COMPLETE" else "PARTIAL",
                encrypted_document,
                cipher.key_ref,
                payload.source_document_hash,
                payload.semantic_text_hash,
                successful_count,
                payload.expires_at,
                now,
            )
            if updated != "UPDATE 1":
                raise IdempotencyInProgressError(
                    "screenshot upload claim changed before completion"
                )
            for asset in payload.assets:
                await conn.execute(
                    """
                    UPDATE trip_understanding_screenshot_assets
                    SET ocr_status = $3, cleanup_status = 'CLEANED'
                    WHERE batch_id = $1 AND upload_position = $2
                    """,
                    batch_id,
                    asset.upload_position,
                    asset.ocr_status,
                )
            asset_rows = await conn.fetch(
                """
                SELECT asset_id, upload_position, content_hash, storage_locator
                FROM trip_understanding_screenshot_assets
                WHERE batch_id = $1
                """,
                batch_id,
            )
            assets_by_position = {
                int(row["upload_position"]): (
                    row["asset_id"],
                    row["content_hash"].strip(),
                    row["storage_locator"],
                )
                for row in asset_rows
            }
            await _insert_screenshot_cleanup_receipts(
                conn,
                batch_id=batch_id,
                assets_by_position=assets_by_position,
                submitted_assets=payload.assets,
                receipts=payload.cleanup_receipts,
                now=now,
            )

        return ScreenshotBatchCreateOutcome(
            accepted=_screenshot_accepted(
                payload.batch_ref,
                expires_at=payload.expires_at,
                outcome=payload.outcome,
            )
        )

    async def store_screenshot_batch_failure(
        self,
        payload: ScreenshotBatchFailurePersistenceInput,
        *,
        now: datetime,
    ) -> None:
        key_hash = _sha256_text(payload.idempotency_key)
        ref_hash = _sha256_text(payload.batch_ref)
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                f"screenshot-upload:{payload.owner_user_id}:{key_hash}",
            )
            existing = await conn.fetchrow(
                """
                SELECT batch_id, request_hash, batch_ref_hash, status
                FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1 AND idempotency_key_hash = $2
                FOR UPDATE
                """,
                payload.owner_user_id,
                key_hash,
            )
            if existing is not None:
                if (
                    existing["request_hash"].strip() != payload.request_hash
                    or existing["batch_ref_hash"].strip() != ref_hash
                ):
                    raise IdempotencyConflictError(
                        "screenshot upload idempotency key was reused"
                    )
                if existing["status"] != "PROCESSING":
                    return
                batch_id = existing["batch_id"]
            else:
                batch_id = str(uuid4())
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_screenshot_batches (
                        batch_id, owner_user_id, batch_ref_hash, idempotency_key_hash,
                        request_hash, status, image_count, successful_image_count,
                        expires_at, last_error_category, created_at, updated_at
                    ) VALUES ($1, $2, $3, $4, $5, 'PROCESSING', $6, 0, $7, $8, $9, $9)
                    """,
                    batch_id,
                    payload.owner_user_id,
                    ref_hash,
                    key_hash,
                    payload.request_hash,
                    len(payload.assets),
                    payload.expires_at,
                    payload.last_error_category,
                    now,
                )

            stored_rows = await conn.fetch(
                """
                SELECT asset_id, upload_position, content_hash, storage_locator
                FROM trip_understanding_screenshot_assets
                WHERE batch_id = $1
                """,
                batch_id,
            )
            assets_by_position = {
                int(row["upload_position"]): (
                    row["asset_id"],
                    row["content_hash"].strip(),
                    row["storage_locator"],
                )
                for row in stored_rows
            }
            final_cleanup = _latest_cleanup_statuses(payload.cleanup_receipts)
            for asset in payload.assets:
                stored = assets_by_position.get(asset.upload_position)
                cleanup_status = (
                    "CLEANED"
                    if final_cleanup.get(asset.upload_position)
                    in {"DELETED", "ALREADY_ABSENT"}
                    else "CLEANUP_FAILED"
                    if final_cleanup.get(asset.upload_position) == "DELETE_FAILED"
                    else "PENDING"
                )
                if stored is None:
                    asset_id = str(uuid4())
                    await conn.execute(
                        """
                        INSERT INTO trip_understanding_screenshot_assets (
                            asset_id, batch_id, upload_position, content_hash,
                            media_type, byte_size, storage_locator, ocr_status,
                            cleanup_status, created_at
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                        """,
                        asset_id,
                        batch_id,
                        asset.upload_position,
                        asset.content_hash,
                        asset.media_type,
                        asset.byte_size,
                        asset.storage_locator,
                        asset.ocr_status,
                        cleanup_status,
                        now,
                    )
                    assets_by_position[asset.upload_position] = (
                        asset_id,
                        asset.content_hash,
                        asset.storage_locator,
                    )
                elif stored[1:] == (asset.content_hash, asset.storage_locator):
                    await conn.execute(
                        """
                        UPDATE trip_understanding_screenshot_assets
                        SET ocr_status = $3, cleanup_status = $4
                        WHERE batch_id = $1 AND upload_position = $2
                        """,
                        batch_id,
                        asset.upload_position,
                        asset.ocr_status,
                        cleanup_status,
                    )
                else:
                    raise IdempotencyConflictError(
                        "claimed screenshot assets no longer match the request"
                    )

            successful_count = sum(
                asset.ocr_status == "SUCCEEDED" for asset in payload.assets
            )
            await conn.execute(
                """
                UPDATE trip_understanding_screenshot_batches
                SET status = $2, encrypted_source_document = NULL,
                    encryption_key_ref = NULL, source_document_hash = NULL,
                    semantic_text_hash = NULL, successful_image_count = $3,
                    expires_at = $4, document_purged_at = $5,
                    last_error_category = $6, updated_at = $5
                WHERE batch_id = $1 AND status = 'PROCESSING'
                """,
                batch_id,
                payload.status,
                successful_count,
                payload.expires_at,
                now,
                payload.last_error_category,
            )
            await _insert_screenshot_cleanup_receipts(
                conn,
                batch_id=batch_id,
                assets_by_position=assets_by_position,
                submitted_assets=payload.assets,
                receipts=payload.cleanup_receipts,
                now=now,
            )

    async def create_full_from_screenshot(
        self,
        *,
        owner_user_id: str,
        batch_ref: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> CreateOutcome:
        if len(idempotency_key) > 200:
            raise ValueError("idempotency key is too long")
        expires_at = now + timedelta(days=retention_days)
        batch_ref_hash = _sha256_text(batch_ref)
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
                    raise IdempotencyInProgressError(
                        "matching create request is still in progress"
                    )
                return CreateOutcome(
                    accepted=TripUnderstandingAcceptedView.model_validate(
                        _json_value(existing["response_json"])
                    ),
                    replayed=True,
                )

            active_count = await conn.fetchval(
                """
                SELECT COUNT(*) FROM trip_understandings
                WHERE owner_user_id = $1 AND state = 'PROCESSING'
                """,
                owner_user_id,
            )
            if active_count >= 2:
                raise ConcurrentJobLimitError(
                    "user already has two understanding jobs in progress"
                )
            batch = await conn.fetchrow(
                """
                SELECT * FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1 AND batch_ref_hash = $2
                FOR UPDATE
                """,
                owner_user_id,
                batch_ref_hash,
            )
            if batch is None:
                raise ScreenshotBatchNotFoundError("screenshot batch does not exist")
            if batch["status"] == "EXPIRED" or (
                batch["expires_at"] <= now and batch["status"] in {"READY", "PARTIAL"}
            ):
                raise ScreenshotBatchExpiredError("screenshot batch has expired")
            if batch["status"] == "CONSUMED":
                raise ScreenshotBatchAlreadyUsedError("screenshot batch was already consumed")
            if batch["status"] == "PROCESSING":
                raise ScreenshotBatchNotReadyError("screenshot batch is not ready")
            if batch["status"] not in {"READY", "PARTIAL"}:
                raise ScreenshotBatchUnusableError("screenshot batch cannot be consumed")
            if batch["encrypted_source_document"] is None:
                raise ScreenshotBatchUnusableError("screenshot source document is unavailable")

            cipher = self._get_source_cipher()
            if batch["encryption_key_ref"] != cipher.key_ref:
                raise ScreenshotBatchUnusableError("screenshot encryption key is unavailable")
            document_json = cipher.decrypt(
                bytes(batch["encrypted_source_document"]),
                source_id=batch["batch_id"],
                content_hash=batch["source_document_hash"].strip(),
                purpose="screenshot-batch",
            )
            document = ScreenshotSourceDocumentV1.model_validate_json(document_json)
            content_hash = _sha256_text(document.semantic_text)
            if (
                document.document_hash != batch["source_document_hash"].strip()
                or content_hash != batch["semantic_text_hash"].strip()
            ):
                raise ValueError("screenshot source document integrity check failed")

            understanding_id = str(uuid4())
            public_resource_id = secrets.token_urlsafe(24)
            source_id = str(uuid4())
            job_id = str(uuid4())
            encrypted_source = cipher.encrypt(
                document.model_dump_json(),
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
                ) VALUES ($1, $2, 'SCREENSHOT_OCR', $3, $4, $5, $6, $7)
                """,
                source_id,
                understanding_id,
                content_hash,
                encrypted_source,
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
            await conn.execute(
                """
                UPDATE trip_understanding_screenshot_batches
                SET status = 'CONSUMED', encrypted_source_document = NULL,
                    encryption_key_ref = NULL, consumed_at = $2,
                    consumed_understanding_id = $3, document_purged_at = $2,
                    updated_at = $2
                WHERE batch_id = $1
                """,
                batch["batch_id"],
                now,
                understanding_id,
            )
            await conn.execute(
                """
                INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                    receipt_id, batch_id, attempt_number, terminal_reason,
                    cleanup_status, attempted_at, created_at
                ) VALUES ($1, $2, 1, 'CONSUMED_SOURCE_MOVED',
                          'CIPHERTEXT_PURGED', $3, $3)
                """,
                str(uuid4()),
                batch["batch_id"],
                now,
            )
            accepted = _accepted(public_resource_id)
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
                json.dumps(accepted.model_dump(mode="json"), ensure_ascii=False),
                now,
            )
        return CreateOutcome(accepted=accepted)

    async def purge_expired_private_data(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> dict[str, int]:
        if not 1 <= limit <= 1000:
            raise ValueError("maintenance limit must be between 1 and 1000")
        source_count = 0
        batch_count = 0
        pool = await self._get_pool()
        async with pool.acquire() as conn, conn.transaction():
            candidates = await conn.fetch(
                """
                SELECT private_kind, private_id
                FROM (
                    SELECT 'BATCH'::text AS private_kind,
                           batch_id AS private_id,
                           expires_at AS purge_due_at
                    FROM trip_understanding_screenshot_batches
                    WHERE expires_at <= $1 AND document_purged_at IS NULL
                      AND (
                          encrypted_source_document IS NOT NULL
                          OR status = 'PROCESSING'
                      )
                    UNION ALL
                    SELECT 'SOURCE'::text AS private_kind,
                           source_id AS private_id,
                           retention_until AS purge_due_at
                    FROM trip_understanding_sources
                    WHERE retention_until <= $1 AND encrypted_content IS NOT NULL
                ) AS expired_private_data
                ORDER BY purge_due_at, private_kind, private_id
                LIMIT $2
                """,
                now,
                limit,
            )
            for candidate in candidates:
                if candidate["private_kind"] == "BATCH":
                    row = await conn.fetchrow(
                        """
                        SELECT batch_id, status
                        FROM trip_understanding_screenshot_batches
                        WHERE batch_id = $1 AND expires_at <= $2
                          AND document_purged_at IS NULL
                          AND (
                              encrypted_source_document IS NOT NULL
                              OR status = 'PROCESSING'
                          )
                        FOR UPDATE SKIP LOCKED
                        """,
                        candidate["private_id"],
                        now,
                    )
                    if row is None:
                        continue
                    asset_state = await conn.fetchrow(
                        """
                        SELECT COUNT(*) AS asset_count,
                               COUNT(*) FILTER (
                                   WHERE cleanup_status = 'CLEANED'
                               ) AS cleaned_count
                        FROM trip_understanding_screenshot_assets
                        WHERE batch_id = $1
                        """,
                        row["batch_id"],
                    )
                    cleanup_confirmed = (
                        int(asset_state["asset_count"]) > 0
                        and int(asset_state["cleaned_count"])
                        == int(asset_state["asset_count"])
                    )
                    privacy_blocked = (
                        row["status"] == "PROCESSING" and not cleanup_confirmed
                    )
                    await conn.execute(
                        """
                        UPDATE trip_understanding_screenshot_batches
                        SET status = $2, encrypted_source_document = NULL,
                            encryption_key_ref = NULL, document_purged_at = $4,
                            last_error_category = $3, updated_at = $4
                        WHERE batch_id = $1
                        """,
                        row["batch_id"],
                        "PRIVACY_BLOCKED" if privacy_blocked else "EXPIRED",
                        (
                            "SCREENSHOT_CLEANUP_NOT_CONFIRMED_AT_TTL"
                            if privacy_blocked
                            else None
                        ),
                        now,
                    )
                    await conn.execute(
                        """
                        INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                            receipt_id, batch_id, attempt_number, terminal_reason,
                            cleanup_status, error_category, attempted_at, created_at
                        ) VALUES ($1, $2, 1, 'BATCH_TTL_EXPIRED',
                                  $3, $4, $5, $5)
                        """,
                        str(uuid4()),
                        row["batch_id"],
                        "DELETE_FAILED" if privacy_blocked else "CIPHERTEXT_PURGED",
                        (
                            "SCREENSHOT_CLEANUP_NOT_CONFIRMED_AT_TTL"
                            if privacy_blocked
                            else None
                        ),
                        now,
                    )
                    batch_count += 1
                    continue

                row = await conn.fetchrow(
                    """
                    SELECT source_id
                    FROM trip_understanding_sources
                    WHERE source_id = $1 AND retention_until <= $2
                      AND encrypted_content IS NOT NULL
                    FOR UPDATE SKIP LOCKED
                    """,
                    candidate["private_id"],
                    now,
                )
                if row is None:
                    continue
                await conn.execute(
                    "DELETE FROM trip_understanding_source_claims WHERE source_id = $1",
                    row["source_id"],
                )
                receipt_hash = canonical_sha256(
                    {
                        "source_id": row["source_id"],
                        "reason": "SOURCE_TTL_EXPIRED",
                        "purged_at": now.isoformat(),
                    }
                )
                await conn.execute(
                    """
                    UPDATE trip_understanding_sources
                    SET encrypted_content = NULL, encryption_key_ref = NULL,
                        deleted_at = $2, deletion_receipt_hash = $3
                    WHERE source_id = $1
                    """,
                    row["source_id"],
                    now,
                    receipt_hash,
                )
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                        receipt_id, source_id, attempt_number, terminal_reason,
                        cleanup_status, attempted_at, created_at
                    ) VALUES ($1, $2, 1, 'SOURCE_TTL_EXPIRED',
                              'CIPHERTEXT_PURGED', $3, $3)
                    """,
                    str(uuid4()),
                    row["source_id"],
                    now,
                )
                source_count += 1
        return {"sources_purged": source_count, "batches_purged": batch_count}

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
            stay = await self._project_stay_view(
                conn,
                resource.understanding_id,
                int(row["revision"]),
            )
        return StoredResult(
            result=result.model_copy(update={"map": readiness, "stay": stay}),
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
            aggregate = await conn.fetchrow(
                "SELECT * FROM trip_understandings WHERE understanding_id = $1 FOR UPDATE",
                resource.understanding_id,
            )
            if aggregate is None:
                raise ResourceNotFoundError("trip resource does not exist")
            if aggregate["state"] == "DELETED":
                raise ResourceGoneError("trip resource is no longer available")
            if aggregate["public_resource_id"] != resource.public_resource_id:
                raise ResourceAccessDeniedError("trip resource binding changed")
            if aggregate["current_result_id"] is None:
                raise ResourceNotReadyError("trip cards are not ready for editing")
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
            await self._copy_stay_selection_to_revision(
                conn,
                resource.understanding_id,
                parent_revision,
                result_revision,
                now=now,
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
            screenshot_batches = await conn.fetch(
                """
                SELECT batch_id, status
                FROM trip_understanding_screenshot_batches
                WHERE owner_user_id = $1
                  AND consumed_understanding_id IS NULL
                FOR UPDATE
                """,
                user_id,
            )
            batch_ids = [str(row["batch_id"]) for row in screenshot_batches]
            if batch_ids:
                cleanup_rows = await conn.fetch(
                    """
                    SELECT batch_id,
                           COUNT(*) AS asset_count,
                           COUNT(*) FILTER (
                               WHERE cleanup_status = 'CLEANED'
                           ) AS cleaned_count
                    FROM trip_understanding_screenshot_assets
                    WHERE batch_id = ANY($1::text[])
                    GROUP BY batch_id
                    """,
                    batch_ids,
                )
                cleanup_by_batch = {
                    str(row["batch_id"]): (
                        int(row["asset_count"]),
                        int(row["cleaned_count"]),
                    )
                    for row in cleanup_rows
                }
                pending_batch_ids = [
                    batch_id
                    for batch_id in batch_ids
                    if cleanup_by_batch.get(batch_id, (0, 0))[0] == 0
                    or cleanup_by_batch[batch_id][0]
                    != cleanup_by_batch[batch_id][1]
                ]
                if pending_batch_ids:
                    view = TravelDataDeletionStatusView(
                        status="RETRY_REQUIRED",
                        message="部分截图仍在确认清理，请稍后重试",
                        next_action="RETRY",
                    )
                    await conn.execute(
                        """
                        DELETE FROM trip_understanding_deletion_jobs
                        WHERE owner_user_id = $1
                          AND scope = 'ACCOUNT_TRAVEL_DATA'
                          AND status <> 'COMPLETED'
                        """,
                        user_id,
                    )
                    await conn.execute(
                        """
                        INSERT INTO trip_understanding_deletion_jobs (
                            deletion_job_id, scope, owner_user_id, status,
                            request_hash, created_at, updated_at
                        ) VALUES (
                            $1, 'ACCOUNT_TRAVEL_DATA', $2, 'RETRY_REQUIRED',
                            $3, $4, $4
                        )
                        """,
                        str(uuid4()),
                        user_id,
                        request_hash,
                        now,
                    )
                    retry_receipt_hash = canonical_sha256(
                        {
                            "scope": "ACCOUNT_TRAVEL_DATA",
                            "status": "RETRY_REQUIRED",
                            "pending_screenshot_batch_count": len(
                                pending_batch_ids
                            ),
                        }
                    )
                    await conn.execute(
                        """
                        UPDATE trip_understanding_idempotency_records
                        SET state = 'COMPLETED', response_status = 202,
                            response_json = $3::jsonb,
                            response_headers_json = $4::jsonb,
                            lease_until = NULL, completed_at = $5
                        WHERE scope = $1 AND key_hash = $2
                        """,
                        scope,
                        key_hash,
                        json.dumps(view.model_dump(mode="json"), ensure_ascii=False),
                        json.dumps(
                            {"retry_receipt_hash": retry_receipt_hash},
                            ensure_ascii=False,
                        ),
                        now,
                    )
                    return TravelDataDeletionOutcome(view=view)
            rows = await conn.fetch(
                """
                SELECT understanding_id, public_resource_id
                FROM trip_understandings WHERE owner_user_id = $1
                FOR UPDATE
                """,
                user_id,
            )
            for batch in screenshot_batches:
                await conn.execute(
                    """
                    UPDATE trip_understanding_screenshot_batches
                    SET status = 'EXPIRED', encrypted_source_document = NULL,
                        encryption_key_ref = NULL, document_purged_at = $2,
                        updated_at = $2, last_error_category = 'ACCOUNT_DATA_DELETED'
                    WHERE batch_id = $1
                    """,
                    batch["batch_id"],
                    now,
                )
                await conn.execute(
                    """
                    INSERT INTO trip_understanding_screenshot_cleanup_receipts (
                        receipt_id, batch_id, attempt_number, terminal_reason,
                        cleanup_status, attempted_at, created_at
                    ) VALUES ($1, $2, 1, 'ACCOUNT_DATA_DELETED',
                              'CIPHERTEXT_PURGED', $3, $3)
                    """,
                    str(uuid4()),
                    batch["batch_id"],
                    now,
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
                    "purged_screenshot_batch_count": len(screenshot_batches),
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
        if row["source_type"] not in {"TEXT", "SCREENSHOT_OCR"} or row["encrypted_content"] is None:
            raise SourceUnavailableError("recoverable source is unavailable")
        cipher = self._get_source_cipher()
        if row["encryption_key_ref"] != cipher.key_ref:
            raise SourceUnavailableError("source encryption key is unavailable")
        text = cipher.decrypt(
            bytes(row["encrypted_content"]),
            source_id=row["source_id"],
            content_hash=content_hash,
        )
        if row["source_type"] == "TEXT":
            if _sha256_text(text) != content_hash:
                raise ValueError("decrypted source hash mismatch")
            return TripUnderstandingSourcePayload(source_type="TEXT", text=text)
        document = ScreenshotSourceDocumentV1.model_validate_json(text)
        if _sha256_text(document.semantic_text) != content_hash:
            raise ValueError("decrypted screenshot semantic hash mismatch")
        return TripUnderstandingSourcePayload(
            source_type="SCREENSHOT_OCR",
            text=document.semantic_text,
            requires_confirmation_spans=tuple(
                ConfirmationSourceSpan(
                    start=line.semantic_span.start,
                    end=line.semantic_span.end,
                )
                for line in document.lines
                if line.requires_confirmation
            ),
            partial_source=document.partial,
        )

    async def renew_lease(
        self,
        job: TripUnderstandingJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            renewed = await conn.fetchval(
                """
                UPDATE trip_understanding_jobs
                SET lease_until = $4, updated_at = $3
                WHERE job_id = $1 AND status = 'RUNNING'
                  AND lease_owner = $2 AND attempt = $5 AND lease_until > $3
                RETURNING lease_until
                """,
                job.job_id,
                job.lease_owner,
                now,
                now + timedelta(seconds=lease_seconds),
                job.attempt,
            )
        return renewed is not None

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
        inference_external_calls = sum(
            int(value)
            for key, value in output.inference_binding.items()
            if key in {"external_calls", "primary_external_call_count"}
            and isinstance(value, int)
        )
        place_external_calls = sum(
            int(item.resolver_receipt.get("external_calls", 0))
            for item in output.activities
            if isinstance(item.resolver_receipt.get("external_calls", 0), int)
        )
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
                SELECT s.source_id, s.source_type, s.content_hash,
                       s.encrypted_content, s.deleted_at, s.retention_until
                FROM trip_understanding_revisions r
                JOIN trip_understanding_sources s ON s.source_id = r.source_id
                WHERE r.understanding_id = $1 AND r.revision = $2
                FOR UPDATE OF s
                """,
                job.understanding_id,
                job.revision,
            )
            if source_row is None:
                raise ResourceNotFoundError("understanding source does not exist")
            source_id = source_row["source_id"]
            source_type = source_row["source_type"]
            source_hash = source_row["content_hash"].strip()
            if (
                source_row["deleted_at"] is not None
                or source_row["retention_until"] <= now
                or (
                    source_type in {"TEXT", "SCREENSHOT_OCR"}
                    and source_row["encrypted_content"] is None
                )
            ):
                raise SourceUnavailableError("understanding source is unavailable")
            if source_hash != job.input_hash:
                raise SourceUnavailableError("understanding source binding differs")
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
                json.dumps(
                    {
                        **output.compiler_receipt,
                        "place_resolution": output.resolution_receipt,
                    },
                    ensure_ascii=False,
                ),
                now,
            )
            for activity in output.activities:
                mention = activity.compiled.mention
                place = activity.place
                resolver_receipt = activity.resolver_receipt or (
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
                if source_type in {"TEXT", "SCREENSHOT_OCR"}:
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
                        "place_resolution": output.resolution_receipt,
                        "inference_external_calls": inference_external_calls,
                        "place_external_calls": place_external_calls,
                        "external_calls": inference_external_calls + place_external_calls,
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
            await self._enqueue_initial_stay_job(
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


class InMemoryTripUnderstandingRepository(
    InMemoryG03RepositoryMixin,
    InMemoryStayRecommendationRepositoryMixin,
    InMemoryMapRenderRepositoryMixin,
):
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
        self.source_expiries: dict[str, datetime] = {}
        self.screenshot_batches: dict[tuple[str, str], dict[str, Any]] = {}
        self.screenshot_upload_idempotency: dict[
            tuple[str, str], tuple[str, str]
        ] = {}
        self.screenshot_cleanup_receipts: list[dict[str, Any]] = []
        self.command_idempotency: dict[tuple[str, str], tuple[str, CommandOutcome]] = {}
        self.claim_idempotency: dict[tuple[str, str, str], tuple[str, ClaimOutcome]] = {}
        self.privacy_idempotency: dict[tuple[str, str], str] = {}
        self.trip_deletion_idempotency: dict[
            tuple[str, str], tuple[str, str, str]
        ] = {}
        self.tombstones: dict[str, dict[str, str | None]] = {}
        self.account_deletion_status: dict[str, TravelDataDeletionStatusView] = {}
        self._geometry_cache = InMemoryRouteGeometryCache()
        self._init_map_store()
        self._init_stay_store()
        self._init_g03_store()

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
        self.source_expiries[job_id] = session["expires_at"]
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
        self.source_expiries[job_id] = now + timedelta(days=retention_days)
        self.events[understanding_id] = [
            PublicEventRecord(
                event_id=1,
                event_type="progress",
                payload=PublicEventPayload(status="PROCESSING", message="正在整理每天行程"),
            )
        ]
        return CreateOutcome(accepted=accepted)

    async def preflight_screenshot_batch(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        batch_ref: str,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome | None:
        del now
        key = (owner_user_id, _sha256_text(idempotency_key))
        existing = self.screenshot_upload_idempotency.get(key)
        if existing is None:
            return None
        ref_hash = _sha256_text(batch_ref)
        if existing[1] != ref_hash:
            raise IdempotencyConflictError(
                "screenshot upload idempotency key binding is invalid"
            )
        row = self.screenshot_batches[(owner_user_id, ref_hash)]
        if row["status"] in {"READY", "PARTIAL"}:
            return ScreenshotBatchCreateOutcome(
                accepted=_screenshot_accepted(
                    batch_ref,
                    expires_at=row["expires_at"],
                    outcome=row["outcome"],
                ),
                replayed=True,
            )
        if row["status"] == "PROCESSING":
            raise IdempotencyInProgressError(
                "matching screenshot upload is still in progress"
            )
        raise IdempotencyConflictError(
            "screenshot upload idempotency key belongs to a terminal request"
        )

    async def claim_screenshot_batch(
        self,
        payload: ScreenshotBatchClaimInput,
        *,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome | None:
        key = (payload.owner_user_id, _sha256_text(payload.idempotency_key))
        ref_hash = _sha256_text(payload.batch_ref)
        existing = self.screenshot_upload_idempotency.get(key)
        if existing is not None:
            if existing != (payload.request_hash, ref_hash):
                raise IdempotencyConflictError(
                    "screenshot upload idempotency key was reused"
                )
            row = self.screenshot_batches[(payload.owner_user_id, ref_hash)]
            if row["status"] in {"READY", "PARTIAL"}:
                return ScreenshotBatchCreateOutcome(
                    accepted=_screenshot_accepted(
                        payload.batch_ref,
                        expires_at=row["expires_at"],
                        outcome=row["outcome"],
                    ),
                    replayed=True,
                )
            if row["status"] == "PROCESSING":
                raise IdempotencyInProgressError(
                    "matching screenshot upload is still in progress"
                )
            raise IdempotencyConflictError(
                "screenshot upload idempotency key belongs to a terminal request"
            )
        self.screenshot_upload_idempotency[key] = (payload.request_hash, ref_hash)
        self.screenshot_batches[(payload.owner_user_id, ref_hash)] = {
            "batch_id": str(uuid4()),
            "status": "PROCESSING",
            "outcome": None,
            "source_document_json": None,
            "source_document_hash": None,
            "semantic_text_hash": None,
            "expires_at": payload.expires_at,
            "consumed_understanding_id": None,
            "assets": payload.assets,
            "asset_cleanup_statuses": {
                asset.upload_position: "PENDING" for asset in payload.assets
            },
            "recorded_at": now,
        }
        return None

    async def record_screenshot_cleanup(
        self,
        payload: ScreenshotCleanupPersistenceInput,
        *,
        now: datetime,
    ) -> None:
        key = (payload.owner_user_id, _sha256_text(payload.idempotency_key))
        binding = self.screenshot_upload_idempotency.get(key)
        if binding is None:
            return
        row = self.screenshot_batches[(payload.owner_user_id, binding[1])]
        submitted_bindings = {
            asset.upload_position: (asset.content_hash, asset.storage_locator)
            for asset in payload.assets
        }
        stored_bindings = {
            asset.upload_position: (asset.content_hash, asset.storage_locator)
            for asset in row["assets"]
        }
        for receipt in payload.cleanup_receipts:
            value = receipt.model_dump(mode="python")
            value["batch_id"] = row["batch_id"]
            position = receipt.upload_position
            value["asset_content_hash"] = (
                submitted_bindings.get(position, (None, None))[0]
                if position is not None
                else None
            )
            value["asset_matched"] = (
                position is not None
                and stored_bindings.get(position) == submitted_bindings.get(position)
            )
            self.screenshot_cleanup_receipts.append(value)
        for position, final_status in _latest_cleanup_statuses(
            payload.cleanup_receipts
        ).items():
            if stored_bindings.get(position) != submitted_bindings.get(position):
                continue
            row["asset_cleanup_statuses"][position] = (
                "CLEANED"
                if final_status in {"DELETED", "ALREADY_ABSENT"}
                else "CLEANUP_FAILED"
            )
        if payload.privacy_blocked and row["status"] != "CONSUMED":
            row.update(
                {
                    "status": "PRIVACY_BLOCKED",
                    "source_document_json": None,
                    "source_document_hash": None,
                    "semantic_text_hash": None,
                    "last_error_category": "SCREENSHOT_CLEANUP_FAILED",
                    "document_purged_at": now,
                }
            )

    async def reconcile_local_screenshot_recovery(
        self,
        report: LocalScreenshotRecoveryReport,
        *,
        now: datetime,
    ) -> dict[str, int]:
        locator_bindings: dict[str, tuple[dict[str, Any], Any]] = {}
        for row in self.screenshot_batches.values():
            for asset in row.get("assets", ()):
                locator_bindings[asset.storage_locator] = (row, asset)

        observed_locators = {
            locator
            for recovered in report.batches
            for locator in recovered.asset_locators
        }
        matched_locators: set[str] = set()
        receipts_recorded = 0
        orphan_receipts = 0
        known_event_hashes = {
            receipt.get("recovery_event_hash")
            for receipt in self.screenshot_cleanup_receipts
            if receipt.get("recovery_event_hash")
        }
        affected_rows: dict[str, dict[str, Any]] = {}
        cleanup_failed_rows: set[str] = set()
        for recovered in report.batches:
            batch_locator_hash = _sha256_text(recovered.batch_locator)
            for attempt in recovered.attempts:
                attempt_locators = recovered.asset_locators or (None,)
                for locator in attempt_locators:
                    binding = locator_bindings.get(locator)
                    cleanup_status = (
                        _local_recovery_status(attempt, locator)
                        if locator is not None
                        else "DELETED"
                        if attempt.succeeded
                        else "DELETE_FAILED"
                    )
                    asset_locator_hash = (
                        _sha256_text(locator) if locator is not None else None
                    )
                    event_hash = canonical_sha256(
                        {
                            "batch_locator_hash": batch_locator_hash,
                            "asset_locator_hash": asset_locator_hash,
                            "attempt_number": attempt.attempt_number,
                            "terminal_reason": attempt.terminal_reason,
                            "cleanup_status": cleanup_status,
                            "error_categories": list(attempt.error_categories),
                            "attempted_at": attempt.attempted_at.isoformat(),
                        }
                    )
                    if event_hash not in known_event_hashes:
                        row, asset = binding if binding is not None else (None, None)
                        self.screenshot_cleanup_receipts.append(
                            {
                                "batch_id": row["batch_id"] if row else None,
                                "upload_position": (
                                    asset.upload_position if asset else None
                                ),
                                "asset_content_hash": (
                                    asset.content_hash if asset else None
                                ),
                                "orphan_batch_locator_hash": (
                                    None if row else batch_locator_hash
                                ),
                                "orphan_asset_locator_hash": (
                                    None if row else asset_locator_hash
                                ),
                                "recovery_event_hash": event_hash,
                                "attempt_number": attempt.attempt_number,
                                "terminal_reason": attempt.terminal_reason,
                                "cleanup_status": cleanup_status,
                                "error_category": (
                                    attempt.error_categories[0]
                                    if attempt.error_categories
                                    else None
                                ),
                                "attempted_at": attempt.attempted_at,
                                "created_at": now,
                            }
                        )
                        known_event_hashes.add(event_hash)
                        receipts_recorded += 1
                        if binding is None:
                            orphan_receipts += 1
                    if binding is None:
                        continue
                    row, asset = binding
                    matched_locators.add(locator)
                    row["asset_cleanup_statuses"][asset.upload_position] = (
                        "CLEANED"
                        if cleanup_status in {"DELETED", "ALREADY_ABSENT"}
                        else "CLEANUP_FAILED"
                    )
                    affected_rows[row["batch_id"]] = row
                    if not attempt.succeeded or not attempt.directory_removed:
                        cleanup_failed_rows.add(row["batch_id"])

        for issue in report.issues:
            batch_locator_hash = _sha256_text(issue.batch_locator)
            event_hash = canonical_sha256(
                {
                    "batch_locator_hash": batch_locator_hash,
                    "asset_locator_hash": None,
                    "attempt_number": 1,
                    "cleanup_status": "DELETE_FAILED",
                    "error_categories": [issue.category],
                    "attempted_at": issue.observed_at.isoformat(),
                }
            )
            if event_hash in known_event_hashes:
                continue
            self.screenshot_cleanup_receipts.append(
                {
                    "batch_id": None,
                    "upload_position": None,
                    "asset_content_hash": None,
                    "orphan_batch_locator_hash": batch_locator_hash,
                    "orphan_asset_locator_hash": None,
                    "recovery_event_hash": event_hash,
                    "attempt_number": 1,
                    "terminal_reason": "CRASH_RECOVERY",
                    "cleanup_status": "DELETE_FAILED",
                    "error_category": issue.category,
                    "attempted_at": issue.observed_at,
                    "created_at": now,
                }
            )
            known_event_hashes.add(event_hash)
            receipts_recorded += 1
            orphan_receipts += 1

        batches_finalized = 0
        for row in affected_rows.values():
            if row["status"] == "CONSUMED":
                continue
            cleanup_states = tuple(row["asset_cleanup_statuses"].values())
            failed = (
                "CLEANUP_FAILED" in cleanup_states
                or row["batch_id"] in cleanup_failed_rows
            )
            all_cleaned = cleanup_states.count("CLEANED") == len(cleanup_states)
            if failed:
                final_status = "PRIVACY_BLOCKED"
                error_category = "SCREENSHOT_CLEANUP_FAILED"
            elif row["status"] == "PROCESSING" and all_cleaned:
                final_status = "FAILED"
                error_category = "CRASH_RECOVERED_NO_RESULT"
            else:
                continue
            row.update(
                {
                    "status": final_status,
                    "source_document_json": None,
                    "source_document_hash": None,
                    "semantic_text_hash": None,
                    "document_purged_at": now,
                    "last_error_category": error_category,
                }
            )
            batches_finalized += 1
        return {
            "matched_assets": len(matched_locators),
            "receipts_recorded": receipts_recorded,
            "orphan_receipts": orphan_receipts,
            "batches_finalized": batches_finalized,
            "unmatched_assets": len(observed_locators - matched_locators),
            "local_issues": len(report.issues),
        }

    async def store_screenshot_batch(
        self,
        payload: ScreenshotBatchPersistenceInput,
        *,
        now: datetime,
    ) -> ScreenshotBatchCreateOutcome:
        document = ScreenshotSourceDocumentV1.model_validate_json(
            payload.source_document_json
        )
        if document.document_hash != payload.source_document_hash:
            raise ValueError("screenshot source document hash binding differs")
        if _sha256_text(document.semantic_text) != payload.semantic_text_hash:
            raise ValueError("screenshot semantic text hash binding differs")
        if len(payload.assets) != len(document.images):
            raise ValueError("screenshot asset count differs from source document")
        _require_document_asset_binding(document, payload.assets)
        _require_confirmed_asset_cleanup(payload.assets, payload.cleanup_receipts)
        key = (payload.owner_user_id, _sha256_text(payload.idempotency_key))
        ref_hash = _sha256_text(payload.batch_ref)
        existing = self.screenshot_upload_idempotency.get(key)
        if existing is not None:
            if existing != (payload.request_hash, ref_hash):
                raise IdempotencyConflictError(
                    "screenshot upload idempotency key was reused"
                )
            row = self.screenshot_batches[(payload.owner_user_id, ref_hash)]
            if row["status"] in {"READY", "PARTIAL"}:
                return ScreenshotBatchCreateOutcome(
                    accepted=_screenshot_accepted(
                        payload.batch_ref,
                        expires_at=row["expires_at"],
                        outcome=row["outcome"],
                    ),
                    replayed=True,
                )
            if row["status"] != "PROCESSING":
                raise IdempotencyConflictError(
                    "screenshot upload idempotency key belongs to a terminal request"
                )
            if [
                (
                    item.upload_position,
                    item.content_hash,
                    item.media_type,
                    item.byte_size,
                    item.storage_locator,
                )
                for item in row["assets"]
            ] != [
                (
                    item.upload_position,
                    item.content_hash,
                    item.media_type,
                    item.byte_size,
                    item.storage_locator,
                )
                for item in payload.assets
            ]:
                raise IdempotencyConflictError(
                    "claimed screenshot assets no longer match the request"
                )
        else:
            self.screenshot_upload_idempotency[key] = (payload.request_hash, ref_hash)
            row = {
                "batch_id": str(uuid4()),
                "consumed_understanding_id": None,
            }
            self.screenshot_batches[(payload.owner_user_id, ref_hash)] = row
        row.update(
            {
                "status": "READY" if payload.outcome == "COMPLETE" else "PARTIAL",
                "outcome": payload.outcome,
                "source_document_json": payload.source_document_json,
                "source_document_hash": payload.source_document_hash,
                "semantic_text_hash": payload.semantic_text_hash,
                "expires_at": payload.expires_at,
                "assets": payload.assets,
                "asset_cleanup_statuses": {
                    asset.upload_position: "CLEANED" for asset in payload.assets
                },
                "recorded_at": now,
            }
        )
        for receipt in payload.cleanup_receipts:
            value = receipt.model_dump(mode="python")
            value["batch_id"] = row["batch_id"]
            value["asset_content_hash"] = next(
                (
                    asset.content_hash
                    for asset in payload.assets
                    if asset.upload_position == receipt.upload_position
                ),
                None,
            )
            self.screenshot_cleanup_receipts.append(value)
        return ScreenshotBatchCreateOutcome(
            accepted=_screenshot_accepted(
                payload.batch_ref,
                expires_at=payload.expires_at,
                outcome=payload.outcome,
            )
        )

    async def store_screenshot_batch_failure(
        self,
        payload: ScreenshotBatchFailurePersistenceInput,
        *,
        now: datetime,
    ) -> None:
        key = (payload.owner_user_id, _sha256_text(payload.idempotency_key))
        ref_hash = _sha256_text(payload.batch_ref)
        existing = self.screenshot_upload_idempotency.get(key)
        if existing is not None:
            if existing != (payload.request_hash, ref_hash):
                raise IdempotencyConflictError(
                    "screenshot upload idempotency key was reused"
                )
            row = self.screenshot_batches[(payload.owner_user_id, ref_hash)]
            if row["status"] != "PROCESSING":
                return
        else:
            self.screenshot_upload_idempotency[key] = (payload.request_hash, ref_hash)
            row = {
                "batch_id": str(uuid4()),
                "consumed_understanding_id": None,
            }
            self.screenshot_batches[(payload.owner_user_id, ref_hash)] = row
        final_cleanup = _latest_cleanup_statuses(payload.cleanup_receipts)
        row.update(
            {
                "status": payload.status,
                "outcome": None,
                "source_document_json": None,
                "source_document_hash": None,
                "semantic_text_hash": None,
                "expires_at": payload.expires_at,
                "assets": payload.assets,
                "asset_cleanup_statuses": {
                    asset.upload_position: (
                        "CLEANED"
                        if final_cleanup.get(asset.upload_position)
                        in {"DELETED", "ALREADY_ABSENT"}
                        else "CLEANUP_FAILED"
                        if final_cleanup.get(asset.upload_position) == "DELETE_FAILED"
                        else "PENDING"
                    )
                    for asset in payload.assets
                },
                "last_error_category": payload.last_error_category,
                "recorded_at": now,
            }
        )
        for receipt in payload.cleanup_receipts:
            value = receipt.model_dump(mode="python")
            value["batch_id"] = row["batch_id"]
            self.screenshot_cleanup_receipts.append(value)

    async def create_full_from_screenshot(
        self,
        *,
        owner_user_id: str,
        batch_ref: str,
        idempotency_key: str,
        request_hash: str,
        now: datetime,
        retention_days: int,
    ) -> CreateOutcome:
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
            raise ConcurrentJobLimitError(
                "user already has two understanding jobs in progress"
            )
        ref_hash = _sha256_text(batch_ref)
        row = self.screenshot_batches.get((owner_user_id, ref_hash))
        if row is None:
            raise ScreenshotBatchNotFoundError("screenshot batch does not exist")
        if row["status"] == "EXPIRED" or (
            row["expires_at"] <= now and row["status"] in {"READY", "PARTIAL"}
        ):
            raise ScreenshotBatchExpiredError("screenshot batch has expired")
        if row["status"] == "CONSUMED":
            raise ScreenshotBatchAlreadyUsedError("screenshot batch was already consumed")
        if row["status"] == "PROCESSING":
            raise ScreenshotBatchNotReadyError("screenshot batch is not ready")
        if row["status"] not in {"READY", "PARTIAL"}:
            raise ScreenshotBatchUnusableError("screenshot batch cannot be consumed")
        document_json = row["source_document_json"]
        if not document_json:
            raise ScreenshotBatchUnusableError("screenshot source document is unavailable")
        document = ScreenshotSourceDocumentV1.model_validate_json(document_json)
        content_hash = _sha256_text(document.semantic_text)
        if (
            document.document_hash != row["source_document_hash"]
            or content_hash != row["semantic_text_hash"]
        ):
            raise ValueError("screenshot source document integrity check failed")

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
            "input_hash": content_hash,
            "available_at": now,
        }
        self.sources[job_id] = TripUnderstandingSourcePayload(
            source_type="SCREENSHOT_OCR",
            text=document.semantic_text,
            requires_confirmation_spans=tuple(
                ConfirmationSourceSpan(
                    start=line.semantic_span.start,
                    end=line.semantic_span.end,
                )
                for line in document.lines
                if line.requires_confirmation
            ),
            partial_source=document.partial,
        )
        self.source_expiries[job_id] = now + timedelta(days=retention_days)
        self.events[understanding_id] = [
            PublicEventRecord(
                event_id=1,
                event_type="progress",
                payload=PublicEventPayload(
                    status="PROCESSING", message="正在整理每天行程"
                ),
            )
        ]
        row.update(
            {
                "status": "CONSUMED",
                "source_document_json": None,
                "consumed_understanding_id": understanding_id,
            }
        )
        self.screenshot_cleanup_receipts.append(
            {
                "attempt_number": 1,
                "terminal_reason": "CONSUMED_SOURCE_MOVED",
                "cleanup_status": "CIPHERTEXT_PURGED",
                "attempted_at": now,
            }
        )
        return CreateOutcome(accepted=accepted)

    async def purge_expired_private_data(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> dict[str, int]:
        if not 1 <= limit <= 1000:
            raise ValueError("maintenance limit must be between 1 and 1000")
        batch_count = 0
        source_count = 0
        candidates: list[tuple[datetime, str, Any]] = []
        candidates.extend(
            (row["expires_at"], "BATCH", key)
            for key, row in self.screenshot_batches.items()
            if row["expires_at"] <= now
            and (
                row.get("source_document_json") is not None
                or row.get("status") == "PROCESSING"
            )
            and row.get("document_purged_at") is None
        )
        candidates.extend(
            (expiry, "SOURCE", job_id)
            for job_id, expiry in self.source_expiries.items()
            if expiry <= now and job_id in self.sources
        )
        candidates.sort(key=lambda item: (item[0], item[1], str(item[2])))
        for _due_at, private_kind, private_id in candidates[:limit]:
            if private_kind == "BATCH":
                row = self.screenshot_batches[private_id]
                cleanup_states = tuple(row.get("asset_cleanup_statuses", {}).values())
                cleanup_confirmed = bool(cleanup_states) and all(
                    state == "CLEANED" for state in cleanup_states
                )
                privacy_blocked = (
                    row.get("status") == "PROCESSING" and not cleanup_confirmed
                )
                error_category = (
                    "SCREENSHOT_CLEANUP_NOT_CONFIRMED_AT_TTL"
                    if privacy_blocked
                    else None
                )
                row.update(
                    {
                        "status": "PRIVACY_BLOCKED" if privacy_blocked else "EXPIRED",
                        "source_document_json": None,
                        "source_document_hash": None,
                        "semantic_text_hash": None,
                        "document_purged_at": now,
                        "last_error_category": error_category,
                    }
                )
                batch_count += 1
                self.screenshot_cleanup_receipts.append(
                    {
                        "batch_id": row.get("batch_id"),
                        "attempt_number": 1,
                        "terminal_reason": "BATCH_TTL_EXPIRED",
                        "cleanup_status": (
                            "DELETE_FAILED"
                            if privacy_blocked
                            else "CIPHERTEXT_PURGED"
                        ),
                        "error_category": error_category,
                        "attempted_at": now,
                    }
                )
                continue
            self.sources.pop(private_id, None)
            source_count += 1
            self.screenshot_cleanup_receipts.append(
                {
                    "source_job_id": private_id,
                    "attempt_number": 1,
                    "terminal_reason": "SOURCE_TTL_EXPIRED",
                    "cleanup_status": "CIPHERTEXT_PURGED",
                    "attempted_at": now,
                }
            )
        return {
            "sources_purged": source_count,
            "batches_purged": batch_count,
        }

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
        if public_id != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        aggregate = self.resources[public_id]
        readiness = self._project_map_readiness_memory(
            resource.understanding_id,
            int(aggregate["current_revision"]),
        )
        stay = self._memory_stay_view(
            resource.understanding_id,
            int(aggregate["current_revision"]),
        )
        return stored.model_copy(
            update={"result": stored.result.model_copy(update={"map": readiness, "stay": stay})}
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
        public_id = self.resources_by_understanding[resource.understanding_id]
        if public_id != resource.public_resource_id:
            raise ResourceAccessDeniedError("trip resource binding changed")
        scope = f"understanding:{resource.understanding_id}:command"
        key = (scope, _sha256_text(idempotency_key))
        existing = self.command_idempotency.get(key)
        if existing:
            if existing[0] != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was already used with a different request"
                )
            return existing[1].model_copy(update={"replayed": True})
        aggregate = self.resources[public_id]
        stored = self.results.get(aggregate["current_result_id"] or "")
        if stored is None:
            raise ResourceNotReadyError("trip cards are not ready for editing")
        if not hmac.compare_digest(stored.opaque_etag, expected_etag):
            raise RevisionConflictError("command precondition does not match current result")
        source_revision = int(aggregate["current_revision"])
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
        previous_input = self.g03_pipeline_inputs.get(
            (resource.understanding_id, source_revision), {}
        )
        prior_assumptions = {
            str(item.get("key")): dict(item)
            for item in previous_input.get("assumptions", [])
            if isinstance(item, dict) and item.get("key")
        }
        self.g03_pipeline_inputs[
            (resource.understanding_id, int(aggregate["current_revision"]))
        ] = {
            "destination": dict(previous_input.get("destination") or {}),
            "assumptions": [
                {
                    **prior_assumptions.get(item.key, {}),
                    "key": item.key,
                    "value": item.value,
                    "source": (
                        "USER_EDIT"
                        if command.command_type == "ASSUMPTION_SET"
                        and item.key == command.key
                        else prior_assumptions.get(item.key, {}).get(
                            "source", "SOFT_ASSUMPTION"
                        )
                    ),
                }
                for item in mutation.result.assumptions
            ],
        }
        self._copy_stay_selection_memory(
            resource.understanding_id,
            source_revision,
            int(aggregate["current_revision"]),
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
        self._delete_stay_memory(resource.understanding_id)
        self._delete_g03_memory(resource.understanding_id)
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
        pending_screenshot_cleanup = False
        for (owner_user_id, _ref_hash), row in self.screenshot_batches.items():
            if owner_user_id != user_id or row.get("consumed_understanding_id") is not None:
                continue
            cleanup_statuses = row.get("asset_cleanup_statuses")
            if isinstance(cleanup_statuses, dict):
                if not cleanup_statuses or any(
                    status != "CLEANED" for status in cleanup_statuses.values()
                ):
                    pending_screenshot_cleanup = True
                    break
            elif row.get("status") in {"PROCESSING", "PRIVACY_BLOCKED"}:
                pending_screenshot_cleanup = True
                break
        if pending_screenshot_cleanup:
            view = TravelDataDeletionStatusView(
                status="RETRY_REQUIRED",
                message="部分截图仍在确认清理，请稍后重试",
                next_action="RETRY",
            )
            self.account_deletion_status[subject_hash] = view
            self.privacy_idempotency[key] = request_hash
            return TravelDataDeletionOutcome(view=view)
        for (owner_user_id, _ref_hash), row in self.screenshot_batches.items():
            if owner_user_id != user_id or row.get("consumed_understanding_id") is not None:
                continue
            row.update(
                {
                    "status": "EXPIRED",
                    "source_document_json": None,
                    "last_error_category": "ACCOUNT_DATA_DELETED",
                }
            )
            self.screenshot_cleanup_receipts.append(
                {
                    "attempt_number": 1,
                    "terminal_reason": "ACCOUNT_DATA_DELETED",
                    "cleanup_status": "CIPHERTEXT_PURGED",
                    "attempted_at": now,
                }
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
        source = self.sources.get(job.job_id)
        expiry = self.source_expiries.get(job.job_id)
        if (
            source is None
            or (expiry is not None and expiry <= now)
            or _sha256_text(source.text) != job.input_hash
        ):
            raise SourceUnavailableError("understanding source is unavailable")
        return source

    async def renew_lease(
        self,
        job: TripUnderstandingJobRecord,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> bool:
        item = self.jobs.get(job.job_id)
        if (
            item is None
            or item["status"] != "RUNNING"
            or item["lease_owner"] != job.lease_owner
            or item["attempt"] != job.attempt
            or item["lease_until"] is None
            or item["lease_until"] <= now
        ):
            return False
        item["lease_until"] = now + timedelta(seconds=lease_seconds)
        return True

    async def complete_job(
        self,
        job: TripUnderstandingJobRecord,
        output: PipelineOutput,
        *,
        now: datetime,
    ) -> bool:
        item = self.jobs[job.job_id]
        source = self.sources.get(job.job_id)
        source_expiry = self.source_expiries.get(job.job_id)
        if (
            source is None
            or source_expiry is None
            or source_expiry <= now
            or _sha256_text(source.text) != job.input_hash
            or output.source_hash != job.input_hash
        ):
            raise SourceUnavailableError("understanding source is unavailable")
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
        self.g03_pipeline_inputs[(job.understanding_id, 2)] = {
            "destination": dict(output.destination),
            "assumptions": [dict(item) for item in output.assumptions],
            "bindings": {
                activity.compiled.public_activity_token: {
                    "canonical_place_id": (
                        activity.place.canonical_place_id if activity.place else None
                    ),
                    "resolution_status": activity.resolution_status.value,
                    "resolver_receipt": dict(activity.resolver_receipt),
                }
                for activity in output.activities
                if activity.compiled.mention.role.value == "PLANNED"
            },
        }
        self._enqueue_initial_map_job_memory(
            job.understanding_id,
            2,
            now=now,
        )
        self._enqueue_initial_stay_job_memory(
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
