from __future__ import annotations

import hashlib

import pytest

from app.trip_understanding.models import (
    InferenceProposal,
    ProposedMention,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline


class TwoPlaceInferenceProvider:
    async def propose(self, source_text: str) -> InferenceProposal:
        mentions = []
        for index, name in enumerate(("故宫博物院", "景山公园")):
            start = source_text.index(name)
            mentions.append(
                ProposedMention(
                    mention_id=f"g04-{index}",
                    raw_text=name,
                    span_start=start,
                    span_end=start + len(name),
                    role="PLANNED",
                    day_index=1,
                    sequence_index=index,
                    atomic_place_name=name,
                    category_hint="景点",
                )
            )
        return InferenceProposal(
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            destination_name="北京",
            mentions=mentions,
            binding={"provider": "g04-test-double", "external_calls": 0},
        )


class ThreePlaceInferenceProvider:
    async def propose(self, source_text: str) -> InferenceProposal:
        mentions = []
        for index, name in enumerate(("故宫博物院", "景山公园", "天坛公园")):
            start = source_text.index(name)
            mentions.append(
                ProposedMention(
                    mention_id=f"g04-three-{index}",
                    raw_text=name,
                    span_start=start,
                    span_end=start + len(name),
                    role="PLANNED",
                    day_index=1,
                    sequence_index=index,
                    atomic_place_name=name,
                    category_hint="景点",
                )
            )
        return InferenceProposal(
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            destination_name="北京",
            mentions=mentions,
            binding={"provider": "g04-test-double", "external_calls": 0},
        )


class RecordingPlaceResolver:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> ResolvedPlace:
        assert city == "北京"
        assert category_hint == "景点"
        self.calls.append(atomic_place_name)
        return ResolvedPlace(
            canonical_place_id=f"g04-{atomic_place_name}",
            name=atomic_place_name,
            category="景点",
            area_or_address="北京",
            provider_binding={"provider": "g04-test-double", "external_calls": 1},
        )


@pytest.mark.asyncio
async def test_low_confidence_intersection_suppresses_only_the_affected_poi_call() -> None:
    source = "北京 Day 1 故宫博物院 景山公园"
    guarded_start = source.index("故宫博物院")
    resolver = RecordingPlaceResolver()

    output = await TripUnderstandingPipeline(
        TwoPlaceInferenceProvider(),
        resolver,
    ).run(
        source,
        requires_confirmation_spans=((guarded_start, guarded_start + len("故宫博物院")),),
    )

    assert resolver.calls == ["景山公园"]
    by_name = {
        activity.compiled.mention.atomic_place_name: activity
        for activity in output.activities
    }
    assert by_name["故宫博物院"].place is None
    assert by_name["故宫博物院"].resolution_status.value == "NEEDS_CONFIRMATION"
    assert by_name["故宫博物院"].resolver_receipt["external_calls"] == 0
    assert by_name["景山公园"].place is not None
    assert [
        card.name for card in output.public_result.days[0].activities
    ] == ["地点待确认", "景山公园"]
    assert output.public_result.days[0].activities[0].area_or_address == "地点待确认"
    assert output.resolution_receipt["source_confirmation_required_count"] == 1
    assert output.resolution_receipt["place_external_call_count"] == 1


@pytest.mark.asyncio
async def test_one_confirmation_span_suppresses_every_intersecting_place() -> None:
    source = "北京 Day 1 故宫博物院 景山公园 天坛公园"
    guarded_start = source.index("故宫博物院")
    guarded_end = source.index("景山公园") + len("景山公园")
    resolver = RecordingPlaceResolver()

    output = await TripUnderstandingPipeline(
        ThreePlaceInferenceProvider(),
        resolver,
    ).run(
        source,
        requires_confirmation_spans=((guarded_start, guarded_end),),
    )

    assert resolver.calls == ["天坛公园"]
    by_name = {
        activity.compiled.mention.atomic_place_name: activity
        for activity in output.activities
    }
    for name in ("故宫博物院", "景山公园"):
        assert by_name[name].place is None
        assert by_name[name].compiled.eligible_for_place_search is False
        assert by_name[name].resolver_receipt["status"] == (
            "SOURCE_CONFIRMATION_REQUIRED"
        )
        assert by_name[name].resolver_receipt["external_calls"] == 0
    assert by_name["天坛公园"].place is not None
    assert [
        card.name for card in output.public_result.days[0].activities
    ] == ["地点待确认", "地点待确认", "天坛公园"]
    assert output.resolution_receipt["source_confirmation_required_count"] == 2
    assert output.resolution_receipt["place_external_call_count"] == 1


@pytest.mark.asyncio
async def test_partial_screenshot_source_caps_public_result_at_partial() -> None:
    source = "北京 Day 1 故宫博物院 景山公园"
    resolver = RecordingPlaceResolver()

    output = await TripUnderstandingPipeline(
        TwoPlaceInferenceProvider(),
        resolver,
    ).run(source, partial_source=True)

    assert resolver.calls == ["故宫博物院", "景山公园"]
    assert output.public_result.status == "PARTIAL_RESULT"
    assert output.resolution_receipt["partial_source"] is True


@pytest.mark.asyncio
async def test_invalid_confirmation_span_fails_before_provider_calls() -> None:
    source = "北京 Day 1 故宫博物院 景山公园"
    resolver = RecordingPlaceResolver()

    with pytest.raises(ValueError, match="confirmation spans"):
        await TripUnderstandingPipeline(
            TwoPlaceInferenceProvider(),
            resolver,
        ).run(source, requires_confirmation_spans=((0, len(source) + 1),))

    assert resolver.calls == []
