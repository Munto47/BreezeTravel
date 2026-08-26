from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.trip_intake.extraction import (
    HybridTripIntakeExtractor,
    SchemaConstrainedTripIntakeExtractor,
    UnavailableHybridTripIntakeExtractor,
)
from app.trip_intake.llm_client import (
    DeepSeekJsonClient,
    StructuredExtractionClientError,
    StructuredJsonReceipt,
    StructuredJsonResult,
)
from app.trip_intake.models import IntakeSource, IntakeSourceType, IntakeStatus
from app.trip_intake.runtime import build_trip_intake_extractor
from app.trip_intake.semantic import (
    TripIntakeSemanticDraft,
    compile_semantic_draft,
    trip_intake_semantic_prompt_schema,
)


def _source(text: str, source_id: str = "source-1") -> IntakeSource:
    return IntakeSource(
        source_id=source_id,
        source_type=IntakeSourceType.MANUAL_TEXT,
        text=text,
        text_sha256=sha256(text.encode("utf-8")).hexdigest(),
    )


def _result(payload: dict) -> StructuredJsonResult:
    return StructuredJsonResult(
        payload=payload,
        receipt=StructuredJsonReceipt(
            requested_model="deepseek-v4-flash",
            actual_model="DeepSeek-V4-Flash-0731",
            input_tokens=100,
            output_tokens=50,
            latency_ms=123.0,
            finish_reason="stop",
            system_fingerprint="fp-test",
        ),
    )


class StubStructuredClient:
    def __init__(self, result: StructuredJsonResult):
        self.result = result

    async def generate_json(self, **_kwargs):
        return self.result


class FailingStructuredClient:
    async def generate_json(self, **kwargs):
        receipt = StructuredJsonReceipt(
            requested_model=kwargs["model_name"],
            actual_model=None,
            input_tokens=0,
            output_tokens=0,
            latency_ms=4500,
            finish_reason=None,
            system_fingerprint=None,
        )
        raise StructuredExtractionClientError("timeout", receipt)


def test_semantic_compiler_resolves_unicode_and_repeated_quote() -> None:
    source = _source("去杭州玩😊三天，杭州不要太赶")
    draft = TripIntakeSemanticDraft.model_validate(
        {
            "locations": [
                {
                    "raw_text": "杭州",
                    "normalized_name": "杭州市",
                    "country_code": "CN",
                    "entity_type": "CITY",
                    "role": "EXCLUDED",
                    "evidence": [
                        {"source_id": "source-1", "quote": "杭州", "occurrence": 1}
                    ],
                }
            ],
            "location_status": "UNCERTAIN",
        }
    )

    extraction = compile_semantic_draft(draft, [source])

    span = extraction.locations.mentions[0].evidence[0]
    assert (span.start, span.end, span.quote) == (8, 10, "杭州")


def test_model_prompt_schema_is_compact_and_keeps_local_validation_separate() -> None:
    rendered = json.dumps(
        trip_intake_semantic_prompt_schema(),
        ensure_ascii=False,
        separators=(",", ":"),
    )

    assert len(rendered) < 5000
    assert '"title"' not in rendered
    assert TripIntakeSemanticDraft.model_json_schema()["title"] == "TripIntakeSemanticDraft"


def test_semantic_compiler_drops_invalid_field_without_inventing_offsets() -> None:
    source = _source("目的地还没定")
    draft = TripIntakeSemanticDraft.model_validate(
        {
            "locations": [
                {
                    "raw_text": "北京",
                    "role": "PRIMARY_DESTINATION",
                    "evidence": [{"source_id": "source-1", "quote": "北京"}],
                }
            ],
            "location_status": "EXACT",
            "primary_location_index": 0,
        }
    )

    extraction = compile_semantic_draft(draft, [source])

    assert extraction.locations.status.value == "MISSING"
    assert extraction.locations.mentions == []
    assert any(issue.code == "SEMANTIC_FIELD_DROPPED" for issue in extraction.issues)


