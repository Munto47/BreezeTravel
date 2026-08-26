from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest
from pydantic import ValidationError

from app.trip_intake.models import (
    ConfirmedField,
    DateRangeExpression,
    EvidenceSpan,
    IntakeReadiness,
    IntakeSource,
    IntakeSourceType,
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
    TripIntakeRevision,
    validate_extraction_evidence,
    with_intake_content_hash,
)


def _span(source_id: str, text: str, quote: str, occurrence: int = 0) -> EvidenceSpan:
    start = -1
    search_from = 0
    for _ in range(occurrence + 1):
        start = text.index(quote, search_from)
        search_from = start + len(quote)
    return EvidenceSpan(
        source_id=source_id,
        start=start,
        end=start + len(quote),
        quote=quote,
    )


def _source(source_id: str, text: str) -> IntakeSource:
    return IntakeSource(
        source_id=source_id,
        source_type=IntakeSourceType.MANUAL_TEXT,
        text=text,
        text_sha256=sha256(text.encode("utf-8")).hexdigest(),
    )


def _ready_extraction(source_id: str, text: str) -> TripIntakeExtraction:
    return TripIntakeExtraction(
        locations=LocationExtraction(
            status=LocationStatus.EXACT,
            primary_mention_id="city-1",
            mentions=[
                LocationMention(
                    mention_id="city-1",
                    raw_text="成都",
                    normalized_name="成都市",
                    country_code="CN",
                    entity_type=LocationEntityType.CITY,
                    role=LocationRole.PRIMARY_DESTINATION,
                    confidence=0.99,
                    evidence=[_span(source_id, text, "成都")],
                )
            ],
        ),
        party_size=PartySizeExtraction(
            total=QuantifiedValue(
                min=3,
                max=3,
                quantifier=QuantityQuantifier.EXACT,
                derivation=QuantityDerivation.EXPLICIT_COUNT,
                evidence=[_span(source_id, text, "三个人")],
            )
        ),
        temporal=TemporalExtraction(
            date_range=DateRangeExpression(
                raw_text="2027年10月1日到10月7日",
                start=PartialDate(year=2027, month=10, day=1),
                end=PartialDate(year=2027, month=10, day=7),
                evidence=[_span(source_id, text, "2027年10月1日到10月7日")],
            )
        ),
        readiness=IntakeReadiness.READY,
    )


