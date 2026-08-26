from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.trip_intake.models import (
    DateRangeExpression,
    EvidenceSpan,
    ExtractionIssue,
    IntakeReadiness,
    IntakeSource,
    LocationEntityType,
    LocationExtraction,
    LocationMention,
    LocationRole,
    LocationStatus,
    PacePreference,
    PaceValue,
    PartialDate,
    PartyComposition,
    PartySizeExtraction,
    PreferenceExtraction,
    PreferenceItem,
    PreferencePolarity,
    PreferenceStatus,
    QuantifiedValue,
    QuantityDerivation,
    QuantityQuantifier,
    RequirementOperator,
    TemporalExtraction,
    TravelCommitment,
    TripIntakeExtraction,
    validate_extraction_evidence,
)


class SemanticEvidenceRef(BaseModel):
    """A model-facing quote reference; offsets are compiled locally."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)
    occurrence: int = Field(default=0, ge=0)


class SemanticQuantityDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min: int | None = Field(default=None, ge=0)
    max: int | None = Field(default=None, ge=0)
    quantifier: QuantityQuantifier = QuantityQuantifier.UNKNOWN
    derivation: QuantityDerivation = QuantityDerivation.MISSING
    evidence: list[SemanticEvidenceRef] = Field(default_factory=list)


class SemanticPartyCompositionDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    adults: SemanticQuantityDraft | None = None
    children: SemanticQuantityDraft | None = None
    elderly: SemanticQuantityDraft | None = None
    tags: list[str] = Field(default_factory=list)


class SemanticPartySizeDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    total: SemanticQuantityDraft = Field(default_factory=SemanticQuantityDraft)
    composition: SemanticPartyCompositionDraft = Field(
        default_factory=SemanticPartyCompositionDraft
    )


class SemanticLocationDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_text: str = Field(min_length=1, max_length=200)
    normalized_name: str | None = Field(default=None, min_length=1, max_length=200)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    entity_type: LocationEntityType = LocationEntityType.UNKNOWN
    role: LocationRole
    confidence: float = Field(default=1, ge=0, le=1)
    evidence: list[SemanticEvidenceRef] = Field(min_length=1)


class SemanticDateRangeDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_text: str = Field(min_length=1, max_length=200)
    start: PartialDate
    end: PartialDate
    inclusive: bool = True
    evidence: list[SemanticEvidenceRef] = Field(min_length=1)


class SemanticTravelCommitmentDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    location_text: str | None = Field(default=None, max_length=200)
    at_text: str | None = Field(default=None, max_length=200)
    evidence: list[SemanticEvidenceRef] = Field(default_factory=list)


class SemanticTemporalDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    days: SemanticQuantityDraft = Field(default_factory=SemanticQuantityDraft)
    nights: SemanticQuantityDraft = Field(default_factory=SemanticQuantityDraft)
    date_range: SemanticDateRangeDraft | None = None
    arrival: SemanticTravelCommitmentDraft | None = None
    departure: SemanticTravelCommitmentDraft | None = None


class SemanticPreferenceItemDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=200)
    polarity: PreferencePolarity
    operator: RequirementOperator | None = None
    value: Any = None
    unit: str | None = Field(default=None, max_length=40)
    currency: str | None = Field(default=None, max_length=8)
    applies_to: str | None = Field(default=None, max_length=120)
    confidence: float = Field(default=1, ge=0, le=1)
    evidence: list[SemanticEvidenceRef] = Field(min_length=1)


class SemanticPaceDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    value: PaceValue = PaceValue.UNSPECIFIED
    confidence: float = Field(default=1, ge=0, le=1)
    evidence: list[SemanticEvidenceRef] = Field(default_factory=list)


class SemanticPreferencesDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: PreferenceStatus = PreferenceStatus.UNSPECIFIED
    items: list[SemanticPreferenceItemDraft] = Field(default_factory=list)
    pace: SemanticPaceDraft = Field(default_factory=SemanticPaceDraft)
    no_preference_evidence: list[SemanticEvidenceRef] = Field(default_factory=list)


class SemanticIssueDraft(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1, max_length=80)
    field_path: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=500)
    blocking: bool = True
    evidence: list[SemanticEvidenceRef] = Field(default_factory=list)


class TripIntakeSemanticDraft(BaseModel):
    """Strict private schema returned by the model before evidence compilation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "trip-intake-semantic-draft-v1"
    locations: list[SemanticLocationDraft] = Field(default_factory=list)
    location_status: LocationStatus = LocationStatus.MISSING
    primary_location_index: int | None = Field(default=None, ge=0)
    party_size: SemanticPartySizeDraft = Field(default_factory=SemanticPartySizeDraft)
    temporal: SemanticTemporalDraft = Field(default_factory=SemanticTemporalDraft)
    preferences: SemanticPreferencesDraft = Field(default_factory=SemanticPreferencesDraft)
    issues: list[SemanticIssueDraft] = Field(default_factory=list)


