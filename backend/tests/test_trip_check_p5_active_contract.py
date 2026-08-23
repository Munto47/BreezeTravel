from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

from evals.trip_check_v1.p5.active_contract import (
    P5ContractNotReadyError,
    load_active_contract,
    reject_v1_formal,
    require_v2_formal_ready,
)
from evals.trip_check_v1.p5.final_blind_scorer import (
    P5BlindScoringError,
    score_external_blind_run_group,
)
from scripts import run_trip_check_p5_eval, run_trip_check_p5_gate, score_trip_check_p5_eval


def test_v1_is_superseded_and_v2_formal_evidence_is_blocked_until_sealed() -> None:
    active = load_active_contract()

    assert active["active_contract"] == "trip-check-p5-v2"
    assert active["formal_evidence_status"] == "PENDING_V2_SEAL"
    assert active["deprecated_contracts"] == [
        {
            "contract_id": "trip-check-p5-v1",
            "formal_evidence_eligible": False,
            "reason": "SUPERSEDED_BY_USER_APPROVED_P5_V2",
        }
    ]
    with pytest.raises(P5ContractNotReadyError, match="P5_V2_FORMAL_CONTRACT_NOT_READY"):
        require_v2_formal_ready()


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
    contract = load_active_contract()
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
