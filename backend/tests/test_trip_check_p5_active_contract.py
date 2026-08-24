from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from evals.trip_check_v1.p5.active_contract import (
    P5ContractNotReadyError,
    SOURCE_V2_CONTRACT_PATH,
    load_active_contract,
    reject_v1_formal,
    require_v2_formal_ready,
    require_v3_formal_ready,
    require_v4_formal_ready,
    require_v5_formal_ready,
)
from evals.trip_check_v1.p5.final_blind_scorer import (
    P5BlindScoringError,
    score_external_blind_run_group,
)
from scripts import run_trip_check_p5_eval, run_trip_check_p5_gate, score_trip_check_p5_eval


def test_v5_is_active_and_v4_v3_v2_remain_immutable_source_anchors() -> None:
    active = load_active_contract()

    assert active["active_contract"] == "trip-check-p5-v5"
    assert active["formal_evidence_status"] == "READY"
    assert len(active["candidate_freeze_commit"]) == 40
    assert len(active["blind_seal_v5_sha256"]) == 64
    assert [item["contract_id"] for item in active["deprecated_contracts"]] == [
        "trip-check-p5-v1",
        "trip-check-p5-v2",
        "trip-check-p5-v3",
        "trip-check-p5-v4",
    ]
    assert active["source_v4_contract"]["active_contract"] == "trip-check-p5-v4"
    assert active["source_v4_contract"]["source_v3_contract"]["active_contract"] == (
        "trip-check-p5-v3"
    )
    assert active["source_v4_contract"]["source_v3_contract"]["source_v2_contract"]["path"] == (
        "evals/trip_check_v1/p5/source_active_contract_v2.json"
    )
    assert require_v5_formal_ready() == active
    with pytest.raises(P5ContractNotReadyError, match="P5_V4_FORMAL_CONTRACT_SUPERSEDED"):
        require_v4_formal_ready()
    with pytest.raises(P5ContractNotReadyError, match="P5_V3_FORMAL_CONTRACT_SUPERSEDED"):
        require_v3_formal_ready()
    with pytest.raises(P5ContractNotReadyError, match="P5_V2_FORMAL_CONTRACT_SUPERSEDED"):
        require_v2_formal_ready()
    assert require_v2_formal_ready(SOURCE_V2_CONTRACT_PATH)["active_contract"] == "trip-check-p5-v2"


def test_v2_formal_evidence_fails_closed_before_seal(tmp_path) -> None:
    contract = load_active_contract(SOURCE_V2_CONTRACT_PATH)
    contract["formal_evidence_status"] = "PENDING_V2_SEAL"
    contract.pop("candidate_freeze_commit")
    contract.pop("blind_seal_v2_sha256")
    path = tmp_path / "active_contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")

    with pytest.raises(P5ContractNotReadyError, match="P5_V2_FORMAL_CONTRACT_NOT_READY"):
        require_v2_formal_ready(path)


def test_v1_blind_scorer_rejects_before_reading_any_external_bundle(tmp_path) -> None:
    with pytest.raises(P5BlindScoringError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        score_external_blind_run_group(
            repo_root=tmp_path,
            run_dir=tmp_path / "missing-run",
            expected_bundle_sha256="0" * 64,
            bundle_bytes=json.dumps({"must_not_be_read": True}).encode(),
            require_current_subject=True,
        )


def test_v1_formal_rejection_is_permanent_after_v2_becomes_ready(tmp_path) -> None:
    contract = load_active_contract(SOURCE_V2_CONTRACT_PATH)
    contract["formal_evidence_status"] = "READY"
    path = tmp_path / "active_contract.json"
    path.write_text(json.dumps(contract), encoding="utf-8")

    assert require_v2_formal_ready(path)["formal_evidence_status"] == "READY"
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        reject_v1_formal(path)


@pytest.mark.asyncio
async def test_v1_formal_runner_rejects_before_loading_cases() -> None:
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        await run_trip_check_p5_eval._execute(SimpleNamespace(require_formal=True))


def test_v1_formal_scorer_and_gate_scripts_reject_permanently(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["score_trip_check_p5_eval.py", "--run-dir", "missing"],
    )
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        score_trip_check_p5_eval.main()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_trip_check_p5_gate.py",
            "--nonblind-run-dir",
            "missing",
            "--nonblind-score",
            "missing",
            "--blind-run-dir",
            "missing",
            "--blind-score",
            "missing",
            "--judge-panel",
            "missing",
        ],
    )
    with pytest.raises(P5ContractNotReadyError, match="P5_V1_FORMAL_CONTRACT_SUPERSEDED"):
        run_trip_check_p5_gate.main()
