"""Strict envelopes for the P5 v4 dataset-only supersession.

P5 v4 deliberately keeps the v3 case and materialization payload contracts.
Only the dataset manifest, blind seal, and sealing commitment are versioned so
the two non-blind route-evidence repairs cannot be confused with sealed v3.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


Sha256V4 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
CommitShaV4 = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{40}$"),
]


class P5BlindSealV4(BaseModel):
    """Label-free repository seal for the v4 dataset envelope."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-blind-seal-v4"] = (
        "trip-check-p5-blind-seal-v4"
    )
    split: Literal["frozen_blind"] = "frozen_blind"
    case_count: Literal[90] = 90
    candidate_freeze_commit: CommitShaV4
    candidate_dataset_manifest_hash: Sha256V4
    case_ids_sha256: Sha256V4
    nonblind_cases_file_sha256: Sha256V4
    nonblind_materializations_file_sha256: Sha256V4
    inputs_file_sha256: Sha256V4
    inputs_content_sha256: Sha256V4
    materializations_file_sha256: Sha256V4
    materializations_content_sha256: Sha256V4
    case_set_hash: Sha256V4
    materialization_set_hash: Sha256V4
    contracts_v3_sha256: Sha256V4
    dataset_contracts_v4_sha256: Sha256V4
    run_spec_template_sha256: Sha256V4
    rubric_sha256: Sha256V4
    variant_ids_sha256: Sha256V4
    labels_canonical_sha256: Sha256V4
    external_bundle_sha256: Sha256V4
    review_receipt_sha256: Sha256V4
    source_v2_blind_seal_file_sha256: Sha256V4
    source_v3_blind_seal_file_sha256: Sha256V4
    source_v3_inputs_file_sha256: Sha256V4
    source_v3_materializations_file_sha256: Sha256V4
    source_v3_dataset_manifest_hash: Sha256V4
    source_truth_contract: Literal[
        "trip-check-p5-blind-label-bundle-v2-unchanged"
    ] = "trip-check-p5-blind-label-bundle-v2-unchanged"
    payload_contract: Literal[
        "trip-check-p5-eval-case-v3+trip-check-p5-materialization-v3"
    ] = "trip-check-p5-eval-case-v3+trip-check-p5-materialization-v3"
    label_storage: Literal["external_bundle_only"] = "external_bundle_only"
    label_access: Literal["isolated_scorer_only"] = "isolated_scorer_only"
    scoring_payload_present: Literal[False] = False
    human_evidence: Literal[False] = False


class P5SealingCommitmentV4(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-sealing-commitment-v4"] = (
        "trip-check-p5-sealing-commitment-v4"
    )
    status: Literal["SEALED"] = "SEALED"
    candidate_freeze_commit: CommitShaV4
    candidate_dataset_manifest_hash: Sha256V4
    blind_seal_path: Literal[
        "backend/evals/trip_check_v1/p5/sealed/frozen_blind.v4.seal.json"
    ]
    blind_seal_file_sha256: Sha256V4
    labels_canonical_sha256: Sha256V4
    external_bundle_sha256: Sha256V4
    review_receipt_sha256: Sha256V4


class P5RouteEvidenceRepairV4(BaseModel):
    """Manifest binding for one authorized non-blind P1 route repair."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: Literal["p5.pilot.bj.004", "p5.pilot.sh.001"]
    source_case_id: Literal["TC-P1-BJ-04", "TC-P1-SH-01"]
    source_path: str = Field(min_length=1)
    source_file_sha256: Sha256V4
    source_snapshot_id: str = Field(min_length=1)
    source_fact_id: str = Field(min_length=1)
    source_response_hash: Sha256V4
    duration_minutes: Literal[90] = 90
    blind_lane_touched: Literal[False] = False


__all__ = [
    "P5BlindSealV4",
    "P5RouteEvidenceRepairV4",
    "P5SealingCommitmentV4",
]
