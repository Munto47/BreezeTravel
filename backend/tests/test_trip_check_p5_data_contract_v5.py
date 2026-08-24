from __future__ import annotations

from pathlib import Path

import pytest

from evals.trip_check_v1.p5 import data_contract_v5 as contract


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_v5_pending_manifest_proves_v4_payload_identity_and_source_anchor() -> None:
    manifest = contract.validate_manifest_v5(
        REPO_ROOT, manifest_path=contract.MANIFEST_PATH_V5, require_sealed=False
    )
    assert manifest["dataset_id"] == "trip-check-p5-360-v5"
    assert manifest["generation"] == {
        "mode": "V4_ORACLE_POLICY_SUPERSESSION",
        "blind_bytes_copied_from_v4": True,
        "nonblind_bytes_copied_from_v4": True,
        "blind_labels_read": False,
        "ocr_executed": False,
    }
    assert manifest["source_v4_anchor"]["active_contract"] == "trip-check-p5-v4"


def test_v5_identity_rejects_single_byte_payload_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = Path.read_bytes

    def changed(path: Path) -> bytes:
        payload = original(path)
        if path.name == "frozen_blind.v5.inputs.jsonl":
            return payload + b"x"
        return payload

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(contract.P5DataContractErrorV5, match="differs from v4"):
        contract.validate_v4_v5_byte_identity(REPO_ROOT)


def test_v5_identity_rejects_run_spec_behavior_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contract._load_json

    def changed(path: Path) -> dict:
        payload = original(path)
        if path.name == "run_spec_template_v5.json":
            payload["budget"]["max_retries"] = 2
        return payload

    monkeypatch.setattr(contract, "_load_json", changed)
    with pytest.raises(contract.P5DataContractErrorV5, match="outside envelope"):
        contract.validate_v4_v5_byte_identity(REPO_ROOT)


def test_v5_source_anchor_rejects_caller_supplied_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = contract._load_json

    def changed(path: Path) -> dict:
        payload = original(path)
        if path.name == "source_active_contract_v4.json":
            payload["dataset_manifest_hash"] = "0" * 64
        return payload

    monkeypatch.setattr(contract, "_load_json", changed)
    with pytest.raises(contract.P5DataContractErrorV5, match="source contract"):
        contract.validate_v4_source_anchor(REPO_ROOT)
