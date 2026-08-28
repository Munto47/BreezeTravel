from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import DestinationBasis
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider


class _FakeCompletions:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, object]] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.outputs.pop(0)
        return SimpleNamespace(
            id=f"provider-request-{len(self.calls)}",
            _request_id=f"http-request-{len(self.calls)}",
            model="qwen-exact-snapshot",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
        )


class _FakeClient:
    def __init__(self, outputs: list[str]) -> None:
        self.chat = SimpleNamespace(completions=_FakeCompletions(outputs))


def _valid_output(source: str, *, basis: str = "EXPLICIT") -> str:
    city_start = source.index("北京")
    place_start = source.index("故宫博物院")
    destination = {
        "name": "北京",
        "basis": basis,
        "evidence_span_start": city_start if basis == "EXPLICIT" else None,
        "evidence_span_end": city_start + 2 if basis == "EXPLICIT" else None,
    }
    return json.dumps(
        {
            "destination": destination,
            "mentions": [
                {
                    "span_start": place_start,
                    "span_end": place_start + len("故宫博物院"),
                    "role": "PLANNED",
                    "day_index": 1,
                    "sequence_index": 0,
                    "atomic_place_name": "故宫博物院",
                    "category_hint": "景点",
                    "time_hint": None,
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_qwen_provider_returns_model_neutral_proposal_and_redacted_receipt() -> None:
    source = "北京三日攻略。Day 1 去故宫博物院。"
    client = _FakeClient([_valid_output(source)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
        input_cny_per_million=1.0,
        output_cny_per_million=2.0,
    )

    proposal = await provider.propose(source)

    assert proposal.destination_name == "北京"
    assert proposal.destination_basis == DestinationBasis.EXPLICIT
    assert proposal.mentions[0].raw_text == "故宫博物院"
    assert proposal.binding["external_calls"] == 1
    assert proposal.binding["repair_call_count"] == 0
    assert proposal.binding["estimated_cost_status"] == (
        "CALCULATED_FROM_PROVIDER_FIELDS"
    )
    assert proposal.binding["calls"][0]["provider_reported_model"] == (
        "qwen-exact-snapshot"
    )
    assert proposal.binding["calls"][0]["provider_request_id_sha256"] != (
        "NOT_EXPOSED_BY_PROVIDER"
    )
    assert source not in json.dumps(proposal.binding, ensure_ascii=False)
    call = client.chat.completions.calls[0]
    assert call["temperature"] == 0.1
    assert call["extra_body"] == {"enable_thinking": False}
    assert call["response_format"]["type"] == "json_schema"
    assert call["response_format"]["json_schema"]["strict"] is True


@pytest.mark.asyncio
async def test_qwen_provider_uses_at_most_one_schema_repair() -> None:
    source = "北京 Day 1 去故宫博物院。"
    client = _FakeClient(["{}", _valid_output(source)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    assert len(client.chat.completions.calls) == 2
    assert proposal.binding["external_calls"] == 2
    assert proposal.binding["repair_call_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_deterministically_narrows_atomic_place_span() -> None:
    source = "北京 Day 1 上午去故宫博物院，下午休息。"
    output = json.loads(_valid_output(source))
    clause_start = source.index("上午去")
    output["mentions"][0]["span_start"] = clause_start
    output["mentions"][0]["span_end"] = source.index("，")
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    mention = proposal.mentions[0]
    assert mention.raw_text == "故宫博物院"
    assert source[mention.span_start : mention.span_end] == "故宫博物院"
    assert proposal.binding["atomic_span_narrowing_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_relocates_unique_verbatim_atomic_place() -> None:
    source = "北京 Day 1 上午去故宫博物院，下午休息。"
    output = json.loads(_valid_output(source))
    output["mentions"][0]["span_start"] = 0
    output["mentions"][0]["span_end"] = 2
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    mention = proposal.mentions[0]
    assert source[mention.span_start : mention.span_end] == "故宫博物院"
    assert proposal.binding["atomic_span_relocation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_ignores_duplicate_atomic_name_inside_url() -> None:
    source = (
        "北京 Day 1 上午去故宫博物院。"
        "说明见 https://example.invalid/booking?place=故宫博物院。"
    )
    output = json.loads(_valid_output(source))
    output["mentions"][0]["span_start"] = source.index("https://")
    output["mentions"][0]["span_end"] = source.index("https://") + len("https://")
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    mention = proposal.mentions[0]
    assert mention.span_start == source.index("故宫博物院")
    assert source[mention.span_start : mention.span_end] == "故宫博物院"
    assert proposal.binding["atomic_span_relocation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_recovers_atomic_place_from_empty_model_span() -> None:
    source = "北京 Day 1 上午去故宫博物院。"
    output = json.loads(_valid_output(source))
    output["mentions"][0]["span_start"] = 4
    output["mentions"][0]["span_end"] = 4
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    mention = proposal.mentions[0]
    assert source[mention.span_start : mention.span_end] == "故宫博物院"
    assert proposal.binding["atomic_span_relocation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_uses_model_offset_to_disambiguate_verbatim_place() -> None:
    source = "北京 Day 1 去故宫博物院。参考段再次提到故宫博物院。"
    output = json.loads(_valid_output(source))
    second_start = source.rindex("故宫博物院")
    output["mentions"][0]["span_start"] = second_start - 2
    output["mentions"][0]["span_end"] = second_start - 1
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    assert proposal.mentions[0].span_start == second_start
    assert proposal.binding["atomic_span_disambiguation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_fills_missing_planned_day_from_source_heading() -> None:
    source = "北京 Day 1 休息。Day 2 上午去故宫博物院。"
    output = json.loads(_valid_output(source))
    output["mentions"][0]["day_index"] = None
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    assert proposal.mentions[0].day_index == 2
    assert proposal.binding["planned_day_fill_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_rejects_non_atomic_planned_span_after_one_repair() -> None:
    source = "北京 Day 1 预约说明：https://example.invalid。"
    start = source.index("预约说明")
    invalid = json.dumps(
        {
            "destination": {
                "name": "北京",
                "basis": "EXPLICIT",
                "evidence_span_start": 0,
                "evidence_span_end": 2,
            },
            "mentions": [
                {
                    "span_start": start,
                    "span_end": start + len("预约说明"),
                    "role": "PLANNED",
                    "day_index": 1,
                    "sequence_index": 0,
                    "atomic_place_name": "预约说明",
                    "category_hint": None,
                    "time_hint": None,
                }
            ],
        },
        ensure_ascii=False,
    )
    client = _FakeClient([invalid, invalid])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    with pytest.raises(InferenceProviderUnavailableError) as raised:
        await provider.propose(source)

    assert getattr(raised.value, "category", None) == "SCHEMA_REPAIR_EXHAUSTED"
    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_qwen_provider_deadline_records_the_started_call_without_raw_text() -> None:
    source = "北京 Day 1 去故宫博物院。"

    class _SlowCompletions:
        async def create(self, **_kwargs):
            await asyncio.sleep(0.05)
            raise AssertionError("deadline should cancel the Provider call")

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=_SlowCompletions()),
    )
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
        deadline_seconds=0.01,
    )

    with pytest.raises(InferenceProviderUnavailableError) as raised:
        await provider.propose(source)

    assert raised.value.category == "DEADLINE_EXCEEDED"
    assert raised.value.external_call_count == 1
    binding = raised.value.provider_binding
    assert binding["external_calls"] == 1
    assert binding["calls"][0]["outcome"] == "NO_RESPONSE"
    assert len(binding["calls"][0]["request_sha256"]) == 64
    assert binding["estimated_cost_cny"] is None
    assert binding["estimated_cost_status"] == (
        "NOT_EXPOSED_BY_PROVIDER_FOR_INCOMPLETE_CALL"
    )
    assert source not in json.dumps(binding, ensure_ascii=False)
