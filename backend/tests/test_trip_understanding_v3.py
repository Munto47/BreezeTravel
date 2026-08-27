from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.trip_understanding.access_log import redact_trip_understanding_path
from app.trip_understanding.capability import capability_hash, mint_capability
from app.trip_understanding.demo import (
    DEMO_SOURCE_SHA256,
    DEMO_SOURCE_TEXT,
    FixedBeijingDemoInferenceProvider,
    FixedBeijingPlaceResolver,
)
from app.trip_understanding.errors import (
    CommandTargetChangedError,
    IdempotencyConflictError,
    JobLeaseLostError,
    ResourceAccessDeniedError,
)
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import (
    ActivityDeleteCommand,
    ActivityInsertCommand,
    ActivityMoveCommand,
    ActivityRole,
    ActivityTextEditCommand,
    AssumptionSetCommand,
    PlaceReplaceCommand,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.service import DEMO_CREATE_REQUEST_HASH, TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher


FORBIDDEN_PUBLIC_KEYS = {
    "raw_text",
    "source",
    "source_id",
    "span",
    "span_start",
    "span_end",
    "offset",
    "confidence",
    "model",
    "provider",
    "uuid",
    "hash",
    "revision",
    "receipt",
    "run",
    "stage",
}


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key.casefold()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


FULL_BEIJING_TEXT = """北京三日行程
Day 1：故宫博物院、景山公园。
Day 2：天坛公园、前门大街。
Day 3：颐和园、圆明园。
有空可以考虑南锣鼓巷，不去上海迪士尼乐园。
预约说明：https://example.com/booking
"""


@pytest.mark.asyncio
async def test_fixed_demo_runs_the_real_compiler_resolver_projector_chain() -> None:
    assert DEMO_SOURCE_SHA256 == "864dd50d49c38f92cf78e33abf2bf03fc86e23c6f14977919e6c4f16a64f1222"
    pipeline = TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        FixedBeijingPlaceResolver(),
    )

    output = await pipeline.run(DEMO_SOURCE_TEXT)

    assert output.compiler_receipt == {
        "compiler": "trip-understanding-evidence-compiler-v1",
        "unicode_basis": "CODE_POINT_HALF_OPEN",
        "mention_count": 10,
        "valid_span_count": 10,
        "eligible_place_count": 6,
    }
    assert [item.compiled.mention.role for item in output.activities].count(ActivityRole.PLANNED) == 6
    assert [item.compiled.mention.role for item in output.activities].count(ActivityRole.OPTIONAL) == 1
    assert [item.compiled.mention.role for item in output.activities].count(ActivityRole.EXCLUDED) == 1
    result = output.public_result.model_dump(mode="json")
    assert set(result) == {"status", "assumptions", "days", "map", "stay", "available_actions"}
    assert [[card["name"] for card in day["activities"]] for day in result["days"]] == [
        ["故宫博物院", "景山公园"],
        ["天坛公园", "前门大街"],
        ["颐和园", "圆明园"],
    ]
    assert [day["label"] for day in result["days"]] == ["Day 1", "Day 2", "Day 3"]
    assert result["map"]["status"] == result["stay"]["status"] == "UNAVAILABLE"
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(_walk_keys(result))
    serialized = json.dumps(result, ensure_ascii=False)
    assert "南锣鼓巷" not in serialized
    assert "北京环球影城" not in serialized
    assert "提前预约" not in serialized
    assert "https://" not in serialized


@pytest.mark.asyncio
async def test_full_text_conservative_chain_resolves_only_planned_atomic_places() -> None:
    output = await build_full_text_pipeline().run(FULL_BEIJING_TEXT)

    result = output.public_result.model_dump(mode="json")
    assert result["status"] == "READY"
    assert [[card["name"] for card in day["activities"]] for day in result["days"]] == [
        ["故宫博物院", "景山公园"],
        ["天坛公园", "前门大街"],
        ["颐和园", "圆明园"],
    ]
    roles = {item.compiled.mention.raw_text: item.compiled.mention.role for item in output.activities}
    assert roles["南锣鼓巷"] == ActivityRole.OPTIONAL
    assert roles["上海迪士尼乐园"] == ActivityRole.EXCLUDED
    assert output.compiler_receipt["eligible_place_count"] == 6
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(_walk_keys(result))
    serialized = json.dumps(result, ensure_ascii=False)
    assert "南锣鼓巷" not in serialized
    assert "上海迪士尼乐园" not in serialized
    assert "https://" not in serialized


