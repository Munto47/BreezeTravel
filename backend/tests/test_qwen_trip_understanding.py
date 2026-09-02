from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import DestinationBasis
from app.trip_understanding.qwen_provider import (
    QwenStructuredInferenceProvider,
    qwen_effective_run_config_sha256,
)


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
        self.closed = False
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _valid_output(source: str, *, basis: str = "EXPLICIT") -> str:
    city_start = source.index("北京")
    place_start = source.index("故宫博物院")
    destination = {
        "basis": basis,
        "evidence_span_start": city_start if basis == "EXPLICIT" else None,
        "evidence_span_end": city_start + 2 if basis == "EXPLICIT" else None,
    }
    if basis == "SOFT_ASSUMPTION":
        destination["name"] = "北京"
    return json.dumps(
        {
            "destination": destination,
            "mentions": [
                {
                    "span_start": place_start,
                    "span_end": place_start + len("故宫博物院"),
                    "role": "PLANNED",
                    "atomic_place_name": "故宫博物院",
                }
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_qwen_provider_does_not_close_an_injected_client() -> None:
    client = _FakeClient([])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    await provider.aclose()

    assert client.closed is False


@pytest.mark.asyncio
async def test_qwen_provider_closes_its_owned_client_once(monkeypatch) -> None:
    from app.trip_understanding import qwen_provider as qwen_module

    client = _FakeClient([])
    monkeypatch.setattr(qwen_module, "AsyncOpenAI", lambda **_kwargs: client)
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
    )

    await provider.aclose()
    await provider.aclose()

    assert client.closed is True
    assert client.close_calls == 1


@pytest.mark.asyncio
async def test_qwen_provider_serializes_concurrent_callers() -> None:
    source = "北京三日攻略。Day 1 去故宫博物院。"

    class _ConcurrencyProbe:
        def __init__(self) -> None:
            self.active = 0
            self.peak = 0

        async def create(self, **_kwargs):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return SimpleNamespace(
                    id="provider-request",
                    _request_id="http-request",
                    model="qwen-exact-snapshot",
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=_valid_output(source))
                        )
                    ],
                    usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
                )
            finally:
                self.active -= 1

    completions = _ConcurrencyProbe()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposals = await asyncio.gather(*(provider.propose(source) for _ in range(3)))

    assert len(proposals) == 3
    assert completions.peak == 1
    assert all(proposal.binding["max_concurrency"] == 1 for proposal in proposals)


def test_qwen_provider_rejects_non_serial_configuration() -> None:
    with pytest.raises(ValueError, match="max_concurrency must remain exactly 1"):
        QwenStructuredInferenceProvider(
            api_key="test-only",
            base_url="https://provider.example/v1",
            model="qwen-exact-snapshot",
            client=_FakeClient([]),
            max_concurrency=2,
        )


def test_qwen_effective_run_config_binds_serial_batch_contract() -> None:
    serial = qwen_effective_run_config_sha256(
        model_role="PRODUCTION_CANDIDATE",
        splits=["dev", "validation"],
        batch_concurrency=1,
        provider_effective_config_sha256="a" * 64,
    )
    parallel = qwen_effective_run_config_sha256(
        model_role="PRODUCTION_CANDIDATE",
        splits=["dev", "validation"],
        batch_concurrency=2,
        provider_effective_config_sha256="a" * 64,
    )

    assert len(serial) == 64
    assert serial != parallel


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
    assert proposal.binding["max_concurrency"] == 1
    assert proposal.binding["max_output_tokens"] == 768
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
async def test_qwen_provider_recovers_atomic_name_from_safe_exact_source_span() -> None:
    source = "北京 Day 1 上午去故宫博物院。"
    output = json.loads(_valid_output(source))
    output["mentions"][0]["atomic_place_name"] = "Forbidden Palace Museum"
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
    assert mention.atomic_place_name == "故宫博物院"
    assert proposal.binding["atomic_name_source_recovery_count"] == 1
    assert proposal.binding["repair_call_count"] == 0


@pytest.mark.asyncio
async def test_qwen_provider_does_not_recover_atomic_name_from_activity_clause() -> None:
    source = "北京 Day 1 上午去故宫博物院。"
    output = json.loads(_valid_output(source))
    output["mentions"][0]["span_start"] = source.index("上午")
    output["mentions"][0]["span_end"] = source.index("。")
    output["mentions"][0]["atomic_place_name"] = "Forbidden Palace Museum"
    invalid = json.dumps(output, ensure_ascii=False)
    client = _FakeClient([invalid, invalid])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    with pytest.raises(InferenceProviderUnavailableError) as raised:
        await provider.propose(source)

    assert raised.value.category == "SCHEMA_REPAIR_EXHAUSTED"
    assert len(client.chat.completions.calls) == 2


