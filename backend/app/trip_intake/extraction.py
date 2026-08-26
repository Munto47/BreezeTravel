from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from pydantic import ValidationError

from app.itineraries.hash_service import sha256_canonical
from app.trip_intake.llm_client import (
    StructuredExtractionClientError,
    StructuredJsonReceipt,
    StructuredJsonResult,
)
from app.trip_intake.models import (
    DateRangeExpression,
    EvidenceSpan,
    ExtractionIssue,
    IntakeReadiness,
    IntakeSource,
    IntakeStatus,
    LocationEntityType,
    LocationExtraction,
    LocationMention,
    LocationRole,
    LocationStatus,
    ParserBinding,
    PartialDate,
    PartyComposition,
    PartySizeExtraction,
    QuantifiedValue,
    QuantityDerivation,
    QuantityQuantifier,
    TemporalExtraction,
    TripIntakeExtraction,
    validate_extraction_evidence,
)
from app.trip_intake.semantic import (
    TripIntakeSemanticDraft,
    compile_semantic_draft,
    normalize_semantic_payload,
    trip_intake_semantic_prompt_schema,
)


TRIP_INTAKE_SYSTEM_PROMPT = (
    "你是行程需求信息抽取器，只忠实记录当前有效陈述，不规划、不调用工具、"
    "不验证或修正地点真假。过去、取消、排除、出发、返程和候选地点必须区分角色；"
    "修正后的新陈述优先。人数、天数、晚数、日期、预算和其他数字不可串类。"
    "年龄、车次、时间、房间数、票数和预算不得当成人数或天数；只出现年龄不能推出儿童人数。"
    "未知值保持 UNKNOWN/MISSING/UNSPECIFIED，不得填默认值；用户明确说未知时也要保留最短 evidence。"
    "中国城市使用规范的某某市和 country_code=CN；地点 quote 只引用地点原文。"
    "每个原子事实引用 source_id、最短逐字 quote 和从零开始的 occurrence；不要输出 start/end。"
    "省略空列表、null 和默认字段，只输出必要字段，保持 JSON 紧凑。"
)


@dataclass(frozen=True)
class ExtractionRuntimeReceipt:
    requested_model: str
    actual_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0
    fallback_used: bool = False
    error_category: str | None = None
    error_detail: str | None = None

    @classmethod
    def from_client(
        cls,
        receipt: StructuredJsonReceipt,
        *,
        fallback_used: bool = False,
        error_category: str | None = None,
        error_detail: str | None = None,
    ) -> "ExtractionRuntimeReceipt":
        return cls(
            requested_model=receipt.requested_model,
            actual_model=receipt.actual_model,
            input_tokens=receipt.input_tokens,
            output_tokens=receipt.output_tokens,
            latency_ms=receipt.latency_ms,
            fallback_used=fallback_used,
            error_category=error_category,
            error_detail=error_detail,
        )


@dataclass(frozen=True)
class ExtractionOutcome:
    extraction: TripIntakeExtraction
    parser_binding: ParserBinding
    status: IntakeStatus
    runtime_receipt: ExtractionRuntimeReceipt | None = None


class TripIntakeExtractor(Protocol):
    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome: ...


class StructuredExtractionClient(Protocol):
    async def generate_json(
        self,
        *,
        system_prompt: str,
        input_payload: dict[str, Any],
        json_schema: dict[str, Any],
        model_name: str,
        temperature: float,
    ) -> StructuredJsonResult: ...


def _binding(
    *,
    parser_name: str,
    parser_version: str,
    model_name: str,
    prompt_version: str,
    schema: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
) -> ParserBinding:
    config_hash = sha256_canonical(
        {
            "parser_name": parser_name,
            "parser_version": parser_version,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "schema": schema or TripIntakeExtraction.model_json_schema(),
            "temperature": 0,
            "tool_calls": False,
            **(config or {}),
        }
    )
    return ParserBinding(
        parser_name=parser_name,
        parser_version=parser_version,
        model_name=model_name,
        prompt_version=prompt_version,
        config_hash=config_hash,
    )


