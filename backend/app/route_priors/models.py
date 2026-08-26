from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PriorContribution(str, Enum):
    CONTENT_RELEVANCE = "CONTENT_RELEVANCE"
    DIVERSITY = "DIVERSITY"
    ROUTE_ADJACENCY = "ROUTE_ADJACENCY"


class ProhibitedClaim(str, Enum):
    CURRENT_OPENING = "CURRENT_OPENING"
    CURRENT_RESERVATION = "CURRENT_RESERVATION"
    CURRENT_PRICE = "CURRENT_PRICE"
    CURRENT_ACCESSIBILITY = "CURRENT_ACCESSIBILITY"
    CURRENT_ROUTE_TIME = "CURRENT_ROUTE_TIME"
    CURRENT_POPULARITY = "CURRENT_POPULARITY"
    CANONICAL_IDENTITY = "CANONICAL_IDENTITY"
    COORDINATES = "COORDINATES"


class RouteSequenceKind(str, Enum):
    EXPLICIT_DIRECTIONAL_SEQUENCE = "EXPLICIT_DIRECTIONAL_SEQUENCE"
    ARTICLE_CLUSTER_ORDER = "ARTICLE_CLUSTER_ORDER"


class RouteSequencePrior(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prior_id: str = Field(min_length=1, max_length=120)
    sequence_kind: RouteSequenceKind
    query_hints: tuple[str, ...] = Field(min_length=2)
    basis_code: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def validate_query_hints(self) -> "RouteSequencePrior":
        normalized = [" ".join(value.split()).casefold() for value in self.query_hints]
        if any(not value for value in normalized):
            raise ValueError("route prior query hints must be non-empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("route prior query hints must be unique")
        return self


class PriorLicence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    spdx: Literal["CC-BY-SA-4.0"]
    url: Literal["https://creativecommons.org/licenses/by-sa/4.0/"]
    attribution: str = Field(min_length=12)
    rights_source_document_id: str = Field(min_length=3)


class PriorDerivation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    raw_archive_path: str = Field(min_length=1)
    source_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_fields: tuple[str, ...] = Field(min_length=1)
    method: str = Field(min_length=12)


class CommunityRoutePrior(BaseModel):
    """Unresolved query hints, never canonical places or current facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["community-route-prior-v1"]
    artifact_kind: Literal["COMMUNITY_ROUTE_PRIOR"]
    source_kind: Literal["wikivoyage_community"]
    source_document_id: str = Field(min_length=3)
    canonical_url: str = Field(pattern=r"^https://")
    city: Literal["北京", "上海", "杭州"]
    article_title: str = Field(min_length=1)
    source_revision: str = Field(pattern=r"^[1-9][0-9]*$")
    revision_url: str = Field(pattern=r"^https://en\.wikivoyage\.org/w/index\.php\?.*oldid=[1-9][0-9]*")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    licence: PriorLicence
    identity_policy: Literal["UNRESOLVED_QUERY_HINTS_ONLY_PROVIDER_RECEIPT_REQUIRED"]
    allowed_use: tuple[Literal["STRUCTURE", "RETRIEVAL", "EVAL_ONLY"], ...] = Field(min_length=1)
    allowed_contributions: frozenset[PriorContribution]
    route_sequences: tuple[RouteSequencePrior, ...] = ()
    related_place_query_hints: tuple[str, ...] = ()
    experience_tags: tuple[str, ...] = ()
    season_hints: tuple[str, ...] = ()
    audience_hints: tuple[str, ...] = ()
    prohibited_claims: frozenset[ProhibitedClaim]
    derivation: PriorDerivation

    @model_validator(mode="after")
    def enforce_non_fact_boundary(self) -> "CommunityRoutePrior":
        required = set(ProhibitedClaim)
        if not required <= set(self.prohibited_claims):
            missing = sorted(item.value for item in required - set(self.prohibited_claims))
            raise ValueError(f"community prior omits prohibited claims: {missing}")
        allowed = set(self.allowed_contributions)
        if not allowed or not allowed <= set(PriorContribution):
            raise ValueError("community prior has unsupported contribution")
        if self.route_sequences and PriorContribution.ROUTE_ADJACENCY not in allowed:
            raise ValueError("route sequences require ROUTE_ADJACENCY contribution")
        normalized = [" ".join(value.split()).casefold() for value in self.related_place_query_hints]
        if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("related place query hints must be non-empty and unique")
        return self


class PriorCandidateHint(BaseModel):
    """A non-canonical suggestion input that still requires Provider resolution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_hint: str = Field(min_length=1)
    city: Literal["北京", "上海", "杭州"]
    contributions: frozenset[PriorContribution]
    explanation_codes: tuple[str, ...]
    source_document_id: str
    source_revision: str
    revision_url: str
    content_sha256: str
    attribution: str
    license_spdx: Literal["CC-BY-SA-4.0"]
    requires_provider_resolution: Literal[True] = True


class OfficialPriorAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class OfficialPriorReference(BaseModel):
    """Hash-bound reference to a minimal official archive, not a fact receipt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_document_id: str = Field(min_length=3)
    canonical_url: str = Field(pattern=r"^https://")
    city: Literal["北京", "上海", "杭州"]
    raw_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    extract_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    captured_at: str = Field(min_length=10)
    allowed_use: tuple[Literal["STRUCTURE", "EVAL_ONLY"], Literal["STRUCTURE", "EVAL_ONLY"]]
    requires_provider_resolution: Literal[True] = True
    establishes_current_facts: Literal[False] = False

    @model_validator(mode="after")
    def enforce_allowed_use(self) -> "OfficialPriorReference":
        if self.allowed_use != ("STRUCTURE", "EVAL_ONLY"):
            raise ValueError("official prior allowed_use must be STRUCTURE/EVAL_ONLY")
        return self


class OfficialCandidateHint(BaseModel):
    """An unresolved adjacent query derived from a verified official route."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_hint: str = Field(min_length=1)
    city: Literal["北京", "上海", "杭州"]
    contributions: frozenset[Literal[PriorContribution.ROUTE_ADJACENCY]]
    explanation_codes: tuple[
        Literal["OFFICIAL_ROUTE_NEIGHBOR"], Literal["PROVIDER_RESOLUTION_REQUIRED"]
    ]
    official_prior_refs: tuple[OfficialPriorReference, ...] = Field(min_length=1)
    requires_provider_resolution: Literal[True] = True


class CityOfficialPriorStatus(BaseModel):
    """Makes missing/unarchived official evidence explicit to product callers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    city: Literal["北京", "上海", "杭州"]
    availability: OfficialPriorAvailability
    available_source_refs: tuple[OfficialPriorReference, ...] = ()
    unavailable_source_ids: tuple[str, ...] = ()
    reason_code: Literal["VERIFIED_ARCHIVE_AVAILABLE", "OFFICIAL_ARCHIVE_UNAVAILABLE"]

    @model_validator(mode="after")
    def validate_availability(self) -> "CityOfficialPriorStatus":
        if self.availability is OfficialPriorAvailability.AVAILABLE:
            if not self.available_source_refs or self.reason_code != "VERIFIED_ARCHIVE_AVAILABLE":
                raise ValueError("available official status requires a verified archive reference")
        elif self.available_source_refs or self.reason_code != "OFFICIAL_ARCHIVE_UNAVAILABLE":
            raise ValueError("unavailable official status cannot contain an archive reference")
        return self


class RoutePriorSignals(BaseModel):
    """Unified safe projection for later ranking integration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    city: Literal["北京", "上海", "杭州"]
    community_hints: tuple[PriorCandidateHint, ...]
    official_hints: tuple[OfficialCandidateHint, ...]
    official_status: CityOfficialPriorStatus
