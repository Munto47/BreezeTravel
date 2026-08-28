from __future__ import annotations

import asyncio
import hashlib
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
    InferenceProviderUnavailableError,
    JobLeaseLostError,
    PlaceProviderUnavailableError,
    ResourceAccessDeniedError,
    RouteProviderUnavailableError,
)
from app.trip_understanding.full_text import build_full_text_pipeline
from app.trip_understanding.map_render import (
    ROUTE_CONFIG_SHA256,
    ControlledFixtureRouteProvider,
    MapRenderPlan,
    MapRenderer,
    MapStop,
    PlanRevisionRef,
)
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import (
    ActivityDeleteCommand,
    ActivityInsertCommand,
    ActivityMoveCommand,
    ActivityRole,
    ActivityTextEditCommand,
    AssumptionSetCommand,
    InferenceProposal,
    PlaceReplaceCommand,
    ProposedMention,
    ResolvedPlace,
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


@pytest.mark.asyncio
async def test_only_atomic_planned_mentions_reach_place_provider_or_public_cards() -> None:
    source = (
        "北京 Day 1 故宫博物院；天坛公园仅作备选；景山公园只是参考；"
        "路过王府井地铁站；排除颐和园；预约说明；https://example.invalid/place"
    )
    values = [
        ("故宫博物院", "PLANNED"),
        ("天坛公园", "OPTIONAL"),
        ("景山公园", "REFERENCE"),
        ("王府井地铁站", "PASS_THROUGH"),
        ("颐和园", "EXCLUDED"),
        ("预约说明", "PLANNED"),
        ("https://example.invalid/place", "PLANNED"),
    ]

    class RoleProvider:
        async def propose(self, source_text: str):
            mentions = []
            for index, (raw, role) in enumerate(values):
                start = source_text.index(raw)
                mentions.append(
                    ProposedMention(
                        mention_id=f"role-{index}",
                        raw_text=raw,
                        span_start=start,
                        span_end=start + len(raw),
                        role=role,
                        day_index=1 if role == "PLANNED" else None,
                        sequence_index=index,
                        atomic_place_name=raw,
                    )
                )
            return InferenceProposal(
                source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
                destination_name="北京",
                mentions=mentions,
                binding={"provider": "role-test-double", "external_calls": 0},
            )

    class RecordingResolver:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ):
            del city, category_hint
            self.calls.append(atomic_place_name)
            return None

    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(RoleProvider(), resolver).run(source)

    assert resolver.calls == ["故宫博物院"]
    cards = [card for day in output.public_result.days for card in day.activities]
    assert [card.name for card in cards] == ["故宫博物院"]
    public = json.dumps(output.public_result.model_dump(mode="json"), ensure_ascii=False)
    assert "预约说明" not in public
    assert "https://" not in public


@pytest.mark.asyncio
async def test_typed_inference_outage_uses_explicit_local_fallback_and_returns_editable_partial() -> None:
    class UnavailableInferenceProvider:
        async def propose(self, source_text: str):
            del source_text
            raise InferenceProviderUnavailableError(
                "DEADLINE_EXCEEDED",
                provider_binding={
                    "provider": "qwen-test-double",
                    "region": "test-region",
                    "exact_model_id": "test-only",
                },
                external_call_count=1,
            )

    output = await build_full_text_pipeline(UnavailableInferenceProvider()).run(FULL_BEIJING_TEXT)

    assert output.public_result.status == "PARTIAL_RESULT"
    assert sum(len(day.activities) for day in output.public_result.days) == 6
    assert output.inference_binding["fallback_used"] is True
    assert output.inference_binding["fallback_reason"] == "DEADLINE_EXCEEDED"
    assert output.inference_binding["primary_external_call_count"] == 1
    assert output.resolution_receipt["inference_fallback_used"] is True
    public = output.public_result.model_dump(mode="json")
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(_walk_keys(public))
    assert "DEADLINE_EXCEEDED" not in json.dumps(public, ensure_ascii=False)


