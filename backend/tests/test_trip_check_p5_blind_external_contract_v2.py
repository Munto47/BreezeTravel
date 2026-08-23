from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.blind_external_contract_v2 import (
    P5ExternalCustodyContractError,
    expected_blind_dataset_binding_v2,
    validate_external_blind_bundle_v2,
    validate_external_blind_review_receipt_v2,
)
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.final_blind_scorer_v2 import canonical_labels_hash_v2


REPO_ROOT = Path(__file__).resolve().parents[2]
SUBJECT_COMMIT = "a" * 40


def _write_external_contracts(tmp_path: Path) -> tuple[Path, str, Path, str, str, dict]:
    binding, case_ids = expected_blind_dataset_binding_v2(REPO_ROOT)
    labels = [
        {
            "schema_version": "trip-check-p5-blind-label-v2",
            "case_id": case_id,
            "oracle": {},
        }
        for case_id in case_ids
    ]
    bundle = {
        "schema_version": "trip-check-p5-blind-label-bundle-v2",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "dataset_binding": binding,
        "labels": labels,
    }
    bundle_path = tmp_path / "external.bundle.json"
    bundle_bytes = canonical_bytes(bundle) + b"\n"
    bundle_path.write_bytes(bundle_bytes)
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    labels_sha256 = canonical_labels_hash_v2(labels)
    receipt = {
        "schema_version": "trip-check-p5-blind-review-receipt-v2",
        "review_status": "PASS",
        "candidate_subject_commit": SUBJECT_COMMIT,
        "bundle_byte_sha256": bundle_sha256,
        "bundle_canonical_sha256": digest(bundle),
        "labels_canonical_sha256": labels_sha256,
        "oracle_derivation_sha256": "b" * 64,
        "dataset_binding": binding,
        "checks": {
            "case_count": 90,
            "case_set_exact": True,
            "binding_recomputed": True,
            "oracle_exact": True,
            "privacy_findings_count": 0,
            "candidate_output_dependency_count": 0,
            "network_api_calls": 0,
            "unknown_preserved_count": 18,
            "candidate_set_mode_counts": {
                "VALID": 10,
                "EMPTY": 10,
                "MISSING_RECEIPT": 10,
                "NOT_APPLICABLE": 60,
            },
            "concurrency_case_count": 20,
        },
    }
    receipt["receipt_sha256"] = digest(receipt)
    receipt_path = tmp_path / "external.review.json"
    receipt_bytes = canonical_bytes(receipt) + b"\n"
    receipt_path.write_bytes(receipt_bytes)
    return (
        bundle_path,
        bundle_sha256,
        receipt_path,
        hashlib.sha256(receipt_bytes).hexdigest(),
        labels_sha256,
        binding,
    )


def test_consumer_validates_external_structure_and_commitments_without_oracle_derivation(tmp_path: Path) -> None:
    bundle_path, bundle_sha256, receipt_path, receipt_sha256, labels_sha256, binding = (
        _write_external_contracts(tmp_path)
    )
    _binding, case_ids = expected_blind_dataset_binding_v2(REPO_ROOT)
    bundle = validate_external_blind_bundle_v2(
        repo_root=REPO_ROOT,
        bundle_path=bundle_path,
        expected_bundle_sha256=bundle_sha256,
        expected_labels_canonical_sha256=labels_sha256,
        expected_dataset_binding=binding,
        expected_case_ids=case_ids,
    )
    review = validate_external_blind_review_receipt_v2(
        repo_root=REPO_ROOT,
        receipt_path=receipt_path,
        expected_receipt_sha256=receipt_sha256,
        expected_candidate_subject_commit=SUBJECT_COMMIT,
        expected_bundle_sha256=bundle_sha256,
        expected_bundle_canonical_sha256=bundle["bundle_canonical_sha256"],
        expected_labels_canonical_sha256=labels_sha256,
        expected_dataset_binding=binding,
    )
    assert bundle["status"] == "PASS"
    assert review["status"] == "PASS"
    assert "labels" not in bundle
    assert "labels" not in review


def test_bundle_consumer_rejects_candidate_output_dependency_field(tmp_path: Path) -> None:
    bundle_path, _bundle_sha256, _receipt_path, _receipt_sha256, labels_sha256, binding = (
        _write_external_contracts(tmp_path)
    )
    payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    payload["candidate_outputs"] = []
    attacked = tmp_path / "attacked.bundle.json"
    attacked_bytes = canonical_bytes(payload) + b"\n"
    attacked.write_bytes(attacked_bytes)
    _binding, case_ids = expected_blind_dataset_binding_v2(REPO_ROOT)
    with pytest.raises(P5ExternalCustodyContractError, match="schema mismatch"):
        validate_external_blind_bundle_v2(
            repo_root=REPO_ROOT,
            bundle_path=attacked,
            expected_bundle_sha256=hashlib.sha256(attacked_bytes).hexdigest(),
            expected_labels_canonical_sha256=labels_sha256,
            expected_dataset_binding=binding,
            expected_case_ids=case_ids,
        )


def test_review_consumer_rejects_subject_and_bundle_commitment_drift(tmp_path: Path) -> None:
    bundle_path, bundle_sha256, receipt_path, receipt_sha256, labels_sha256, binding = (
        _write_external_contracts(tmp_path)
    )
    _binding, case_ids = expected_blind_dataset_binding_v2(REPO_ROOT)
    bundle = validate_external_blind_bundle_v2(
        repo_root=REPO_ROOT,
        bundle_path=bundle_path,
        expected_bundle_sha256=bundle_sha256,
        expected_labels_canonical_sha256=labels_sha256,
        expected_dataset_binding=binding,
        expected_case_ids=case_ids,
    )
    with pytest.raises(P5ExternalCustodyContractError, match="subject commitment mismatch"):
        validate_external_blind_review_receipt_v2(
            repo_root=REPO_ROOT,
            receipt_path=receipt_path,
            expected_receipt_sha256=receipt_sha256,
            expected_candidate_subject_commit="c" * 40,
            expected_bundle_sha256=bundle_sha256,
            expected_bundle_canonical_sha256=bundle["bundle_canonical_sha256"],
            expected_labels_canonical_sha256=labels_sha256,
            expected_dataset_binding=binding,
        )


def test_external_consumer_rejects_repository_path(tmp_path: Path) -> None:
    _bundle_path, bundle_sha256, _receipt_path, _receipt_sha256, labels_sha256, binding = (
        _write_external_contracts(tmp_path)
    )
    _binding, case_ids = expected_blind_dataset_binding_v2(REPO_ROOT)
    with pytest.raises(P5ExternalCustodyContractError, match="outside the repository"):
        validate_external_blind_bundle_v2(
            repo_root=REPO_ROOT,
            bundle_path=REPO_ROOT / "backend/evals/trip_check_v1/p5/blind_bundle_v2.schema.json",
            expected_bundle_sha256=bundle_sha256,
            expected_labels_canonical_sha256=labels_sha256,
            expected_dataset_binding=binding,
            expected_case_ids=case_ids,
        )
