from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.active_contract import (
    P5ContractNotReadyError,
    require_v4_formal_ready,
    require_v5_formal_ready,
)
from evals.trip_check_v1.p5.data_contract import file_sha256
from evals.trip_check_v1.p5.data_contract_v5 import (
    BLIND_INPUT_PATH_V5,
    BLIND_MATERIALIZATIONS_PATH_V5,
    validate_v4_source_anchor,
)
from evals.trip_check_v1.p5.dataset_contracts_v5 import P5BlindSealV5
from evals.trip_check_v1.p5.seal_v5 import (
    SealPathsV5,
    build_active_contract_v5,
    build_blind_seal_v5,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_v5_seal_binds_new_custody_and_unchanged_v4_payloads() -> None:
    paths = SealPathsV5.for_repo(REPO_ROOT)
    source = validate_v4_source_anchor(REPO_ROOT)
    commitments = {
        "labels_canonical_sha256": "1" * 64,
        "external_bundle_sha256": "2" * 64,
        "correction_receipt_sha256": "3" * 64,
        "review_receipt_sha256": "4" * 64,
        "policy_mapping_sha256": "5" * 64,
    }
    seal = build_blind_seal_v5(
        paths=paths,
        candidate_freeze_commit="a" * 40,
        candidate_manifest_hash="b" * 64,
        custody_commitments=commitments,
    )
    assert P5BlindSealV5.model_validate(seal).model_dump(mode="json") == seal
    assert seal["inputs_file_sha256"] == file_sha256(BLIND_INPUT_PATH_V5)
    assert seal["materializations_file_sha256"] == file_sha256(BLIND_MATERIALIZATIONS_PATH_V5)
    assert seal["source_v4_external_bundle_sha256"] == source["external_bundle_sha256"]
    assert seal["external_bundle_sha256"] != source["external_bundle_sha256"]
    assert seal["oracle_correction_scope"] == ("specific_place_allowed_payload_policy_only")
    assert seal["blind_payload_changed"] is False
    assert seal["scoring_payload_present"] is False


def test_v5_active_contract_is_ready_and_supersedes_v4(tmp_path: Path) -> None:
    payload = build_active_contract_v5(
        paths=SealPathsV5.for_repo(REPO_ROOT),
        candidate_freeze_commit="a" * 40,
        sealed_dataset_manifest_hash="b" * 64,
        blind_seal_file_sha256="c" * 64,
    )
    active = tmp_path / "active_contract.json"
    active.write_text(json.dumps(payload), encoding="utf-8")
    assert require_v5_formal_ready(active) == payload
    with pytest.raises(P5ContractNotReadyError, match="P5_V4_FORMAL_CONTRACT_SUPERSEDED"):
        require_v4_formal_ready(active)


def test_v5_active_contract_rejects_unsealed_fixture(tmp_path: Path) -> None:
    active = tmp_path / "active_contract.json"
    active.write_text(
        json.dumps(
            {
                "schema_version": "trip-check-p5-active-contract-v1",
                "active_contract": "trip-check-p5-v5",
                "formal_evidence_status": "NOT_READY",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(P5ContractNotReadyError, match="P5_V5_FORMAL_CONTRACT_NOT_READY"):
        require_v5_formal_ready(active)
