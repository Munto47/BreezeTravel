from __future__ import annotations

from copy import deepcopy

from evals.trip_check_v1.p5.data_contract import load_jsonl
from evals.trip_check_v1.p5.data_contract_v3 import BLIND_INPUT_PATH_V3
import scripts.validate_trip_check_p5_dataset_v3 as validator_v3


def test_checked_in_v3_candidate_passes_nonformal_validation() -> None:
    result = validator_v3.validate(formal=False)

    assert result["status"] == "PASS", result["errors"]
    assert result["blind_labels_read"] is False
    assert result["counts"]["total"] == 360
    assert result["historical_ocr"] == {
        "receipt_count": 171,
        "unique_image_hashes": 171,
        "fresh_actual_ocr_execution": "NOT_RUN",
    }
    assert result["seal_status"] == "PENDING_V3_SEAL"


def test_checked_in_v3_candidate_is_not_formal_before_blind_seal() -> None:
    result = validator_v3.validate(formal=True)

    assert result["status"] == "REJECT"
    assert "formal validation requires a SEALED v3 manifest commitment" in result["errors"]


def test_validator_rejects_nested_blind_label_even_after_case_rehash(monkeypatch) -> None:
    original_load = validator_v3.load_jsonl
    blind = load_jsonl(BLIND_INPUT_PATH_V3)
    tampered = deepcopy(blind)
    tampered[0]["provenance"]["blind_label"] = "secret"

    def load_with_leak(path):
        if path == BLIND_INPUT_PATH_V3:
            return tampered
        return original_load(path)

    monkeypatch.setattr(validator_v3, "load_jsonl", load_with_leak)
    result = validator_v3.validate(formal=False)

    assert result["status"] == "REJECT"
    assert any("forbidden label fields" in error for error in result["errors"])


def test_validator_rejects_blind_materialization_label_injection(monkeypatch) -> None:
    original_load = validator_v3.load_jsonl
    original_rows = original_load(validator_v3.BLIND_MATERIALIZATIONS_PATH_V3)
    tampered = deepcopy(original_rows)
    tampered[0]["fault_script"]["blind_label"] = {"answer": "secret"}

    def load_with_leak(path):
        if path == validator_v3.BLIND_MATERIALIZATIONS_PATH_V3:
            return tampered
        return original_load(path)

    monkeypatch.setattr(validator_v3, "load_jsonl", load_with_leak)
    result = validator_v3.validate(formal=False)

    assert result["status"] == "REJECT"
    assert any("blind materialization contains label fields" in error for error in result["errors"])
