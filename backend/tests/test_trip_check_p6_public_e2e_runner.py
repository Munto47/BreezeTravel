from __future__ import annotations

from pathlib import Path

import pytest

from evals.trip_check_v1.p6.contracts_v1 import P6ContractError
from evals.trip_check_v1.p6.public_e2e_runner import run_public_e2e
from tests.test_trip_check_p6_performance_runner import _spec


def _flow(spec, credentials):  # noqa: ANN001, ANN202
    assert spec["subject_commit"] == "1" * 40
    assert set(credentials) == {"P6_PUBLIC_TEST_EMAIL", "P6_PUBLIC_TEST_PASSWORD"}
    return {
        "health_http_status": 200,
        "health_response_body_sha256": "2" * 64,
        "step_count": 16,
        "step_set_sha256": "3" * 64,
        "step_receipt_set_sha256": "4" * 64,
        "final_resume_body_sha256": "5" * 64,
        "initial_revision": 1,
        "final_revision": 2,
        "advice_action_count": 1,
        "repair_count": 1,
        "provider_failure_count": 0,
        "postcheck_status": "SUCCEEDED",
        "controlled_snapshot": True,
    }


def test_public_e2e_runner_emits_bound_health_and_chain_receipts(tmp_path: Path) -> None:
    spec_path, _ = _spec(tmp_path)
    output = tmp_path / "public"
    health, e2e = run_public_e2e(
        candidate_run_spec_path=spec_path,
        output_root=output,
        repo_root=Path(__file__).parents[2],
        formal=False,
        flow_runner=_flow,
    )
    assert health["status"] == "PASS"
    assert e2e["status"] == "PASS"
    assert (output / "public_full_chain_readback.json").is_file()
    assert (output / "public_health_receipt.json").is_file()
    assert (output / "public_e2e_receipt.json").is_file()


def test_public_e2e_runner_rejects_missing_postcheck(tmp_path: Path) -> None:
    spec_path, _ = _spec(tmp_path)

    def failed(spec, credentials):  # noqa: ANN001, ANN202
        value = _flow(spec, credentials)
        value["postcheck_status"] = "WAITING"
        return value

    with pytest.raises(P6ContractError, match="P6_G5_PUBLIC_FLOW_INVALID"):
        run_public_e2e(
            candidate_run_spec_path=spec_path,
            output_root=tmp_path / "public",
            repo_root=Path(__file__).parents[2],
            formal=False,
            flow_runner=failed,
        )
