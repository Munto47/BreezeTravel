from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.itineraries.hash_service import sha256_canonical


class IntakeSourceType(str, Enum):
    AI_TEXT = "AI_TEXT"
    MANUAL_TEXT = "MANUAL_TEXT"
    SCREENSHOT_OCR = "SCREENSHOT_OCR"


class IntakeSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    source_type: IntakeSourceType
    text: str = Field(min_length=1, max_length=12000)
    text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_hash(self) -> "IntakeSource":
        if self.text_sha256 != sha256(self.text.encode("utf-8")).hexdigest():
            raise ValueError("text_sha256 does not match UTF-8 source text")
        return self


class IntakeStatus(str, Enum):
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    READY = "READY"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"


class IntakeReadiness(str, Enum):
    NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
    READY = "READY"


class EvidenceSpan(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_order(self) -> "EvidenceSpan":
        if self.end <= self.start:
            raise ValueError("evidence span end must be after start")
        return self


class LocationEntityType(str, Enum):
    CITY = "CITY"
    DISTRICT = "DISTRICT"
    PLACE = "PLACE"
    TRANSPORT_HUB = "TRANSPORT_HUB"
    ACCOMMODATION = "ACCOMMODATION"
    UNKNOWN = "UNKNOWN"


class LocationRole(str, Enum):
    PRIMARY_DESTINATION = "PRIMARY_DESTINATION"
    DESTINATION_CANDIDATE = "DESTINATION_CANDIDATE"
    REQUESTED_PLACE = "REQUESTED_PLACE"
    ORIGIN = "ORIGIN"
    RETURN_LOCATION = "RETURN_LOCATION"
    EXCLUDED = "EXCLUDED"
    OTHER_MENTION = "OTHER_MENTION"


class LocationStatus(str, Enum):
    EXACT = "EXACT"
    MULTIPLE = "MULTIPLE"
    UNCERTAIN = "UNCERTAIN"
    MISSING = "MISSING"


class LocationMention(BaseModel):
    model_config = ConfigDict(frozen=True)

    mention_id: str = Field(min_length=1)
    raw_text: str = Field(min_length=1, max_length=200)
    normalized_name: str | None = Field(default=None, min_length=1, max_length=200)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    entity_type: LocationEntityType = LocationEntityType.UNKNOWN
    role: LocationRole
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceSpan] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_raw_text_evidence(self) -> "LocationMention":
        if not any(self.raw_text in span.quote for span in self.evidence):
            raise ValueError("location raw_text must occur in its evidence quote")
        return self


class LocationExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    mentions: list[LocationMention] = Field(default_factory=list)
    primary_mention_id: str | None = None
    status: LocationStatus = LocationStatus.MISSING

    @model_validator(mode="after")
    def validate_location_contract(self) -> "LocationExtraction":
        ids = [item.mention_id for item in self.mentions]
        if len(ids) != len(set(ids)):
            raise ValueError("location mention_id must be unique")
        primary = next((item for item in self.mentions if item.mention_id == self.primary_mention_id), None)
        if self.primary_mention_id is not None and primary is None:
            raise ValueError("primary_mention_id must reference a location mention")
        if primary is not None and primary.role != LocationRole.PRIMARY_DESTINATION:
            raise ValueError("primary location mention must use PRIMARY_DESTINATION role")
        if self.status == LocationStatus.EXACT and primary is None:
            raise ValueError("EXACT locations require a primary destination")
        if self.status != LocationStatus.EXACT and self.primary_mention_id is not None:
            raise ValueError("only EXACT locations may select a primary destination")
        primary_mentions = [
            item for item in self.mentions if item.role == LocationRole.PRIMARY_DESTINATION
        ]
        if len(primary_mentions) > 1:
            raise ValueError("locations may contain at most one primary destination")
        if self.status == LocationStatus.MISSING and primary_mentions:
            raise ValueError("MISSING locations cannot contain a primary destination")
        return self


class QuantityQuantifier(str, Enum):
    EXACT = "EXACT"
    RANGE = "RANGE"
    APPROXIMATE = "APPROXIMATE"
    AT_LEAST = "AT_LEAST"
    AT_MOST = "AT_MOST"
    UNKNOWN = "UNKNOWN"


