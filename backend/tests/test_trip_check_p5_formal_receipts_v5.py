from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.formal_receipts_v5 import (
    P5FormalReceiptErrorV5,
    RepoBindingV5,
    build_verification_receipt_v5,
    execute_command_receipt_v5,
    mint_blind_nonce_v5,
    validate_verification_receipt_v5,
)


SUBJECT = "a" * 40
BINDING = RepoBindingV5(SUBJECT, "origin/codex/p5-v5", SUBJECT, False)
REPO_ROOT = Path(__file__).parents[2]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_real_command_result_can_be_wrapped_and_tamper_fails_closed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config.json"
    artifact = tmp_path / "evidence.json"
    _write_json(config, {"version": 4})
    _write_json(artifact, {"status": "PASS"})
    command = execute_command_receipt_v5(
        repo_root=repo,
        kind="ruff",
        command=[sys.executable, "-c", "print('ruff passed')"],
        command_cwd=repo,
        config_artifacts={"config": config.resolve()},
        expected_artifacts={"ruff_evidence": artifact.resolve()},
        output_dir=tmp_path / "command",
        repo_binding=BINDING,
    )
    assert command["status"] == "PASS"
    wrapper_path = tmp_path / "receipts" / "ruff.json"
    wrapper = build_verification_receipt_v5(
        repo_root=repo,
        command_result_path=Path(command["receipt_path"]),
        output_path=wrapper_path,
    )
    assert wrapper["status"] == "PASS"
    assert wrapper["config_hash"] == command["config_hash"]
    assert wrapper["artifact_set_hash"] == command["artifact_set_hash"]

    _write_json(artifact, {"status": "changed"})
    with pytest.raises(P5FormalReceiptErrorV5):
        validate_verification_receipt_v5(wrapper_path)


def test_failed_command_never_becomes_pass_wrapper(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config.json"
    _write_json(config, {"version": 4})
    command = execute_command_receipt_v5(
        repo_root=repo,
        kind="backend_pytest",
        command=[sys.executable, "-c", "raise SystemExit(7)"],
        command_cwd=repo,
        config_artifacts={"config": config.resolve()},
        expected_artifacts={},
        output_dir=tmp_path / "command",
        repo_binding=BINDING,
    )
    assert command["status"] == "FAIL"
    wrapper = build_verification_receipt_v5(
        repo_root=repo,
        command_result_path=Path(command["receipt_path"]),
        output_path=tmp_path / "receipt.json",
    )
    assert wrapper["status"] == "FAIL"
    assert wrapper["readback_verified"] is False


def test_blind_nonce_is_external_single_use_shaped_and_label_free(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    p5 = repo / "backend" / "evals" / "trip_check_v1" / "p5"
    seal = p5 / "sealed" / "frozen_blind.v5.seal.json"
    dataset = p5 / "dataset_v5.manifest.json"
    active = p5 / "active_contract.json"
    _write_json(seal, {"schema_version": "trip-check-p5-blind-seal-v5"})
    manifest = {
        "dataset_id": "trip-check-p5-360-v5",
        "frozen": True,
        "formal_validation_eligible": True,
        "seal_status": "SEALED",
    }
    manifest["manifest_hash"] = digest(manifest)
    _write_json(dataset, manifest)
    seal_sha = hashlib.sha256(seal.read_bytes()).hexdigest()
    _write_json(
        active,
        {
            "active_contract": "trip-check-p5-v5",
            "formal_evidence_status": "READY",
            "dataset_manifest_hash": manifest["manifest_hash"],
            "blind_seal_v5_sha256": seal_sha,
        },
    )
    schema = (
        Path(__file__).parents[1]
        / "evals"
        / "trip_check_v1"
        / "p5"
        / "blind_run_nonce_v5.schema.json"
    )
    output = tmp_path / "custody" / "nonce.json"
    receipt = mint_blind_nonce_v5(
        repo_root=repo,
        output_path=output,
        active_contract_path=active,
        dataset_manifest_path=dataset,
        seal_path=seal,
        nonce_schema_path=schema,
        repo_binding=BINDING,
    )
    payload = output.read_text(encoding="utf-8").lower()
    assert receipt["status"] == "MINTED_NOT_CONSUMED"
    assert receipt["label_payload_present"] is False
    assert all(token not in payload for token in ("label", "oracle", "answer"))
    with pytest.raises(P5FormalReceiptErrorV5, match="OVERWRITE_FORBIDDEN"):
        mint_blind_nonce_v5(
            repo_root=repo,
            output_path=output,
            active_contract_path=active,
            dataset_manifest_path=dataset,
            seal_path=seal,
            nonce_schema_path=schema,
            repo_binding=BINDING,
        )
    with pytest.raises(P5FormalReceiptErrorV5, match="OUTPUT_MUST_BE_EXTERNAL"):
        mint_blind_nonce_v5(
            repo_root=repo,
            output_path=repo / "nonce.json",
            active_contract_path=active,
            dataset_manifest_path=dataset,
            seal_path=seal,
            nonce_schema_path=schema,
            repo_binding=BINDING,
        )


@pytest.mark.parametrize(
    "script",
    (
        "manage_trip_check_p5_v5_receipts.py",
        "mint_trip_check_p5_v5_blind_nonce.py",
    ),
)
def test_formal_receipt_clis_run_from_repository_root(script: str) -> None:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "backend" / "scripts" / script), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
