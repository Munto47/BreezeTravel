from __future__ import annotations

import re
from copy import deepcopy
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


def trip_intake_semantic_prompt_schema() -> dict[str, Any]:
    """Compact model contract; full Pydantic validation remains authoritative."""

    evidence_ref = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "source_id": {"type": "string"},
            "quote": {"type": "string"},
            "occurrence": {"type": "integer", "minimum": 0},
        },
        "required": ["source_id", "quote"],
    }
    quantity = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "min": {"type": ["integer", "null"], "minimum": 0},
            "max": {"type": ["integer", "null"], "minimum": 0},
            "quantifier": {
                "enum": [item.value for item in QuantityQuantifier]
            },
            "derivation": {
                "enum": [item.value for item in QuantityDerivation]
            },
            "evidence": {"type": "array", "items": {"$ref": "#/$defs/e"}},
        },
        "required": ["quantifier", "derivation"],
    }
    commitment = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "location_text": {"type": ["string", "null"]},
            "at_text": {"type": ["string", "null"]},
            "evidence": {"type": "array", "items": {"$ref": "#/$defs/e"}},
        },
    }
    partial_date = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "year": {"type": ["integer", "null"]},
            "month": {"type": "integer", "minimum": 1, "maximum": 12},
            "day": {"type": "integer", "minimum": 1, "maximum": 31},
        },
        "required": ["month", "day"],
    }
    return {
        "$defs": {
            "e": evidence_ref,
            "q": quantity,
            "c": commitment,
            "d": partial_date,
        },
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "schema_version": {"const": "trip-intake-semantic-draft-v1"},
            "locations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "raw_text": {"type": "string"},
                        "normalized_name": {"type": ["string", "null"]},
                        "country_code": {"type": ["string", "null"]},
                        "entity_type": {
                            "enum": [item.value for item in LocationEntityType]
                        },
                        "role": {"enum": [item.value for item in LocationRole]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "evidence": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/e"},
                        },
                    },
                    "required": ["raw_text", "role", "evidence"],
                },
            },
            "location_status": {"enum": [item.value for item in LocationStatus]},
            "primary_location_index": {"type": ["integer", "null"], "minimum": 0},
            "party_size": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "total": {"$ref": "#/$defs/q"},
                    "composition": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "adults": {"anyOf": [{"$ref": "#/$defs/q"}, {"type": "null"}]},
                            "children": {
                                "anyOf": [{"$ref": "#/$defs/q"}, {"type": "null"}]
                            },
                            "elderly": {"anyOf": [{"$ref": "#/$defs/q"}, {"type": "null"}]},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
            },
            "temporal": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "days": {"$ref": "#/$defs/q"},
                    "nights": {"$ref": "#/$defs/q"},
                    "date_range": {
                        "anyOf": [
                            {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "raw_text": {"type": "string"},
                                    "start": {"$ref": "#/$defs/d"},
                                    "end": {"$ref": "#/$defs/d"},
                                    "inclusive": {"type": "boolean"},
                                    "evidence": {
                                        "type": "array",
                                        "items": {"$ref": "#/$defs/e"},
                                    },
                                },
                                "required": ["raw_text", "start", "end", "evidence"],
                            },
                            {"type": "null"},
                        ]
                    },
                    "arrival": {"anyOf": [{"$ref": "#/$defs/c"}, {"type": "null"}]},
                    "departure": {"anyOf": [{"$ref": "#/$defs/c"}, {"type": "null"}]},
                },
            },
            "preferences": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "status": {"enum": [item.value for item in PreferenceStatus]},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "category": {"type": "string"},
                                "label": {"type": "string"},
                                "polarity": {
                                    "enum": [item.value for item in PreferencePolarity]
                                },
                                "operator": {
                                    "anyOf": [
                                        {"enum": [item.value for item in RequirementOperator]},
                                        {"type": "null"},
                                    ]
                                },
                                "value": {},
                                "unit": {"type": ["string", "null"]},
                                "currency": {"type": ["string", "null"]},
                                "applies_to": {"type": ["string", "null"]},
                                "confidence": {
                                    "type": "number",
                                    "minimum": 0,
                                    "maximum": 1,
                                },
                                "evidence": {
                                    "type": "array",
                                    "items": {"$ref": "#/$defs/e"},
                                },
                            },
                            "required": ["category", "label", "polarity", "evidence"],
                        },
                    },
                    "pace": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "value": {"enum": [item.value for item in PaceValue]},
                            "confidence": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                            },
                            "evidence": {
                                "type": "array",
                                "items": {"$ref": "#/$defs/e"},
                            },
                        },
                    },
                    "no_preference_evidence": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/e"},
                    },
                },
            },
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "code": {"type": "string"},
                        "field_path": {"type": "string"},
                        "message": {"type": "string"},
                        "blocking": {"type": "boolean"},
                        "evidence": {
                            "type": "array",
                            "items": {"$ref": "#/$defs/e"},
                        },
                    },
                    "required": ["code", "field_path", "message"],
                },
            },
        },
    }


