from __future__ import annotations

import json

import pytest

from scripts.run_trip_check_p3_integrity_gate import (
    DEFAULT_OUTPUT,
    _real_ocr_evidence,
    _safe_reset_output,
    _write_json,
)


def test_p3_gate_refuses_noncanonical_output(tmp_path):
    with pytest.raises(ValueError):
        _safe_reset_output(tmp_path / "wrong")


def test_real_ocr_evidence_requires_bound_metrics(tmp_path):
    path = tmp_path / "real-ocr.json"
    _write_json(
        path,
        {
            "status": "PASS",
            "subject_commit": "a" * 40,
            "metrics": {
                "case_count": 12,
                "key_field_f1": 0.96,
                "low_confidence_confirmation_recall": 1.0,
                "original_image_leak_hits": 0,
            },
        },
    )

    assert _real_ocr_evidence(str(path), subject="a" * 40)["status"] == "PASS"
    assert _real_ocr_evidence(str(path), subject="b" * 40)["status"] == "FAIL"
    assert _real_ocr_evidence(None, subject="a" * 40)["status"] == "NOT_RUN"
    assert json.loads(path.read_text("utf-8"))["metrics"]["case_count"] == 12


def test_p3_default_output_is_isolated_from_historical_evidence():
    assert DEFAULT_OUTPUT.as_posix().endswith("backend/evidence/trip_check_v1/p3")
