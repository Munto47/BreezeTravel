from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from app.itineraries.hash_service import sha256_canonical
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
    PartySizeExtraction,
    QuantifiedValue,
    QuantityDerivation,
    QuantityQuantifier,
    TemporalExtraction,
    TripIntakeExtraction,
    validate_extraction_evidence,
)


@dataclass(frozen=True)
class ExtractionOutcome:
    extraction: TripIntakeExtraction
    parser_binding: ParserBinding
    status: IntakeStatus


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
    ) -> dict[str, Any]: ...


def _binding(*, parser_name: str, parser_version: str, model_name: str, prompt_version: str) -> ParserBinding:
    config_hash = sha256_canonical(
        {
            "parser_name": parser_name,
            "parser_version": parser_version,
            "model_name": model_name,
            "prompt_version": prompt_version,
            "schema": TripIntakeExtraction.model_json_schema(),
            "temperature": 0,
            "tool_calls": False,
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
    """LLM extractor with no tools and a deterministic validation boundary."""

    parser_version = "trip-intake-llm-v2"
    prompt_version = "trip-intake-extraction-zh-v2"

    def __init__(self, client: StructuredExtractionClient, *, model_name: str):
        self.client = client
        self.model_name = model_name

    async def extract(self, sources: list[IntakeSource]) -> ExtractionOutcome:
        binding = _binding(
            parser_name="schema-constrained-llm",
            parser_version=self.parser_version,
            model_name=self.model_name,
            prompt_version=self.prompt_version,
        )
        source_payload = [
            {"source_id": source.source_id, "source_type": source.source_type.value, "text": source.text}
            for source in sources
        ]
        try:
            payload = await self.client.generate_json(
                system_prompt=(
                    "忠实抽取行程需求，不规划、不调用工具、不验证地点真假。"
                    "未知值保持 UNKNOWN/MISSING/UNSPECIFIED，所有原子事实必须给出 Unicode code-point 半开区间证据。"
                ),
                input_payload={"schema_version": "trip-intake-extraction-v2", "sources": source_payload},
                json_schema=TripIntakeExtraction.model_json_schema(),
                model_name=self.model_name,
                temperature=0,
            )
            extraction = TripIntakeExtraction.model_validate(payload)
            validate_extraction_evidence(
                extraction,
                {source.source_id: source.text for source in sources},
            )
            # Model output never self-confirms the materialization prerequisites.
            extraction = extraction.model_copy(update={"readiness": IntakeReadiness.NEEDS_CONFIRMATION})
            extraction = TripIntakeExtraction.model_validate(extraction.model_dump())
            return ExtractionOutcome(extraction, binding, IntakeStatus.NEEDS_CONFIRMATION)
        except Exception as exc:
            return ExtractionOutcome(
                _failed_extraction(f"schema-constrained extraction failed: {type(exc).__name__}"),
                binding,
                IntakeStatus.EXTRACTION_FAILED,
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
                count_match = re.search(r"(?<!\d)([1-9]\d*)\s*(?:人|位)", source.text)
                chinese_match = re.search(r"([一二两三四五六七八九十])\s*(?:人|位)", source.text)
                match = count_match or chinese_match
                if match:
                    value = int(match.group(1)) if count_match else _CHINESE_NUMBER[match.group(1)]
                    party = QuantifiedValue(
                        min=value,
                        max=value,
                        quantifier=QuantityQuantifier.EXACT,
                        derivation=QuantityDerivation.EXPLICIT_COUNT,
                        evidence=[_span(source, match.start(), match.end())],
                    )

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

            day_match = re.search(r"(?<!\d)([1-9]\d*)\s*天", source.text)
            day_cn_match = re.search(r"([一二两三四五六七八九十])\s*天", source.text)
            selected_day_match = day_match or day_cn_match
            if selected_day_match and temporal.days.quantifier == QuantityQuantifier.UNKNOWN:
                value = (
                    int(selected_day_match.group(1))
                    if day_match
                    else _CHINESE_NUMBER[selected_day_match.group(1)]
                )
                temporal = temporal.model_copy(
                    update={
                        "days": QuantifiedValue(
                            min=value,
                            max=value,
                            quantifier=QuantityQuantifier.EXACT,
                            derivation=QuantityDerivation.EXPLICIT_COUNT,
                            evidence=[
                                _span(source, selected_day_match.start(), selected_day_match.end())
                            ],
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
            party_size=PartySizeExtraction(total=party) if party else PartySizeExtraction(),
            temporal=temporal,
            issues=issues,
            readiness=IntakeReadiness.NEEDS_CONFIRMATION,
        )
        validate_extraction_evidence(
            extraction,
            {source.source_id: source.text for source in sources},
        )
        return ExtractionOutcome(extraction, binding, IntakeStatus.NEEDS_CONFIRMATION)