@pytest.mark.asyncio
async def test_typed_place_provider_outage_keeps_every_planned_card_editable() -> None:
    class UnavailablePlaceResolver:
        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ):
            del city, atomic_place_name, category_hint
            raise PlaceProviderUnavailableError(
                "PROVIDER_UNAVAILABLE",
                provider_binding={"provider": "amap-test-double"},
                external_call_count=1,
            )

    output = await TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        UnavailablePlaceResolver(),
    ).run(DEMO_SOURCE_TEXT)

    assert output.public_result.status == "PARTIAL_RESULT"
    cards = [card for day in output.public_result.days for card in day.activities]
    assert len(cards) == 6
    assert all(card.status == "NEEDS_CONFIRMATION" for card in cards)
    assert output.resolution_receipt["provider_unavailable_count"] == 6
    assert all(
        item.resolver_receipt.get("failure_category") == "PROVIDER_UNAVAILABLE"
        for item in output.activities
        if item.compiled.eligible_for_place_search
    )
    public = output.public_result.model_dump(mode="json")
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(_walk_keys(public))
    assert "PROVIDER_UNAVAILABLE" not in json.dumps(public, ensure_ascii=False)


@pytest.mark.asyncio
async def test_typed_route_mode_outage_isolated_to_that_mode_and_snapshot_stays_available() -> None:
    class TransitUnavailableProvider:
        def __init__(self) -> None:
            self.fixture = ControlledFixtureRouteProvider()

        async def route(self, origin, destination, mode, *, observed_at):
            if mode == "transit":
                raise RouteProviderUnavailableError(
                    "ROUTE_PROVIDER_UNAVAILABLE",
                    provider_binding={"provider": "amap-test-double"},
                    external_call_count=1,
                )
            return await self.fixture.route(
                origin,
                destination,
                mode,
                observed_at=observed_at,
            )

    stops = [
        MapStop(
            day_index=1,
            day_label="Day 1",
            sequence_index=index,
            name=name,
            canonical_place_id=f"fixture-{index}",
            resolution_status="AUTO_MATCHED",
        )
        for index, name in enumerate(("故宫博物院", "景山公园"))
    ]
    plan = MapRenderPlan(
        understanding_id="route-provider-outage",
        plan_ref=PlanRevisionRef(
            kind="UNDERSTANDING",
            aggregate_id="route-provider-outage",
            revision=1,
            stop_set_hash="a" * 64,
        ),
        route_config_hash=ROUTE_CONFIG_SHA256,
        stops=stops,
    )

    output = await MapRenderer(TransitUnavailableProvider()).render(
        plan,
        observed_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )

    assert output.status == "READY"
    assert output.edges[0].selected_mode == "walking"
    assert output.edges[0].walking.status == "AVAILABLE"
    assert output.edges[0].transit.status == "UNAVAILABLE"
    assert output.provider_binding["external_calls"] == 1


@pytest.mark.asyncio
async def test_executable_activity_budget_preserves_all_cards_and_returns_limited() -> None:
    names = [f"测试地点{index:02d}" for index in range(81)]
    source_text = "、".join(names)

    class ManyActivitiesProvider:
        async def propose(self, source: str):
            cursor = 0
            mentions = []
            for index, name in enumerate(names):
                start = source.index(name, cursor)
                cursor = start + len(name)
                mentions.append(
                    ProposedMention(
                        mention_id=f"budget-{index}",
                        raw_text=name,
                        span_start=start,
                        span_end=cursor,
                        role="PLANNED",
                        day_index=1,
                        sequence_index=index,
                        atomic_place_name=name,
                        category_hint="地点",
                    )
                )
            return InferenceProposal(
                source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
                destination_name="北京",
                mentions=mentions,
                binding={"provider": "budget-test-double", "external_calls": 0},
            )

    class CountingUnresolvedResolver:
        def __init__(self) -> None:
            self.calls = 0

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ):
            del city, atomic_place_name, category_hint
            self.calls += 1
            return None

    resolver = CountingUnresolvedResolver()
    output = await TripUnderstandingPipeline(ManyActivitiesProvider(), resolver).run(source_text)

    assert output.public_result.status == "LIMITED"
    assert sum(len(day.activities) for day in output.public_result.days) == 81
    assert resolver.calls == 80
    assert output.resolution_receipt["attempted_count"] == 80
    assert output.resolution_receipt["budget_limited_count"] == 1
    assert output.resolution_receipt["max_executable_activities"] == 80
    assert output.activities[-1].resolver_receipt["status"] == "BUDGET_LIMITED"


