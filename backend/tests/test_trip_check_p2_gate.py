from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_trip_check_p2_reliability_gate import DEFAULT_OUTPUT, _safe_reset_output


def test_p2_gate_refuses_to_reset_any_noncanonical_output(tmp_path: Path):
    with pytest.raises(ValueError):
        _safe_reset_output(tmp_path / "not-p2-evidence")
    assert DEFAULT_OUTPUT.as_posix().endswith("evidence/trip_check_v1/p2")