@pytest.mark.asyncio
async def test_full_text_unknown_or_reference_only_input_does_not_invent_a_place() -> None:
    output = await build_full_text_pipeline().run(
        "北京随手记：攻略提到故宫博物院。预约说明 https://example.com/place/天坛公园"
    )

    assert output.public_result.status == "BASIC_ONLY"
    assert [day.activities for day in output.public_result.days] == [[]]
    assert all(not item.compiled.eligible_for_place_search for item in output.activities)


def test_source_cipher_is_randomized_and_bound_to_source_identity() -> None:
    cipher = SourceCipher("unit-test-root-secret")
    source_hash = "a" * 64
    first = cipher.encrypt("北京行程", source_id="source-a", content_hash=source_hash)
    second = cipher.encrypt("北京行程", source_id="source-a", content_hash=source_hash)

    assert first != second
    assert b"\xe5\x8c\x97\xe4\xba\xac" not in first
    assert cipher.decrypt(first, source_id="source-a", content_hash=source_hash) == "北京行程"
    with pytest.raises(Exception):
        cipher.decrypt(first, source_id="source-b", content_hash=source_hash)


@pytest.mark.asyncio
async def test_all_card_commands_create_stale_map_projection_without_provider_side_effects() -> None:
    current = (await build_full_text_pipeline().run(FULL_BEIJING_TEXT)).public_result
    first_token = current.days[0].activities[0].activity_token

    inserted = apply_public_command(
        current,
        ActivityInsertCommand(
            command_type="ACTIVITY_INSERT",
            day_index=2,
            position=1,
            name="北京动物园",
        ),
    )
    assert inserted.changed_days == ["Day 2"]
    assert inserted.result.days[1].activities[1].name == "北京动物园"
    assert inserted.result.days[1].activities[1].status == "NEEDS_CONFIRMATION"
    assert inserted.result.map.status == "NEEDS_UPDATE"
    assert inserted.result.map.available_actions == ["RENDER_MAP"]

    deleted = apply_public_command(
        current,
        ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=first_token),
    )
    assert [item.name for item in deleted.result.days[0].activities] == ["景山公园"]

    moved = apply_public_command(
        current,
        ActivityMoveCommand(
            command_type="ACTIVITY_MOVE",
            activity_token=first_token,
            target_day_index=3,
            target_position=1,
        ),
    )
    assert moved.changed_days == ["Day 1", "Day 3"]
    assert [item.name for item in moved.result.days[2].activities] == [
        "颐和园",
        "故宫博物院",
        "圆明园",
    ]

    edited = apply_public_command(
        current,
        ActivityTextEditCommand(
            command_type="ACTIVITY_TEXT_EDIT",
            activity_token=first_token,
            name="故宫入口待确认",
        ),
    )
    assert edited.result.days[0].activities[0].status == "NEEDS_CONFIRMATION"
    assert edited.result.days[0].activities[0].area_or_address == "地点待确认"

    replaced = apply_public_command(
        current,
        PlaceReplaceCommand.model_validate(
            {
                "command_type": "PLACE_REPLACE",
                "activity_token": first_token,
                "replacement": {
                    "name": "北海公园",
                    "category": "公园",
                    "area_or_address": "西城区·文津街1号",
                },
            }
        ),
    )
    assert replaced.result.days[0].activities[0].name == "北海公园"
    assert replaced.result.days[0].activities[0].status == "NEEDS_CONFIRMATION"

    assumption = apply_public_command(
        current,
        AssumptionSetCommand(
            command_type="ASSUMPTION_SET",
            key="party_size",
            value="3 人",
        ),
    )
    assert assumption.changed_days == ["Day 1", "Day 2", "Day 3"]
    assert next(item for item in assumption.result.assumptions if item.key == "party_size").value == "3 人"
    assert all(
        new != old for old, new in assumption.token_map.items()
    )

    with pytest.raises(CommandTargetChangedError):
        apply_public_command(
            current,
            ActivityDeleteCommand(
                command_type="ACTIVITY_DELETE",
                activity_token="missing-activity-token-0000",
            ),
        )


