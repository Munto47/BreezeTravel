"""Fail-closed P6 G6 immutable release and Candidate Gate runner."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p6.contracts_v1 import (
    GATE_EVIDENCE_LEVELS,
    P6ContractError,
    candidate_final_disclosure_valid,
    candidate_gate_eligible,
    digest,
    file_sha256,
    read_actual_repo_state,
    read_public_candidate,
    validate_candidate_evidence,
    validate_candidate_gate_decision,
    validate_candidate_gate_readback,
    validate_candidate_run_spec,
    validate_final_candidate_evidence,
    validate_final_disclosure_readback,
    validate_gate_receipt,
    validate_public_receipt,
    validate_release_artifact_files,
    validate_release_manifest,
)


KNOWN_GAPS = [
    "HUMAN_EVIDENCE_NOT_RUN",
    "CONTROLLED_SNAPSHOT_PUBLIC_ONLY",
    "NO_MAIN_MERGE",
    "NO_PRODUCTION_RELEASE",
    "NO_H1_HUMAN_TESTING",
]
DELIVERABLES = {
    "demo_90_seconds": ("deliverables/demo_90_seconds.mp4", "automated_proxy_judge"),
    "demo_5_minutes": ("deliverables/demo_5_minutes.mp4", "automated_proxy_judge"),
    "architecture_diagram": ("deliverables/architecture_diagram.svg", "automated_proxy_judge"),
    "recovery_sequence": ("deliverables/recovery_sequence.svg", "automated_proxy_judge"),
    "p5_ablation_table": ("deliverables/p5_ablation_table.json", "automated_proxy_judge"),
    "reproduce_commands": ("deliverables/reproduce_commands.md", "automated_proxy_judge"),
}


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except OSError as exc:
        raise P6ContractError("P6_G6_ARTIFACT_WRITE_FAILED") from exc


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _artifact(root: Path, logical_name: str, relative: str, evidence_level: str) -> dict[str, Any]:
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        size = resolved.stat().st_size
    except (OSError, ValueError) as exc:
        raise P6ContractError("P6_G6_ARTIFACT_UNREADABLE") from exc
    if size < 1:
        raise P6ContractError("P6_G6_ARTIFACT_EMPTY")
    return {
        "logical_name": logical_name,
        "path": Path(relative).as_posix(),
        "sha256": file_sha256(resolved),
        "size_bytes": size,
        "evidence_level": evidence_level,
    }


def build_pre_gate_release(
    *,
    candidate_run_spec_path: Path,
    repo_root: Path,
    formal: bool = True,
    released_at: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    repo_resolved = repo_root.resolve(strict=True)
    evidence_root = Path(spec["evidence_root"]).resolve(strict=True)
    try:
        evidence_root.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_G6_EXTERNAL_ROOT_REQUIRED")
    if formal:
        expected = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if read_actual_repo_state(repo_resolved) != expected:
            raise P6ContractError("P6_G6_REPO_BINDING_INVALID")
    g6_root = evidence_root / "g6"
    if g6_root.exists() and any(g6_root.iterdir()):
        raise P6ContractError("P6_G6_OUTPUT_NOT_EMPTY")
    gate_receipts: dict[str, dict[str, Any]] = {}
    artifacts: list[dict[str, Any]] = []
    for index in range(6):
        gate = f"g{index}"
        relative = f"{gate}/{gate}_receipt.json"
        receipt = validate_gate_receipt(
            _load_json(evidence_root / relative, "P6_G6_GATE_RECEIPT_INVALID"),
            gate,
            spec,
        )
        gate_receipts[gate] = receipt
        artifacts.append(_artifact(evidence_root, f"{gate}_receipt", relative, GATE_EVIDENCE_LEVELS[gate]))
    public_health_relative = "g5/public/public_health_receipt.json"
    public_e2e_relative = "g5/public/public_e2e_receipt.json"
    validate_public_receipt(
        _load_json(evidence_root / public_health_relative, "P6_G6_PUBLIC_RECEIPT_INVALID"),
        "health",
        spec,
    )
    validate_public_receipt(
        _load_json(evidence_root / public_e2e_relative, "P6_G6_PUBLIC_RECEIPT_INVALID"),
        "e2e",
        spec,
    )
    artifacts.extend([
        _artifact(evidence_root, "public_health_receipt", public_health_relative, "public_e2e"),
        _artifact(evidence_root, "public_e2e_receipt", public_e2e_relative, "public_e2e"),
    ])
    for logical_name, (relative, evidence_level) in DELIVERABLES.items():
        artifacts.append(_artifact(evidence_root, logical_name, relative, evidence_level))
    artifact_by_name = {item["logical_name"]: item for item in artifacts}
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p6-release-manifest-v1",
        "subject_commit": spec["subject_commit"],
        "upstream_ref": spec["upstream_ref"],
        "upstream_commit": spec["upstream_commit"],
        "dirty_tree": False,
        "candidate_run_spec_hash": spec["run_spec_hash"],
        "p5_gate_manifest_hash": spec["p5_gate_manifest_hash"],
        "scope": spec["scope"],
        "gates": {
            gate: {
                "status": "PASS",
                "receipt_sha256": artifact_by_name[f"{gate}_receipt"]["sha256"],
                "evidence_level": GATE_EVIDENCE_LEVELS[gate],
            }
            for gate in gate_receipts
        },
        "artifacts": artifacts,
        "public_e2e": {
            "status": "PASS",
            "url": spec["public_candidate"]["base_url"],
            "health_receipt_sha256": artifact_by_name["public_health_receipt"]["sha256"],
            "e2e_receipt_sha256": artifact_by_name["public_e2e_receipt"]["sha256"],
            "controlled_snapshot": True,
        },
        "known_gaps": KNOWN_GAPS,
        "human_evidence": False,
        "read_only_mount": True,
        "released_at": released_at or _utc_now(),
    }
    manifest["manifest_hash"] = digest(manifest)
    manifest = validate_release_manifest(manifest)
    validate_release_artifact_files(manifest, evidence_root, spec)
    evidence: dict[str, Any] = {
        "schema_version": "trip-check-p6-candidate-evidence-v1",
        "subject_commit": spec["subject_commit"],
        "scope": spec["scope"],
        "gates": {f"g{index}": "PASS" for index in range(6)} | {"g6": "NOT_RUN"},
        "evidence_levels": {
            "controlled_snapshot": "PASS",
            "live_provider": "PASS",
            "public_e2e": "PASS",
            "automated_proxy_judge": "PASS",
        },
        "public_e2e": {
            "status": "PASS",
            "url": spec["public_candidate"]["base_url"],
            "health_status": "PASS",
            "controlled_snapshot": True,
        },
        "known_gaps": KNOWN_GAPS,
        "human_evidence": False,
        "manifest_hash": manifest["manifest_hash"],
        "candidate_gate_receipt_hash": None,
        "candidate_gate_status": "NOT_RUN",
    }
    evidence = validate_candidate_evidence(evidence)
    _write_json_new(g6_root / "release_manifest.json", manifest)
    _write_json_new(g6_root / "candidate_evidence_pre_gate.json", evidence)
    return manifest, evidence


def capture_pre_gate_readback(
    *,
    candidate_run_spec_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    evidence_root = Path(spec["evidence_root"])
    g6_root = evidence_root / "g6"
    manifest = validate_release_manifest(
        _load_json(g6_root / "release_manifest.json", "P6_G6_RELEASE_MANIFEST_INVALID")
    )
    evidence = validate_candidate_evidence(
        _load_json(g6_root / "candidate_evidence_pre_gate.json", "P6_G6_PRE_GATE_EVIDENCE_INVALID")
    )
    actual = read_public_candidate(spec["public_candidate"]["base_url"])
    if actual["candidate_evidence"] != evidence:
        raise P6ContractError("P6_G6_PUBLIC_PRE_GATE_EVIDENCE_MISMATCH")
    readback: dict[str, Any] = {
        "schema_version": "trip-check-p6-candidate-gate-readback-v1",
        "subject_commit": spec["subject_commit"],
        "manifest_hash": manifest["manifest_hash"],
        "candidate_evidence_sha256": digest(evidence),
        "url": spec["public_candidate"]["base_url"],
        "health_route": "/health",
        "evidence_route": "/api/evidence/latest",
        "health_http_status": actual["health_http_status"],
        "evidence_http_status": actual["evidence_http_status"],
        "health_response_body_sha256": actual["health_response_body_sha256"],
        "evidence_response_body_sha256": actual["evidence_response_body_sha256"],
        "health_status": "PASS",
        "evidence_status": "PASS",
        "read_only_mount": True,
        "observed_at": _utc_now(),
    }
    readback["receipt_hash"] = digest(readback)
    readback = validate_candidate_gate_readback(readback)
    if not candidate_gate_eligible(evidence, manifest, spec, readback, repo_root):
        raise P6ContractError("P6_CANDIDATE_GATE_NOT_ELIGIBLE")
    _write_json_new(g6_root / "candidate_gate_readback.json", readback)
    return readback


def build_candidate_gate_decision(
    *,
    candidate_run_spec_path: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    evidence_root = Path(spec["evidence_root"])
    g6_root = evidence_root / "g6"
    manifest = validate_release_manifest(
        _load_json(g6_root / "release_manifest.json", "P6_G6_RELEASE_MANIFEST_INVALID")
    )
    pre_gate = validate_candidate_evidence(
        _load_json(g6_root / "candidate_evidence_pre_gate.json", "P6_G6_PRE_GATE_EVIDENCE_INVALID")
    )
    readback = validate_candidate_gate_readback(
        _load_json(g6_root / "candidate_gate_readback.json", "P6_G6_READBACK_INVALID")
    )
    if not candidate_gate_eligible(pre_gate, manifest, spec, readback, repo_root):
        raise P6ContractError("P6_CANDIDATE_GATE_NOT_ELIGIBLE")
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p6-candidate-gate-receipt-v1",
        "subject_commit": spec["subject_commit"],
        "upstream_ref": spec["upstream_ref"],
        "upstream_commit": spec["upstream_commit"],
        "dirty_tree": False,
        "run_spec_hash": spec["run_spec_hash"],
        "manifest_hash": manifest["manifest_hash"],
        "pre_gate_evidence_sha256": digest(pre_gate),
        "pre_gate_evidence_response_body_sha256": readback["evidence_response_body_sha256"],
        "pre_gate_health_response_body_sha256": readback["health_response_body_sha256"],
        "g6_readback_receipt_hash": readback["receipt_hash"],
        "decision": "PASS",
        "decided_at": _utc_now(),
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt = validate_candidate_gate_decision(receipt, pre_gate, manifest, readback)
    final_evidence = deepcopy(pre_gate)
    final_evidence["gates"]["g6"] = "PASS"
    final_evidence["candidate_gate_status"] = "PASS"
    final_evidence["candidate_gate_receipt_hash"] = receipt["receipt_hash"]
    final_evidence = validate_final_candidate_evidence(
        final_evidence,
        pre_gate,
        manifest,
        receipt,
        readback,
    )
    _write_json_new(g6_root / "candidate_gate_receipt.json", receipt)
    _write_json_new(g6_root / "candidate_evidence_final.json", final_evidence)
    return receipt, final_evidence


def capture_final_disclosure(
    *,
    candidate_run_spec_path: Path,
    repo_root: Path,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    g6_root = Path(spec["evidence_root"]) / "g6"
    manifest = _load_json(g6_root / "release_manifest.json", "P6_G6_RELEASE_MANIFEST_INVALID")
    pre_gate = _load_json(g6_root / "candidate_evidence_pre_gate.json", "P6_G6_PRE_GATE_EVIDENCE_INVALID")
    final = _load_json(g6_root / "candidate_evidence_final.json", "P6_G6_FINAL_EVIDENCE_INVALID")
    receipt = _load_json(g6_root / "candidate_gate_receipt.json", "P6_G6_GATE_RECEIPT_INVALID")
    readback = _load_json(g6_root / "candidate_gate_readback.json", "P6_G6_READBACK_INVALID")
    actual = read_public_candidate(spec["public_candidate"]["base_url"])
    if actual["candidate_evidence"] != final:
        raise P6ContractError("P6_G6_PUBLIC_FINAL_EVIDENCE_MISMATCH")
    disclosure: dict[str, Any] = {
        "schema_version": "trip-check-p6-final-disclosure-readback-v1",
        "subject_commit": spec["subject_commit"],
        "manifest_hash": manifest["manifest_hash"],
        "candidate_gate_receipt_hash": receipt["receipt_hash"],
        "final_evidence_sha256": digest(final),
        "url": spec["public_candidate"]["base_url"],
        "health_route": "/health",
        "evidence_route": "/api/evidence/latest",
        "health_http_status": actual["health_http_status"],
        "evidence_http_status": actual["evidence_http_status"],
        "health_response_body_sha256": actual["health_response_body_sha256"],
        "evidence_response_body_sha256": actual["evidence_response_body_sha256"],
        "observed_at": _utc_now(),
    }
    disclosure["receipt_hash"] = digest(disclosure)
    disclosure = validate_final_disclosure_readback(disclosure)
    if not candidate_final_disclosure_valid(
        final,
        pre_gate,
        manifest,
        spec,
        receipt,
        readback,
        disclosure,
        repo_root,
    ):
        raise P6ContractError("P6_FINAL_DISCLOSURE_INVALID")
    _write_json_new(g6_root / "final_disclosure_readback.json", disclosure)
    return disclosure