class QuantityDerivation(str, Enum):
    EXPLICIT_COUNT = "EXPLICIT_COUNT"
    SEMANTIC_INFERENCE = "SEMANTIC_INFERENCE"
    DATE_RANGE = "DATE_RANGE"
    MISSING = "MISSING"


class QuantifiedValue(BaseModel):
    model_config = ConfigDict(frozen=True)

    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    quantifier: QuantityQuantifier
    derivation: QuantityDerivation
    evidence: list[EvidenceSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_quantity(self) -> "QuantifiedValue":
        if self.quantifier in {QuantityQuantifier.EXACT, QuantityQuantifier.APPROXIMATE}:
            if self.min is None or self.max is None or self.min != self.max:
                raise ValueError(f"{self.quantifier.value} quantity requires equal min and max")
        elif self.quantifier == QuantityQuantifier.RANGE:
            if self.min is None or self.max is None or self.min >= self.max:
                raise ValueError("RANGE quantity requires min < max")
        elif self.quantifier == QuantityQuantifier.AT_LEAST:
            if self.min is None or self.max is not None:
                raise ValueError("AT_LEAST quantity requires min and no max")
        elif self.quantifier == QuantityQuantifier.AT_MOST:
            if self.min is not None or self.max is None:
                raise ValueError("AT_MOST quantity requires max and no min")
        elif self.min is not None or self.max is not None:
            raise ValueError("UNKNOWN quantity cannot contain bounds")
        if self.derivation == QuantityDerivation.MISSING and self.evidence:
            raise ValueError("MISSING quantity cannot contain evidence")
        if self.derivation != QuantityDerivation.MISSING and not self.evidence:
            raise ValueError("non-missing quantity requires evidence")
        return self


def unknown_quantity() -> QuantifiedValue:
    return QuantifiedValue(
        quantifier=QuantityQuantifier.UNKNOWN,
        derivation=QuantityDerivation.MISSING,
    )


class PartyComposition(BaseModel):
    model_config = ConfigDict(frozen=True)

    adults: QuantifiedValue | None = None
    children: QuantifiedValue | None = None
    elderly: QuantifiedValue | None = None
    tags: list[str] = Field(default_factory=list)


class PartySizeExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    total: QuantifiedValue = Field(default_factory=unknown_quantity)
    composition: PartyComposition = Field(default_factory=PartyComposition)


class PartialDate(BaseModel):
    model_config = ConfigDict(frozen=True)

    year: int | None = Field(default=None, ge=1900, le=2200)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def validate_calendar_date(self) -> "PartialDate":
        # Leap year 2000 permits an unknown-year February 29 without inventing a year.
        date(self.year or 2000, self.month, self.day)
        return self


class DateRangeExpression(BaseModel):
    model_config = ConfigDict(frozen=True)

    raw_text: str = Field(min_length=1, max_length=200)
    start: PartialDate
    end: PartialDate
    inclusive: bool = True
    evidence: list[EvidenceSpan] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_date_order(self) -> "DateRangeExpression":
        if self.start.year is not None and self.end.year is not None:
            start = date(self.start.year, self.start.month, self.start.day)
            end = date(self.end.year, self.end.month, self.end.day)
            if end < start:
                raise ValueError("date range end cannot be before start")
        return self


class TravelCommitment(BaseModel):
    model_config = ConfigDict(frozen=True)

    location_text: str | None = Field(default=None, max_length=200)
    at_text: str | None = Field(default=None, max_length=200)
    evidence: list[EvidenceSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_commitment_evidence(self) -> "TravelCommitment":
        if (self.location_text or self.at_text) and not self.evidence:
            raise ValueError("travel commitment values require evidence")
        if not self.location_text and not self.at_text and self.evidence:
            raise ValueError("empty travel commitment cannot contain evidence")
        return self


class TemporalExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    days: QuantifiedValue = Field(default_factory=unknown_quantity)
    nights: QuantifiedValue = Field(default_factory=unknown_quantity)
    date_range: DateRangeExpression | None = None
    arrival: TravelCommitment | None = None
    departure: TravelCommitment | None = None


class PreferencePolarity(str, Enum):
    LIKE = "LIKE"
    DISLIKE = "DISLIKE"
    REQUIREMENT = "REQUIREMENT"


class PreferenceStatus(str, Enum):
    UNSPECIFIED = "UNSPECIFIED"
    SPECIFIED = "SPECIFIED"
    NO_PREFERENCE = "NO_PREFERENCE"


class RequirementOperator(str, Enum):
    PREFER = "PREFER"
    AVOID = "AVOID"
    REQUIRED = "REQUIRED"
    MAX = "MAX"
    MIN = "MIN"
    EQUALS = "EQUALS"


class PaceValue(str, Enum):
    RELAXED = "RELAXED"
    BALANCED = "BALANCED"
    INTENSIVE = "INTENSIVE"
    UNSPECIFIED = "UNSPECIFIED"
    NO_PREFERENCE = "NO_PREFERENCE"


class PreferenceItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    item_id: str = Field(min_length=1)
    category: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    polarity: PreferencePolarity
    operator: RequirementOperator | None = None
    value: Any = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=8)
    applies_to: str | None = Field(default=None, max_length=120)
    confidence: float = Field(ge=0, le=1)
    evidence: list[EvidenceSpan] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_requirement_operator(self) -> "PreferenceItem":
        if self.polarity == PreferencePolarity.REQUIREMENT and self.operator is None:
            raise ValueError("requirement preference items require an operator")
        if self.polarity != PreferencePolarity.REQUIREMENT and self.operator is not None:
            raise ValueError("only requirement items may contain an operator")
        return self


class PacePreference(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: PaceValue = PaceValue.UNSPECIFIED
    confidence: float = Field(default=1, ge=0, le=1)
    evidence: list[EvidenceSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence(self) -> "PacePreference":
        if self.value == PaceValue.UNSPECIFIED and self.evidence:
            raise ValueError("UNSPECIFIED pace cannot contain evidence")
        if self.value != PaceValue.UNSPECIFIED and not self.evidence:
            raise ValueError("specified pace requires evidence")
        return self


class PreferenceExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: PreferenceStatus = PreferenceStatus.UNSPECIFIED
    items: list[PreferenceItem] = Field(default_factory=list)
    pace: PacePreference = Field(default_factory=PacePreference)
    no_preference_evidence: list[EvidenceSpan] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ids(self) -> "PreferenceExtraction":
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("preference item_id must be unique")
        has_values = bool(self.items) or self.pace.value not in {
            PaceValue.UNSPECIFIED,
            PaceValue.NO_PREFERENCE,
        }
        if self.status == PreferenceStatus.UNSPECIFIED and (
            has_values or self.no_preference_evidence
        ):
            raise ValueError("UNSPECIFIED preferences cannot contain values or evidence")
        if self.status == PreferenceStatus.SPECIFIED and not has_values:
            raise ValueError("SPECIFIED preferences require at least one value")
        if self.status == PreferenceStatus.NO_PREFERENCE:
            if self.items or self.pace.value != PaceValue.NO_PREFERENCE:
                raise ValueError("NO_PREFERENCE cannot contain preference items")
            if not self.no_preference_evidence:
                raise ValueError("NO_PREFERENCE requires explicit user evidence")
        return self


class ExtractionIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str = Field(min_length=1, max_length=80)
    field_path: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=500)
    blocking: bool = True
    evidence: list[EvidenceSpan] = Field(default_factory=list)


class ParserBinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    parser_name: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ConfirmedField(str, Enum):
    PRIMARY_CITY = "locations.primary_city"
    DATE_RANGE = "temporal.date_range"
    PARTY_SIZE = "party_size.total"
    PREFERENCES = "preferences"


class TripIntakeExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "trip-intake-extraction-v2"
    locations: LocationExtraction = Field(default_factory=LocationExtraction)
    party_size: PartySizeExtraction = Field(default_factory=PartySizeExtraction)
    temporal: TemporalExtraction = Field(default_factory=TemporalExtraction)
    preferences: PreferenceExtraction = Field(default_factory=PreferenceExtraction)
    issues: list[ExtractionIssue] = Field(default_factory=list)
    readiness: IntakeReadiness = IntakeReadiness.NEEDS_CONFIRMATION

    @model_validator(mode="after")
    def validate_readiness(self) -> "TripIntakeExtraction":
        if self.readiness == IntakeReadiness.READY:
            exact_party = (
                self.party_size.total.quantifier == QuantityQuantifier.EXACT
                and self.party_size.total.min is not None
                and self.party_size.total.min > 0
            )
            complete_dates = bool(
                self.temporal.date_range
                and self.temporal.date_range.start.year is not None
                and self.temporal.date_range.end.year is not None
            )
            primary = next(
                (
                    item
                    for item in self.locations.mentions
                    if item.mention_id == self.locations.primary_mention_id
                ),
                None,
            )
            domestic_city = bool(
                primary
                and primary.entity_type == LocationEntityType.CITY
                and primary.normalized_name
                and primary.country_code == "CN"
            )
            if (
                self.locations.status != LocationStatus.EXACT
                or not domestic_city
                or not exact_party
                or not complete_dates
            ):
                raise ValueError("READY intake requires exact city, positive party size, and complete date range")
            if any(issue.blocking for issue in self.issues):
                raise ValueError("READY intake cannot contain blocking issues")
        return self


class TripIntakeRevision(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = "trip-intake-revision-v2"
    intake_id: str = Field(min_length=1)
    room_id: str = Field(min_length=1)
    revision: int = Field(gt=0)
    parent_revision: int | None = Field(default=None, gt=0)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_type: IntakeSourceType
    raw_text: str = Field(min_length=1, max_length=12000)
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sources: list[IntakeSource] = Field(min_length=1, max_length=6)
    parser_binding: ParserBinding
    extraction: TripIntakeExtraction
    confirmed_fields: set[ConfirmedField] = Field(default_factory=set)
    status: IntakeStatus
    created_by: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    confirmed_by: str | None = None
    confirmed_at: datetime | None = None

    @model_validator(mode="after")
    def validate_revision(self) -> "TripIntakeRevision":
        if self.parent_revision is not None and self.parent_revision >= self.revision:
            raise ValueError("parent intake revision must be older")
        if self.raw_text_sha256 != sha256(self.raw_text.encode("utf-8")).hexdigest():
            raise ValueError("raw_text_sha256 does not match raw_text")
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("intake source_id must be unique")
        validate_extraction_evidence(
            self.extraction,
            {source.source_id: source.text for source in self.sources},
        )
        if self.status == IntakeStatus.READY:
            required = {
                ConfirmedField.PRIMARY_CITY,
                ConfirmedField.DATE_RANGE,
                ConfirmedField.PARTY_SIZE,
            }
            if self.extraction.readiness != IntakeReadiness.READY or not required.issubset(self.confirmed_fields):
                raise ValueError("READY intake requires confirmed materialization fields")
            if not self.confirmed_by or self.confirmed_at is None:
                raise ValueError("READY intake requires confirmation receipt")
        elif self.confirmed_by is not None or self.confirmed_at is not None:
            raise ValueError("only READY intake may contain confirmation receipt")
        return self


def _walk_evidence(value: Any):
    if isinstance(value, BaseModel):
        yield from _walk_evidence(value.model_dump())
    elif isinstance(value, dict):
        if {"source_id", "start", "end", "quote"}.issubset(value):
            yield EvidenceSpan.model_validate(value)
        else:
            for child in value.values():
                yield from _walk_evidence(child)
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            yield from _walk_evidence(child)


def validate_extraction_evidence(
    extraction: TripIntakeExtraction,
    source_texts: dict[str, str],
) -> None:
    for span in _walk_evidence(extraction):
        raw_text = source_texts.get(span.source_id)
        if raw_text is None:
            raise ValueError("evidence source_id must reference an intake source")
        if span.end > len(raw_text) or raw_text[span.start : span.end] != span.quote:
            raise ValueError("evidence quote must exactly match source text code-point span")


def with_intake_content_hash(intake: TripIntakeRevision) -> TripIntakeRevision:
    payload = {
        "schema_version": intake.schema_version,
        "source_type": intake.source_type.value,
        "raw_text_sha256": intake.raw_text_sha256,
        "sources": [source.model_dump(mode="json") for source in intake.sources],
        "parser_binding": intake.parser_binding.model_dump(mode="json"),
        "extraction": intake.extraction.model_dump(mode="json"),
        "confirmed_fields": sorted(field.value for field in intake.confirmed_fields),
        "status": intake.status.value,
    }
    return intake.model_copy(update={"content_hash": sha256_canonical(payload)})