@pytest.mark.asyncio
async def test_hybrid_preserves_model_range_instead_of_rule_suffix_number() -> None:
    source = _source("孩子6岁，玩3天，同行3到5人，这次目的地是杭州")
    payload = {
        "locations": [
            {
                "raw_text": "杭州",
                "normalized_name": "杭州",
                "country_code": None,
                "entity_type": "CITY",
                "role": "PRIMARY_DESTINATION",
                "evidence": [
                    {"source_id": "source-1", "quote": "这次目的地是杭州"}
                ],
            }
        ],
        "location_status": "EXACT",
        "primary_location_index": 0,
        "party_size": {
            "total": {
                "min": 3,
                "max": 5,
                "quantifier": "RANGE",
                "derivation": "EXPLICIT_COUNT",
                "evidence": [{"source_id": "source-1", "quote": "3到5人"}],
            },
            "composition": {
                "children": {
                    "min": 1,
                    "max": 1,
                    "quantifier": "EXACT",
                    "derivation": "EXPLICIT_COUNT",
                    "evidence": [{"source_id": "source-1", "quote": "孩子6岁"}],
                }
            },
        },
        "temporal": {
            "days": {
                "min": 3,
                "max": 3,
                "quantifier": "EXACT",
                "derivation": "EXPLICIT_COUNT",
                "evidence": [{"source_id": "source-1", "quote": "玩3天"}],
            }
        },
    }
    extractor = HybridTripIntakeExtractor(
        SchemaConstrainedTripIntakeExtractor(
            StubStructuredClient(_result(payload)),
            model_name="deepseek-v4-flash",
        )
    )

    outcome = await extractor.extract([source])

    assert outcome.status == IntakeStatus.NEEDS_CONFIRMATION
    assert outcome.extraction.party_size.total.quantifier.value == "RANGE"
    assert outcome.extraction.party_size.total.min == 3
    assert outcome.extraction.party_size.total.max == 5
    assert outcome.extraction.party_size.composition.children is None
    primary = outcome.extraction.locations.mentions[0]
    assert primary.normalized_name == "杭州市"
    assert primary.country_code == "CN"
    assert primary.evidence[0].quote == "杭州"
    assert [issue.code for issue in outcome.extraction.issues] == [
        "PRIMARY_CITY_CONFIRMATION_REQUIRED",
        "PARTY_SIZE_NEEDS_CONFIRMATION",
        "DATE_RANGE_MISSING_OR_INCOMPLETE",
    ]
    assert outcome.runtime_receipt is not None
    assert outcome.runtime_receipt.actual_model == "DeepSeek-V4-Flash-0731"


@pytest.mark.asyncio
async def test_hybrid_timeout_falls_back_and_keeps_failure_receipt() -> None:
    source = _source("2027年10月1日到10月7日去成都，12人")
    extractor = HybridTripIntakeExtractor(
        SchemaConstrainedTripIntakeExtractor(
            FailingStructuredClient(),
            model_name="deepseek-v4-flash",
        )
    )

    outcome = await extractor.extract([source])

    assert outcome.status == IntakeStatus.EXTRACTION_FAILED
    assert outcome.extraction.party_size.total.min == 12
    assert outcome.runtime_receipt is not None
    assert outcome.runtime_receipt.fallback_used is True
    assert outcome.runtime_receipt.error_category == "timeout"
    assert any(issue.code == "EXTRACTION_FAILED" for issue in outcome.extraction.issues)


def test_explicit_hybrid_without_key_is_fail_closed() -> None:
    settings = Settings(
        _env_file=None,
        trip_intake_extractor_mode="hybrid",
        deepseek_api_key="",
    )

    extractor = build_trip_intake_extractor(settings)

    assert isinstance(extractor, UnavailableHybridTripIntakeExtractor)


class RecordingCompletions:
    def __init__(self, content: str, *, finish_reason: str = "stop") -> None:
        self.content = content
        self.finish_reason = finish_reason
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            model="DeepSeek-V4-Flash-0731",
            system_fingerprint="fp-v4",
            usage=SimpleNamespace(prompt_tokens=21, completion_tokens=8),
            choices=[
                SimpleNamespace(
                    finish_reason=self.finish_reason,
                    message=SimpleNamespace(content=self.content),
                )
            ],
        )


@pytest.mark.asyncio
async def test_deepseek_client_uses_one_shot_non_thinking_json_mode() -> None:
    completions = RecordingCompletions(json.dumps({"locations": []}))
    sdk_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        timeout_seconds=4.5,
        max_output_tokens=4096,
        sdk_client=sdk_client,
    )

    result = await client.generate_json(
        system_prompt="返回 JSON",
        input_payload={"sources": []},
        json_schema={"type": "object"},
        model_name="deepseek-v4-flash",
        temperature=0,
    )

    assert result.payload == {"locations": []}
    assert result.receipt.input_tokens == 21
    assert completions.kwargs["response_format"] == {"type": "json_object"}
    assert completions.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert completions.kwargs["max_tokens"] == 4096
    assert "tools" not in completions.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "finish_reason", "category"),
    [("", "stop", "empty_output"), ("{}", "length", "truncated_output")],
)
async def test_deepseek_client_classifies_empty_and_truncated_output(
    content: str,
    finish_reason: str,
    category: str,
) -> None:
    completions = RecordingCompletions(content, finish_reason=finish_reason)
    sdk_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = DeepSeekJsonClient(
        api_key="test-key",
        base_url="https://api.deepseek.com/v1",
        sdk_client=sdk_client,
    )

    with pytest.raises(StructuredExtractionClientError) as captured:
        await client.generate_json(
            system_prompt="返回 JSON",
            input_payload={"sources": []},
            json_schema={"type": "object"},
            model_name="deepseek-v4-flash",
            temperature=0,
        )

    assert captured.value.category == category
