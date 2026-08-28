from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from evals.agent_gate_v1.contracts import (
    AgentTaskAttestation,
    DetachedAuthoritySignature,
)
from evals.trip_text_cards_v1.contracts import (
    DATASET_VERSION,
    Role,
    SemanticKind,
    TextCardPrediction,
    TextCardInputCase,
    canonical_sha256,
    normalized_text,
)


AGENT_ANNOTATION_SCHEMA_VERSION = "g01-text-card-agent-annotation-bundle-v2"
AGENT_ADJUDICATION_SCHEMA_VERSION = "g01-text-card-agent-adjudication-bundle-v2"
AGENT_EVALUATION_CONTRACT_VERSION = "g01-text-card-agent-evaluation-contract-v2"
G01_GOAL_ID = "TC-VNEXT-G01-TEXT-CARDS"

AgentEvaluationSplit = Literal["dev", "validation"]
AgentRuntimeSplit = Literal["dev", "validation", "frozen_blind"]
DestinationBasis = Literal["EXPLICIT", "SOFT_ASSUMPTION"]
ProviderResolutionStatus = Literal["MATCHED", "UNRESOLVED", "AMBIGUOUS"]
ProviderExecutionMode = Literal["LIVE", "CONTROLLED_FIXTURE"]
InferenceExecutionMode = Literal["LIVE", "CONTROLLED_FIXTURE"]
PlaceBoundaryStatus = Literal["VERIFIED_ATOMIC", "UNCERTAIN", "NONE"]
PlaceBoundaryBasis = Literal[
    "PROVIDER_ACCEPTED_EXACT",
    "NONE",
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


def agent_input_bundle_sha256(
    split: str,
    source_cases: list[TextCardInputCase],
    provider_receipt_index_sha256: str,
) -> str:
    return canonical_sha256(
        {
            "goal_id": G01_GOAL_ID,
            "dataset_version": DATASET_VERSION,
            "split": split,
            "provider_receipt_index_sha256": provider_receipt_index_sha256,
            "cases": [
                {
                    "case_id": case.case_id,
                    "source_sha256": case.normalized_input_sha256,
                }
                for case in source_cases
            ],
        }
    )


class AgentAnnotationAttestation(AgentTaskAttestation):
    task_role: Literal["ANNOTATOR_A", "ANNOTATOR_B"]
    reasoning_effort: Literal["xhigh"] = "xhigh"
    saw_peer_output_before_submission: Literal[False] = False
    saw_candidate_predictions_before_submission: Literal[False] = False
    peer_output_visibility: Literal["NONE"] = "NONE"
    candidate_output_visibility: Literal["NONE"] = "NONE"
    raw_output_storage: Literal["REPOSITORY_EXTERNAL"] = "REPOSITORY_EXTERNAL"
    provider_receipts_used: Literal[True] = True


class ProviderReceiptRef(StrictModel):
    receipt_id: str = Field(min_length=8, max_length=160)
    provider: Literal["AMAP"] = "AMAP"
    execution_mode: ProviderExecutionMode
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_ref: str = Field(min_length=1, max_length=300)
    runtime_effect_id: str = Field(min_length=8, max_length=160)
    runtime_effect_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observed_at: datetime
    resolution_status: ProviderResolutionStatus
    accepted_source_name: str | None = Field(default=None, min_length=1, max_length=100)
    authorization_basis: Literal["OWNER_ATTESTED_EXISTING_AUTHORIZATION"] = (
        "OWNER_ATTESTED_EXISTING_AUTHORIZATION"
    )
    raw_response_in_git: Literal[False] = False
    retention: Literal["REDACTED_MINIMAL"] = "REDACTED_MINIMAL"

    @model_validator(mode="after")
    def accepted_source_name_matches_status(self) -> "ProviderReceiptRef":
        if self.resolution_status == "MATCHED" and self.accepted_source_name is None:
            raise ValueError("MATCHED provider receipts require the accepted source name")
        if self.resolution_status != "MATCHED" and self.accepted_source_name is not None:
            raise ValueError("non-matched provider receipts cannot accept a source name")
        return self


class ProviderPlaceReceiptRecord(ProviderReceiptRef):
    place_id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, min_length=1, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    accepted_source_names: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def selected_place_matches_resolution_status(self) -> "ProviderPlaceReceiptRecord":
        selected = (self.place_id, self.name, self.city, self.category)
        if self.resolution_status == "MATCHED" and any(value is None for value in selected):
            raise ValueError("MATCHED provider receipts require complete selected place facts")
        if self.resolution_status != "MATCHED" and any(value is not None for value in selected):
            raise ValueError("unresolved or ambiguous receipts cannot claim a selected place")
        if self.resolution_status == "MATCHED" and not self.accepted_source_names:
            raise ValueError("MATCHED receipts require at least one provider-bound source name")
        if (
            self.resolution_status == "MATCHED"
            and self.accepted_source_name not in self.accepted_source_names
        ):
            raise ValueError("selected source name is absent from provider-bound names")
        if self.resolution_status != "MATCHED" and self.accepted_source_names:
            raise ValueError("unresolved or ambiguous receipts cannot claim accepted source names")
        return self


class ProviderRuntimeEffectReceipt(StrictModel):
    effect_id: str = Field(min_length=8, max_length=160)
    effect_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider: Literal["AMAP"] = "AMAP"
    execution_mode: ProviderExecutionMode
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_status: ProviderResolutionStatus
    place_id: str | None = Field(default=None, min_length=1, max_length=200)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    city: str | None = Field(default=None, min_length=1, max_length=80)
    category: str | None = Field(default=None, min_length=1, max_length=80)
    accepted_source_names: list[str] = Field(max_length=20)
    started_at: datetime
    completed_at: datetime
    status: Literal["SUCCEEDED"] = "SUCCEEDED"
    raw_response_in_repository: Literal[False] = False

    @model_validator(mode="after")
    def runtime_effect_is_consistent(self) -> "ProviderRuntimeEffectReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("provider effect completed before it started")
        selected = (self.place_id, self.name, self.city, self.category)
        if self.resolution_status == "MATCHED":
            if any(value is None for value in selected) or not self.accepted_source_names:
                raise ValueError("MATCHED runtime effects require selected place and source names")
        elif any(value is not None for value in selected) or self.accepted_source_names:
            raise ValueError("non-matched runtime effects cannot carry selected place facts")
        return self


class ProviderDatabaseEffectRecord(StrictModel):
    effect_id: str = Field(min_length=8, max_length=160)
    effect_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_status: ProviderResolutionStatus
    started_at: datetime
    completed_at: datetime
    persisted_status: Literal["SUCCEEDED"] = "SUCCEEDED"

    @model_validator(mode="after")
    def persisted_effect_time_is_valid(self) -> "ProviderDatabaseEffectRecord":
        if self.completed_at < self.started_at:
            raise ValueError("persisted provider effect completed before it started")
        return self


class ProviderDatabaseExportReceipt(StrictModel):
    schema_version: Literal["g01-amap-database-export-receipt-v2"] = (
        "g01-amap-database-export-receipt-v2"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: ProviderExecutionMode
    source_registry: Literal[
        "POSTGRESQL_PROVIDER_EFFECT_REGISTRY",
        "CONTROLLED_CONTRACT_FIXTURE",
    ]
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transaction_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_instance_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    exported_at: datetime
    effects: list[ProviderDatabaseEffectRecord] = Field(min_length=1)
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def exported_effects_are_unique(self) -> "ProviderDatabaseExportReceipt":
        ids = [item.effect_id for item in self.effects]
        if len(ids) != len(set(ids)):
            raise ValueError("database-exported provider effect IDs must be unique")
        if any(item.completed_at > self.exported_at for item in self.effects):
            raise ValueError("database export predates a persisted effect")
        if self.execution_mode == "LIVE":
            if (
                self.source_registry != "POSTGRESQL_PROVIDER_EFFECT_REGISTRY"
                or self.database_instance_sha256 is None
            ):
                raise ValueError("LIVE database exports require persisted database provenance")
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "AMAP_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED AMap authority fields are incomplete")
        elif self.source_registry != "CONTROLLED_CONTRACT_FIXTURE" or any(
            value is not None
            for value in (
                self.authority_signature,
                self.database_instance_sha256,
                self.authority_policy_sha256,
            )
        ):
            raise ValueError("controlled Provider fixtures cannot carry live authority fields")
        return self


class ProviderHttpExchangeReceipt(StrictModel):
    effect_id: str = Field(min_length=8, max_length=160)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    http_status: int = Field(ge=200, le=299)
    provider_status: Literal["SUCCESS"] = "SUCCESS"
    provider_request_id_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    completed_at: datetime
    raw_response_retained: Literal[False] = False


class ProviderHttpReceiptBundle(StrictModel):
    schema_version: Literal["g01-amap-http-receipt-bundle-v2"] = (
        "g01-amap-http-receipt-bundle-v2"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: ProviderExecutionMode
    captured_at: datetime
    exchanges: list[ProviderHttpExchangeReceipt] = Field(min_length=1)
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def exchanges_are_unique(self) -> "ProviderHttpReceiptBundle":
        ids = [item.effect_id for item in self.exchanges]
        if len(ids) != len(set(ids)):
            raise ValueError("provider HTTP exchange effect IDs must be unique")
        if any(item.completed_at > self.captured_at for item in self.exchanges):
            raise ValueError("provider HTTP bundle predates an exchange")
        if self.execution_mode == "LIVE":
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "AMAP_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED AMap HTTP authority fields are incomplete")
        elif (
            self.authority_signature is not None
            or self.authority_policy_sha256 is not None
            or any(item.provider_request_id_sha256 is not None for item in self.exchanges)
        ):
            raise ValueError("controlled Provider fixtures cannot carry live authority fields")
        return self


class ProviderRuntimeReceiptBundle(StrictModel):
    schema_version: Literal["g01-amap-runtime-receipt-bundle-v2"] = (
        "g01-amap-runtime-receipt-bundle-v2"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: ProviderExecutionMode
    database_export_receipt_path: str = Field(min_length=1, max_length=500)
    database_export_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_http_receipt_bundle_path: str = Field(min_length=1, max_length=500)
    provider_http_receipt_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: Literal[
        "G01_AMAP_LIVE_RECEIPT_EXPORTER",
        "G01_AMAP_CONTROLLED_FIXTURE_EXPORTER",
    ]
    source_runtime: Literal[
        "PERSISTED_PROVIDER_EFFECT_REGISTRY",
        "CONTROLLED_CONTRACT_FIXTURE",
    ]
    evidence_level: Literal["LIVE_PROVIDER_EVIDENCE", "AUTOMATED_TEST"]
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    exporter_path: str | None = Field(default=None, min_length=1, max_length=500)
    exporter_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    effects: list[ProviderRuntimeEffectReceipt] = Field(min_length=1)
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def effect_ids_are_unique_and_bound(self) -> "ProviderRuntimeReceiptBundle":
        ids = [item.effect_id for item in self.effects]
        if len(ids) != len(set(ids)):
            raise ValueError("provider runtime effect IDs must be unique")
        if any(item.provider_binding_sha256 != self.provider_binding_sha256 for item in self.effects):
            raise ValueError("provider runtime effects must bind the bundle provider config")
        if any(item.execution_mode != self.execution_mode for item in self.effects):
            raise ValueError("provider runtime effects must use the bundle execution mode")
        live = self.execution_mode == "LIVE"
        if live != (self.evidence_level == "LIVE_PROVIDER_EVIDENCE"):
            raise ValueError("provider execution mode and evidence level disagree")
        if live != (self.generated_by == "G01_AMAP_LIVE_RECEIPT_EXPORTER"):
            raise ValueError("provider execution mode and exporter disagree")
        if live != (self.source_runtime == "PERSISTED_PROVIDER_EFFECT_REGISTRY"):
            raise ValueError("provider execution mode and source runtime disagree")
        if any(item.completed_at > self.generated_at for item in self.effects):
            raise ValueError("provider runtime bundle cannot predate an included effect")
        if live:
            if (
                self.exporter_path is None
                or self.exporter_sha256 is None
            ):
                raise ValueError("LIVE runtime receipts require frozen exporter bytes")
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "AMAP_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED AMap runtime authority fields are incomplete")
        elif any(
            value is not None
            for value in (
                self.authority_signature,
                self.authority_policy_sha256,
                self.exporter_path,
                self.exporter_sha256,
            )
        ):
            raise ValueError("controlled Provider fixtures cannot carry live authority fields")
        return self


class ProviderReceiptIndex(StrictModel):
    schema_version: Literal["g01-text-card-provider-receipt-index-v2"] = (
        "g01-text-card-provider-receipt-index-v2"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    split: AgentRuntimeSplit
    subject_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    subject_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: ProviderExecutionMode
    evidence_level: Literal["LIVE_PROVIDER_EVIDENCE", "AUTOMATED_TEST"]
    runtime_receipt_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    frozen_at: datetime
    receipts: list[ProviderPlaceReceiptRecord] = Field(min_length=1)
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def receipt_ids_are_unique(self) -> "ProviderReceiptIndex":
        ids = [item.receipt_id for item in self.receipts]
        if len(ids) != len(set(ids)):
            raise ValueError("provider receipt IDs must be unique")
        effect_ids = [item.runtime_effect_id for item in self.receipts]
        if len(effect_ids) != len(set(effect_ids)):
            raise ValueError("provider runtime effect references must be unique")
        if any(item.provider_binding_sha256 != self.provider_binding_sha256 for item in self.receipts):
            raise ValueError("provider index receipts must bind the same provider config")
        if any(item.execution_mode != self.execution_mode for item in self.receipts):
            raise ValueError("provider index receipts must use the index execution mode")
        if (self.execution_mode == "LIVE") != (
            self.evidence_level == "LIVE_PROVIDER_EVIDENCE"
        ):
            raise ValueError("provider index execution mode and evidence level disagree")
        if self.execution_mode == "LIVE":
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "AMAP_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED AMap index authority fields are incomplete")
        elif self.authority_signature is not None or self.authority_policy_sha256 is not None:
            raise ValueError("controlled Provider indexes cannot carry a live authority signature")
        return self


class AgentCanonicalPlaceLabel(StrictModel):
    place_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    city: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    authority: Literal["PROVIDER_BOUND_AGENT_REFERENCE"] = "PROVIDER_BOUND_AGENT_REFERENCE"
    provider_receipt: ProviderReceiptRef

    @model_validator(mode="after")
    def provider_receipt_is_matched(self) -> "AgentCanonicalPlaceLabel":
        if self.provider_receipt.resolution_status != "MATCHED":
            raise ValueError("canonical place labels require a MATCHED provider receipt")
        return self


class AgentMentionAnnotation(StrictModel):
    mention_id: str = Field(min_length=1, max_length=100)
    span_start: int = Field(ge=0)
    span_end: int = Field(gt=0)
    raw_text: str = Field(min_length=1)
    semantic_kind: SemanticKind
    role: Role
    day_index: int | None = Field(default=None, ge=1, le=14)
    place_boundary_status: PlaceBoundaryStatus
    place_boundary_basis: PlaceBoundaryBasis
    atomic_place_name: str | None = Field(default=None, max_length=100)
    executable_place: bool
    provider_resolution_receipt: ProviderReceiptRef | None = None
    canonical_place: AgentCanonicalPlaceLabel | None = None

    @model_validator(mode="after")
    def semantic_consistency(self) -> "AgentMentionAnnotation":
        if self.span_end <= self.span_start:
            raise ValueError("annotation span must be non-empty")
        expected_executable = (
            self.semantic_kind == "PLACE"
            and self.role == "PLANNED"
            and self.day_index is not None
            and self.place_boundary_status == "VERIFIED_ATOMIC"
            and bool((self.atomic_place_name or "").strip())
        )
        if self.executable_place != expected_executable:
            raise ValueError("executable_place disagrees with the locked eligibility rule")
        if self.semantic_kind != "PLACE" and (
            self.atomic_place_name is not None
            or self.provider_resolution_receipt is not None
            or self.canonical_place is not None
        ):
            raise ValueError("non-place annotations cannot carry place truth")
        if self.semantic_kind != "PLACE" and (
            self.place_boundary_status != "NONE"
            or self.place_boundary_basis != "NONE"
        ):
            raise ValueError("non-place annotations cannot claim a place boundary")
        if self.place_boundary_status == "UNCERTAIN":
            if (
                self.place_boundary_basis != "NONE"
                or self.atomic_place_name is not None
                or self.provider_resolution_receipt is not None
                or self.canonical_place is not None
                or self.executable_place
            ):
                raise ValueError("uncertain place boundaries must stay non-executable")
        if self.place_boundary_status == "NONE" and self.semantic_kind == "PLACE":
            raise ValueError("place annotations require verified or uncertain boundaries")
        if (
            self.place_boundary_status == "VERIFIED_ATOMIC"
            and self.place_boundary_basis != "PROVIDER_ACCEPTED_EXACT"
        ):
            raise ValueError("verified place boundaries require an exact authority basis")
        if self.canonical_place is not None and not self.executable_place:
            raise ValueError("canonical place truth is only valid for executable mentions")
        if self.executable_place and self.provider_resolution_receipt is None:
            raise ValueError("executable agent references require a live provider resolution receipt")
        if not self.executable_place and self.provider_resolution_receipt is not None:
            raise ValueError("non-executable mentions cannot carry provider resolution receipts")
        if self.executable_place and normalized_text(self.raw_text).strip() != normalized_text(
            self.atomic_place_name or ""
        ).strip():
            raise ValueError("executable source spans must contain only the atomic place text")
        if self.executable_place and self.provider_resolution_receipt is not None:
            receipt = self.provider_resolution_receipt
            if receipt.resolution_status != "MATCHED":
                raise ValueError("only Provider-matched exact boundaries can be executable")
            if receipt.accepted_source_name != self.raw_text:
                raise ValueError("matched source span is not the Provider-accepted place name")
            if self.place_boundary_basis != "PROVIDER_ACCEPTED_EXACT":
                raise ValueError("Provider executable places require Provider exact boundary evidence")
        if self.canonical_place is not None:
            if self.provider_resolution_receipt != self.canonical_place.provider_receipt:
                raise ValueError("canonical place must bind the mention provider resolution receipt")
        elif (
            self.provider_resolution_receipt is not None
            and self.provider_resolution_receipt.resolution_status == "MATCHED"
        ):
            raise ValueError("MATCHED provider resolution receipts require canonical place truth")
        return self


class AgentCaseAnnotation(StrictModel):
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    destination_name: str = Field(min_length=1, max_length=80)
    destination_basis: DestinationBasis
    destination_evidence_span_start: int | None = Field(default=None, ge=0)
    destination_evidence_span_end: int | None = Field(default=None, gt=0)
    destination_evidence_raw_text: str | None = Field(default=None, min_length=1, max_length=80)
    mentions: list[AgentMentionAnnotation] = Field(min_length=1)

    @model_validator(mode="after")
    def destination_evidence_matches_basis(self) -> "AgentCaseAnnotation":
        evidence = (
            self.destination_evidence_span_start,
            self.destination_evidence_span_end,
            self.destination_evidence_raw_text,
        )
        if self.destination_basis == "EXPLICIT" and any(value is None for value in evidence):
            raise ValueError("EXPLICIT destinations require a source evidence span")
        if self.destination_basis == "SOFT_ASSUMPTION" and any(value is not None for value in evidence):
            raise ValueError("SOFT_ASSUMPTION destinations cannot claim source evidence")
        return self


class AgentAnnotationBundle(StrictModel):
    schema_version: Literal["g01-text-card-agent-annotation-bundle-v2"] = (
        AGENT_ANNOTATION_SCHEMA_VERSION
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    assignment_id: str = Field(min_length=8, max_length=120)
    split: AgentEvaluationSplit
    attestation: AgentAnnotationAttestation
    cases: list[AgentCaseAnnotation] = Field(min_length=1)


class AgentConflictResolution(StrictModel):
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    conflict_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resolution_note: str = Field(min_length=12, max_length=1000)


class AgentAdjudicationAttestation(AgentTaskAttestation):
    task_role: Literal["ADJUDICATOR"] = "ADJUDICATOR"
    reasoning_effort: Literal["ultra"] = "ultra"
    reviewed_both_frozen_bundles: Literal[True] = True
    saw_candidate_predictions_before_submission: Literal[False] = False
    candidate_output_visibility: Literal["NONE"] = "NONE"
    raw_output_storage: Literal["REPOSITORY_EXTERNAL"] = "REPOSITORY_EXTERNAL"


class AgentAdjudicationBundle(StrictModel):
    schema_version: Literal["g01-text-card-agent-adjudication-bundle-v2"] = (
        AGENT_ADJUDICATION_SCHEMA_VERSION
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    split: AgentEvaluationSplit
    source_assignment_ids: list[str] = Field(min_length=2, max_length=2)
    source_bundle_sha256: list[str] = Field(min_length=2, max_length=2)
    attestation: AgentAdjudicationAttestation
    conflicts: list[AgentConflictResolution]
    agent_reference_cases: list[AgentCaseAnnotation] = Field(min_length=1)

    @model_validator(mode="after")
    def distinct_sources(self) -> "AgentAdjudicationBundle":
        if len(set(self.source_assignment_ids)) != 2:
            raise ValueError("agent adjudication requires two distinct assignments")
        if len(set(self.source_bundle_sha256)) != 2:
            raise ValueError("agent adjudication requires two distinct source bundles")
        return self


class SealedAgentReferenceAttestation(AgentTaskAttestation):
    task_role: Literal["SEALED_REFERENCE_CUSTODIAN"] = "SEALED_REFERENCE_CUSTODIAN"
    reasoning_effort: Literal["ultra"] = "ultra"
    saw_candidate_predictions_before_submission: Literal[False] = False
    candidate_output_visibility: Literal["NONE"] = "NONE"
    raw_output_storage: Literal["REPOSITORY_EXTERNAL"] = "REPOSITORY_EXTERNAL"
    provider_receipts_used: Literal[True] = True


class SealedAgentReferenceBundle(StrictModel):
    """Repository-external truth contract for one sealed blind tranche.

    This deliberately does not reuse the ordinary dev/validation adjudication
    bundle: doing so would make it possible for normal evaluation tooling to
    open blind truth by changing only a split argument.
    """

    schema_version: Literal["g01-sealed-agent-reference-bundle-v1"] = (
        "g01-sealed-agent-reference-bundle-v1"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    split: Literal["frozen_blind"] = "frozen_blind"
    attestation: SealedAgentReferenceAttestation
    agent_reference_cases: list[AgentCaseAnnotation] = Field(min_length=1)
    human_evidence: Literal[False] = False

    @model_validator(mode="after")
    def reference_cases_are_unique(self) -> "SealedAgentReferenceBundle":
        case_ids = [item.case_id for item in self.agent_reference_cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("sealed agent reference case IDs must be unique")
        return self


class AgentDestinationPrediction(StrictModel):
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    destination_name: str = Field(min_length=1, max_length=80)
    destination_basis: DestinationBasis
    evidence_span_start: int | None = Field(default=None, ge=0)
    evidence_span_end: int | None = Field(default=None, gt=0)
    evidence_raw_text: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def prediction_evidence_matches_basis(self) -> "AgentDestinationPrediction":
        evidence = self.evidence_span_start, self.evidence_span_end, self.evidence_raw_text
        if self.destination_basis == "EXPLICIT" and any(value is None for value in evidence):
            raise ValueError("EXPLICIT destination predictions require source evidence")
        if self.destination_basis == "SOFT_ASSUMPTION" and any(value is not None for value in evidence):
            raise ValueError("SOFT_ASSUMPTION destination predictions cannot claim source evidence")
        return self


class AgentInferenceCaseOutputV2(StrictModel):
    schema_version: Literal["g01-agent-inference-case-output-v2"] = (
        "g01-agent-inference-case-output-v2"
    )
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    text_card_prediction: TextCardPrediction
    destination_prediction: AgentDestinationPrediction

    @model_validator(mode="after")
    def projections_are_identical(self) -> "AgentInferenceCaseOutputV2":
        if (
            self.text_card_prediction.case_id != self.case_id
            or self.destination_prediction.case_id != self.case_id
            or self.text_card_prediction.source_sha256 != self.source_sha256
            or self.text_card_prediction.destination_name
            != self.destination_prediction.destination_name
        ):
            raise ValueError("combined inference output projections disagree")
        return self


class InferenceEffectReceipt(StrictModel):
    effect_id: str = Field(min_length=8, max_length=160)
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_id_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    latency_ms: float = Field(ge=0)
    repair_call_count: int = Field(ge=0, le=1)
    started_at: datetime
    completed_at: datetime
    status: Literal["SUCCEEDED"] = "SUCCEEDED"

    @model_validator(mode="after")
    def inference_effect_time_is_valid(self) -> "InferenceEffectReceipt":
        if self.completed_at < self.started_at:
            raise ValueError("inference effect completed before it started")
        return self


class InferenceDatabaseEffectRecord(StrictModel):
    effect_id: str = Field(min_length=8, max_length=160)
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    started_at: datetime
    completed_at: datetime
    persisted_status: Literal["SUCCEEDED"] = "SUCCEEDED"

    @model_validator(mode="after")
    def persisted_inference_time_is_valid(self) -> "InferenceDatabaseEffectRecord":
        if self.completed_at < self.started_at:
            raise ValueError("persisted inference completed before it started")
        return self


class InferenceDatabaseExportReceipt(StrictModel):
    schema_version: Literal["g01-qwen-database-export-receipt-v1"] = (
        "g01-qwen-database-export-receipt-v1"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: InferenceExecutionMode
    source_registry: Literal[
        "POSTGRESQL_INFERENCE_EFFECT_REGISTRY",
        "CONTROLLED_CONTRACT_FIXTURE",
    ]
    query_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transaction_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_instance_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    exported_at: datetime
    effects: list[InferenceDatabaseEffectRecord] = Field(min_length=1)
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def database_export_is_bound(self) -> "InferenceDatabaseExportReceipt":
        ids = [item.effect_id for item in self.effects]
        if len(ids) != len(set(ids)):
            raise ValueError("inference database effect IDs must be unique")
        if any(item.completed_at > self.exported_at for item in self.effects):
            raise ValueError("inference database export predates a persisted effect")
        if self.execution_mode == "LIVE":
            if (
                self.source_registry != "POSTGRESQL_INFERENCE_EFFECT_REGISTRY"
                or self.database_instance_sha256 is None
            ):
                raise ValueError("LIVE inference database exports require persisted provenance")
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "QWEN_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED Qwen database authority fields are incomplete")
        elif self.source_registry != "CONTROLLED_CONTRACT_FIXTURE" or any(
            value is not None
            for value in (
                self.database_instance_sha256,
                self.authority_policy_sha256,
                self.authority_signature,
            )
        ):
            raise ValueError("controlled inference exports cannot carry live authority fields")
        return self


class InferenceHttpExchangeReceipt(StrictModel):
    effect_id: str = Field(min_length=8, max_length=160)
    case_id: str = Field(pattern=r"^G01-TC-\d{3}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_id_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    http_status: int = Field(ge=200, le=299)
    completed_at: datetime
    raw_response_retained: Literal[False] = False


class InferenceHttpReceiptBundle(StrictModel):
    schema_version: Literal["g01-qwen-http-receipt-bundle-v1"] = (
        "g01-qwen-http-receipt-bundle-v1"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    model_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_mode: InferenceExecutionMode
    captured_at: datetime
    exchanges: list[InferenceHttpExchangeReceipt] = Field(min_length=1)
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def http_receipts_are_bound(self) -> "InferenceHttpReceiptBundle":
        ids = [item.effect_id for item in self.exchanges]
        if len(ids) != len(set(ids)):
            raise ValueError("inference HTTP effect IDs must be unique")
        if any(item.completed_at > self.captured_at for item in self.exchanges):
            raise ValueError("inference HTTP bundle predates an exchange")
        if self.execution_mode == "LIVE":
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "QWEN_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED Qwen HTTP authority fields are incomplete")
        elif (
            self.authority_policy_sha256 is not None
            or self.authority_signature is not None
            or any(item.provider_request_id_sha256 is not None for item in self.exchanges)
        ):
            raise ValueError("controlled inference HTTP receipts cannot carry live fields")
        return self


class InferenceRuntimeReceiptBundle(StrictModel):
    schema_version: Literal["g01-qwen-inference-receipt-bundle-v2"] = (
        "g01-qwen-inference-receipt-bundle-v2"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    split: AgentRuntimeSplit
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    provider: Literal["QWEN"] = "QWEN"
    execution_mode: InferenceExecutionMode
    evidence_level: Literal["LIVE_PROVIDER_EVIDENCE", "AUTOMATED_TEST"]
    region: str = Field(min_length=2, max_length=80)
    endpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    exact_model_id: str = Field(min_length=2, max_length=160)
    model_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predictions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inference_outputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predictions_path: str | None = Field(default=None, min_length=1, max_length=500)
    inference_outputs_path: str | None = Field(default=None, min_length=1, max_length=500)
    database_export_receipt_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    database_export_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    provider_http_receipt_bundle_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )
    provider_http_receipt_bundle_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    authority_policy_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    exporter_path: str | None = Field(default=None, min_length=1, max_length=500)
    exporter_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    generated_by: Literal[
        "G01_QWEN_LIVE_RECEIPT_EXPORTER",
        "G01_QWEN_CONTROLLED_FIXTURE_EXPORTER",
    ]
    raw_request_or_response_in_repository: Literal[False] = False
    effects: list[InferenceEffectReceipt] = Field(min_length=1)
    authority_signature: DetachedAuthoritySignature | None = None

    @model_validator(mode="after")
    def inference_effects_are_unique(self) -> "InferenceRuntimeReceiptBundle":
        ids = [item.effect_id for item in self.effects]
        cases = [item.case_id for item in self.effects]
        if len(ids) != len(set(ids)) or len(cases) != len(set(cases)):
            raise ValueError("inference effect and case IDs must be unique")
        if any(item.completed_at > self.generated_at for item in self.effects):
            raise ValueError("inference bundle predates an included effect")
        live = self.execution_mode == "LIVE"
        if live != (self.evidence_level == "LIVE_PROVIDER_EVIDENCE"):
            raise ValueError("Qwen execution mode and evidence level disagree")
        if live != (self.generated_by == "G01_QWEN_LIVE_RECEIPT_EXPORTER"):
            raise ValueError("Qwen execution mode and exporter disagree")
        if live:
            if (
                self.exporter_path is None
                or self.exporter_sha256 is None
                or self.predictions_path is None
                or self.inference_outputs_path is None
                or self.database_export_receipt_path is None
                or self.database_export_receipt_sha256 is None
                or self.provider_http_receipt_bundle_path is None
                or self.provider_http_receipt_bundle_sha256 is None
            ):
                raise ValueError("LIVE Qwen receipts require pinned runtime provenance")
            if (self.authority_signature is None) != (
                self.authority_policy_sha256 is None
            ) or (
                self.authority_signature is not None
                and self.authority_signature.authority_role != "QWEN_LIVE_EXPORTER"
            ):
                raise ValueError("optional HARDENED Qwen authority fields are incomplete")
        elif any(
            value is not None
            for value in (
                self.authority_signature,
                self.authority_policy_sha256,
                self.exporter_path,
                self.exporter_sha256,
                self.predictions_path,
                self.inference_outputs_path,
                self.database_export_receipt_path,
                self.database_export_receipt_sha256,
                self.provider_http_receipt_bundle_path,
                self.provider_http_receipt_bundle_sha256,
            )
        ) or any(item.provider_request_id_sha256 is not None for item in self.effects):
            raise ValueError("controlled Qwen fixtures cannot carry live authority fields")
        return self


class AgentPredictionRunEnvelope(StrictModel):
    schema_version: Literal["g01-text-card-agent-prediction-run-v2"] = (
        "g01-text-card-agent-prediction-run-v2"
    )
    goal_id: Literal["TC-VNEXT-G01-TEXT-CARDS"] = G01_GOAL_ID
    dataset_version: Literal["g01-text-card-dataset-v1"] = DATASET_VERSION
    split: AgentRuntimeSplit
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_tree: str = Field(pattern=r"^[0-9a-f]{40}$")
    predictions_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inference_outputs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inference_receipt_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    destination_predictions: list[AgentDestinationPrediction] = Field(min_length=1)

    @model_validator(mode="after")
    def destination_case_ids_are_unique(self) -> "AgentPredictionRunEnvelope":
        case_ids = [item.case_id for item in self.destination_predictions]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("destination prediction case IDs must be unique")
        return self


def _is_clause_free_place_text(value: str) -> bool:
    normalized = normalized_text(value).strip()
    if not normalized or len(normalized) > 40:
        return False
    if urlparse(normalized).scheme or any(marker in normalized for marker in "。！？；\n"):
        return False
    forbidden = (
        "上午",
        "下午",
        "晚上",
        "早上",
        "先到",
        "随后",
        "然后",
        "再到",
        "前往",
        "步行",
        "乘坐",
        "出来",
        "建议",
        "至少",
        "小时",
        "分钟",
        "可以",
        "安排",
        "游览",
        "吃饭",
        "入住",
    )
    return not any(token in normalized for token in forbidden)


def _is_conservative_unresolved_place_name(value: str) -> bool:
    normalized = normalized_text(value).strip()
    if not _is_clause_free_place_text(normalized) or len(normalized) > 24:
        return False
    place_suffixes = (
        "博物馆",
        "纪念馆",
        "美术馆",
        "艺术馆",
        "科技馆",
        "图书馆",
        "大剧院",
        "体育馆",
        "游乐园",
        "动物园",
        "植物园",
        "湿地公园",
        "森林公园",
        "主题公园",
        "公园",
        "广场",
        "景区",
        "古镇",
        "古城",
        "寺",
        "庙",
        "宫",
        "院",
        "园",
        "馆",
        "塔",
        "门",
        "街",
        "巷",
        "路",
        "站",
        "山",
        "湖",
        "滩",
        "坊",
        "区",
        "中心",
        "酒店",
        "宾馆",
        "客栈",
        "餐厅",
        "饭店",
        "酒吧",
        "咖啡馆",
        "茶馆",
        "商场",
        "市场",
        "码头",
        "机场",
    )
    return normalized.endswith(place_suffixes)


def validate_agent_case_annotation(
    case: AgentCaseAnnotation,
    source: TextCardInputCase,
) -> None:
    if case.case_id != source.case_id:
        raise ValueError("annotation case_id does not match source")
    if case.source_sha256 != source.normalized_input_sha256:
        raise ValueError(f"{case.case_id} source hash does not match input")
    if case.destination_basis == "EXPLICIT" and case.destination_name not in normalized_text(
        source.input_text
    ):
        raise ValueError(f"{case.case_id} explicit destination does not occur in source text")
    if case.destination_basis == "EXPLICIT":
        start = case.destination_evidence_span_start
        end = case.destination_evidence_span_end
        raw = case.destination_evidence_raw_text
        if start is None or end is None or raw is None:
            raise ValueError(f"{case.case_id} explicit destination evidence is incomplete")
        text = normalized_text(source.input_text)
        if text[start:end] != raw or raw != case.destination_name:
            raise ValueError(f"{case.case_id} explicit destination evidence does not match source")
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