@pytest.mark.asyncio
async def test_place_resolution_is_deduplicated_bounded_and_order_preserving() -> None:
    names = ["故宫博物院", "景山公园", "故宫博物院", "天坛公园"]
    source_text = "北京 Day 1 " + "、".join(names)

    class OrderedProvider:
        async def propose(self, source: str):
            mentions = []
            cursor = 0
            for index, name in enumerate(names):
                start = source.index(name, cursor)
                cursor = start + len(name)
                mentions.append(
                    ProposedMention(
                        mention_id=f"ordered-{index}",
                        raw_text=name,
                        span_start=start,
                        span_end=cursor,
                        role="PLANNED",
                        day_index=1,
                        sequence_index=index,
                        atomic_place_name=name,
                        category_hint="景点",
                    )
                )
            return InferenceProposal(
                source_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
                destination_name="北京",
                mentions=mentions,
                binding={"provider": "ordered-test-double", "external_calls": 0},
            )

    class BoundedResolver:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.active = 0
            self.maximum_active = 0

        async def resolve(
            self,
            *,
            city: str,
            atomic_place_name: str,
            category_hint: str | None = None,
        ):
            assert city == "北京"
            assert category_hint == "景点"
            self.calls.append(atomic_place_name)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0.01)
            self.active -= 1
            return ResolvedPlace(
                canonical_place_id=f"test-{atomic_place_name}",
                name=atomic_place_name,
                category="景点",
                area_or_address="测试地址",
                provider_binding={"provider": "test-double", "external_calls": 1},
            )

    resolver = BoundedResolver()
    output = await TripUnderstandingPipeline(
        OrderedProvider(),
        resolver,
        max_place_concurrency=2,
    ).run(source_text)

    cards = [card for day in output.public_result.days for card in day.activities]
    assert [card.name for card in cards] == names
    assert resolver.calls == ["故宫博物院", "景山公园", "天坛公园"]
    assert resolver.maximum_active == 2
    assert output.resolution_receipt["unique_resolution_count"] == 3
    assert output.resolution_receipt["deduplicated_resolution_count"] == 1
    assert output.resolution_receipt["place_external_call_count"] == 3
    assert output.activities[2].resolver_receipt["deduplicated"] is True
    assert output.activities[2].resolver_receipt["external_calls"] == 0


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