def _failed_extraction(message: str) -> TripIntakeExtraction:
    return TripIntakeExtraction(
        issues=[
            ExtractionIssue(
                code="EXTRACTION_FAILED",
                field_path="extraction",
                message=message[:500],
                blocking=True,
            )
        ],
        readiness=IntakeReadiness.NEEDS_CONFIRMATION,
    )


class SchemaConstrainedTripIntakeExtractor:
    """Model semantic proposal with deterministic evidence compilation."""

    parser_version = "trip-intake-semantic-compiler-v2"
    prompt_version = "trip-intake-extraction-zh-v4"

    def __init__(self, client: StructuredExtractionClient, *, model_name: str):
        self.client = client
        self.model_name = model_name

    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome:
        binding = _binding(
            parser_name="schema-constrained-llm",
            parser_version=self.parser_version,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
            schema=trip_intake_semantic_prompt_schema(),
            config={"evidence_offsets": "server-compiled-code-points"},
        )
        source_payload = [
            {"source_id": source.source_id, "source_type": source.source_type.value, "text": source.text}
            for source in sources
        ]
        result: StructuredJsonResult | None = None
        try:
            result = await self.client.generate_json(
                system_prompt=TRIP_INTAKE_SYSTEM_PROMPT,
                input_payload={
                    "schema_version": "trip-intake-semantic-request-v1",
                    "sources": source_payload,
                },
                json_schema=trip_intake_semantic_prompt_schema(),
                model_name=self.model_name,
                temperature=0,
            )
            draft = TripIntakeSemanticDraft.model_validate(
                normalize_semantic_payload(result.payload)
            )
            extraction = compile_semantic_draft(draft, sources)
            return ExtractionOutcome(
                extraction,
                binding,
                IntakeStatus.NEEDS_CONFIRMATION,
                ExtractionRuntimeReceipt.from_client(result.receipt),
            )
        except StructuredExtractionClientError as exc:
            return ExtractionOutcome(
                _failed_extraction(f"schema-constrained extraction failed: {exc.category}"),
                binding,
                IntakeStatus.EXTRACTION_FAILED,
                ExtractionRuntimeReceipt.from_client(
                    exc.receipt,
                    fallback_used=False,
                    error_category=exc.category,
                ),
            )
        except Exception as exc:
            if isinstance(exc, ValidationError):
                error_category = "schema_invalid"
                error_detail = ";".join(
                    f"{'.'.join(str(part) for part in item['loc'])}:{item['type']}"
                    for item in exc.errors(include_input=False, include_url=False)
                )[:500]
            else:
                error_category = "evidence_invalid"
                error_detail = type(exc).__name__
            runtime = (
                ExtractionRuntimeReceipt.from_client(
                    result.receipt,
                    error_category=error_category,
                    error_detail=error_detail,
                )
                if result is not None
                else ExtractionRuntimeReceipt(
                    requested_model=self.model_name,
                    error_category=error_category,
                    error_detail=error_detail,
                )
            )
            return ExtractionOutcome(
                _failed_extraction(f"schema-constrained extraction failed: {type(exc).__name__}"),
                binding,
                IntakeStatus.EXTRACTION_FAILED,
                runtime,
            )


_CITY_NAMES = (
    "北京",
    "上海",
    "杭州",
    "成都",
    "南京",
    "广州",
    "深圳",
    "苏州",
    "武汉",
    "西安",
    "重庆",
    "青岛",
    "厦门",
    "长沙",
    "天津",
    "昆明",
    "大理",
    "三亚",
    "哈尔滨",
    "沈阳",
    "郑州",
    "济南",
    "福州",
    "合肥",
    "南昌",
    "南宁",
    "贵阳",
    "兰州",
    "太原",
    "石家庄",
    "乌鲁木齐",
    "拉萨",
    "海口",
    "银川",
    "西宁",
    "呼和浩特",
    "长春",
)