class SemanticCompilationError(ValueError):
    pass


def _compile_evidence(
    refs: list[SemanticEvidenceRef],
    source_texts: dict[str, str],
) -> list[EvidenceSpan]:
    spans: list[EvidenceSpan] = []
    for ref in refs:
        text = source_texts.get(ref.source_id)
        if text is None:
            raise SemanticCompilationError("evidence source_id is unknown")
        start = -1
        search_from = 0
        for _ in range(ref.occurrence + 1):
            start = text.find(ref.quote, search_from)
            if start < 0:
                raise SemanticCompilationError("evidence quote occurrence is absent")
            search_from = start + len(ref.quote)
        spans.append(
            EvidenceSpan(
                source_id=ref.source_id,
                start=start,
                end=start + len(ref.quote),
                quote=ref.quote,
            )
        )
    return spans


def _narrow_location_evidence(
    spans: list[EvidenceSpan],
    raw_text: str,
) -> list[EvidenceSpan]:
    narrowed: list[EvidenceSpan] = []
    for span in spans:
        offset = span.quote.find(raw_text)
        if offset < 0:
            raise SemanticCompilationError("location raw_text is absent from evidence quote")
        narrowed.append(
            EvidenceSpan(
                source_id=span.source_id,
                start=span.start + offset,
                end=span.start + offset + len(raw_text),
                quote=raw_text,
            )
        )
    return narrowed


def _compiler_issue(field_path: str, reason: str) -> ExtractionIssue:
    return ExtractionIssue(
        code="SEMANTIC_FIELD_DROPPED",
        field_path=field_path,
        message=reason[:500],
        blocking=True,
    )


def _preference_item_id(
    item: SemanticPreferenceItemDraft,
    used: set[str],
) -> str:
    if item.polarity == PreferencePolarity.LIKE:
        base = "preference-like"
    elif item.polarity == PreferencePolarity.DISLIKE:
        base = "preference-dislike"
    elif item.category == "budget":
        base = "requirement-budget"
    elif item.category == "transport":
        base = "requirement-transport"
    else:
        base = "requirement-themed"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _compile_quantity(
    draft: SemanticQuantityDraft,
    source_texts: dict[str, str],
    field_path: str,
    issues: list[ExtractionIssue],
) -> QuantifiedValue:
    try:
        return QuantifiedValue(
            min=draft.min,
            max=draft.max,
            quantifier=draft.quantifier,
            derivation=draft.derivation,
            evidence=_compile_evidence(draft.evidence, source_texts),
        )
    except (ValueError, SemanticCompilationError) as exc:
        issues.append(_compiler_issue(field_path, f"invalid semantic quantity: {type(exc).__name__}"))
        return QuantifiedValue(
            quantifier=QuantityQuantifier.UNKNOWN,
            derivation=QuantityDerivation.MISSING,
        )


def _compile_composition_quantity(
    draft: SemanticQuantityDraft | None,
    source_texts: dict[str, str],
    field_path: str,
    issues: list[ExtractionIssue],
) -> QuantifiedValue | None:
    if draft is None:
        return None
    if any("岁" in item.quote for item in draft.evidence):
        count_pattern = r"(?:[1-9]\d*|[一二两三四五六七八九十]+)\s*(?:个|名|位)?(?:孩子|儿童|小孩|老人|长辈)"
        if not any(re.search(count_pattern, item.quote) for item in draft.evidence):
            issues.append(
                _compiler_issue(field_path, "age evidence cannot establish party composition")
            )
            return None
    return _compile_quantity(draft, source_texts, field_path, issues)


