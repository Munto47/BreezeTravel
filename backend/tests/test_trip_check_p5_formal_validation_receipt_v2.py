from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from evals.trip_check_v1.p5.active_contract import P5ContractNotReadyError
from evals.trip_check_v1.p5.data_contract import digest, file_sha256
import evals.trip_check_v1.p5.formal_validation_receipt_v2 as receipt_v2
from evals.trip_check_v1.p5.formal_validation_receipt_v2 import (
    DEFAULT_RECEIPT_PATH,
    P5FormalValidationReceiptError,
    generate_formal_validation_receipt,
)
from scripts import seal_trip_check_p5_blind
from scripts.validate_trip_check_p5_dataset_v2 import validate


SUBJECT = "5" * 40


def _formal_result() -> dict:
    result = validate(formal=True)
    assert result["status"] == "PASS", result["errors"]
    return result


def test_formal_receipt_default_output_is_absolute_and_outside_repository() -> None:
    assert DEFAULT_RECEIPT_PATH.is_absolute()
    with pytest.raises(ValueError):
        DEFAULT_RECEIPT_PATH.resolve().relative_to(receipt_v2.REPO_ROOT.resolve())


def test_formal_receipt_binds_commit_dataset_validator_and_atomic_readback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(receipt_v2, "_assert_clean_subject", lambda repo_root, subject_commit: SUBJECT)
    output = tmp_path / "receipt.json"

    receipt = generate_formal_validation_receipt(
        subject_commit=SUBJECT,
        output_path=output,
        validator=lambda *, formal: _formal_result(),
        created_at="2026-08-23T12:00:00Z",
    )

    assert receipt["subject_commit"] == SUBJECT
    assert receipt["formal"] is True
    assert receipt["status"] == "PASS"
    assert receipt["counts"]["total"] == 360
    assert receipt["counts"]["screenshots"] == 171
    assert receipt["dataset_manifest"]["file_sha256"] == file_sha256(receipt_v2.MANIFEST_PATH)
    assert receipt["validator"] == {
        "path": "backend/scripts/validate_trip_check_p5_dataset_v2.py",
        "code_sha256": file_sha256(receipt_v2.VALIDATOR_PATH),
    }
    assert receipt["receipt_hash"] == digest(
        {key: value for key, value in receipt.items() if key != "receipt_hash"}
    )
    assert json.loads(output.read_text(encoding="utf-8")) == receipt
    assert not list(tmp_path.glob("*.tmp"))

    extra = dict(receipt)
    extra["unexpected"] = True
    extra["receipt_hash"] = digest({key: value for key, value in extra.items() if key != "receipt_hash"})
    with pytest.raises(P5FormalValidationReceiptError, match="RECEIPT_SCHEMA"):
        receipt_v2._validate_receipt(extra)


def test_formal_receipt_rejects_dirty_and_mixed_subject_before_validation(tmp_path, monkeypatch) -> None:
    calls: list[bool] = []

    def validator(*, formal: bool) -> dict:
        calls.append(formal)
        return _formal_result()

    monkeypatch.setattr(
        receipt_v2,
        "_assert_clean_subject",
        lambda repo_root, subject_commit: (_ for _ in ()).throw(
            P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_DIRTY_TREE")
        ),
    )
    with pytest.raises(P5FormalValidationReceiptError, match="DIRTY_TREE"):
        generate_formal_validation_receipt(output_path=tmp_path / "dirty.json", validator=validator)
    assert calls == []

    monkeypatch.setattr(
        receipt_v2,
        "_assert_clean_subject",
        lambda repo_root, subject_commit: (_ for _ in ()).throw(
            P5FormalValidationReceiptError("P5_FORMAL_VALIDATION_MIXED_SUBJECT_COMMIT")
        ),
    )
    with pytest.raises(P5FormalValidationReceiptError, match="MIXED_SUBJECT_COMMIT"):
        generate_formal_validation_receipt(
            subject_commit="6" * 40,
            output_path=tmp_path / "mixed.json",
            validator=validator,
        )
    assert calls == []


