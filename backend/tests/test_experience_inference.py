from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import (
    ExperienceQwenProvider, SemanticDraft, proposal_from_draft,
)
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline


class Client:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.outputs.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason="stop")],
            model="configured-model", usage=SimpleNamespace(prompt_tokens=200, completion_tokens=100),
        )


def provider(client):
    return ExperienceQwenProvider(
        api_key="unit-test", base_url="https://example.test/v1", model="configured-model",
        input_cny_per_million=1, output_cny_per_million=2, client=client,
    )


def draft(city, activities, **kwargs):
    return dict(destination=city, activities=activities, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("city,places", [
    ("北京", ["故宫博物院", "景山公园"]),
    ("上海", ["外滩", "豫园"]),
    ("杭州", ["西湖", "灵隐寺"]),
])
async def test_live_adapter_contract_preserves_dates_order_and_duration(city, places):
    # Injected model output checks integration, not model accuracy/live evidence.
    source = f"{city} 9月12日，10点到{places[0]}游览两小时，11点到{places[1]}。"
    payload = draft(city, [
        dict(source_quote=f"10点到{places[0]}游览两小时", place_name=places[0], role="PLANNED", day_index=1,
             start_time="10:00", visit_duration_minutes=120, time_evidence=f"10点到{places[0]}游览两小时"),
        dict(source_quote=f"11点到{places[1]}", place_name=places[1], role="PLANNED", day_index=1,
             start_time="11:00", time_evidence=f"11点到{places[1]}"),
    ], day_labels=["9月12日"])
    client = Client(json.dumps(payload, ensure_ascii=False))
    output = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(source)
    assert [m.compiled.mention.atomic_place_name for m in output.activities] == places
    assert output.public_result.days[0].label == "9月12日"
    assert output.activities[0].compiled.mention.visit_duration_minutes == 120
    assert output.activities[1].compiled.mention.start_time == "11:00"
    assert output.inference_binding["external_calls"] == 1
    assert output.inference_binding["estimated_cost_cny"] == 0.0004
    assert source not in json.dumps(output.inference_binding, ensure_ascii=False)


@pytest.mark.asyncio
async def test_model_order_is_not_rewritten_by_source_paragraph_order_or_local_cues():
    source = "北京：第二天去天坛公园。第一天故宫博物院没有取消；景山公园作为备选；取消颐和园。"
    payload = draft("北京", [
        dict(source_quote="故宫博物院没有取消", place_name="故宫博物院", role="PLANNED", day_index=1),
        dict(source_quote="天坛公园", place_name="天坛公园", role="PLANNED", day_index=2),
        dict(source_quote="景山公园作为备选", place_name="景山公园", role="OPTIONAL"),
        dict(source_quote="取消颐和园", place_name="颐和园", role="EXCLUDED"),
    ])
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(payload, ensure_ascii=False))),
                                             ControlledSnapshotPlaceResolver()).run(source)
    assert [len(day.activities) for day in output.public_result.days] == [1, 1]
    assert output.activities[0].compiled.mention.day_index == 1
    assert output.activities[1].compiled.mention.day_index == 2
    assert output.resolution_receipt["attempted_count"] == 2


@pytest.mark.asyncio
async def test_unprocessed_source_cannot_be_reported_as_complete():
    source = "北京故宫博物院，然后那个地方再看。"
    payload = draft("北京", [dict(source_quote="故宫博物院", place_name="故宫博物院", role="PLANNED", day_index=1)],
                    unprocessed_quotes=["然后那个地方再看"])
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(payload, ensure_ascii=False))),
                                             ControlledSnapshotPlaceResolver()).run(source)
    assert output.public_result.status == "PARTIAL_RESULT"
    assert output.resolution_receipt["unprocessed_count"] == 1


def test_unverbatim_model_place_is_rejected_instead_of_invented():
    with pytest.raises(ValueError, match="PLACE_NOT_IN_SOURCE_QUOTE"):
        proposal_from_draft("北京去故宫", SemanticDraft.model_validate(draft("北京", [
            dict(source_quote="去故宫", place_name="故宫博物院", role="PLANNED", day_index=1),
        ])))


def test_time_requires_a_source_anchor_and_does_not_default_morning_to_clock():
    value = dict(source_quote="上午去故宫", place_name="故宫", role="PLANNED", day_index=1)
    proposal = proposal_from_draft("北京上午去故宫", SemanticDraft.model_validate(draft("北京", [value])))
    assert proposal.mentions[0].start_time is None
    assert proposal.mentions[0].timing_source == "UNSPECIFIED"
    with pytest.raises(ValueError, match="TIME_EVIDENCE_NOT_IN_SOURCE"):
        proposal_from_draft("北京上午去故宫", SemanticDraft.model_validate(draft("北京", [dict(value, start_time="09:00")])) )


@pytest.mark.asyncio
async def test_invalid_model_response_gets_one_repair_then_redacted_failure():
    client = Client("{broken", "{still broken")
    with pytest.raises(InferenceProviderUnavailableError) as caught:
        await provider(client).propose("我的私人攻略 北京故宫")
    assert len(client.calls) == 2
    assert caught.value.provider_binding["fallback_used"] is False
    assert caught.value.external_call_count == 2
    assert "私人攻略" not in str(caught.value.provider_binding)
    assert "still broken" not in str(caught.value.provider_binding)


@pytest.mark.asyncio
async def test_transport_timeout_does_not_fall_back_to_example_or_rules():
    client = Client()
    async def timeout(**_kwargs):
        raise TimeoutError()
    client.chat = SimpleNamespace(completions=SimpleNamespace(create=timeout))
    with pytest.raises(InferenceProviderUnavailableError) as caught:
        await provider(client).propose("北京故宫")
    assert caught.value.category == "DEADLINE_EXCEEDED"
    assert caught.value.provider_binding["input_tokens"] is None
    assert caught.value.provider_binding["estimated_cost_cny"] is None
