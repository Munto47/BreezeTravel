from __future__ import annotations

from datetime import date

import pytest

from app.importing.parser import ItineraryTextParser
from app.trip_intake.materialization import (
    InMemoryTripIntakeMaterializationRepository,
    TripIntakeMaterializationService,
)
from app.trip_intake.models import IntakeSourceType
from app.trip_intake.repository import InMemoryTripIntakeRepository
from app.trip_intake.service import TripIntakeApplicationService


async def _confirmed_intake(repository: InMemoryTripIntakeRepository):
    service = TripIntakeApplicationService(repository)
    created = await service.create(
        room_id="room-1",
        source_type=IntakeSourceType.MANUAL_TEXT,
        source_texts=[
            "2027年10月1日到10月7日去成都，12人。\n"
            "第六天：09:00 成都博物馆\n第七天：10:00 人民公园"
        ],
        actor_user_id="user-1",
    )
    confirmed, _ = await service.confirm(
        intake_id=created.intake_id,
        revision=created.revision,
        actor_user_id="user-1",
        idempotency_key="confirm-1",
    )
    return confirmed


@pytest.mark.asyncio
async def test_materialization_builds_one_workspace_brief_import_and_lineage() -> None:
    intake_repository = InMemoryTripIntakeRepository()
    intake = await _confirmed_intake(intake_repository)
    materialization_repository = InMemoryTripIntakeMaterializationRepository()
    service = TripIntakeMaterializationService(
        intake_repository=intake_repository,
        materialization_repository=materialization_repository,
    )

    result = await service.materialize(
        intake_id=intake.intake_id,
        revision=intake.revision,
        actor_user_id="user-1",
        idempotency_key="materialize-1",
    )

    receipt = result.materialization
    assert receipt.workspace.city == "成都市"
    assert receipt.brief.traveler_count == 12
    assert receipt.brief.source_intake_id == intake.intake_id
    assert receipt.brief.source_intake_revision == intake.revision
    assert receipt.workspace.current_import_id == receipt.itinerary_import.import_id
    assert {5, 6}.issubset({stop.day_index for stop in receipt.itinerary_import.raw_stops})
    assert result.resolution_dispatch == "NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_materialization_replay_does_not_duplicate_resources_and_provider_failure_is_retryable() -> None:
    intake_repository = InMemoryTripIntakeRepository()
    intake = await _confirmed_intake(intake_repository)
    materialization_repository = InMemoryTripIntakeMaterializationRepository()
    attempts: list[str] = []

    async def fail_resolution(import_id: str) -> None:
        attempts.append(import_id)
        raise RuntimeError("provider unavailable")

    service = TripIntakeMaterializationService(
        intake_repository=intake_repository,
        materialization_repository=materialization_repository,
        provider_resolution_trigger=fail_resolution,
    )
    first = await service.materialize(
        intake_id=intake.intake_id,
        revision=intake.revision,
        actor_user_id="user-1",
        idempotency_key="materialize-1",
    )
    replay = await service.materialize(
        intake_id=intake.intake_id,
        revision=intake.revision,
        actor_user_id="user-1",
        idempotency_key="materialize-1",
    )

    assert first.materialization.materialization_id == replay.materialization.materialization_id
    assert replay.idempotent_replay
    assert first.resolution_dispatch == replay.resolution_dispatch == "FAILED_RETRYABLE"
    assert attempts == [
        first.materialization.itinerary_import.import_id,
        first.materialization.itinerary_import.import_id,
    ]


def test_itinerary_parser_accepts_arbitrary_positive_day_numbers() -> None:
    parsed = ItineraryTextParser().parse(
        "第十二天：09:00 成都博物馆",
        import_id="import-1",
    )
    assert parsed.raw_stops[0].day_index == 11


@pytest.mark.asyncio
async def test_materialization_does_not_parse_confirmation_evidence_as_stops() -> None:
    intake_repository = InMemoryTripIntakeRepository()
    application = TripIntakeApplicationService(intake_repository)
    created = await application.create(
        room_id="room-correction",
        source_type=IntakeSourceType.MANUAL_TEXT,
        source_texts=["日期和人数稍后确认。第1天：09:00 灵隐寺"],
        actor_user_id="user-1",
    )
    corrected, _ = await application.patch_confirmed_values(
        intake_id=created.intake_id,
        revision=created.revision,
        city="杭州",
        start_date=date(2027, 10, 1),
        end_date=date(2027, 10, 2),
        party_size=2,
        actor_user_id="user-1",
        idempotency_key="correct-core-values",
    )
    confirmed, _ = await application.confirm(
        intake_id=corrected.intake_id,
        revision=corrected.revision,
        actor_user_id="user-1",
        idempotency_key="confirm-corrected",
    )
    result = await TripIntakeMaterializationService(
        intake_repository=intake_repository,
        materialization_repository=InMemoryTripIntakeMaterializationRepository(),
    ).materialize(
        intake_id=confirmed.intake_id,
        revision=confirmed.revision,
        actor_user_id="user-1",
        idempotency_key="materialize-corrected",
    )

    itinerary_import = result.materialization.itinerary_import
    assert result.materialization.workspace.city == "杭州"
    assert result.materialization.brief.city == "杭州"
    assert [stop.raw_name for stop in itinerary_import.raw_stops] == ["灵隐寺"]
    assert "确认目的城市" not in itinerary_import.raw_text
