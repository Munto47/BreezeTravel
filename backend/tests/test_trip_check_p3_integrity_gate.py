from __future__ import annotations

import json

import pytest

from scripts.run_trip_check_p3_integrity_gate import (
    DEFAULT_OUTPUT,
    SECRET_PATTERNS,
    _ensure_postgres,
    _evaluate_phase_and_candidate,
    _normalized_log,
    _real_ocr_evidence,
    _safe_reset_output,
    _synthetic_ocr_evidence,
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


def test_synthetic_ocr_evidence_requires_v2_cleanup_and_subject_binding(tmp_path):
    path = tmp_path / "synthetic-ocr.json"
    _write_json(
        path,
        {
            "schema_version": "trip-check-p3-synthetic-ocr-manifest-v2",
            "evidence_class": "synthetic_stress",
            "status": "PASS",
            "subject_commit": "a" * 40,
            "spec_sha256": "b" * 64,
            "render_set_sha256": "c" * 64,
            "render_integrity": {
                "status": "PASS",
                "review_type": "deterministic_automated",
                "case_count": 12,
                "unique_render_count": 12,
            },
            "spec_receipt": {
                "source_schema": "trip-check-ocr-synthetic-v2-overlay",
                "resolved_spec_sha256": "d" * 64,
            },
            "cleanup_receipt": {"status": "DELETED", "run_dir_removed": True},
            "metrics": {
                "case_count": 12,
                "key_field_f1": 0.95,
                "low_confidence_confirmation_recall": 1.0,
                "original_image_leak_hits": 0,
            },
        },
    )

    assert _synthetic_ocr_evidence(path, subject="a" * 40)["status"] == "PASS"
    assert _synthetic_ocr_evidence(path, subject="d" * 40)["status"] == "FAIL"
    payload = json.loads(path.read_text("utf-8"))
    payload["cleanup_receipt"]["status"] = "CLEANUP_FAILED"
    _write_json(path, payload)
    assert _synthetic_ocr_evidence(path, subject="a" * 40)["status"] == "FAIL"


def test_p3_default_output_is_isolated_from_historical_evidence():
    assert DEFAULT_OUTPUT.as_posix().endswith("backend/evidence/trip_check_v1/p3")


def test_private_key_scan_requires_an_actual_pem_body():
    pattern = SECRET_PATTERNS["private_key"]

    assert pattern.search('f"-----BEGIN PRIVATE KEY-----\\n{private_key}\\n-----END PRIVATE KEY-----"') is None
    assert pattern.search(
        "-----BEGIN PRIVATE KEY-----\n"
        + "A" * 80
        + "\n-----END PRIVATE KEY-----"
    )


def test_gate_logs_are_portable_lf_and_strip_progress_whitespace():
    value = "one  \r\nD:\\munto\\code\\claudeProject\\agentTravel\\frontend  \r\n"

    assert _normalized_log(value) == "one\n<repo>\\frontend\n"


def test_p3_phase_can_pass_while_live_candidate_gate_is_not_run():
    phase, candidate = _evaluate_phase_and_candidate(
        required_checks_pass=True,
        synthetic_phase_gate="PASS",
        g2="PASS",
        g3="PASS",
        candidate_gates={"G1": "NOT_RUN", "G4": "NOT_RUN", "G5": "NOT_RUN", "G6": "NOT_RUN"},
        phase_contracts={"offline": True},
        candidate_contracts={"live_receipts_18": False},
        sensitive_scan_status="PASS",
    )

    assert phase == "PASS"
    assert candidate == "REJECT"


def test_p3_phase_still_rejects_missing_postgres_or_snapshot():
    phase, candidate = _evaluate_phase_and_candidate(
        required_checks_pass=True,
        synthetic_phase_gate="PASS",
        g2="NOT_RUN",
        g3="PASS",
        candidate_gates={"G1": "PASS", "G4": "PASS", "G5": "PASS", "G6": "PASS"},
        phase_contracts={"offline": True},
        candidate_contracts={"live_receipts_18": True},
        sensitive_scan_status="PASS",
    )

    assert phase == "REJECT"
    assert candidate == "REJECT"


def test_postgres_auto_mode_reuses_an_existing_service(monkeypatch):
    monkeypatch.setattr(
        "scripts.run_trip_check_p3_integrity_gate._postgres_preflight",
        lambda: (True, "127.0.0.1:5432"),
    )

    result = _ensure_postgres("auto")

    assert result == {
        "status": "READY",
        "mode": "auto",
        "endpoint": "127.0.0.1:5432",
        "started_by_gate": False,
    }
