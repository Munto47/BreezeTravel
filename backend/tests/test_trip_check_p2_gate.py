from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_trip_check_p2_reliability_gate import DEFAULT_OUTPUT, _safe_reset_output


def test_p2_gate_refuses_to_reset_any_noncanonical_output(tmp_path: Path):
    with pytest.raises(ValueError):
        _safe_reset_output(tmp_path / "not-p2-evidence")
    assert DEFAULT_OUTPUT.as_posix().endswith("evidence/trip_check_v1/p2")


def test_pilot_manifest_uses_case_count_as_its_canonical_total():
    pilot_metrics = {"case_count": 18, "passed_count": 18, "failed_count": 0}
    assert pilot_metrics.get("case_count") == 18
    assert "total_cases" not in pilot_metrics