def test_formal_receipt_rejects_relative_or_repository_output_before_validation(
    tmp_path,
    monkeypatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        receipt_v2,
        "_assert_clean_subject",
        lambda repo_root, subject_commit: SUBJECT,
    )

    def validator(*, formal: bool) -> dict:
        calls.append(formal)
        return _formal_result()

    with pytest.raises(P5FormalValidationReceiptError, match="EXTERNAL_ABSOLUTE"):
        generate_formal_validation_receipt(
            subject_commit=SUBJECT,
            output_path=Path("receipt.json"),
            validator=validator,
        )
    with pytest.raises(P5FormalValidationReceiptError, match="INSIDE_REPOSITORY"):
        generate_formal_validation_receipt(
            subject_commit=SUBJECT,
            output_path=receipt_v2.REPO_ROOT / "receipt.json",
            validator=validator,
        )
    assert calls == []


def test_formal_receipt_rejects_validator_failure_and_does_not_write(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(receipt_v2, "_assert_clean_subject", lambda repo_root, subject_commit: SUBJECT)
    output = tmp_path / "receipt.json"
    failed = _formal_result()
    failed["status"] = "FAIL"
    failed["errors"] = ["controlled failure"]

    with pytest.raises(P5FormalValidationReceiptError, match="VALIDATOR_FAILED"):
        generate_formal_validation_receipt(
            subject_commit=SUBJECT,
            output_path=output,
            validator=lambda *, formal: failed,
        )

    assert not output.exists()


def test_formal_receipt_rejects_existing_receipt_overwrite_drift(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(receipt_v2, "_assert_clean_subject", lambda repo_root, subject_commit: SUBJECT)
    output = tmp_path / "receipt.json"
    result = _formal_result()
    first = generate_formal_validation_receipt(
        subject_commit=SUBJECT,
        output_path=output,
        validator=lambda *, formal: result,
        created_at="2026-08-23T12:00:00Z",
    )
    drifted = dict(first)
    drifted["subject_commit"] = "6" * 40
    drifted["receipt_hash"] = digest({key: value for key, value in drifted.items() if key != "receipt_hash"})
    output.write_text(json.dumps(drifted), encoding="utf-8")

    with pytest.raises(P5FormalValidationReceiptError, match="OVERWRITE_DRIFT"):
        generate_formal_validation_receipt(
            subject_commit=SUBJECT,
            output_path=output,
            validator=lambda *, formal: result,
            created_at="2026-08-23T12:00:01Z",
        )


def test_formal_receipt_existing_identical_binding_is_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(receipt_v2, "_assert_clean_subject", lambda repo_root, subject_commit: SUBJECT)
    output = tmp_path / "receipt.json"
    result = _formal_result()
    first = generate_formal_validation_receipt(
        subject_commit=SUBJECT,
        output_path=output,
        validator=lambda *, formal: result,
        created_at="2026-08-23T12:00:00Z",
    )
    second = generate_formal_validation_receipt(
        subject_commit=SUBJECT,
        output_path=output,
        validator=lambda *, formal: result,
        created_at="2026-08-23T12:00:01Z",
    )

    assert second == first


def test_v1_seal_function_is_permanently_fail_closed() -> None:
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        seal_trip_check_p5_blind.seal(
            labels_canonical_sha256="0" * 64,
            external_bundle_sha256="1" * 64,
            review_receipt_sha256="2" * 64,
        )


@pytest.mark.parametrize(
    "arguments",
    [
        [],
        [
            "--labels-canonical-sha256",
            "0" * 64,
            "--external-bundle-sha256",
            "1" * 64,
            "--review-receipt-sha256",
            "2" * 64,
        ],
    ],
)
def test_v1_seal_cli_rejects_before_argument_parsing_and_never_mutates_v1_files(arguments) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    seal_path = backend_root / "evals" / "trip_check_v1" / "p5" / "sealed" / "frozen_blind.seal.json"
    manifest_path = backend_root / "evals" / "trip_check_v1" / "p5" / "dataset_v1.manifest.json"
    before = (file_sha256(seal_path), file_sha256(manifest_path))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(backend_root)

    completed = subprocess.run(
        [sys.executable, "-m", "scripts.seal_trip_check_p5_blind", *arguments],
        cwd=backend_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )

    assert completed.returncode != 0
    assert "P5_V1_FORMAL_CONTRACT_SUPERSEDED" in completed.stderr
    assert "the following arguments are required" not in completed.stderr
    assert (file_sha256(seal_path), file_sha256(manifest_path)) == before
