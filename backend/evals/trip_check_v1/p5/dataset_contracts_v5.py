"""Strict envelopes for the user-authorized P5 v5 oracle supersession.

P5 v5 keeps every repository-side case and materialization byte from sealed
v4.  Only the external blind truth commitment may change, and only after an
independent review receipt is bound to the exact v5 candidate commit.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


Sha256V5 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CommitShaV5 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]


class P5BlindSealV5(BaseModel):
    """Label-free repository seal for the v5 dataset envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-blind-seal-v5"] = (
        "trip-check-p5-blind-seal-v5"
    )
    split: Literal["frozen_blind"] = "frozen_blind"
    case_count: Literal[90] = 90
    candidate_freeze_commit: CommitShaV5
    candidate_dataset_manifest_hash: Sha256V5
    case_ids_sha256: Sha256V5
    nonblind_cases_file_sha256: Sha256V5
    nonblind_materializations_file_sha256: Sha256V5
    inputs_file_sha256: Sha256V5
    inputs_content_sha256: Sha256V5
    materializations_file_sha256: Sha256V5
    materializations_content_sha256: Sha256V5
    case_set_hash: Sha256V5
    materialization_set_hash: Sha256V5
    contracts_v3_sha256: Sha256V5
    dataset_contracts_v5_sha256: Sha256V5
    run_spec_template_sha256: Sha256V5
    rubric_sha256: Sha256V5
    variant_ids_sha256: Sha256V5
    labels_canonical_sha256: Sha256V5
    external_bundle_sha256: Sha256V5
    correction_receipt_sha256: Sha256V5
    review_receipt_sha256: Sha256V5
    policy_mapping_sha256: Sha256V5
    source_v4_blind_seal_file_sha256: Sha256V5
    source_v4_inputs_file_sha256: Sha256V5
    source_v4_materializations_file_sha256: Sha256V5
    source_v4_dataset_manifest_hash: Sha256V5
    source_v4_labels_canonical_sha256: Sha256V5
    source_v4_external_bundle_sha256: Sha256V5
    source_v4_review_receipt_sha256: Sha256V5
    oracle_correction_scope: Literal[
        "specific_place_allowed_payload_policy_only"
    ] = "specific_place_allowed_payload_policy_only"
    source_truth_contract: Literal[
        "trip-check-p5-blind-label-bundle-v2+v5-specific-place-policy-correction"
    ] = "trip-check-p5-blind-label-bundle-v2+v5-specific-place-policy-correction"
    payload_contract: Literal[
        "trip-check-p5-eval-case-v3+trip-check-p5-materialization-v3"
    ] = "trip-check-p5-eval-case-v3+trip-check-p5-materialization-v3"
    label_storage: Literal["external_bundle_only"] = "external_bundle_only"
    label_access: Literal["isolated_scorer_only"] = "isolated_scorer_only"
    scoring_payload_present: Literal[False] = False
    blind_payload_changed: Literal[False] = False
    human_evidence: Literal[False] = False


class P5SealingCommitmentV5(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-sealing-commitment-v5"] = (
        "trip-check-p5-sealing-commitment-v5"
    )
    status: Literal["SEALED"] = "SEALED"
    candidate_freeze_commit: CommitShaV5
    candidate_dataset_manifest_hash: Sha256V5
    blind_seal_path: Literal[
        "backend/evals/trip_check_v1/p5/sealed/frozen_blind.v5.seal.json"
    ]
    blind_seal_file_sha256: Sha256V5
    labels_canonical_sha256: Sha256V5
    external_bundle_sha256: Sha256V5
    correction_receipt_sha256: Sha256V5
    review_receipt_sha256: Sha256V5
    policy_mapping_sha256: Sha256V5
    oracle_correction_scope: Literal[
        "specific_place_allowed_payload_policy_only"
    ] = "specific_place_allowed_payload_policy_only"


__all__ = ["P5BlindSealV5", "P5SealingCommitmentV5"]
