"""P5 Evaluation Gate manifest builder and artifact readback checks."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.data_contract import digest


class P5GateError(RuntimeError):
    pass


_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:api[_-]?key|access[_-]?token|secret)\s*[:=]\s*['\"]?[A-Za-z0-9_/-]{20,}", re.I),
)


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5GateError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise P5GateError(f"{label} must be an object")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_hashed_object(value: dict[str, Any], hash_field: str, label: str) -> None:
    claimed = value.get(hash_field)
    body = {key: item for key, item in value.items() if key != hash_field}
    if claimed != digest(body):
        raise P5GateError(f"{label} hash mismatch")


def _repo_state(repo_root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P5GateError("repository state unavailable") from exc
    return head, dirty


def _artifact(logical_name: str, path: Path, repo_root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(repo_root.resolve()).as_posix()
        storage = "repository"
    except ValueError:
        relative = resolved.name
        storage = "external"
    return {
        "logical_name": logical_name,
        "storage": storage,
        "path": relative,
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _validate_run_manifest(
    *,
    run_dir: Path,
    lane: str,
    case_count: int,
    terminal_count: int,
) -> dict[str, Any]:
    manifest_path = run_dir / "run_group_manifest.json"
    manifest = _load_json(manifest_path, f"{lane} run manifest")
    _validate_hashed_object(manifest, "manifest_hash", f"{lane} run manifest")
    if (
        manifest.get("status") != "PASS"
        or manifest.get("formal_evidence") is not True
        or manifest.get("executable_evidence_status") != "PASS"
        or manifest.get("lane") != lane
        or manifest.get("dirty_tree") is not False
        or manifest.get("case_count") != case_count
        or manifest.get("variant_count") != 3
        or manifest.get("terminal_count") != terminal_count
        or manifest.get("expected_terminal_count") != terminal_count
        or manifest.get("replay_executed") is not True
        or manifest.get("replay_match_count") != terminal_count
        or manifest.get("replay_mismatches") != []
        or manifest.get("external_api_calls") != 0
        or manifest.get("human_evidence") is not False
    ):
        raise P5GateError(f"{lane} run contract rejected")
    terminal_path = run_dir / str(manifest["terminal_outputs_path"])
    if _sha256(terminal_path) != manifest["terminal_outputs_file_sha256"]:
        raise P5GateError(f"{lane} terminal file hash mismatch")
    return manifest


def _secret_scan(values: list[dict[str, Any]]) -> dict[str, Any]:
    matches = []
    for index, value in enumerate(values):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        for pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                matches.append({"artifact_index": index, "pattern": pattern.pattern})
    return {"status": "PASS" if not matches else "REJECT", "match_count": len(matches)}


def build_p5_gate_manifest(
    *,
    repo_root: Path,
    nonblind_run_dir: Path,
    nonblind_score_path: Path,
    blind_run_dir: Path,
    blind_score_path: Path,
    judge_panel_path: Path,
    output_path: Path,
    require_current_subject: bool = True,
) -> dict[str, Any]:
    root = repo_root.resolve()
    p5_root = root / "backend" / "evals" / "trip_check_v1" / "p5"
    dataset_path = p5_root / "dataset_v1.manifest.json"
    p4_gate_path = root / "backend" / "evidence" / "trip_check_v1" / "p4" / "p4_gate_manifest.json"
    dataset = _load_json(dataset_path, "P5 dataset manifest")
    _validate_hashed_object(dataset, "manifest_hash", "P5 dataset manifest")
    if (
        dataset.get("counts", {}).get("total") != 360
        or dataset.get("counts", {}).get("by_split")
        != {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
        or dataset.get("counts", {}).get("by_city")
        != {"北京": 120, "上海": 120, "杭州": 120}
    ):
        raise P5GateError("P5 dataset count contract rejected")
    nonblind_run = _validate_run_manifest(
        run_dir=nonblind_run_dir.resolve(),
        lane="nonblind",
        case_count=270,
        terminal_count=810,
    )
    blind_run = _validate_run_manifest(
        run_dir=blind_run_dir.resolve(),
        lane="frozen_blind",
        case_count=90,
        terminal_count=270,
    )
    if nonblind_run["subject_commit"] != blind_run["subject_commit"]:
        raise P5GateError("run-group subject commits differ")
    subject_commit = nonblind_run["subject_commit"]
    if require_current_subject:
        head, dirty = _repo_state(root)
        if dirty or head != subject_commit:
            raise P5GateError("current repository does not match the clean P5 subject")
    nonblind_score = _load_json(nonblind_score_path.resolve(), "non-blind score")
    _validate_hashed_object(nonblind_score, "report_hash", "non-blind score")
    blind_score = _load_json(blind_score_path.resolve(), "blind score")
    judge_panel = _load_json(judge_panel_path.resolve(), "Judge panel")
    if (
        nonblind_score.get("subject_commit") != subject_commit
        or nonblind_score.get("run_group_manifest_hash") != nonblind_run["manifest_hash"]
        or nonblind_score.get("case_count") != 270
        or nonblind_score.get("terminal_count") != 810
    ):
        raise P5GateError("non-blind score binding rejected")
    blind_bindings = blind_score.get("bindings", {})
    if (
        blind_bindings.get("subject_commit") != subject_commit
        or blind_bindings.get("run_group_manifest_hash") != blind_run["manifest_hash"]
        or blind_score.get("case_count") != 90
        or blind_score.get("terminal_count") != 270
        or blind_score.get("human_evidence") is not False
    ):
        raise P5GateError("blind score binding rejected")
    if (
        judge_panel.get("run_group_manifest_hash") != blind_run["manifest_hash"]
        or judge_panel.get("round_count") != 3
        or judge_panel.get("candidate_count") != 270
        or judge_panel.get("human_calibration_performed") is not False
        or judge_panel.get("judge_may_override_deterministic_failure") is not False
    ):
        raise P5GateError("Judge panel binding rejected")
    p4_gate = _load_json(p4_gate_path, "P4 gate")
    p4_solver = p4_gate.get("solver_admission", {})
    if (
        p4_gate.get("status") != "PASS"
        or p4_gate.get("p4_phase_status") != "PASS"
        or p4_solver.get("status") != "REJECT"
        or p4_solver.get("default_strategy") != "bounded_repair_v1"
    ):
        raise P5GateError("P4 solver admission inheritance rejected")
    security_scan = _secret_scan(
        [dataset, nonblind_run, blind_run, nonblind_score, blind_score, judge_panel]
    )
    checks = {
        "dataset_contract": True,
        "exact_1080_terminal_outputs": (
            nonblind_run["terminal_count"] + blind_run["terminal_count"] == 1080
        ),
        "nonblind_deterministic_gate": nonblind_score.get("status") == "PASS",
        "blind_deterministic_gate": blind_score.get("status") == "PASS",
        "judge_semantic_gate": judge_panel.get("status") == "PASS",
        "p4_phase_pass": True,
        "cp_sat_admission_remains_reject": True,
        "secret_scan": security_scan["status"] == "PASS",
        "same_subject_commit": True,
    }
    gate_passed = all(checks.values())
    promotion_decision = "KEEP_CORE_B" if gate_passed else "REJECT_ALL_CANDIDATES"
    artifacts = [
        _artifact("dataset_manifest", dataset_path, root),
        _artifact("p4_gate_manifest", p4_gate_path, root),
        _artifact("nonblind_run_manifest", nonblind_run_dir / "run_group_manifest.json", root),
        _artifact("nonblind_terminal_outputs", nonblind_run_dir / nonblind_run["terminal_outputs_path"], root),
        _artifact("nonblind_score", nonblind_score_path, root),
        _artifact("blind_run_manifest", blind_run_dir / "run_group_manifest.json", root),
        _artifact("blind_terminal_outputs", blind_run_dir / blind_run["terminal_outputs_path"], root),
        _artifact("blind_aggregate_score", blind_score_path, root),
        _artifact("judge_panel", judge_panel_path, root),
    ]
    manifest = {
        "schema_version": "trip-check-p5-gate-manifest-v1",
        "goal_id": "TC-P5-G01-evaluation-ablation",
        "status": "PASS" if gate_passed else "REJECT",
        "subject_commit": subject_commit,
        "dirty_tree": False,
        "dataset_manifest_hash": dataset["manifest_hash"],
        "counts": {
            "cases": 360,
            "nonblind_cases": 270,
            "blind_cases": 90,
            "variants": 3,
            "nonblind_terminal_outputs": 810,
            "blind_terminal_outputs": 270,
            "total_terminal_outputs": 1080,
        },
        "checks": checks,
        "promotion_decision": promotion_decision,
        "default_runtime_strategy": "bounded_repair_v1",
        "solver_admission": {
            "inherited_from_p4_subject_commit": p4_gate["subject_commit"],
            "status": "REJECT",
            "may_be_overridden_by_p5_score": False,
        },
        "evidence_boundaries": {
            "controlled_fixture": "EVALUATED" if gate_passed else "REJECT",
            "automated_proxy_judge": judge_panel.get("status", "BLOCKED"),
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
            "release": "NOT_RUN",
        },
        "secret_scan": security_scan,
        "artifact_index": artifacts,
    }
    manifest["manifest_hash"] = digest(manifest)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output_path.write_text(
        output_payload,
        encoding="utf-8",
        newline="\n",
    )
    readback = _load_json(output_path, "written P5 gate manifest")
    if readback != manifest or output_path.read_text(encoding="utf-8") != output_payload:
        raise P5GateError("P5 gate manifest readback failed")
    return manifest
