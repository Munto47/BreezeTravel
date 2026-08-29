from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


DATASET_VERSION = "g01-text-card-dataset-v1"
INPUT_SCHEMA_VERSION = "g01-text-card-input-v1"
ANNOTATION_SCHEMA_VERSION = "g01-text-card-annotation-bundle-v1"
ADJUDICATION_SCHEMA_VERSION = "g01-text-card-adjudication-bundle-v1"
PREDICTION_SCHEMA_VERSION = "g01-text-card-prediction-v1"
RUNTIME_EVIDENCE_SCHEMA_VERSION = "g01-text-card-runtime-evidence-v1"

Split = Literal["dev", "validation", "frozen_blind"]
Cohort = Literal["DEEP_CITY", "OTHER_CITY", "ADVERSARIAL"]
Role = Literal["PLANNED", "OPTIONAL", "REFERENCE", "EXCLUDED", "PASS_THROUGH"]
SemanticKind = Literal["PLACE", "URL", "DESCRIPTION", "RESERVATION", "OTHER"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def normalized_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(normalized_text(value).encode("utf-8")).hexdigest()


def canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InputLineage(StrictModel):
    data_origin: Literal["HIGH_FIDELITY_SYNTHETIC"]
    generator_id: Literal["g01-text-card-input-generator-v1"]
    template_family_id: str = Field(pattern=r"^g01-template-[a-z0-9-]+$")
    mutation_parent_case_id: str | None = Field(default=None, pattern=r"^G01-TC-\d{3}$")


class TextCardInputCase(StrictModel):
    schema_version: Literal["g01-text-card-input-v1"] = INPUT_SCHEMA_VERSION
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    family_id: str = Field(pattern=r"^G01-F\d{3}$")
    variant_id: Literal["A", "B", "C"]
    split: Split
    cohort: Cohort
    city_scope: list[str] = Field(min_length=1, max_length=4)
    input_text: str = Field(min_length=240, max_length=50000)
    normalized_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lineage: InputLineage

    @model_validator(mode="after")
    def hash_matches_text(self) -> "TextCardInputCase":
        if self.normalized_input_sha256 != sha256_text(self.input_text):
            raise ValueError("normalized_input_sha256 does not match input_text")
        if len(set(self.city_scope)) != len(self.city_scope):
            raise ValueError("city_scope must not contain duplicates")
        return self


class HumanAttestation(StrictModel):
    actor_id: str = Field(min_length=3, max_length=120)
    completed_at: datetime
    is_authorized_human: Literal[True]
    worked_independently: Literal[True]
    saw_peer_labels_before_submission: Literal[False]
    automated_suggestions_used: Literal[False]


class CanonicalPlaceLabel(StrictModel):
    place_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    city: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    authority: Literal["HUMAN_VERIFIED_PROVIDER_RECEIPT"]
    receipt_ref: str = Field(min_length=1, max_length=300)


class MentionAnnotation(StrictModel):
    mention_id: str = Field(min_length=1, max_length=100)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    raw_text: str = Field(min_length=1)
    semantic_kind: SemanticKind
    role: Role
    day_index: int | None = Field(default=None, ge=1, le=14)
    atomic_place_name: str | None = Field(default=None, max_length=100)
    executable_place: bool
    canonical_place: CanonicalPlaceLabel | None = None

    @model_validator(mode="after")
    def semantic_consistency(self) -> "MentionAnnotation":
        if self.span_end <= self.span_start:
            raise ValueError("annotation span must be non-empty")
        expected_executable = (
            self.semantic_kind == "PLACE"
            and self.role == "PLANNED"
            and self.day_index is not None
            and bool((self.atomic_place_name or "").strip())
        )
        if self.executable_place != expected_executable:
            raise ValueError("executable_place disagrees with the locked eligibility rule")
        if self.semantic_kind != "PLACE" and (
            self.atomic_place_name is not None or self.canonical_place is not None
        ):
            raise ValueError("non-place annotations cannot carry place truth")
        if self.canonical_place is not None and not self.executable_place:
            raise ValueError("canonical place truth is only valid for executable mentions")
        return self


class CaseAnnotation(StrictModel):
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_name: str = Field(min_length=1, max_length=80)
    mentions: list[MentionAnnotation] = Field(min_length=1)


class AnnotationBundle(StrictModel):
    schema_version: Literal["g01-text-card-annotation-bundle-v1"] = ANNOTATION_SCHEMA_VERSION
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    assignment_id: str = Field(min_length=8, max_length=120)
    split: Split
    attestation: HumanAttestation
    cases: list[CaseAnnotation] = Field(min_length=1)


class ConflictResolution(StrictModel):
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    conflict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_note: str = Field(min_length=12, max_length=1000)


class AdjudicationAttestation(StrictModel):
    actor_id: str = Field(min_length=3, max_length=120)
    completed_at: datetime
    is_authorized_human: Literal[True]
    reviewed_both_independent_bundles: Literal[True]
    automated_adjudication_used: Literal[False]


class AdjudicationBundle(StrictModel):
    schema_version: Literal["g01-text-card-adjudication-bundle-v1"] = ADJUDICATION_SCHEMA_VERSION
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    split: Split
    source_assignment_ids: list[str] = Field(min_length=2, max_length=2)
    source_bundle_sha256: list[str] = Field(min_length=2, max_length=2)
    attestation: AdjudicationAttestation
    conflicts: list[ConflictResolution]
    gold_cases: list[CaseAnnotation] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_sources(self) -> "AdjudicationBundle":
        if len(set(self.source_assignment_ids)) != 2:
            raise ValueError("adjudication requires two distinct assignments")
        if len(set(self.source_bundle_sha256)) != 2:
            raise ValueError("adjudication requires two distinct source bundles")
        return self


class PredictedMention(StrictModel):
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    raw_text: str = Field(min_length=1)
    role: Role
    day_index: int | None = Field(default=None, ge=1, le=14)
    atomic_place_name: str | None = None
    eligible_for_place_search: bool
    resolution_status: Literal["NOT_ELIGIBLE", "UNRESOLVED", "AUTO_MATCHED", "NEEDS_CONFIRMATION"]
    canonical_place_id: str | None = None
    canonical_city: str | None = None
    canonical_category: str | None = None

    @model_validator(mode="after")
    def resolution_consistency(self) -> "PredictedMention":
        canonical_values = (
            self.canonical_place_id,
            self.canonical_city,
            self.canonical_category,
        )
        if self.resolution_status == "AUTO_MATCHED":
            if any(value is None for value in canonical_values):
                raise ValueError("AUTO_MATCHED predictions require complete canonical truth")
        elif any(value is not None for value in canonical_values):
            raise ValueError("non-auto-matched predictions cannot carry canonical truth")
        return self


class TextCardPrediction(StrictModel):
    schema_version: Literal["g01-text-card-prediction-v1"] = PREDICTION_SCHEMA_VERSION
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_name: str
    provider_binding: dict[str, object]
    mentions: list[PredictedMention]
    public_result: dict[str, object]
    measurement_scope: Literal["LOCAL_PIPELINE_ONLY", "PUBLIC_API_BROWSER"]
    first_progress_ms: float | None = Field(default=None, ge=0)
    cards_ready_ms: float | None = Field(default=None, ge=0)


class EvidenceArtifactRef(StrictModel):
    artifact_path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")


class RuntimeGateEvidence(StrictModel):
    schema_version: Literal["g01-text-card-runtime-evidence-v1"] = RUNTIME_EVIDENCE_SCHEMA_VERSION
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    qwen_binding_status: Literal["EXACT_ACCOUNT_BINDING_CONFIRMED", "NOT_READY"]
    amap_persistence_status: Literal["WRITTEN_PERMISSION_CONFIRMED", "BLOCKED_PENDING_WRITTEN_PERMISSION"]
    first_progress_max_ms: float = Field(ge=0)
    cards_ready_p95_ms: float = Field(ge=0)
    partial_result_on_provider_failure: bool
    browser_scenarios_pass: bool
    job_restart_lease_sse_pass: bool
    duplicate_event_side_effects: int = Field(ge=0)
    full_unauthorized_accesses: int = Field(ge=0)
    demo_unauthorized_accesses: int = Field(ge=0)
    ttl_claim_delete_readback_pass: bool
    privacy_leak_hits: int = Field(ge=0)
    budget_boundaries_pass: bool
    map_edit_provider_calls: int = Field(ge=0)
    map_logical_duplicate_calls: int = Field(ge=0)
    map_late_pointer_overwrites: int = Field(ge=0)
    map_fixture_trip_count: int = Field(ge=0)
    map_fixture_edge_count: int = Field(ge=0)
    map_fixture_usable_coverage: float = Field(ge=0, le=1)
    map_fixture_snapshot_p95_ms: float = Field(ge=0)
    map_live_usable_coverage: float = Field(ge=0, le=1)
    map_live_snapshot_p95_ms: float = Field(ge=0)
    artifacts: list[EvidenceArtifactRef] = Field(min_length=1)


def validate_case_annotation(case: CaseAnnotation, source: TextCardInputCase) -> None:
    if case.case_id != source.case_id:
        raise ValueError("annotation case_id does not match source")
    if case.source_sha256 != source.normalized_input_sha256:
        raise ValueError(f"{case.case_id} source hash does not match input")
    mention_ids: set[str] = set()
    spans: set[tuple[int, int]] = set()
    text = normalized_text(source.input_text)
    for mention in case.mentions:
        if mention.mention_id in mention_ids:
            raise ValueError(f"{case.case_id} has duplicate mention_id")
        mention_ids.add(mention.mention_id)
        span = (mention.span_start, mention.span_end)
        if span in spans:
            raise ValueError(f"{case.case_id} has duplicate annotation span")
        spans.add(span)
        if text[mention.span_start : mention.span_end] != mention.raw_text:
            raise ValueError(f"{case.case_id} annotation span does not match source text")