def _compile_commitment(
    draft: SemanticTravelCommitmentDraft | None,
    source_texts: dict[str, str],
    field_path: str,
    issues: list[ExtractionIssue],
) -> TravelCommitment | None:
    if draft is None:
        return None
    try:
        return TravelCommitment(
            location_text=draft.location_text,
            at_text=draft.at_text,
            evidence=_compile_evidence(draft.evidence, source_texts),
        )
    except (ValueError, SemanticCompilationError) as exc:
        issues.append(_compiler_issue(field_path, f"invalid travel commitment: {type(exc).__name__}"))
        return None


def compile_semantic_draft(
    draft: TripIntakeSemanticDraft,
    sources: list[IntakeSource],
) -> TripIntakeExtraction:
    """Compile model quotes into validated public v2 values without guessing offsets."""

    source_texts = {source.source_id: source.text for source in sources}
    compiler_issues: list[ExtractionIssue] = []
    mentions: list[LocationMention] = []
    index_to_id: dict[int, str] = {}
    for index, item in enumerate(draft.locations):
        try:
            mention_id = f"location-{len(mentions) + 1}"
            mentions.append(
                LocationMention(
                    mention_id=mention_id,
                    raw_text=item.raw_text,
                    normalized_name=item.normalized_name,
                    country_code=item.country_code,
                    entity_type=item.entity_type,
                    role=item.role,
                    confidence=item.confidence,
                    evidence=_narrow_location_evidence(
                        _compile_evidence(item.evidence, source_texts),
                        item.raw_text,
                    ),
                )
            )
            index_to_id[index] = mention_id
        except (ValueError, SemanticCompilationError) as exc:
            compiler_issues.append(
                _compiler_issue(
                    f"locations[{index}]",
                    f"invalid semantic location: {type(exc).__name__}",
                )
            )

    primary_id = (
        index_to_id.get(draft.primary_location_index)
        if draft.primary_location_index is not None
        else None
    )
    status = draft.location_status
    primary_mentions = [item for item in mentions if item.role == LocationRole.PRIMARY_DESTINATION]
    if status == LocationStatus.EXACT:
        selected = next((item for item in mentions if item.mention_id == primary_id), None)
        if selected is None or selected.role != LocationRole.PRIMARY_DESTINATION:
            status = LocationStatus.UNCERTAIN if mentions else LocationStatus.MISSING
            primary_id = None
            compiler_issues.append(
                _compiler_issue(
                    "locations.primary_city",
                    "EXACT semantic location did not identify a valid primary destination",
                )
            )
    else:
        primary_id = None
        if primary_mentions:
            replacement_role = (
                LocationRole.DESTINATION_CANDIDATE
                if status in {LocationStatus.MULTIPLE, LocationStatus.UNCERTAIN}
                else LocationRole.OTHER_MENTION
            )
            mentions = [
                item.model_copy(update={"role": replacement_role})
                if item.role == LocationRole.PRIMARY_DESTINATION
                else item
                for item in mentions
            ]

    party_issues: list[ExtractionIssue] = []
    party = PartySizeExtraction(
        total=_compile_quantity(
            draft.party_size.total,
            source_texts,
            "party_size.total",
            party_issues,
        ),
        composition=PartyComposition(
            adults=_compile_composition_quantity(
                draft.party_size.composition.adults,
                source_texts,
                "party_size.composition.adults",
                party_issues,
            ),
            children=_compile_composition_quantity(
                draft.party_size.composition.children,
                source_texts,
                "party_size.composition.children",
                party_issues,
            ),
            elderly=_compile_composition_quantity(
                draft.party_size.composition.elderly,
                source_texts,
                "party_size.composition.elderly",
                party_issues,
            ),
            tags=list(dict.fromkeys(draft.party_size.composition.tags)),
        ),
    )

    temporal_issues: list[ExtractionIssue] = []
    date_range = None
    if draft.temporal.date_range is not None:
        item = draft.temporal.date_range
        try:
            date_range = DateRangeExpression(
                raw_text=item.raw_text,
                start=item.start,
                end=item.end,
                inclusive=item.inclusive,
                evidence=_compile_evidence(item.evidence, source_texts),
            )
        except (ValueError, SemanticCompilationError) as exc:
            temporal_issues.append(
                _compiler_issue(
                    "temporal.date_range",
                    f"invalid semantic date range: {type(exc).__name__}",
                )
            )
    temporal = TemporalExtraction(
        days=_compile_quantity(
            draft.temporal.days,
            source_texts,
            "temporal.days",
            temporal_issues,
        ),
        nights=_compile_quantity(
            draft.temporal.nights,
            source_texts,
            "temporal.nights",
            temporal_issues,
        ),
        date_range=date_range,
        arrival=_compile_commitment(
            draft.temporal.arrival,
            source_texts,
            "temporal.arrival",
            temporal_issues,
        ),
        departure=_compile_commitment(
            draft.temporal.departure,
            source_texts,
            "temporal.departure",
            temporal_issues,
        ),
    )

    preference_issues: list[ExtractionIssue] = []
    preference_items: list[PreferenceItem] = []
    preference_ids: set[str] = set()
    for index, item in enumerate(draft.preferences.items):
        try:
            preference_items.append(
                PreferenceItem(
                    item_id=_preference_item_id(item, preference_ids),
                    category=item.category,
                    label=item.label,
                    polarity=item.polarity,
                    operator=item.operator,
                    value=item.value,
                    unit=item.unit,
                    currency=item.currency,
                    applies_to=item.applies_to,
                    confidence=item.confidence,
                    evidence=_compile_evidence(item.evidence, source_texts),
                )
            )
        except (ValueError, SemanticCompilationError) as exc:
            preference_issues.append(
                _compiler_issue(
                    f"preferences.items[{index}]",
                    f"invalid semantic preference: {type(exc).__name__}",
                )
            )
    try:
        pace = PacePreference(
            value=draft.preferences.pace.value,
            confidence=draft.preferences.pace.confidence,
            evidence=_compile_evidence(draft.preferences.pace.evidence, source_texts),
        )
    except (ValueError, SemanticCompilationError) as exc:
        preference_issues.append(
            _compiler_issue("preferences.pace", f"invalid semantic pace: {type(exc).__name__}")
        )
        pace = PacePreference()
    try:
        no_preference_evidence = _compile_evidence(
            draft.preferences.no_preference_evidence,
            source_texts,
        )
    except SemanticCompilationError as exc:
        preference_issues.append(
            _compiler_issue(
                "preferences.no_preference_evidence",
                f"invalid no-preference evidence: {type(exc).__name__}",
            )
        )
        no_preference_evidence = []

    preference_status = draft.preferences.status
    has_specified = bool(preference_items) or pace.value not in {
        PaceValue.UNSPECIFIED,
        PaceValue.NO_PREFERENCE,
    }
    if preference_status == PreferenceStatus.NO_PREFERENCE and (
        has_specified or not no_preference_evidence
    ):
        preference_status = PreferenceStatus.SPECIFIED if has_specified else PreferenceStatus.UNSPECIFIED
        no_preference_evidence = []
        preference_issues.append(
            _compiler_issue(
                "preferences",
                "NO_PREFERENCE requires explicit evidence and no specified values",
            )
        )
    elif preference_status == PreferenceStatus.SPECIFIED and not has_specified:
        preference_status = PreferenceStatus.UNSPECIFIED
        preference_issues.append(
            _compiler_issue("preferences", "SPECIFIED preferences contained no valid values")
        )
    elif preference_status == PreferenceStatus.UNSPECIFIED and has_specified:
        preference_status = PreferenceStatus.SPECIFIED
    if preference_status != PreferenceStatus.NO_PREFERENCE:
        no_preference_evidence = []
        if pace.value == PaceValue.NO_PREFERENCE:
            pace = PacePreference()

    preferences = PreferenceExtraction(
        status=preference_status,
        items=preference_items,
        pace=pace,
        no_preference_evidence=no_preference_evidence,
    )

    model_issues: list[ExtractionIssue] = []
    for index, item in enumerate(draft.issues):
        try:
            model_issues.append(
                ExtractionIssue(
                    code=item.code,
                    field_path=item.field_path,
                    message=item.message,
                    blocking=item.blocking,
                    evidence=_compile_evidence(item.evidence, source_texts),
                )
            )
        except (ValueError, SemanticCompilationError):
            compiler_issues.append(
                _compiler_issue(f"issues[{index}]", "invalid semantic issue was dropped")
            )

    extraction = TripIntakeExtraction(
        locations=LocationExtraction(
            mentions=mentions,
            primary_mention_id=primary_id,
            status=status,
        ),
        party_size=party,
        temporal=temporal,
        preferences=preferences,
        issues=[
            *model_issues,
            *compiler_issues,
            *party_issues,
            *temporal_issues,
            *preference_issues,
        ],
        readiness=IntakeReadiness.NEEDS_CONFIRMATION,
    )
    validate_extraction_evidence(extraction, source_texts)
    return extraction