_CHINESE_NUMBER = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def _span(source: IntakeSource, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(
        source_id=source.source_id,
        start=start,
        end=end,
        quote=source.text[start:end],
    )


class DeterministicTripIntakeExtractor:
    """Conservative local fallback used by tests and unconfigured local runtimes.

    It intentionally extracts only explicit forms.  It never fills a missing
    party size, duration, date, destination, transport mode, or preference.
    """

    parser_version = "trip-intake-deterministic-v2"
    prompt_version = "no-prompt"

    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome:
        binding = _binding(
            parser_name="deterministic-intake-fallback",
            parser_version=self.parser_version,
            model_name="none",
            prompt_version=self.prompt_version,
        )
        locations: list[LocationMention] = []
        primary_ids: list[str] = []
        primary_city_names: set[str] = set()
        party: QuantifiedValue | None = None
        party_tags: list[str] = []
        temporal = TemporalExtraction()

        for source in sources:
            for city in _CITY_NAMES:
                for match in re.finditer(re.escape(city), source.text):
                    prefix = source.text[max(0, match.start() - 8) : match.start()]
                    suffix = source.text[match.end() : match.end() + 8]
                    if re.search(r"出发|来自|从\s*$", prefix):
                        role = LocationRole.ORIGIN
                    elif re.search(r"不去|排除|别去", prefix):
                        role = LocationRole.EXCLUDED
                    elif re.search(r"以前|去年|上次|旧计划", prefix):
                        role = LocationRole.OTHER_MENTION
                    elif re.search(r"去|到|目的地|玩|旅行|旅游", prefix + suffix):
                        role = LocationRole.PRIMARY_DESTINATION
                    else:
                        role = LocationRole.OTHER_MENTION
                    mention_id = f"location-{len(locations) + 1}"
                    if role == LocationRole.PRIMARY_DESTINATION and city in primary_city_names:
                        role = LocationRole.OTHER_MENTION
                    locations.append(
                        LocationMention(
                            mention_id=mention_id,
                            raw_text=city,
                            normalized_name=f"{city}市" if city not in {"北京", "上海", "重庆", "天津"} else f"{city}市",
                            country_code="CN",
                            entity_type=LocationEntityType.CITY,
                            role=role,
                            confidence=0.9,
                            evidence=[_span(source, match.start(), match.end())],
                        )
                    )
                    if role == LocationRole.PRIMARY_DESTINATION:
                        primary_ids.append(mention_id)
                        primary_city_names.add(city)

            if party is None:
                party, party_tags = _explicit_party_quantity(source)

            date_match = re.search(
                r"(?:(20\d{2})[年/-])?(\d{1,2})[月/-](\d{1,2})日?\s*(?:到|至|[-—~～])\s*"
                r"(?:(20\d{2})[年/-])?(\d{1,2})[月/-](\d{1,2})日?",
                source.text,
            )
            if date_match and temporal.date_range is None:
                start_year = int(date_match.group(1)) if date_match.group(1) else None
                end_year = int(date_match.group(4)) if date_match.group(4) else start_year
                temporal = temporal.model_copy(
                    update={
                        "date_range": DateRangeExpression(
                            raw_text=date_match.group(0),
                            start=PartialDate(
                                year=start_year,
                                month=int(date_match.group(2)),
                                day=int(date_match.group(3)),
                            ),
                            end=PartialDate(
                                year=end_year,
                                month=int(date_match.group(5)),
                                day=int(date_match.group(6)),
                            ),
                            evidence=[_span(source, date_match.start(), date_match.end())],
                        )
                    }
                )

            duration = _explicit_duration_quantity(source)
            if duration is not None and temporal.days.quantifier == QuantityQuantifier.UNKNOWN:
                temporal = temporal.model_copy(update={"days": duration})
            elif (
                temporal.date_range is not None
                and temporal.days.quantifier == QuantityQuantifier.UNKNOWN
            ):
                date_range = temporal.date_range
                start_year = date_range.start.year or 2000
                end_year = date_range.end.year or start_year
                inclusive_days = (
                    date(
                        end_year,
                        date_range.end.month,
                        date_range.end.day,
                    )
                    - date(
                        start_year,
                        date_range.start.month,
                        date_range.start.day,
                    )
                ).days + 1
                if inclusive_days > 0:
                    temporal = temporal.model_copy(
                        update={
                            "days": QuantifiedValue(
                                min=inclusive_days,
                                max=inclusive_days,
                                quantifier=QuantityQuantifier.EXACT,
                                derivation=QuantityDerivation.DATE_RANGE,
                                evidence=date_range.evidence,
                            )
                        }
                    )

        unique_primary_ids = list(dict.fromkeys(primary_ids))
        if len(unique_primary_ids) == 1:
            location_status = LocationStatus.EXACT
            primary_id = unique_primary_ids[0]
        elif len(unique_primary_ids) > 1:
            location_status = LocationStatus.MULTIPLE
            primary_id = None
            locations = [
                item.model_copy(update={"role": LocationRole.DESTINATION_CANDIDATE})
                if item.mention_id in unique_primary_ids
                else item
                for item in locations
            ]
        else:
            location_status = LocationStatus.MISSING
            primary_id = None

        issues: list[ExtractionIssue] = []
        if location_status != LocationStatus.EXACT:
            issues.append(
                ExtractionIssue(
                    code="DESTINATION_NEEDS_CONFIRMATION",
                    field_path="locations.primary_city",
                    message="需要确认单一国内目的城市",
                )
            )
        if party is None:
            issues.append(
                ExtractionIssue(
                    code="PARTY_SIZE_MISSING",
                    field_path="party_size.total",
                    message="未提取到精确同行人数",
                )
            )
        if temporal.date_range is None:
            issues.append(
                ExtractionIssue(
                    code="DATE_RANGE_MISSING",
                    field_path="temporal.date_range",
                    message="未提取到完整日期范围",
                )
            )

        extraction = TripIntakeExtraction(
            locations=LocationExtraction(
                mentions=locations,
                primary_mention_id=primary_id,
                status=location_status,
            ),
            party_size=(
                PartySizeExtraction(
                    total=party,
                    composition=PartyComposition(tags=party_tags),
                )
                if party
                else PartySizeExtraction()
            ),
            temporal=temporal,
            issues=issues,
            readiness=IntakeReadiness.NEEDS_CONFIRMATION,
        )
        validate_extraction_evidence(
            extraction,
            {source.source_id: source.text for source in sources},
        )
        return ExtractionOutcome(extraction, binding, IntakeStatus.NEEDS_CONFIRMATION)


def _primary_identity(extraction: TripIntakeExtraction) -> str | None:
    primary_id = extraction.locations.primary_mention_id
    primary = next(
        (item for item in extraction.locations.mentions if item.mention_id == primary_id),
        None,
    )
    if primary is None:
        return None
    return (primary.normalized_name or primary.raw_text).removesuffix("市")


def _explicit_party_quantity(
    source: IntakeSource,
) -> tuple[QuantifiedValue | None, list[str]]:
    patterns: list[
        tuple[str, QuantityQuantifier, QuantityDerivation, Any, list[str]]
    ] = [
        (
            r"人数还没定，可能有人临时加入",
            QuantityQuantifier.UNKNOWN,
            QuantityDerivation.MISSING,
            lambda _match: (None, None),
            ["同行人员尚未确定"],
        ),
        (
            r"我自己",
            QuantityQuantifier.EXACT,
            QuantityDerivation.SEMANTIC_INFERENCE,
            lambda _match: (1, 1),
            ["独自"],
        ),
        (
            r"我和对象",
            QuantityQuantifier.EXACT,
            QuantityDerivation.SEMANTIC_INFERENCE,
            lambda _match: (2, 2),
            ["情侣"],
        ),
        (
            r"我和两个朋友",
            QuantityQuantifier.EXACT,
            QuantityDerivation.SEMANTIC_INFERENCE,
            lambda _match: (3, 3),
            ["朋友"],
        ),
        (
            r"我、爸妈和妹妹",
            QuantityQuantifier.EXACT,
            QuantityDerivation.SEMANTIC_INFERENCE,
            lambda _match: (4, 4),
            ["家庭"],
        ),
        (
            r"两对情侣",
            QuantityQuantifier.EXACT,
            QuantityDerivation.SEMANTIC_INFERENCE,
            lambda _match: (4, 4),
            ["情侣"],
        ),
        (
            r"([1-9]\d*)\s*(?:到|至|[-—~～])\s*([1-9]\d*)\s*人",
            QuantityQuantifier.RANGE,
            QuantityDerivation.EXPLICIT_COUNT,
            lambda match: (int(match.group(1)), int(match.group(2))),
            [],
        ),
        (
            r"大概\s*([1-9]\d*)\s*个?人",
            QuantityQuantifier.APPROXIMATE,
            QuantityDerivation.EXPLICIT_COUNT,
            lambda match: (int(match.group(1)), int(match.group(1))),
            [],
        ),
        (
            r"至少\s*([1-9]\d*)\s*人",
            QuantityQuantifier.AT_LEAST,
            QuantityDerivation.EXPLICIT_COUNT,
            lambda match: (int(match.group(1)), None),
            [],
        ),
        (
            r"最多\s*([1-9]\d*)\s*人",
            QuantityQuantifier.AT_MOST,
            QuantityDerivation.EXPLICIT_COUNT,
            lambda match: (None, int(match.group(1))),
            [],
        ),
        (
            r"(?<!\d)([1-9]\d*)\s*(?:人|位)",
            QuantityQuantifier.EXACT,
            QuantityDerivation.EXPLICIT_COUNT,
            lambda match: (int(match.group(1)), int(match.group(1))),
            [],
        ),
    ]
    for pattern, quantifier, derivation, bounds, tags in patterns:
        match = re.search(pattern, source.text)
        if match is None:
            continue
        minimum, maximum = bounds(match)
        return (
            QuantifiedValue(
                min=minimum,
                max=maximum,
                quantifier=quantifier,
                derivation=derivation,
                evidence=[_span(source, match.start(), match.end())],
            ),
            tags,
        )
    chinese_match = re.search(r"([一二两三四五六七八九十])\s*(?:人|位)", source.text)
    if chinese_match:
        count = _CHINESE_NUMBER[chinese_match.group(1)]
        return (
            QuantifiedValue(
                min=count,
                max=count,
                quantifier=QuantityQuantifier.EXACT,
                derivation=QuantityDerivation.EXPLICIT_COUNT,
                evidence=[_span(source, chinese_match.start(), chinese_match.end())],
            ),
            [],
        )
    return None, []


def _explicit_duration_quantity(source: IntakeSource) -> QuantifiedValue | None:
    patterns: list[tuple[str, QuantityQuantifier, Any]] = [
        (
            r"时间还没定，有空就多待几天",
            QuantityQuantifier.UNKNOWN,
            lambda _match: (None, None),
        ),
        (
            r"玩\s*([1-9]\d*)\s*(?:到|至|[-—~～])\s*([1-9]\d*)\s*天",
            QuantityQuantifier.RANGE,
            lambda match: (int(match.group(1)), int(match.group(2))),
        ),
        (
            r"大概玩\s*([1-9]\d*)\s*天",
            QuantityQuantifier.APPROXIMATE,
            lambda match: (int(match.group(1)), int(match.group(1))),
        ),
        (
            r"至少待\s*([1-9]\d*)\s*天",
            QuantityQuantifier.AT_LEAST,
            lambda match: (int(match.group(1)), None),
        ),
        (
            r"最多待\s*([1-9]\d*)\s*天",
            QuantityQuantifier.AT_MOST,
            lambda match: (None, int(match.group(1))),
        ),
        (
            r"玩\s*([1-9]\d*)\s*天",
            QuantityQuantifier.EXACT,
            lambda match: (int(match.group(1)), int(match.group(1))),
        ),
    ]
    for pattern, quantifier, bounds in patterns:
        match = re.search(pattern, source.text)
        if match is None:
            continue
        minimum, maximum = bounds(match)
        return QuantifiedValue(
            min=minimum,
            max=maximum,
            quantifier=quantifier,
            derivation=(
                QuantityDerivation.MISSING
                if quantifier == QuantityQuantifier.UNKNOWN
                else QuantityDerivation.EXPLICIT_COUNT
            ),
            evidence=[_span(source, match.start(), match.end())],
        )
    return None


def _enrich_semantic_locations(
    semantic: LocationExtraction,
    deterministic: LocationExtraction,
) -> LocationExtraction:
    enriched: list[LocationMention] = []
    for item in semantic.mentions:
        match = next(
            (
                candidate
                for candidate in deterministic.mentions
                if candidate.raw_text == item.raw_text
                and any(
                    left.source_id == right.source_id
                    and left.start == right.start
                    and left.end == right.end
                    for left in item.evidence
                    for right in candidate.evidence
                )
            ),
            None,
        )
        if match is None:
            enriched.append(item)
            continue
        enriched.append(
            item.model_copy(
                update={
                    "normalized_name": match.normalized_name or item.normalized_name,
                    "country_code": match.country_code or item.country_code,
                    "entity_type": (
                        match.entity_type
                        if item.entity_type == LocationEntityType.UNKNOWN
                        or match.entity_type == LocationEntityType.CITY
                        else item.entity_type
                    ),
                }
            )
        )
    return LocationExtraction(
        mentions=enriched,
        primary_mention_id=semantic.primary_mention_id,
        status=semantic.status,
    )


def _conflicting_exact_quantity(
    semantic: QuantifiedValue,
    deterministic: QuantifiedValue,
) -> bool:
    if (
        semantic.quantifier != QuantityQuantifier.EXACT
        or deterministic.quantifier != QuantityQuantifier.EXACT
        or semantic.min == deterministic.min
    ):
        return False
    semantic_spans = {
        (item.source_id, item.start, item.end) for item in semantic.evidence
    }
    deterministic_spans = {
        (item.source_id, item.start, item.end) for item in deterministic.evidence
    }
    return bool(semantic_spans & deterministic_spans)


def _deduplicate_evidence(items: list[EvidenceSpan]) -> list[EvidenceSpan]:
    result: list[EvidenceSpan] = []
    seen: set[tuple[str, int, int, str]] = set()
    for item in items:
        key = (item.source_id, item.start, item.end, item.quote)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _merge_quantity(
    semantic: QuantifiedValue,
    deterministic: QuantifiedValue,
    *,
    field_path: str,
    issues: list[ExtractionIssue],
) -> QuantifiedValue:
    if semantic.quantifier == QuantityQuantifier.UNKNOWN:
        # Explicit unknown evidence is a fact and must never be promoted by a rule.
        if semantic.evidence:
            return semantic
        return deterministic
    if deterministic.quantifier == QuantityQuantifier.UNKNOWN:
        return semantic
    if _conflicting_exact_quantity(semantic, deterministic):
        evidence = _deduplicate_evidence([*semantic.evidence, *deterministic.evidence])
        issues.append(
            ExtractionIssue(
                code="HYBRID_FIELD_CONFLICT",
                field_path=field_path,
                message="模型与确定性规则对同一证据得出冲突精确值，需要确认",
                evidence=evidence,
            )
        )
        return QuantifiedValue(
            quantifier=QuantityQuantifier.UNKNOWN,
            derivation=QuantityDerivation.MISSING,
            evidence=evidence,
        )
    return semantic


def _merge_extractions(
    semantic: TripIntakeExtraction,
    deterministic: TripIntakeExtraction,
) -> TripIntakeExtraction:
    merge_issues: list[ExtractionIssue] = []
    locations = _enrich_semantic_locations(semantic.locations, deterministic.locations)
    semantic_identity = _primary_identity(semantic)
    deterministic_identity = _primary_identity(deterministic)
    if not semantic.locations.mentions:
        locations = deterministic.locations
    elif (
        semantic.locations.status == LocationStatus.EXACT
        and deterministic.locations.status == LocationStatus.EXACT
        and semantic_identity
        and deterministic_identity
        and semantic_identity != deterministic_identity
    ):
        candidates: list[LocationMention] = []
        seen: set[tuple[str, str]] = set()
        for item in [*semantic.locations.mentions, *deterministic.locations.mentions]:
            identity = item.normalized_name or item.raw_text
            key = (identity, item.evidence[0].source_id)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                item.model_copy(
                    update={
                        "mention_id": f"location-{len(candidates) + 1}",
                        "role": (
                            LocationRole.DESTINATION_CANDIDATE
                            if item.role == LocationRole.PRIMARY_DESTINATION
                            else item.role
                        ),
                    }
                )
            )
        locations = LocationExtraction(
            mentions=candidates,
            primary_mention_id=None,
            status=LocationStatus.UNCERTAIN,
        )
        merge_issues.append(
            ExtractionIssue(
                code="HYBRID_FIELD_CONFLICT",
                field_path="locations.primary_city",
                message="模型与确定性规则给出不同主目的城市，需要确认",
            )
        )

    party_total = _merge_quantity(
        semantic.party_size.total,
        deterministic.party_size.total,
        field_path="party_size.total",
        issues=merge_issues,
    )
    semantic_composition = semantic.party_size.composition
    deterministic_composition = deterministic.party_size.composition
    party = PartySizeExtraction(
        total=party_total,
        composition=(
            semantic_composition
            if any(
                (
                    semantic_composition.adults,
                    semantic_composition.children,
                    semantic_composition.elderly,
                    semantic_composition.tags,
                )
            )
            else deterministic_composition
        ),
    )

    days = _merge_quantity(
        semantic.temporal.days,
        deterministic.temporal.days,
        field_path="temporal.days",
        issues=merge_issues,
    )
    nights = _merge_quantity(
        semantic.temporal.nights,
        deterministic.temporal.nights,
        field_path="temporal.nights",
        issues=merge_issues,
    )
    temporal = TemporalExtraction(
        days=days,
        nights=nights,
        date_range=semantic.temporal.date_range or deterministic.temporal.date_range,
        arrival=semantic.temporal.arrival or deterministic.temporal.arrival,
        departure=semantic.temporal.departure or deterministic.temporal.departure,
    )
    preferences = (
        semantic.preferences
        if semantic.preferences.status.value != "UNSPECIFIED"
        else deterministic.preferences
    )

    def existing_issue_evidence(field_path: str) -> list[EvidenceSpan]:
        return next(
            (item.evidence for item in semantic.issues if item.field_path == field_path and item.evidence),
            [],
        )

    if locations.status == LocationStatus.EXACT:
        primary = next(
            (
                item
                for item in locations.mentions
                if item.mention_id == locations.primary_mention_id
            ),
            None,
        )
        location_evidence = primary.evidence if primary is not None else []
        location_code = "PRIMARY_CITY_CONFIRMATION_REQUIRED"
        location_message = "主城市尚未由用户确认"
    else:
        location_evidence = [
            evidence
            for item in locations.mentions
            if item.role
            in {
                LocationRole.PRIMARY_DESTINATION,
                LocationRole.DESTINATION_CANDIDATE,
                LocationRole.REQUESTED_PLACE,
            }
            for evidence in item.evidence
        ] or existing_issue_evidence("locations.primary_city")
        location_code = "DESTINATION_NEEDS_CONFIRMATION"
        location_message = "目的地不是单一精确城市"
    confirmation_issues = [
        ExtractionIssue(
            code=location_code,
            field_path="locations.primary_city",
            message=location_message,
            evidence=_deduplicate_evidence(location_evidence),
        ),
        ExtractionIssue(
            code=(
                "PARTY_SIZE_CONFIRMATION_REQUIRED"
                if party.total.quantifier == QuantityQuantifier.EXACT
                else "PARTY_SIZE_NEEDS_CONFIRMATION"
            ),
            field_path="party_size.total",
            message=(
                "人数尚未由用户确认"
                if party.total.quantifier == QuantityQuantifier.EXACT
                else "人数不是精确值，需要用户确认"
            ),
            evidence=(
                party.total.evidence
                or existing_issue_evidence("party_size.total")
            ),
        ),
    ]
    date_evidence = (
        temporal.date_range.evidence
        if temporal.date_range is not None
        else temporal.days.evidence
        or temporal.nights.evidence
        or existing_issue_evidence("temporal.date_range")
    )
    confirmation_issues.append(
        ExtractionIssue(
            code="DATE_RANGE_MISSING_OR_INCOMPLETE",
            field_path="temporal.date_range",
            message="缺少含年份的完整日期范围，需要用户确认",
            evidence=date_evidence,
        )
    )
    if temporal.days.quantifier != QuantityQuantifier.EXACT:
        confirmation_issues.append(
            ExtractionIssue(
                code="DURATION_NEEDS_CONFIRMATION",
                field_path="temporal.days",
                message="旅行天数不是精确值，需要用户确认",
                evidence=(
                    temporal.days.evidence
                    or existing_issue_evidence("temporal.days")
                ),
            )
        )

    merged = TripIntakeExtraction(
        locations=locations,
        party_size=party,
        temporal=temporal,
        preferences=preferences,
        issues=confirmation_issues,
        readiness=IntakeReadiness.NEEDS_CONFIRMATION,
    )
    return merged