@pytest.mark.asyncio
async def test_qwen_provider_prefers_role_context_over_wrong_model_offset() -> None:
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

    assert proposal.mentions[0].span_start == source.index("故宫博物院")
    assert proposal.binding["atomic_span_disambiguation_count"] == 1
    assert proposal.binding["role_context_relocation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_does_not_relocate_planned_place_into_reference_name() -> None:
    source = (
        "上海。第2天安排豫园和田子坊。"
        "南翔馒头店（豫园店）只是从另一篇攻略里听说的参考项，不表示已经安排。"
    )
    city_start = source.index("上海")
    reference_name = "南翔馒头店（豫园店）"
    reference_start = source.index(reference_name)
    planned_start = source.index("豫园")
    nested_start = source.rindex("豫园")
    output = json.dumps(
        {
            "destination": {
                "basis": "EXPLICIT",
                "evidence_span_start": city_start,
                "evidence_span_end": city_start + 2,
            },
            "mentions": [
                {
                    "span_start": reference_start,
                    "span_end": reference_start + len(reference_name),
                    "role": "REFERENCE",
                    "atomic_place_name": reference_name,
                },
                {
                    "span_start": nested_start,
                    "span_end": nested_start + len("豫园"),
                    "role": "PLANNED",
                    "atomic_place_name": "豫园",
                },
            ],
        },
        ensure_ascii=False,
    )
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=_FakeClient([output]),
    )

    proposal = await provider.propose(source)

    planned = next(item for item in proposal.mentions if item.role.value == "PLANNED")
    assert planned.span_start == planned_start
    assert planned.raw_text == "豫园"
    assert proposal.binding["atomic_span_disambiguation_count"] == 1
    assert proposal.binding["role_context_relocation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_drops_meta_occurrences_and_corrects_conditional_option() -> None:
    source = (
        "上海主题是外滩。Day 1 去外滩。"
        "不要因为‘中国国家博物馆很有名’就自动加入。"
        "中国国家博物馆只是参考；如果太累，田子坊可以完全不去。"
    )
    first_shanghai = source.index("上海")
    first_bund = source.index("外滩")
    planned_bund = source.index("外滩", first_bund + 1)
    meta_reference = source.index("中国国家博物馆")
    actual_reference = source.index("中国国家博物馆", meta_reference + 1)
    optional = source.index("田子坊")

    def mention(
        start: int,
        value: str,
        role: str,
    ) -> dict[str, object]:
        return {
            "span_start": start,
            "span_end": start + len(value),
            "role": role,
            "atomic_place_name": value,
        }

    output = json.dumps(
        {
            "destination": {
                "basis": "EXPLICIT",
                "evidence_span_start": first_shanghai,
                "evidence_span_end": first_shanghai + 2,
            },
            "mentions": [
                mention(first_shanghai, "上海", "PLANNED"),
                mention(first_bund, "外滩", "PLANNED"),
                mention(planned_bund, "外滩", "PLANNED"),
                mention(meta_reference, "中国国家博物馆", "REFERENCE"),
                mention(actual_reference, "中国国家博物馆", "REFERENCE"),
                mention(optional, "田子坊", "EXCLUDED"),
            ],
        },
        ensure_ascii=False,
    )
    client = _FakeClient([output])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    assert [
        (item.raw_text, item.span_start, item.role.value)
        for item in proposal.mentions
    ] == [
        ("外滩", planned_bund, "PLANNED"),
        ("中国国家博物馆", actual_reference, "REFERENCE"),
        ("田子坊", optional, "OPTIONAL"),
    ]
    assert proposal.binding["non_activity_mention_drop_count"] == 3
    assert proposal.binding["conditional_optional_reclassification_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_derives_planned_day_from_source_heading() -> None:
    source = "北京 Day 1 休息。Day 2 上午去故宫博物院。"
    output = json.loads(_valid_output(source))
    client = _FakeClient([json.dumps(output, ensure_ascii=False)])
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=client,
    )

    proposal = await provider.propose(source)

    assert proposal.mentions[0].day_index == 2
    assert proposal.mentions[0].time_hint == "上午"
    assert proposal.binding["planned_day_fill_count"] == 1
    assert proposal.binding["time_hint_derivation_count"] == 1


@pytest.mark.asyncio
async def test_qwen_provider_derives_sequence_from_source_order() -> None:
    source = "北京 Day 1 先去故宫博物院，再去天坛。"
    output = json.loads(_valid_output(source))
    second_start = source.index("天坛")
    second = {
        **output["mentions"][0],
        "span_start": second_start,
        "span_end": second_start + 2,
        "atomic_place_name": "天坛",
    }
    output["mentions"] = [second, output["mentions"][0]]
    provider = QwenStructuredInferenceProvider(
        api_key="test-only",
        base_url="https://provider.example/v1",
        model="qwen-exact-snapshot",
        client=_FakeClient([json.dumps(output, ensure_ascii=False)]),
    )

    proposal = await provider.propose(source)

    assert [item.raw_text for item in proposal.mentions] == ["故宫博物院", "天坛"]
    assert [item.sequence_index for item in proposal.mentions] == [0, 1]


@pytest.mark.asyncio
async def test_qwen_provider_rejects_non_atomic_planned_span_after_one_repair() -> None:
    source = "北京 Day 1 预约说明：https://example.invalid。"
    start = source.index("预约说明")
    invalid = json.dumps(
        {
            "destination": {
                "basis": "EXPLICIT",
                "evidence_span_start": 0,
                "evidence_span_end": 2,
            },
            "mentions": [
                {
                    "span_start": start,
                    "span_end": start + len("预约说明"),
                    "role": "PLANNED",
                    "atomic_place_name": "预约说明",
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
    started_at = datetime.fromisoformat(
        str(binding["calls"][0]["started_at"]).replace("Z", "+00:00")
    )
    completed_at = datetime.fromisoformat(
        str(binding["calls"][0]["completed_at"]).replace("Z", "+00:00")
    )
    assert completed_at >= started_at
    assert binding["estimated_cost_cny"] is None
    assert binding["estimated_cost_status"] == (
        "NOT_EXPOSED_BY_PROVIDER_FOR_INCOMPLETE_CALL"
    )
    assert source not in json.dumps(binding, ensure_ascii=False)
