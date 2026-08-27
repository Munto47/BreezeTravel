from __future__ import annotations

from datetime import datetime, timezone

from app.trip_understanding.models import CreateOutcome, PublicResourceRecord
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.repository import TripUnderstandingRepository


DEMO_CREATE_REQUEST_HASH = canonical_sha256({"mode": "DEMO"})


class TripUnderstandingApplicationService:
    def __init__(self, repository: TripUnderstandingRepository, *, ttl_hours: int = 24) -> None:
        self.repository = repository
        self.ttl_hours = ttl_hours

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
