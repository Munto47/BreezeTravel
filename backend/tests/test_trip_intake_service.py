from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.itineraries.errors import IdempotencyKeyReusedError
from app.trip_intake.models import IntakeSourceType, IntakeStatus, QuantityQuantifier
from app.trip_intake.repository import InMemoryTripIntakeRepository
from app.trip_intake.service import TripIntakeApplicationService


@pytest.mark.asyncio
async def test_create_and_confirm_exact_intake_without_default_injection() -> None:
    repository = InMemoryTripIntakeRepository()
    service = TripIntakeApplicationService(repository)
    created = await service.create(
        room_id="room-1",
        source_type=IntakeSourceType.MANUAL_TEXT,
        source_texts=["2027年10月1日到10月7日去成都，12人玩7天"],
        actor_user_id="user-1",
    )

    assert created.revision == 1
    assert created.extraction.party_size.total.min == 12
    assert created.extraction.temporal.days.min == 7
    assert created.status == IntakeStatus.NEEDS_CONFIRMATION

    confirmed, replayed = await service.confirm(
        intake_id=created.intake_id,
        revision=1,
        actor_user_id="user-1",
        idempotency_key="confirm-1",
    )
    assert confirmed.status == IntakeStatus.READY
    assert confirmed.revision == 2
    assert not replayed

    replay, replayed = await service.confirm(
        intake_id=created.intake_id,
        revision=1,
        actor_user_id="user-1",
        idempotency_key="confirm-1",
    )
    assert replay == confirmed
    assert replayed


@pytest.mark.asyncio
async def test_missing_party_stays_unknown_and_blocks_confirmation() -> None:
    repository = InMemoryTripIntakeRepository()
    service = TripIntakeApplicationService(repository)
    created = await service.create(
        room_id="room-1",
        source_type=IntakeSourceType.AI_TEXT,
        source_texts=["2027年10月1日到10月7日去成都玩7天"],
        actor_user_id="user-1",
    )

    assert created.extraction.party_size.total.quantifier == QuantityQuantifier.UNKNOWN
    assert created.extraction.party_size.total.min is None
    with pytest.raises(ValidationError, match="READY intake requires"):
        await service.confirm(
            intake_id=created.intake_id,
            revision=1,
            actor_user_id="user-1",
            idempotency_key="confirm-missing-party",
        )


@pytest.mark.asyncio
async def test_confirm_idempotency_key_cannot_be_reused_for_another_request() -> None:
    repository = InMemoryTripIntakeRepository()
    service = TripIntakeApplicationService(repository)
    first = await service.create(
        room_id="room-1",
        source_type=IntakeSourceType.MANUAL_TEXT,
        source_texts=["2027年10月1日到10月7日去成都，3人"],
        actor_user_id="user-1",
    )
    await service.confirm(
        intake_id=first.intake_id,
        revision=1,
        actor_user_id="user-1",
        idempotency_key="shared-key",
    )

    with pytest.raises(IdempotencyKeyReusedError):
        await service.confirm(
            intake_id=first.intake_id,
            revision=1,
            actor_user_id="another-user",
            idempotency_key="shared-key",
        )
