from __future__ import annotations

from datetime import datetime, timezone

from app.trip_understanding.models import (
    ClaimOutcome,
    CommandOutcome,
    CreateFullRequest,
    CreateOutcome,
    DeletionOutcome,
    PublicResourceRecord,
    TripUnderstandingCommand,
    TravelDataDeletionOutcome,
    TravelDataDeletionStatusView,
)
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
        owner_user_id: str,
        idempotency_key: str,
        now: datetime | None = None,
    ) -> CreateOutcome:
        request_hash = canonical_sha256(body.model_dump(mode="json"))
        return await self.repository.create_full(
            owner_user_id=owner_user_id,
            source_text=body.source.text,
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            now=now or datetime.now(timezone.utc),
            retention_days=self.full_retention_days,
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
        user_id: str,
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