def _revision(*, intake_id: str = "intake-1", revision: int = 1) -> TripIntakeRevision:
    text = "2027年10月1日到10月7日去成都，三个人"
    confirmed_at = datetime(2026, 8, 26, tzinfo=timezone.utc)
    return TripIntakeRevision(
        intake_id=intake_id,
        room_id="room-1",
        revision=revision,
        content_hash="0" * 64,
        source_type=IntakeSourceType.MANUAL_TEXT,
        raw_text=text,
        raw_text_sha256=sha256(text.encode("utf-8")).hexdigest(),
        sources=[_source("source-1", text)],
        parser_binding=ParserBinding(
            parser_name="fixture",
            parser_version="1",
            model_name="none",
            prompt_version="1",
            config_hash="1" * 64,
        ),
        extraction=_ready_extraction("source-1", text),
        confirmed_fields={
            ConfirmedField.PARTY_SIZE,
            ConfirmedField.PRIMARY_CITY,
            ConfirmedField.DATE_RANGE,
        },
        status=IntakeStatus.READY,
        created_by="user-1",
        confirmed_by="user-1",
        confirmed_at=confirmed_at,
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"quantifier": "EXACT", "min": 2, "max": 3}, "equal min and max"),
        ({"quantifier": "RANGE", "min": 3, "max": 3}, "min < max"),
        ({"quantifier": "AT_LEAST", "min": 2, "max": 4}, "no max"),
        ({"quantifier": "AT_MOST", "min": 1, "max": 4}, "no min"),
        ({"quantifier": "UNKNOWN", "min": 1, "max": None}, "cannot contain bounds"),
    ],
)
def test_quantity_contract_rejects_inconsistent_bounds(payload: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        QuantifiedValue(
            **payload,
            derivation=QuantityDerivation.MISSING,
        )


def test_missing_destination_can_still_preserve_origin_mention() -> None:
    text = "从上海出发，目的地还没定"
    locations = LocationExtraction(
        status=LocationStatus.MISSING,
        mentions=[
            LocationMention(
                mention_id="origin-1",
                raw_text="上海",
                normalized_name="上海市",
                country_code="CN",
                entity_type=LocationEntityType.CITY,
                role=LocationRole.ORIGIN,
                confidence=1,
                evidence=[_span("source-1", text, "上海")],
            )
        ],
    )
    assert locations.mentions[0].role == LocationRole.ORIGIN


def test_source_spans_use_unicode_code_points_and_can_select_repeated_text() -> None:
    text = "去杭州玩😊三天，杭州不要太赶"
    second_hangzhou = _span("source-1", text, "杭州", occurrence=1)
    assert second_hangzhou.start == 8
    extraction = TripIntakeExtraction(
        locations=LocationExtraction(
            status=LocationStatus.UNCERTAIN,
            mentions=[
                LocationMention(
                    mention_id="excluded-1",
                    raw_text="杭州",
                    normalized_name="杭州市",
                    country_code="CN",
                    entity_type=LocationEntityType.CITY,
                    role=LocationRole.EXCLUDED,
                    confidence=0.7,
                    evidence=[second_hangzhou],
                )
            ],
        )
    )
    validate_extraction_evidence(extraction, {"source-1": text})


def test_source_span_rejects_utf16_or_first_occurrence_offset() -> None:
    text = "去杭州玩😊三天，杭州不要太赶"
    extraction = TripIntakeExtraction(
        locations=LocationExtraction(
            status=LocationStatus.UNCERTAIN,
            mentions=[
                LocationMention(
                    mention_id="excluded-1",
                    raw_text="杭州",
                    role=LocationRole.EXCLUDED,
                    confidence=0.7,
                    evidence=[
                        EvidenceSpan(
                            source_id="source-1",
                            start=10,
                            end=12,
                            quote="杭州",
                        )
                    ],
                )
            ],
        )
    )
    with pytest.raises(ValueError, match="code-point span"):
        validate_extraction_evidence(extraction, {"source-1": text})


def test_partial_date_rejects_invalid_calendar_day() -> None:
    with pytest.raises(ValidationError, match="day is out of range"):
        PartialDate(month=2, day=31)


def test_ready_requires_confirmed_domestic_city() -> None:
    text = "2027年10月1日到10月7日去成都，三个人"
    extraction = _ready_extraction("source-1", text)
    foreign = extraction.locations.mentions[0].model_copy(update={"country_code": "FR"})
    with pytest.raises(ValidationError, match="READY intake requires"):
        TripIntakeExtraction(
            locations=extraction.locations.model_copy(update={"mentions": [foreign]}),
            party_size=extraction.party_size,
            temporal=extraction.temporal,
            readiness=IntakeReadiness.READY,
        )


def test_revision_supports_multiple_ocr_sources() -> None:
    revision = _revision()
    city_source = _source("source-city", "去成都")
    count_source = _source("source-count", "三个人")
    extraction = revision.extraction.model_copy(
        update={
            "locations": revision.extraction.locations.model_copy(
                update={
                    "mentions": [
                        revision.extraction.locations.mentions[0].model_copy(
                            update={"evidence": [_span("source-city", "去成都", "成都")]}
                        )
                    ]
                }
            ),
            "party_size": PartySizeExtraction(
                total=revision.extraction.party_size.total.model_copy(
                    update={"evidence": [_span("source-count", "三个人", "三个人")]}
                )
            ),
            "temporal": TemporalExtraction(),
            "readiness": IntakeReadiness.NEEDS_CONFIRMATION,
        }
    )
    candidate = TripIntakeRevision(
        **revision.model_dump(
            exclude={
                "sources",
                "extraction",
                "status",
                "confirmed_fields",
                "confirmed_by",
                "confirmed_at",
            }
        ),
        sources=[city_source, count_source],
        extraction=extraction,
        status=IntakeStatus.NEEDS_CONFIRMATION,
        confirmed_fields=set(),
    )
    assert len(candidate.sources) == 2


def test_content_hash_is_semantic_and_independent_of_revision_identity() -> None:
    first = with_intake_content_hash(_revision(intake_id="intake-1", revision=1))
    second = with_intake_content_hash(_revision(intake_id="intake-2", revision=9))
    assert first.content_hash == second.content_hash


def test_revision_uses_raw_utf8_sha256_not_canonical_json_hash() -> None:
    revision = _revision()
    assert revision.raw_text_sha256 == sha256(revision.raw_text.encode("utf-8")).hexdigest()
