from __future__ import annotations

import json

import pytest

from evals.trip_check_v1.p5.active_contract import (
    P5ContractNotReadyError,
    load_active_contract,
    require_v2_formal_ready,
)
from evals.trip_check_v1.p5.final_blind_scorer import (
    P5BlindScoringError,
    score_external_blind_run_group,
)


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
    with pytest.raises(P5BlindScoringError, match="P5_V2_FORMAL_CONTRACT_NOT_READY"):
        score_external_blind_run_group(
            repo_root=tmp_path,
            run_dir=tmp_path / "missing-run",
            expected_bundle_sha256="0" * 64,
            bundle_bytes=json.dumps({"must_not_be_read": True}).encode(),
            require_current_subject=True,
        )
