"""Fail-closed P6 G5 local-browser, performance, privacy, and public aggregator."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    file_sha256,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
    validate_public_receipt,
)
from evals.trip_check_v1.p6.local_browser_runner import REQUIRED_COVERAGE
from evals.trip_check_v1.p6.performance_runner import PERFORMANCE_THRESHOLDS_MS


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _write_json_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except OSError as exc:
        raise P6ContractError("P6_G5_ARTIFACT_WRITE_FAILED") from exc


def _validate_self_hash(value: Mapping[str, Any], reason: str) -> None:
    expected = digest({key: item for key, item in value.items() if key != "receipt_hash"})
    if value.get("receipt_hash") != expected:
        raise P6ContractError(reason)


def _validate_local(
    value: Mapping[str, Any],
    spec: Mapping[str, Any],
    report_path: Path,
) -> None:
    _validate_self_hash(value, "P6_G5_LOCAL_RECEIPT_HASH_MISMATCH")
    if not (
        value.get("schema_version") == "trip-check-p6-local-browser-receipt-v1"
        and value.get("subject_commit") == spec["subject_commit"]
        and value.get("run_spec_hash") == spec["run_spec_hash"]
        and value.get("status") == "PASS"
        and value.get("evidence_level") == "browser_local"
        and value.get("browser_report_sha256") == file_sha256(report_path)
        and value.get("test_counts")
        == {"expected": 6, "unexpected": 0, "flaky": 0, "skipped": 0}
        and value.get("coverage") == list(REQUIRED_COVERAGE)
        and value.get("human_evidence") is False
    ):
        raise P6ContractError("P6_G5_LOCAL_RECEIPT_INVALID")


def _validate_performance(
    value: Mapping[str, Any],
    spec: Mapping[str, Any],
    sample_path: Path,
) -> None:
    _validate_self_hash(value, "P6_G5_PERFORMANCE_RECEIPT_HASH_MISMATCH")
    metrics = value.get("metrics")
    sample_counts = value.get("sample_counts")
    if not (
        value.get("schema_version") == "trip-check-p6-performance-receipt-v1"
        and value.get("subject_commit") == spec["subject_commit"]
        and value.get("run_spec_hash") == spec["run_spec_hash"]
        and value.get("status") == "PASS"
        and value.get("evidence_level") == "browser_local"
        and value.get("thresholds_ms") == PERFORMANCE_THRESHOLDS_MS
        and value.get("threshold_failures") == []
        and value.get("performance_threshold_failure_count") == 0
        and value.get("sample_file_sha256") == file_sha256(sample_path)
        and value.get("controlled_snapshot") is True
        and value.get("human_evidence") is False
        and isinstance(metrics, Mapping)
        and isinstance(sample_counts, Mapping)
        and sample_counts.get("first_feedback") == 3
        and sample_counts.get("parse_confirmation_ui") == 3
        and sample_counts.get("parse_confirmation_engine") == 20
        and sample_counts.get("three_image_ocr_batches") == 20
        and sample_counts.get("base_report") == 20
        and sample_counts.get("risk_report") == 20
        and all(metrics.get(key, float("inf")) <= threshold for key, threshold in PERFORMANCE_THRESHOLDS_MS.items())
    ):
        raise P6ContractError("P6_G5_PERFORMANCE_RECEIPT_INVALID")


def run_g5_gate(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    repo_root: Path,
    formal: bool = True,
    dependency_root: Path | None = None,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=True)
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_G5_EXTERNAL_ROOT_REQUIRED")
    if formal:
        expected_repo = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if read_actual_repo_state(repo_resolved) != expected_repo:
            raise P6ContractError("P6_G5_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g5").resolve(strict=True):
            raise P6ContractError("P6_G5_OUTPUT_ROOT_INVALID")
    evidence_root = Path(spec["evidence_root"]) if formal else (dependency_root or Path(spec["evidence_root"]))
    receipt_path = output_resolved / "g5_receipt.json"
    if receipt_path.exists():
        raise P6ContractError("P6_G5_RECEIPT_ALREADY_EXISTS")
    local_root = output_resolved / "local"
    performance_root = output_resolved / "performance"
    public_root = output_resolved / "public"
    local = _load_json(local_root / "local_browser_receipt.json", "P6_G5_LOCAL_RECEIPT_INVALID")
    performance = _load_json(
        performance_root / "performance_receipt.json",
        "P6_G5_PERFORMANCE_RECEIPT_INVALID",
    )
    _validate_local(local, spec, local_root / "playwright-report.json")
    _validate_performance(performance, spec, performance_root / "performance_samples.json")
    g1 = validate_gate_receipt(
        _load_json(evidence_root / "g1" / "g1_receipt.json", "P6_G5_G1_RECEIPT_INVALID"),
        "g1",
        spec,
    )
    g2 = validate_gate_receipt(
        _load_json(evidence_root / "g2" / "g2_receipt.json", "P6_G5_G2_RECEIPT_INVALID"),
        "g2",
        spec,
    )
    health = validate_public_receipt(
        _load_json(public_root / "public_health_receipt.json", "P6_G5_PUBLIC_HEALTH_INVALID"),
        "health",
        spec,
    )
    public_e2e = validate_public_receipt(
        _load_json(public_root / "public_e2e_receipt.json", "P6_G5_PUBLIC_E2E_INVALID"),
        "e2e",
        spec,
    )
    if not (
        g1["metrics"].get("privacy_leak_count") == 0
        and g1["metrics"].get("cleanup_failure_count") == 0
        and g2["metrics"].get("restart_readback_failure_count") == 0
    ):
        raise P6ContractError("P6_G5_DEPENDENCY_RECEIPT_INVALID")
    metrics = {
        "local_browser_failure_count": 0,
        "public_e2e_failure_count": 0,
        "performance_threshold_failure_count": 0,
        "privacy_failure_count": 0,
        "local_browser_test_count": 6,
        "local_browser_coverage_count": len(REQUIRED_COVERAGE),
        "process_restart_readback_failure_count": 0,
        "public_receipt_count": 2,
        **performance["metrics"],
    }
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g5",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "browser_local",
        "checks_total": 16,
        "checks_passed": 16,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt = validate_gate_receipt(receipt, "g5", spec)
    readback = {
        "schema_version": "trip-check-p6-g5-binding-readback-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "local_browser_receipt_hash": local["receipt_hash"],
        "performance_receipt_hash": performance["receipt_hash"],
        "g1_receipt_hash": g1["receipt_hash"],
        "g2_receipt_hash": g2["receipt_hash"],
        "public_health_receipt_hash": health["receipt_hash"],
        "public_e2e_receipt_hash": public_e2e["receipt_hash"],
    }
    readback["receipt_hash"] = digest(readback)
    _write_json_new(output_resolved / "g5_binding_readback.json", readback)
    _write_json_new(receipt_path, receipt)
    return receipt