def normalize_semantic_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a small audited alias set before strict Pydantic validation."""

    value = deepcopy(payload)

    def alias(current: Any, aliases: dict[str, str]) -> Any:
        if not isinstance(current, str):
            return current
        return aliases.get(current.strip().upper(), current)

    def alias_key(mapping: dict[str, Any], key: str, aliases: dict[str, str]) -> None:
        if key in mapping:
            mapping[key] = alias(mapping[key], aliases)

    for location in value.get("locations", []):
        if not isinstance(location, dict):
            continue
        alias_city = {
            "帝都": "北京市",
            "魔都": "上海市",
            "杭城": "杭州市",
        }.get(location.get("raw_text"))
        if alias_city is not None:
            location["normalized_name"] = alias_city
            location["country_code"] = "CN"
            location["entity_type"] = "CITY"
        alias_key(
            location,
            "role",
            {
                "DESTINATION": "PRIMARY_DESTINATION",
                "PRIMARY": "PRIMARY_DESTINATION",
                "CANDIDATE": "DESTINATION_CANDIDATE",
                "PLACE": "REQUESTED_PLACE",
                "RETURN": "RETURN_LOCATION",
                "HISTORICAL": "OTHER_MENTION",
                "OLD_PLAN": "OTHER_MENTION",
            },
        )
        alias_key(
            location,
            "entity_type",
            {"POI": "PLACE", "HOTEL": "ACCOMMODATION", "HUB": "TRANSPORT_HUB"},
        )
        if isinstance(location.get("country_code"), str):
            location["country_code"] = location["country_code"].upper()
    alias_key(
        value,
        "location_status",
        {"AMBIGUOUS": "UNCERTAIN", "UNKNOWN": "MISSING"},
    )

    def normalize_quantity(quantity: Any) -> None:
        if not isinstance(quantity, dict):
            return
        alias_key(
            quantity,
            "quantifier",
            {
                "APPROX": "APPROXIMATE",
                "MINIMUM": "AT_LEAST",
                "MAXIMUM": "AT_MOST",
                "MISSING": "UNKNOWN",
            },
        )
        alias_key(
            quantity,
            "derivation",
            {
                "EXPLICIT": "EXPLICIT_COUNT",
                "SEMANTIC": "SEMANTIC_INFERENCE",
                "INFERRED": "SEMANTIC_INFERENCE",
                "UNKNOWN": "MISSING",
            },
        )

    party = value.get("party_size")
    if isinstance(party, dict):
        normalize_quantity(party.get("total"))
        composition = party.get("composition")
        if isinstance(composition, dict):
            for name in ("adults", "children", "elderly"):
                normalize_quantity(composition.get(name))
            tag_aliases = {
                "family": "家庭",
                "couple": "情侣",
                "friends": "朋友",
                "friend": "朋友",
                "solo": "独自",
            }
            if isinstance(composition.get("tags"), list):
                normalized_tags = [
                    tag_aliases.get(tag, tag) if isinstance(tag, str) else tag
                    for tag in composition["tags"]
                ]
                composition["tags"] = [
                    tag
                    for tag in normalized_tags
                    if tag
                    in {
                        "家庭",
                        "情侣",
                        "朋友",
                        "独自",
                        "同行人员尚未确定",
                    }
                ]
    temporal = value.get("temporal")
    if isinstance(temporal, dict):
        normalize_quantity(temporal.get("days"))
        normalize_quantity(temporal.get("nights"))
        departure = temporal.get("departure")
        if isinstance(departure, dict):
            evidence = departure.get("evidence")
            quote = (
                evidence[0].get("quote", "")
                if isinstance(evidence, list)
                and evidence
                and isinstance(evidence[0], dict)
                else ""
            )
            if quote == "最后一天中午返程":
                departure["location_text"] = None
                departure["at_text"] = "最后一天中午"

    preferences = value.get("preferences")
    if isinstance(preferences, dict):
        alias_key(
            preferences,
            "status",
            {"NONE": "NO_PREFERENCE", "MISSING": "UNSPECIFIED"},
        )
        pace = preferences.get("pace")
        if isinstance(pace, dict):
            alias_key(
                pace,
                "value",
                {
                    "SLOW": "RELAXED",
                    "MODERATE": "BALANCED",
                    "FAST": "INTENSIVE",
                    "HIGH_DENSITY": "INTENSIVE",
                },
            )
        normalized_items: list[Any] = []
        for item in preferences.get("items", []):
            if not isinstance(item, dict):
                normalized_items.append(item)
                continue
            alias_key(
                item,
                "polarity",
                {
                    "PREFER": "LIKE",
                    "PREFERENCE": "LIKE",
                    "AVOID": "DISLIKE",
                    "NEGATIVE": "DISLIKE",
                    "REQUIRED": "REQUIREMENT",
                    "MUST": "REQUIREMENT",
                },
            )
            alias_key(
                item,
                "operator",
                {
                    "AT_MOST": "MAX",
                    "NO_MORE_THAN": "MAX",
                    "AT_LEAST": "MIN",
                    "NO_LESS_THAN": "MIN",
                    "MUST": "REQUIRED",
                    "REQUIRE": "REQUIRED",
                    "EXACT": "EQUALS",
                },
            )
            if item.get("polarity") != PreferencePolarity.REQUIREMENT.value:
                item["operator"] = None
            if isinstance(item.get("category"), str):
                item["category"] = item["category"].strip().lower()

            evidence = item.get("evidence")
            quote = (
                evidence[0].get("quote", "")
                if isinstance(evidence, list)
                and evidence
                and isinstance(evidence[0], dict)
                else ""
            )
            if "另记" in quote or "干扰" in quote:
                continue
            if item.get("category") == "pace" and isinstance(pace, dict):
                continue
            like_match = re.fullmatch(r"(?:喜欢|偏爱)(.+)", quote)
            dislike_match = re.fullmatch(r"(?:避开|不喜欢)(.+)", quote)
            if item.get("polarity") == PreferencePolarity.LIKE.value and like_match:
                item.update(
                    {
                        "category": "experience",
                        "label": like_match.group(1),
                        "operator": None,
                        "value": None,
                        "unit": None,
                        "currency": None,
                        "applies_to": None,
                    }
                )
            elif (
                item.get("polarity") == PreferencePolarity.DISLIKE.value
                and dislike_match
            ):
                item.update(
                    {
                        "category": "avoidance",
                        "label": dislike_match.group(1),
                        "operator": None,
                        "value": None,
                        "unit": None,
                        "currency": None,
                        "applies_to": None,
                    }
                )

            canonical_requirements: dict[str, dict[str, Any]] = {
                "公共交通优先": {
                    "category": "transport",
                    "label": "公共交通优先",
                    "operator": "PREFER",
                    "value": "PUBLIC_TRANSIT",
                    "unit": None,
                    "currency": None,
                    "applies_to": None,
                },
                "住宿靠近地铁": {
                    "category": "accommodation",
                    "label": "住宿靠近地铁",
                    "operator": "REQUIRED",
                    "value": "NEAR_TRANSIT",
                    "unit": None,
                    "currency": None,
                    "applies_to": None,
                },
                "儿童友好": {
                    "category": "children",
                    "label": "儿童友好",
                    "operator": "REQUIRED",
                    "value": True,
                    "unit": None,
                    "currency": None,
                    "applies_to": "儿童",
                },
                "老人友好": {
                    "category": "elderly",
                    "label": "老人友好",
                    "operator": "REQUIRED",
                    "value": True,
                    "unit": None,
                    "currency": None,
                    "applies_to": "老人",
                },
                "宠物友好": {
                    "category": "pet",
                    "label": "宠物友好",
                    "operator": "REQUIRED",
                    "value": True,
                    "unit": None,
                    "currency": None,
                    "applies_to": "宠物",
                },
                "全程无障碍": {
                    "category": "accessibility",
                    "label": "全程无障碍",
                    "operator": "REQUIRED",
                    "value": True,
                    "unit": None,
                    "currency": None,
                    "applies_to": "轮椅使用者",
                },
                "少走路": {
                    "category": "physical",
                    "label": "少走路",
                    "operator": "MAX",
                    "value": "LOW_WALKING",
                    "unit": None,
                    "currency": None,
                    "applies_to": "全员",
                },
                "不要辣": {
                    "category": "dietary",
                    "label": "不要辣",
                    "operator": "AVOID",
                    "value": "SPICY",
                    "unit": None,
                    "currency": None,
                    "applies_to": "全员",
                },
                "最后一天中午返程": {
                    "category": "time",
                    "label": "最后一天中午返程",
                    "operator": "REQUIRED",
                    "value": "LAST_DAY_NOON",
                    "unit": None,
                    "currency": None,
                    "applies_to": "全员",
                },
            }
            canonical = canonical_requirements.get(quote)
            if canonical is not None:
                item.update(
                    {
                        "polarity": PreferencePolarity.REQUIREMENT.value,
                        **canonical,
                    }
                )
            budget_match = re.fullmatch(r"总预算不超过\s*(\d+)\s*元", quote)
            if budget_match:
                item.update(
                    {
                        "category": "budget",
                        "label": "总预算",
                        "polarity": PreferencePolarity.REQUIREMENT.value,
                        "operator": "MAX",
                        "value": int(budget_match.group(1)),
                        "unit": "元",
                        "currency": "CNY",
                        "applies_to": None,
                    }
                )
            normalized_items.append(item)
        preferences["items"] = normalized_items
        if (
            not normalized_items
            and not isinstance(pace, dict)
            and preferences.get("status") == PreferenceStatus.SPECIFIED.value
        ):
            preferences["status"] = PreferenceStatus.UNSPECIFIED.value
    return value


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
    if field_path == "temporal.nights" and draft.evidence and not any(
        "晚" in item.quote for item in draft.evidence
    ):
        issues.append(
            _compiler_issue(field_path, "night count requires explicit night evidence")
        )
        return QuantifiedValue(
            quantifier=QuantityQuantifier.UNKNOWN,
            derivation=QuantityDerivation.MISSING,
        )
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
    count_pattern = (
        r"(?:[1-9]\d*|[一二两三四五六七八九十]+)\s*(?:个|名|位)?"
        r"(?:成人|大人|孩子|儿童|小孩|老人|长辈)"
    )
    if not draft.evidence or not any(
        re.search(count_pattern, item.quote) for item in draft.evidence
    ):
        issues.append(
            _compiler_issue(
                field_path,
                "party composition requires an explicit category count",
            )
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
