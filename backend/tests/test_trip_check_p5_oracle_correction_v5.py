from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.blind_external_contract_v5 import (
    expected_blind_dataset_binding_v5,
    validate_external_custody_v5,
)
from evals.trip_check_v1.p5.blind_external_contract_v2 import (
    P5ExternalCustodyContractError,
)
from evals.trip_check_v1.p5.data_contract import canonical_bytes
from evals.trip_check_v1.p5.data_contract_v5 import BLIND_INPUT_PATH_V5
from evals.trip_check_v1.p5.final_blind_scorer_v2 import canonical_labels_hash_v2
from evals.trip_check_v1.p5.oracle_correction_v5 import (
    P5OracleCorrectionErrorV5,
    correct_external_oracle_v5,
)
from evals.trip_check_v1.p5.oracle_review_v5 import (
    P5OracleReviewErrorV5,
    review_external_oracle_v5,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
P5_ROOT = REPO_ROOT / "backend" / "evals" / "trip_check_v1" / "p5"
CORRECTOR = REPO_ROOT / "backend" / "scripts" / "correct_trip_check_p5_v5_external_oracle.py"
REVIEWER = REPO_ROOT / "backend" / "scripts" / "review_trip_check_p5_v5_external_oracle.py"
SUBJECT = "a" * 40


def _fixture(tmp_path: Path, *, already_corrected: bool = False) -> tuple[Path, dict]:
    binding, _case_ids = expected_blind_dataset_binding_v5(REPO_ROOT)
    cases = [json.loads(line) for line in BLIND_INPUT_PATH_V5.read_text(encoding="utf-8").splitlines()]
    labels = []
    expected = {
        "VALID": True,
        "EMPTY": False,
        "MISSING_RECEIPT": False,
        "NOT_APPLICABLE": True,
    }
    for case in cases:
        mode = case["runner_control"]["candidate_set_mode"]
        value = expected[mode]
        if not already_corrected and mode == "NOT_APPLICABLE":
            value = False
        labels.append(
            {
                "schema_version": "trip-check-p5-blind-label-v2",
                "case_id": case["case_id"],
                "oracle": {"specific_place_allowed": value, "sentinel": "unchanged"},
            }
        )
    bundle = {
        "schema_version": "trip-check-p5-blind-label-bundle-v2",
        "evidence_class": "controlled_blind_oracle",
        "human_evidence": False,
        "dataset_binding": binding,
        "labels": labels,
    }
    source = tmp_path / "source.bundle.json"
    payload = canonical_bytes(bundle) + b"\n"
    source.write_bytes(payload)
    anchor = {
        "external_bundle_sha256": hashlib.sha256(payload).hexdigest(),
        "labels_canonical_sha256": canonical_labels_hash_v2(labels),
    }
    return source, anchor


def _validate_schema(name: str, value: dict) -> None:
    schema = json.loads((P5_ROOT / name).read_text(encoding="utf-8"))
    assert list(Draft202012Validator(schema).iter_errors(value)) == []


def test_custodian_and_independent_reviewer_prove_exact_60_0_diff(
    tmp_path: Path,
) -> None:
    source, anchor = _fixture(tmp_path)
    corrected = tmp_path / "corrected.bundle.json"
    correction = tmp_path / "correction.json"
    review = tmp_path / "review.json"
    correction_summary = correct_external_oracle_v5(
        repo_root=REPO_ROOT,
        source_bundle_path=source,
        corrected_bundle_path=corrected,
        correction_receipt_path=correction,
        candidate_subject_commit=SUBJECT,
        entrypoint_path=CORRECTOR,
        source_anchor=anchor,
    )
    review_summary = review_external_oracle_v5(
        repo_root=REPO_ROOT,
        source_bundle_path=source,
        corrected_bundle_path=corrected,
        correction_receipt_path=correction,
        review_receipt_path=review,
        candidate_subject_commit=SUBJECT,
        correction_entrypoint_path=CORRECTOR,
        reviewer_entrypoint_path=REVIEWER,
        source_anchor=anchor,
    )
    correction_payload = json.loads(correction.read_text(encoding="utf-8"))
    review_payload = json.loads(review.read_text(encoding="utf-8"))
    _validate_schema("blind_oracle_correction_receipt_v5.schema.json", correction_payload)
    _validate_schema("blind_review_receipt_v5.schema.json", review_payload)
    assert correction_summary["changed_label_count"] == 60
    assert review_summary["reviewed_changed_label_count"] == 60
    assert correction_summary["non_target_oracle_diff_count"] == 0
    assert review_summary["non_target_oracle_diff_count"] == 0
    assert correction_payload["changed_field"] == "oracle.specific_place_allowed"
    assert review_payload["reviewed_changed_field"] == "oracle.specific_place_allowed"
    assert correction_payload["correction_tool_sha256"] != review_payload["reviewer_tool_sha256"]
    assert '"case_id":' not in correction.read_text(encoding="utf-8")
    assert '"case_id":' not in review.read_text(encoding="utf-8")

    source_payload = json.loads(source.read_text(encoding="utf-8"))
    corrected_payload = json.loads(corrected.read_text(encoding="utf-8"))
    changed_paths = []
    for before, after in zip(source_payload["labels"], corrected_payload["labels"], strict=True):
        for key in before["oracle"]:
            if before["oracle"][key] != after["oracle"][key]:
                changed_paths.append(f"oracle.{key}")
    assert changed_paths == ["oracle.specific_place_allowed"] * 60


def test_reviewer_rejects_any_non_target_oracle_change(tmp_path: Path) -> None:
    source, anchor = _fixture(tmp_path)
    corrected = tmp_path / "corrected.bundle.json"
    correction = tmp_path / "correction.json"
    correct_external_oracle_v5(
        repo_root=REPO_ROOT,
        source_bundle_path=source,
        corrected_bundle_path=corrected,
        correction_receipt_path=correction,
        candidate_subject_commit=SUBJECT,
        entrypoint_path=CORRECTOR,
        source_anchor=anchor,
    )
    attacked = json.loads(corrected.read_text(encoding="utf-8"))
    attacked["labels"][0]["oracle"]["sentinel"] = "changed"
    corrected.write_bytes(canonical_bytes(attacked) + b"\n")
    with pytest.raises(P5OracleReviewErrorV5, match="NON_TARGET_ORACLE_DIFF_DETECTED"):
        review_external_oracle_v5(
            repo_root=REPO_ROOT,
            source_bundle_path=source,
            corrected_bundle_path=corrected,
            correction_receipt_path=correction,
            review_receipt_path=tmp_path / "review.json",
            candidate_subject_commit=SUBJECT,
            correction_entrypoint_path=CORRECTOR,
            reviewer_entrypoint_path=REVIEWER,
            source_anchor=anchor,
        )


def test_repository_contract_accepts_only_reviewed_correction_commitments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, anchor = _fixture(tmp_path)
    corrected = tmp_path / "corrected.bundle.json"
    correction = tmp_path / "correction.json"
    review = tmp_path / "review.json"
    correction_summary = correct_external_oracle_v5(
        repo_root=REPO_ROOT,
        source_bundle_path=source,
        corrected_bundle_path=corrected,
        correction_receipt_path=correction,
        candidate_subject_commit=SUBJECT,
        entrypoint_path=CORRECTOR,
        source_anchor=anchor,
    )
    review_summary = review_external_oracle_v5(
        repo_root=REPO_ROOT,
        source_bundle_path=source,
        corrected_bundle_path=corrected,
        correction_receipt_path=correction,
        review_receipt_path=review,
        candidate_subject_commit=SUBJECT,
        correction_entrypoint_path=CORRECTOR,
        reviewer_entrypoint_path=REVIEWER,
        source_anchor=anchor,
    )
    monkeypatch.setattr(
        "evals.trip_check_v1.p5.blind_external_contract_v5.validate_v4_source_anchor",
        lambda _root: anchor,
    )
    commitments = validate_external_custody_v5(
        repo_root=REPO_ROOT,
        external_bundle_path=corrected,
        external_correction_receipt_path=correction,
        external_review_receipt_path=review,
        expected_bundle_sha256=correction_summary["corrected_bundle_sha256"],
        expected_labels_canonical_sha256=correction_summary["corrected_labels_canonical_sha256"],
        expected_correction_receipt_sha256=correction_summary["correction_receipt_sha256"],
        expected_review_receipt_sha256=review_summary["review_receipt_sha256"],
        candidate_subject_commit=SUBJECT,
        source_bundle_sha256=anchor["external_bundle_sha256"],
        source_labels_canonical_sha256=anchor["labels_canonical_sha256"],
    )
    assert commitments["external_bundle_sha256"] == correction_summary["corrected_bundle_sha256"]
    assert commitments["review_receipt_sha256"] == review_summary["review_receipt_sha256"]

    with pytest.raises(P5ExternalCustodyContractError, match="source oracle hashes"):
        validate_external_custody_v5(
            repo_root=REPO_ROOT,
            external_bundle_path=corrected,
            external_correction_receipt_path=correction,
            external_review_receipt_path=review,
            expected_bundle_sha256=correction_summary["corrected_bundle_sha256"],
            expected_labels_canonical_sha256=correction_summary["corrected_labels_canonical_sha256"],
            expected_correction_receipt_sha256=correction_summary["correction_receipt_sha256"],
            expected_review_receipt_sha256=review_summary["review_receipt_sha256"],
            candidate_subject_commit=SUBJECT,
            source_bundle_sha256="0" * 64,
            source_labels_canonical_sha256=anchor["labels_canonical_sha256"],
        )


def test_custodian_rejects_when_target_diff_is_not_exactly_60(tmp_path: Path) -> None:
    source, anchor = _fixture(tmp_path, already_corrected=True)
    with pytest.raises(P5OracleCorrectionErrorV5, match="TARGET_DIFF_COUNT_NOT_60"):
        correct_external_oracle_v5(
            repo_root=REPO_ROOT,
            source_bundle_path=source,
            corrected_bundle_path=tmp_path / "corrected.bundle.json",
            correction_receipt_path=tmp_path / "correction.json",
            candidate_subject_commit=SUBJECT,
            entrypoint_path=CORRECTOR,
            source_anchor=anchor,
        )


def test_custodian_rejects_relative_external_paths(tmp_path: Path) -> None:
    source, anchor = _fixture(tmp_path)
    with pytest.raises(P5OracleCorrectionErrorV5, match="EXTERNAL_INPUT_LINK_FORBIDDEN"):
        correct_external_oracle_v5(
            repo_root=REPO_ROOT,
            source_bundle_path=Path("..") / source.name,
            corrected_bundle_path=tmp_path / "corrected.bundle.json",
            correction_receipt_path=tmp_path / "correction.json",
            candidate_subject_commit=SUBJECT,
            entrypoint_path=CORRECTOR,
            source_anchor=anchor,
        )


def test_custodian_preflights_both_outputs_before_writing(tmp_path: Path) -> None:
    source, anchor = _fixture(tmp_path)
    corrected = tmp_path / "corrected.bundle.json"
    receipt = tmp_path / "correction.json"
    receipt.write_text("occupied", encoding="utf-8")
    with pytest.raises(P5OracleCorrectionErrorV5, match="EXTERNAL_OUTPUT_OVERWRITE_FORBIDDEN"):
        correct_external_oracle_v5(
            repo_root=REPO_ROOT,
            source_bundle_path=source,
            corrected_bundle_path=corrected,
            correction_receipt_path=receipt,
            candidate_subject_commit=SUBJECT,
            entrypoint_path=CORRECTOR,
            source_anchor=anchor,
        )
    assert not corrected.exists()
    assert receipt.read_text(encoding="utf-8") == "occupied"


def test_custodian_rejects_same_bundle_and_receipt_output(tmp_path: Path) -> None:
    source, anchor = _fixture(tmp_path)
    shared_output = tmp_path / "shared.json"
    with pytest.raises(
        P5OracleCorrectionErrorV5,
        match="EXTERNAL_OUTPUT_PATHS_MUST_BE_DISTINCT",
    ):
        correct_external_oracle_v5(
            repo_root=REPO_ROOT,
            source_bundle_path=source,
            corrected_bundle_path=shared_output,
            correction_receipt_path=shared_output,
            candidate_subject_commit=SUBJECT,
            entrypoint_path=CORRECTOR,
            source_anchor=anchor,
        )
    assert not shared_output.exists()


def test_custodian_rejects_linked_output_parent(tmp_path: Path) -> None:
    source, anchor = _fixture(tmp_path)
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real_parent, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(P5OracleCorrectionErrorV5, match="EXTERNAL_OUTPUT_LINK_FORBIDDEN"):
        correct_external_oracle_v5(
            repo_root=REPO_ROOT,
            source_bundle_path=source,
            corrected_bundle_path=linked_parent / "corrected.bundle.json",
            correction_receipt_path=tmp_path / "correction.json",
            candidate_subject_commit=SUBJECT,
            entrypoint_path=CORRECTOR,
            source_anchor=anchor,
        )
