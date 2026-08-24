from __future__ import annotations

import json

import pytest

from evals.trip_check_v1.p5.active_contract import (
    P5ContractNotReadyError,
    require_v3_formal_ready,
    require_v4_formal_ready,
)
from evals.trip_check_v1.p5.data_contract import file_sha256
from evals.trip_check_v1.p5.data_contract_v3 import (
    BLIND_INPUT_PATH_V3,
    BLIND_MATERIALIZATIONS_PATH_V3,
)
from evals.trip_check_v1.p5.data_contract_v4 import validate_v3_source_anchor
from evals.trip_check_v1.p5.dataset_contracts_v4 import P5BlindSealV4
from evals.trip_check_v1.p5.seal_v4 import (
    SealPathsV4,
    build_active_contract_v4,
    build_blind_seal_v4,
)


REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_v4_seal_reenvelopes_unchanged_v3_blind_and_custody_commitments() -> None:
    paths = SealPathsV4.for_repo(REPO_ROOT)
    source = validate_v3_source_anchor()
    commitments = {
        key: source[key]
        for key in (
            "labels_canonical_sha256",
            "external_bundle_sha256",
            "review_receipt_sha256",
        )
    }
    seal = build_blind_seal_v4(
        paths=paths,
        candidate_freeze_commit="a" * 40,
        candidate_manifest_hash="b" * 64,
        custody_commitments=commitments,
        source_v3_anchor=source,
    )
    assert P5BlindSealV4.model_validate(seal).model_dump(mode="json") == seal
    assert seal["inputs_file_sha256"] == file_sha256(BLIND_INPUT_PATH_V3)
    assert seal["materializations_file_sha256"] == file_sha256(
        BLIND_MATERIALIZATIONS_PATH_V3
    )
    assert seal["external_bundle_sha256"] == source["external_bundle_sha256"]
    assert seal["review_receipt_sha256"] == source["review_receipt_sha256"]
    assert seal["scoring_payload_present"] is False


def test_v4_active_contract_helper_is_fail_closed_and_supersedes_v3(tmp_path) -> None:
    paths = SealPathsV4.for_repo(REPO_ROOT)
    payload = build_active_contract_v4(
        paths=paths,
        candidate_freeze_commit="a" * 40,
        sealed_dataset_manifest_hash="b" * 64,
        blind_seal_file_sha256="c" * 64,
    )
    active = tmp_path / "active_contract.json"
    active.write_text(json.dumps(payload), encoding="utf-8")
    assert require_v4_formal_ready(active) == payload
    with pytest.raises(P5ContractNotReadyError, match="P5_V3_FORMAL_CONTRACT_SUPERSEDED"):
        require_v3_formal_ready(active)


def test_v4_active_contract_rejects_unsealed_temp_fixture(tmp_path) -> None:
    active = tmp_path / "active_contract.json"
    active.write_text(
        json.dumps(
            {
                "schema_version": "trip-check-p5-active-contract-v1",
                "active_contract": "trip-check-p5-v4",
                "formal_evidence_status": "NOT_READY",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(P5ContractNotReadyError, match="P5_V4_FORMAL_CONTRACT_NOT_READY"):
        require_v4_formal_ready(active)