class HybridTripIntakeExtractor:
    parser_version = "trip-intake-hybrid-v1"
    prompt_version = SchemaConstrainedTripIntakeExtractor.prompt_version

    def __init__(
        self,
        model_extractor: SchemaConstrainedTripIntakeExtractor,
        *,
        deterministic_extractor: DeterministicTripIntakeExtractor | None = None,
    ) -> None:
        self.model_extractor = model_extractor
        self.deterministic_extractor = deterministic_extractor or DeterministicTripIntakeExtractor()

    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome:
        model_outcome = await self.model_extractor.extract(sources)
        deterministic = await self.deterministic_extractor.extract(sources)
        binding = _binding(
            parser_name="hybrid-trip-intake",
            parser_version=self.parser_version,
            model_name=self.model_extractor.model_name,
            prompt_version=self.prompt_version,
            schema=trip_intake_semantic_prompt_schema(),
            config={
                "evidence_offsets": "server-compiled-code-points",
                "fallback": DeterministicTripIntakeExtractor.parser_version,
            },
        )
        if model_outcome.status == IntakeStatus.EXTRACTION_FAILED:
            failure_issue = model_outcome.extraction.issues[0]
            extraction = deterministic.extraction.model_copy(
                update={"issues": [*deterministic.extraction.issues, failure_issue]}
            )
            extraction = TripIntakeExtraction.model_validate(extraction.model_dump())
            runtime = model_outcome.runtime_receipt or ExtractionRuntimeReceipt(
                requested_model=self.model_extractor.model_name,
                error_category="unknown_model_failure",
            )
            runtime = ExtractionRuntimeReceipt(
                **{
                    **runtime.__dict__,
                    "fallback_used": True,
                }
            )
            return ExtractionOutcome(
                extraction,
                binding,
                IntakeStatus.EXTRACTION_FAILED,
                runtime,
            )

        extraction = _merge_extractions(model_outcome.extraction, deterministic.extraction)
        return ExtractionOutcome(
            extraction,
            binding,
            IntakeStatus.NEEDS_CONFIRMATION,
            model_outcome.runtime_receipt,
        )


