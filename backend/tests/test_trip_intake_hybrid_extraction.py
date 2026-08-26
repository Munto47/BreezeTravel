from __future__ import annotations

import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.trip_intake.extraction import (
    DeterministicTripIntakeExtractor,
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
    normalize_semantic_payload,
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


def test_semantic_payload_normalizes_only_audited_enum_aliases() -> None:
    payload = {
        "location_status": "AMBIGUOUS",
        "preferences": {
            "status": "UNSPECIFIED",
            "items": [
                {
                    "category": "food",
                    "label": "本帮菜",
                    "polarity": "PREFER",
                    "operator": "PREFER",
                    "evidence": [],
                },
                {
                    "category": "pace",
                    "label": "不要太赶",
                    "polarity": "AVOID",
                    "operator": "AVOID",
                    "evidence": [],
                },
            ],
        },
    }

    normalized = normalize_semantic_payload(payload)

    assert normalized["location_status"] == "UNCERTAIN"
    assert normalized["preferences"]["items"][0]["polarity"] == "LIKE"
    assert normalized["preferences"]["items"][0]["operator"] is None
    assert normalized["preferences"]["items"][1]["polarity"] == "DISLIKE"
    assert normalized["preferences"]["items"][1]["operator"] is None
    assert payload["location_status"] == "AMBIGUOUS"


def test_semantic_payload_does_not_normalize_unapproved_aliases() -> None:
    normalized = normalize_semantic_payload(
        {
            "location_status": "MAYBE",
            "preferences": {"items": []},
        }
    )

    with pytest.raises(ValueError):
        TripIntakeSemanticDraft.model_validate(normalized)


def test_semantic_payload_canonicalizes_explicit_preference_phrases() -> None:
    normalized = normalize_semantic_payload(
        {
            "preferences": {
                "items": [
                    {
                        "category": "SCENERY",
                        "label": "喜欢自然风景",
                        "polarity": "LIKE",
                        "evidence": [{"source_id": "source-1", "quote": "喜欢自然风景"}],
                    },
                    {
                        "category": "BUDGET",
                        "label": "预算",
                        "polarity": "REQUIREMENT",
                        "operator": "MAX",
                        "value": 2000,
                        "unit": "CNY",
                        "evidence": [
                            {"source_id": "source-1", "quote": "总预算不超过2000元"}
                        ],
                    },
                ]
            }
        }
    )

    like, budget = normalized["preferences"]["items"]
    assert (like["category"], like["label"]) == ("experience", "自然风景")
    assert budget == {
        "category": "budget",
        "label": "总预算",
        "polarity": "REQUIREMENT",
        "operator": "MAX",
        "value": 2000,
        "unit": "元",
        "currency": "CNY",
        "applies_to": None,
        "evidence": [{"source_id": "source-1", "quote": "总预算不超过2000元"}],
    }


@pytest.mark.asyncio
async def test_deterministic_rules_preserve_explicit_unknown_evidence_and_tags() -> None:
    source = _source(
        "去北京；人数还没定，可能有人临时加入；时间还没定，有空就多待几天"
    )

    outcome = await DeterministicTripIntakeExtractor().extract([source])

    assert outcome.extraction.party_size.total.quantifier.value == "UNKNOWN"
    assert outcome.extraction.party_size.total.evidence[0].quote == "人数还没定，可能有人临时加入"
    assert outcome.extraction.party_size.composition.tags == ["同行人员尚未确定"]
    assert outcome.extraction.temporal.days.quantifier.value == "UNKNOWN"
    assert outcome.extraction.temporal.days.evidence[0].quote == "时间还没定，有空就多待几天"


@pytest.mark.asyncio
async def test_deterministic_rules_parse_ranges_and_inclusive_date_duration() -> None:
    range_outcome = await DeterministicTripIntakeExtractor().extract(
        [_source("去杭州，4到6人，最多待4天")]
    )
    date_outcome = await DeterministicTripIntakeExtractor().extract(
        [_source("去上海，我自己，10月3日到10月5日")]
    )

    assert range_outcome.extraction.party_size.total.model_dump(exclude={"evidence"}) == {
        "min": 4,
        "max": 6,
        "quantifier": "RANGE",
        "derivation": "EXPLICIT_COUNT",
    }
    assert range_outcome.extraction.temporal.days.max == 4
    assert range_outcome.extraction.temporal.days.quantifier.value == "AT_MOST"
    assert date_outcome.extraction.party_size.total.min == 1
    assert date_outcome.extraction.party_size.composition.tags == ["独自"]
    assert date_outcome.extraction.temporal.days.min == 3
    assert date_outcome.extraction.temporal.days.derivation.value == "DATE_RANGE"


@pytest.mark.asyncio
async def test_hybrid_keeps_more_complete_evidence_for_equal_unknown_values() -> None:
    source = _source("去北京；人数还没定，可能有人临时加入；时间还没定，有空就多待几天")
    payload = {
        "party_size": {
            "total": {
                "quantifier": "UNKNOWN",
                "derivation": "MISSING",
                "evidence": [{"source_id": "source-1", "quote": "人数还没定"}],
            }
        },
        "temporal": {
            "days": {
                "quantifier": "UNKNOWN",
                "derivation": "MISSING",
                "evidence": [{"source_id": "source-1", "quote": "时间还没定"}],
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

    assert outcome.extraction.party_size.total.evidence[0].quote == (
        "人数还没定，可能有人临时加入"
    )
    assert outcome.extraction.temporal.days.evidence[0].quote == (
        "时间还没定，有空就多待几天"
    )


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


@pytest.mark.asyncio
async def test_schema_error_keeps_usage_and_only_reports_safe_field_path() -> None:
    source = _source("目的地还没定")
    extractor = SchemaConstrainedTripIntakeExtractor(
        StubStructuredClient(_result({"unexpected": "must not be echoed"})),
        model_name="deepseek-v4-flash",
    )

    outcome = await extractor.extract([source])

    assert outcome.status == IntakeStatus.EXTRACTION_FAILED
    assert outcome.runtime_receipt is not None
    assert outcome.runtime_receipt.input_tokens == 100
    assert outcome.runtime_receipt.output_tokens == 50
    assert outcome.runtime_receipt.error_category == "schema_invalid"
    assert outcome.runtime_receipt.error_detail == "unexpected:extra_forbidden"
    assert "must not be echoed" not in outcome.runtime_receipt.error_detail


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