def test_signed_capability_is_tamper_evident_and_access_log_path_is_redacted() -> None:
    cookie, digest = mint_capability("fixture-signing-key")

    assert capability_hash(cookie, "fixture-signing-key") == digest
    assert capability_hash(f"{cookie}x", "fixture-signing-key") is None
    assert capability_hash(cookie, "different-key") is None
    path = "/api/v3/trip-understandings/visible-resource-secret/result?x=1"
    redacted = redact_trip_understanding_path(path)
    assert redacted == "/api/v3/trip-understandings/{public_resource_id}/result?x=1"
    assert "visible-resource-secret" not in redacted


@pytest.mark.asyncio
async def test_create_replay_conflict_cross_session_and_durable_result() -> None:
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    first = await service.create_demo(
        capability_hash="a" * 64,
        idempotency_key="demo-create-1",
        now=now,
    )
    replay = await service.create_demo(
        capability_hash="a" * 64,
        idempotency_key="demo-create-1",
        now=now,
    )
    assert replay.replayed is True
    assert replay.accepted == first.accepted

    with pytest.raises(IdempotencyConflictError):
        await repository.create_demo(
            capability_hash="a" * 64,
            idempotency_key="demo-create-1",
            request_hash="f" * 64,
            now=now,
            ttl_hours=24,
        )
    with pytest.raises(ResourceAccessDeniedError):
        await service.authorize(
            first.accepted.public_resource_id,
            capability_hash="b" * 64,
            now=now,
        )

    resource = await service.authorize(
        first.accepted.public_resource_id,
        capability_hash="a" * 64,
        now=now,
    )
    assert await repository.get_result(resource) is None
    job = await repository.claim_next(worker_id="worker-a", now=now, lease_seconds=30)
    assert job is not None
    output = await TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        FixedBeijingPlaceResolver(),
    ).run(DEMO_SOURCE_TEXT)
    assert await repository.complete_job(job, output, now=now + timedelta(seconds=1)) is False
    assert await repository.complete_job(job, output, now=now + timedelta(seconds=2)) is True
    assert repository.side_effect_count == 1

    resource = await service.authorize(
        first.accepted.public_resource_id,
        capability_hash="a" * 64,
        now=now + timedelta(seconds=2),
    )
    stored = await repository.get_result(resource)
    assert stored is not None
    assert stored.result.status == "READY"
    assert stored.opaque_etag.startswith("tu3_")
    assert resource.understanding_id not in stored.opaque_etag
    assert "revision" not in stored.opaque_etag.casefold()
    events = await repository.list_events(resource, after_event_id=1)
    assert [item.event_type for item in events] == ["progress", "result_available"]
    assert [item.event_id for item in events] == [2, 3]


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_commit() -> None:
    repository = InMemoryTripUnderstandingRepository()
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    await repository.create_demo(
        capability_hash="c" * 64,
        idempotency_key="lease-create",
        request_hash=DEMO_CREATE_REQUEST_HASH,
        now=now,
        ttl_hours=24,
    )
    first = await repository.claim_next(worker_id="worker-old", now=now, lease_seconds=5)
    assert first is not None
    assert await repository.claim_next(
        worker_id="worker-early",
        now=now + timedelta(seconds=4),
        lease_seconds=5,
    ) is None
    replacement = await repository.claim_next(
        worker_id="worker-new",
        now=now + timedelta(seconds=6),
        lease_seconds=30,
    )
    assert replacement is not None
    assert replacement.attempt == 2
    output = await TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        FixedBeijingPlaceResolver(),
    ).run(DEMO_SOURCE_TEXT)
    with pytest.raises(JobLeaseLostError):
        await repository.complete_job(first, output, now=now + timedelta(seconds=7))
    await repository.complete_job(replacement, output, now=now + timedelta(seconds=7))
    assert repository.side_effect_count == 1