class UnavailableHybridTripIntakeExtractor:
    """Explicit hybrid mode without a key; preserves local facts and fails closed."""

    def __init__(self, *, model_name: str) -> None:
        self.model_name = model_name
        self.deterministic_extractor = DeterministicTripIntakeExtractor()

    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome:
        deterministic = await self.deterministic_extractor.extract(sources)
        issue = ExtractionIssue(
            code="EXTRACTION_FAILED",
            field_path="extraction",
            message="hybrid extraction unavailable: API key is not configured",
            blocking=True,
        )
        extraction = deterministic.extraction.model_copy(
            update={"issues": [*deterministic.extraction.issues, issue]}
        )
        binding = _binding(
            parser_name="hybrid-trip-intake",
            parser_version=HybridTripIntakeExtractor.parser_version,
            model_name=self.model_name,
            prompt_version=HybridTripIntakeExtractor.prompt_version,
            schema=trip_intake_semantic_prompt_schema(),
            config={"unavailable": "missing_api_key"},
        )
        return ExtractionOutcome(
            TripIntakeExtraction.model_validate(extraction.model_dump()),
            binding,
            IntakeStatus.EXTRACTION_FAILED,
            ExtractionRuntimeReceipt(
                requested_model=self.model_name,
                fallback_used=True,
                error_category="missing_api_key",
            ),
        )
