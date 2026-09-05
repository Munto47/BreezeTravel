from __future__ import annotations

from datetime import datetime, timezone

from app.trip_understanding.models import (
    ChangeAdoptOutcome,
    ChangePreviewOutcome,
    ClaimOutcome,
    CommandOutcome,
    CreateFullRequest,
    CreateOutcome,
    DeletionOutcome,
    MaterializationOutcome,
    PublicTripChecksView,
    PublicResourceRecord,
    ScreenshotBatchCreateOutcome,
    ScreenshotBatchClaimInput,
    ScreenshotBatchFailurePersistenceInput,
    ScreenshotBatchPersistenceInput,
    ScreenshotCleanupPersistenceInput,
    StaySelectionOutcome,
    TripUnderstandingCommand,
    TravelDataDeletionOutcome,
    TravelDataDeletionStatusView,
)
from app.trip_understanding.map_render import MapRenderRequestOutcome
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.repository import TripUnderstandingRepository


DEMO_CREATE_REQUEST_HASH = canonical_sha256({"mode": "DEMO"})


class TripUnderstandingApplicationService:
    def __init__(
        self,
        repository: TripUnderstandingRepository,
        *,
        ttl_hours: int = 24,
        full_retention_days: int = 30,
    ) -> None:
        self.repository = repository
        self.ttl_hours = ttl_hours
        self.full_retention_days = full_retention_days

    async def create_demo(
        self,
        *,
        capability_hash: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> CreateOutcome:
        return await self.repository.create_demo(
            capability_hash=capability_hash,
            idempotency_key=idempotency_key,
            request_hash=DEMO_CREATE_REQUEST_HASH,
            now=now or datetime.now(timezone.utc),
            ttl_hours=self.ttl_hours,
        )

    async def create_full(
        self,
        body: CreateFullRequest,
        *,
        owner_user_id: str | None,
        idempotency_key: str,
        now: datetime | None = None,
        capability_hash: str | None = None,
    ) -> CreateOutcome:
        request_hash = canonical_sha256(body.model_dump(mode="json"))
        if owner_user_id is None:
            if capability_hash is None:
                raise ValueError("anonymous capability is required")
            return await self.repository.create_demo(
                capability_hash=capability_hash,
                source_text=body.source.text,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                now=now or datetime.now(timezone.utc),
                ttl_hours=24,
            )
        return await self.repository.create_full(
            owner_user_id=owner_user_id,
            source_text=body.source.text,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
            retention_days=self.full_retention_days,
        )

    async def store_screenshot_batch(
        self,
        payload: ScreenshotBatchPersistenceInput,
        *,
        now: datetime | None = None,
    ) -> ScreenshotBatchCreateOutcome:
        return await self.repository.store_screenshot_batch(
            payload,
            now=now or datetime.now(timezone.utc),
        )

    async def claim_screenshot_batch(
        self,
        payload: ScreenshotBatchClaimInput,
        *,
        now: datetime | None = None,
    ) -> ScreenshotBatchCreateOutcome | None:
        return await self.repository.claim_screenshot_batch(
            payload,
            now=now or datetime.now(timezone.utc),
        )

    async def preflight_screenshot_batch(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        batch_ref: str,
        now: datetime | None = None,
    ) -> ScreenshotBatchCreateOutcome | None:
        return await self.repository.preflight_screenshot_batch(
            owner_user_id=owner_user_id,
            idempotency_key=idempotency_key,
            batch_ref=batch_ref,
            now=now or datetime.now(timezone.utc),
        )

    async def record_screenshot_cleanup(
        self,
        payload: ScreenshotCleanupPersistenceInput,
        *,
        now: datetime | None = None,
    ) -> None:
        await self.repository.record_screenshot_cleanup(
            payload,
            now=now or datetime.now(timezone.utc),
        )

    async def purge_expired_private_data(
        self,
        *,
        limit: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        return await self.repository.purge_expired_private_data(
            now=now or datetime.now(timezone.utc),
            limit=limit,
        )

    async def store_screenshot_batch_failure(
        self,
        payload: ScreenshotBatchFailurePersistenceInput,
        *,
        now: datetime | None = None,
    ) -> None:
        await self.repository.store_screenshot_batch_failure(
            payload,
            now=now or datetime.now(timezone.utc),
        )

    async def authorize(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None = None,
        now: datetime | None = None,
    ) -> PublicResourceRecord:
        return await self.repository.authorize(
            public_resource_id,
            capability_hash=capability_hash,
            user_id=user_id,
            now=now or datetime.now(timezone.utc),
        )

    async def apply_command(
        self,
        resource: PublicResourceRecord,
        command: TripUnderstandingCommand,
        *,
        expected_etag: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> CommandOutcome:
        request_hash = canonical_sha256(
            {
                "command": command.model_dump(mode="json"),
                "if_match": expected_etag,
            }
        )
        return await self.repository.apply_command(
            resource,
            command,
            expected_etag=expected_etag,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def request_map_render(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> MapRenderRequestOutcome:
        request_hash = canonical_sha256(
            {"action": "RENDER_MAP", "if_match": expected_etag}
        )
        return await self.repository.request_map_render(
            resource,
            expected_etag=expected_etag,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def select_stay(
        self,
        resource: PublicResourceRecord,
        *,
        candidate_token: str,
        expected_etag: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> StaySelectionOutcome:
        request_hash = canonical_sha256(
            {
                "action": "SELECT_STAY",
                "candidate_token": candidate_token,
                "if_match": expected_etag,
            }
        )
        return await self.repository.select_stay(
            resource,
            candidate_token=candidate_token,
            expected_etag=expected_etag,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def materialize_trip(
        self,
        resource: PublicResourceRecord,
        *,
        expected_etag: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> MaterializationOutcome:
        request_hash = canonical_sha256(
            {"action": "MATERIALIZE_TRIP", "if_match": expected_etag}
        )
        return await self.repository.materialize_trip(
            resource,
            expected_etag=expected_etag,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def get_trip_checks(
        self,
        resource: PublicResourceRecord,
    ) -> PublicTripChecksView:
        return await self.repository.get_trip_checks(resource)

    async def preview_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        check_token: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ChangePreviewOutcome:
        request_hash = canonical_sha256(
            {"action": "PREVIEW_CHANGE", "check_token": check_token}
        )
        return await self.repository.preview_trip_change(
            resource,
            check_token=check_token,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def adopt_trip_change(
        self,
        resource: PublicResourceRecord,
        *,
        change_token: str,
        expected_etag: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ChangeAdoptOutcome:
        request_hash = canonical_sha256(
            {
                "action": "ADOPT_CHANGE",
                "change_token": change_token,
                "if_match": expected_etag,
            }
        )
        return await self.repository.adopt_trip_change(
            resource,
            change_token=change_token,
            expected_etag=expected_etag,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def claim_demo(
        self,
        public_resource_id: str,
        *,
        capability_hash: str,
        user_id: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> ClaimOutcome:
        request_hash = canonical_sha256(
            {"public_resource_id": public_resource_id, "user_id": user_id, "action": "CLAIM"}
        )
        return await self.repository.claim_demo(
            public_resource_id,
            capability_hash=capability_hash,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
            retention_days=self.full_retention_days,
        )

    async def delete_source(
        self,
        resource: PublicResourceRecord,
        *,
        user_id: str | None,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> DeletionOutcome:
        request_hash = canonical_sha256(
            {"understanding_id": resource.understanding_id, "action": "DELETE_SOURCE"}
        )
        return await self.repository.delete_source(
            resource,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def delete_trip(
        self,
        resource: PublicResourceRecord,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> DeletionOutcome:
        request_hash = canonical_sha256(
            {"public_resource_id": resource.public_resource_id, "action": "DELETE_TRIP"}
        )
        return await self.repository.delete_trip(
            resource,
            capability_hash=capability_hash,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def replay_trip_deletion(
        self,
        public_resource_id: str,
        *,
        capability_hash: str | None,
        user_id: str | None,
        idempotency_key: str,
    ) -> bool:
        request_hash = canonical_sha256(
            {"public_resource_id": public_resource_id, "action": "DELETE_TRIP"}
        )
        return await self.repository.replay_trip_deletion(
            public_resource_id,
            capability_hash=capability_hash,
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
        )

    async def delete_account_travel_data(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> TravelDataDeletionOutcome:
        request_hash = canonical_sha256(
            {"action": "DELETE_ALL_TRAVEL_DATA"}
        )
        return await self.repository.delete_account_travel_data(
            user_id=user_id,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
        )

    async def get_account_travel_data_deletion(
        self,
        *,
        user_id: str,
    ) -> TravelDataDeletionStatusView:
        return await self.repository.get_account_travel_data_deletion(user_id=user_id)