@pytest.mark.asyncio
async def test_revision_bound_map_lifecycle_dedupes_and_never_auto_renders_edits() -> None:
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    created = await service.create_demo(
        capability_hash="d" * 64,
        idempotency_key="map-demo",
        now=now,
    )
    understanding_job = await repository.claim_next(
        worker_id="understanding-worker",
        now=now,
        lease_seconds=30,
    )
    assert understanding_job is not None
    output = await TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        FixedBeijingPlaceResolver(),
    ).run(DEMO_SOURCE_TEXT)
    await repository.complete_job(
        understanding_job,
        output,
        now=now + timedelta(seconds=1),
    )
    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash="d" * 64,
        now=now + timedelta(seconds=1),
    )

    assert (await repository.get_map_view(resource, now=now)).status == "PREPARING"
    assert repository.map_job_count == 1
    assert await MapRenderWorker(repository).run_once(
        "map-worker", now=now + timedelta(seconds=2)
    )
    map_view = await repository.get_map_view(resource, now=now + timedelta(seconds=3))
    assert map_view.status == "AVAILABLE"
    assert [route.selected_mode for day in map_view.days for route in day.routes] == [
        "walking",
        "transit",
        "transit",
    ]
    assert repository.map_provider_effect_count == 6
    assert (
        await repository.get_map_view(resource, now=now + timedelta(days=2))
    ).status == "AVAILABLE"
    assert FORBIDDEN_PUBLIC_KEYS.isdisjoint(
        _walk_keys(map_view.model_dump(mode="json"))
    )

    stored = await repository.get_result(resource)
    assert stored is not None
    assert stored.result.map.status == "AVAILABLE"
    first_token = stored.result.days[0].activities[0].activity_token
    command = ActivityMoveCommand(
        command_type="ACTIVITY_MOVE",
        activity_token=first_token,
        target_day_index=3,
        target_position=1,
    )
    applied = await service.apply_command(
        resource,
        command,
        expected_etag=stored.opaque_etag,
        idempotency_key="map-edit",
        now=now + timedelta(seconds=4),
    )
    assert repository.map_job_count == 1
    updated = await repository.get_result(resource)
    assert updated is not None
    assert updated.result.map.status == "NEEDS_UPDATE"

    accepted = await service.request_map_render(
        resource,
        expected_etag=applied.opaque_etag,
        idempotency_key="map-manual",
        now=now + timedelta(seconds=5),
    )
    replayed = await service.request_map_render(
        resource,
        expected_etag=applied.opaque_etag,
        idempotency_key="map-manual",
        now=now + timedelta(seconds=5),
    )
    logically_deduped = await service.request_map_render(
        resource,
        expected_etag=applied.opaque_etag,
        idempotency_key="map-manual-second-key",
        now=now + timedelta(seconds=5),
    )
    assert accepted.accepted.status == "PREPARING"
    assert replayed.replayed is True
    assert logically_deduped.replayed is False
    assert repository.map_job_count == 2
    assert await MapRenderWorker(repository).run_once(
        "map-worker", now=now + timedelta(seconds=6)
    )
    assert (
        await repository.get_map_view(resource, now=now + timedelta(seconds=7))
    ).status == "LIMITED"


@pytest.mark.asyncio
async def test_map_lease_takeover_and_late_old_revision_are_isolated() -> None:
    repository = InMemoryTripUnderstandingRepository()
    service = TripUnderstandingApplicationService(repository)
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    created = await service.create_demo(
        capability_hash="e" * 64,
        idempotency_key="map-lease-demo",
        now=now,
    )
    understanding_job = await repository.claim_next(
        worker_id="understanding-worker",
        now=now,
        lease_seconds=30,
    )
    assert understanding_job is not None
    output = await TripUnderstandingPipeline(
        FixedBeijingDemoInferenceProvider(),
        FixedBeijingPlaceResolver(),
    ).run(DEMO_SOURCE_TEXT)
    await repository.complete_job(
        understanding_job,
        output,
        now=now + timedelta(seconds=1),
    )
    old_claim = await repository.claim_next_map(
        worker_id="old-map-worker",
        now=now + timedelta(seconds=2),
        lease_seconds=5,
    )
    assert old_claim is not None
    old_plan = await repository.load_map_plan(old_claim)
    old_output = await MapRenderer().render(old_plan, observed_at=now + timedelta(seconds=2))
    replacement = await repository.claim_next_map(
        worker_id="new-map-worker",
        now=now + timedelta(seconds=8),
        lease_seconds=30,
    )
    assert replacement is not None
    with pytest.raises(JobLeaseLostError):
        await repository.complete_map_job(
            old_claim,
            old_output,
            now=now + timedelta(seconds=9),
        )

    resource = await service.authorize(
        created.accepted.public_resource_id,
        capability_hash="e" * 64,
        now=now + timedelta(seconds=9),
    )
    stored = await repository.get_result(resource)
    assert stored is not None
    await service.apply_command(
        resource,
        ActivityTextEditCommand(
            command_type="ACTIVITY_TEXT_EDIT",
            activity_token=stored.result.days[0].activities[0].activity_token,
            name="故宫入口待确认",
        ),
        expected_etag=stored.opaque_etag,
        idempotency_key="late-map-edit",
        now=now + timedelta(seconds=9),
    )
    replacement_output = await MapRenderer().render(
        await repository.load_map_plan(replacement),
        observed_at=now + timedelta(seconds=9),
    )
    await repository.complete_map_job(
        replacement,
        replacement_output,
        now=now + timedelta(seconds=9),
    )
    assert (
        await repository.get_map_view(resource, now=now + timedelta(seconds=10))
    ).status == "NEEDS_UPDATE"
