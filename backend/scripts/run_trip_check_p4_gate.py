"""Execute the P4 Advice/CandidateSet/Repair phase gate."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.run_trip_check_p3_integrity_gate import (
    _artifact_index,
    _ensure_postgres,
    _git,
    _not_run,
    _run,
    _scan,
    _sha256,
    _stop_gate_postgres,
    _write_json,
)
from app.itineraries.hash_service import sha256_canonical


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = REPO_ROOT / "frontend"
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p4"
DEFAULT_BASELINE = "3ea92a4dcf58d029ddd06d115fe682ed6b986524"
PILOT_DATASET = BACKEND_ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
P4_DATA_MANIFEST = BACKEND_ROOT / "evals" / "trip_check_v1" / "p4" / "dataset_v1.manifest.json"
P4_BAKEOFF = BACKEND_ROOT / "evals" / "trip_check_v1" / "p4" / "solver_bakeoff_v1.jsonl"


def _safe_reset(output: Path) -> None:
    resolved = output.resolve()
    if resolved != DEFAULT_OUTPUT.resolve():
        raise ValueError(f"P4 output must be exactly {DEFAULT_OUTPUT.resolve()}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def evaluate_p4_phase(
    *,
    required_checks: list[dict[str, Any]],
    postgres_status: str,
    p2_status: str,
    contracts: dict[str, bool],
    sensitive_scan_status: str,
) -> str:
    return (
        "PASS"
        if all(item["status"] == "PASS" for item in required_checks)
        and postgres_status == "PASS"
        and p2_status == "PASS"
        and all(contracts.values())
        and sensitive_scan_status == "PASS"
        else "REJECT"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the Trip Check P4 phase gate")
    parser.add_argument("--subject-commit", default=None)
    parser.add_argument("--baseline-commit", default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--postgres-mode", choices=("auto", "external", "skip"), default="auto")
    args = parser.parse_args()
    subject = args.subject_commit or _git("rev-parse", "HEAD")
    if _git("rev-parse", "HEAD") != subject:
        raise RuntimeError("P4 subject must be the checked-out HEAD")
    if _git("status", "--porcelain"):
        raise RuntimeError("P4 gate requires a clean worktree")
    upstream = _git("rev-parse", "@{upstream}")
    if upstream != subject:
        raise RuntimeError("P4 subject must already be pushed to its upstream")
    _safe_reset(args.output)
    log_dir = args.output / "logs"
    log_dir.mkdir(parents=True)
    python = str(Path(sys.executable).resolve())
    npm = "npm.cmd" if os.name == "nt" else "npm"
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    checks = [
        _run(
            name="p4_targeted_contracts",
            command=[
                python, "-m", "pytest",
                "tests/test_trip_check_p4_advice_candidate_contract.py",
                "tests/test_repairs.py",
                "tests/test_repairs_api.py",
                "tests/test_repair_concurrency.py",
                "tests/test_repair_route_objective.py",
                "tests/test_trip_check_executor.py",
                "-q",
            ],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=300,
        ),
        _run(
            name="p4_dataset_contract",
            command=[python, "-m", "scripts.validate_trip_check_p4_datasets"],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=120,
        ),
        _run(
            name="p4_bakeoff_contract",
            command=[python, "-m", "scripts.validate_trip_check_p4_bakeoff"],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=120,
        ),
        _run(
            name="p4_solver_bakeoff",
            command=[
                python, "-m", "scripts.run_trip_check_p4_solver_bakeoff",
                "--commit-sha", subject,
                "--output", str(args.output / "solver_bakeoff"),
            ],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=300,
        ),
        _run(
            name="p4_browser_repair_flow",
            command=[npm, "run", "test:e2e:trip-check-p4"],
            cwd=FRONTEND_ROOT, log_dir=log_dir, env=env, timeout=360,
        ),
        _run(
            name="p1_pilot_regression",
            command=[
                python, "-m", "scripts.run_trip_check_pilot",
                "--commit-sha", subject,
                "--output", str(args.output / "pilot_regression"),
            ],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=300,
        ),
        _run(
            name="p3_snapshot_regression",
            command=[
                python, "-m", "scripts.run_trip_check_p3_integrity",
                "--commit-sha", subject,
                "--output", str(args.output / "p3_snapshot_regression"),
            ],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=300,
        ),
        _run(
            name="backend_full_pytest",
            command=[python, "-m", "pytest", "tests/", "-q"],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=1200,
        ),
        _run(
            name="backend_ruff",
            command=[python, "-m", "ruff", "check", "app", "evals", "scripts", "tests"],
            cwd=BACKEND_ROOT, log_dir=log_dir, env=env, timeout=240,
        ),
        _run(
            name="frontend_build",
            command=[npm, "run", "build"],
            cwd=FRONTEND_ROOT, log_dir=log_dir, env=env, timeout=480,
        ),
        _run(
            name="dual_entry_testset_validation",
            command=[python, "backend/scripts/validate_dual_entry_testset.py"],
            cwd=REPO_ROOT, log_dir=log_dir, env=env, timeout=180,
        ),
    ]

    postgres_service = _ensure_postgres(args.postgres_mode)
    if postgres_service["status"] == "READY":
        pg_env = {**env, "RUN_SERVICE_INTEGRATION": "1"}
        try:
            postgres = _run(
                name="postgres_p4_repair_integration",
                command=[
                    python, "-m", "pytest",
                    "tests/test_migrations_integration.py",
                    "tests/test_trip_check_postgres_integration.py",
                    "tests/test_repair_concurrency_postgres.py",
                    "tests/test_workspace_resume_postgres.py",
                    "-q",
                ],
                cwd=BACKEND_ROOT, log_dir=log_dir, env=pg_env, timeout=1200,
            )
            p2 = _run(
                name="p2_reliability_regression",
                command=[
                    python, "-m", "scripts.run_trip_check_reliability",
                    "--commit-sha", subject,
                    "--output", str(args.output / "p2_reliability_regression"),
                ],
                cwd=BACKEND_ROOT, log_dir=log_dir, env=pg_env, timeout=600,
            )
        finally:
            if postgres_service["started_by_gate"]:
                postgres_service["stop_receipt"] = _stop_gate_postgres()
    else:
        reason = postgres_service.get("reason", "PostgreSQL unavailable")
        postgres = _not_run("postgres_p4_repair_integration", reason)
        p2 = _not_run("p2_reliability_regression", reason)

    solver_path = args.output / "solver_bakeoff" / "solver_bakeoff_manifest.json"
    solver = json.loads(solver_path.read_text(encoding="utf-8")) if solver_path.exists() else {}
    data_manifest = json.loads(P4_DATA_MANIFEST.read_text(encoding="utf-8"))
    pilot_path = args.output / "pilot_regression" / "pilot_manifest.json"
    pilot = json.loads(pilot_path.read_text(encoding="utf-8")) if pilot_path.exists() else {}
    snapshot_path = args.output / "p3_snapshot_regression" / "provider_integrity_manifest.json"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
    browser_path = args.output / "browser-playwright.json"
    browser = json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.exists() else {}
    check_by_name = {item["name"]: item for item in checks}
    admission = solver.get("admission", {})
    metrics = solver.get("metrics", {})
    contracts = {
        "advice_candidate_repair_contracts": check_by_name["p4_targeted_contracts"]["status"] == "PASS",
        "dataset_18_180_72_0": (
            data_manifest["splits"]["pilot"]["count"] == 18
            and data_manifest["splits"]["dev"]["count"] == 180
            and data_manifest["splits"]["regression"]["count"] == 72
            and data_manifest["splits"]["frozen_blind"]["count"] == 0
        ),
        "bakeoff_36_same_run_spec": solver.get("case_count") == 36 and solver.get("record_count") == 108,
        "solver_safety_postcheck_replay": (
            admission.get("safety_pass") is True
            and admission.get("authoritative_postcheck_pass") is True
            and admission.get("replay_pass") is True
        ),
        "solver_performance": admission.get("performance_pass") is True,
        "failed_solver_not_promoted": (
            admission.get("status") == "PASS"
            or (
                admission.get("status") == "REJECT"
                and admission.get("default_strategy") == "bounded_repair_v1"
            )
        ),
        "pilot_18": pilot.get("metrics", {}).get("passed_count") == 18,
        "p3_snapshot_6": snapshot.get("canonical_cases_passed") == 6,
        "browser_three_cities_plus_ambiguity": browser.get("stats", {}).get("expected") == 4,
        "browser_no_unexpected": browser.get("stats", {}).get("unexpected") == 0,
    }
    sensitive_scan = _scan(baseline=args.baseline_commit, subject=subject, output=args.output)
    _write_json(args.output / "sensitive_scan.json", sensitive_scan)
    phase_status = evaluate_p4_phase(
        required_checks=checks,
        postgres_status=postgres["status"],
        p2_status=p2["status"],
        contracts=contracts,
        sensitive_scan_status=sensitive_scan["status"],
    )
    manifest: dict[str, Any] = {
        "schema_version": "trip-check-p4-gate-manifest-v1",
        "goal_id": "TC-P4-G01-advice-candidate-repair",
        "subject_commit": subject,
        "upstream_commit_at_start": upstream,
        "baseline_commit": args.baseline_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": phase_status,
        "p4_phase_status": phase_status,
        "goal_status": "COMPLETE" if phase_status == "PASS" else "IN_PROGRESS",
        "candidate_readiness_status": "REJECT",
        "solver_admission": admission,
        "default_runtime_strategy": admission.get("default_strategy", "bounded_repair_v1"),
        "solver_metrics": metrics,
        "contracts": contracts,
        "proof_classes": {
            "controlled_fixture": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
            "postgresql_integration": postgres["status"],
            "controlled_browser_fixture": check_by_name["p4_browser_repair_flow"]["status"],
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
            "frozen_blind": "NOT_RUN",
        },
        "dataset": {
            "pilot_sha256": _sha256(PILOT_DATASET),
            "p4_manifest_sha256": _sha256(P4_DATA_MANIFEST),
            "bakeoff_sha256": _sha256(P4_BAKEOFF),
        },
        "postgresql": {"service": postgres_service, "verification": postgres},
        "p2_reliability": p2,
        "verification_checks": [*checks, postgres, p2],
        "sensitive_scan": sensitive_scan,
        "blockers": [
            key for key, passed in contracts.items() if not passed
        ] + ([] if postgres["status"] == "PASS" else ["POSTGRES_P4_NOT_PASS"]),
        "candidate_evidence_debt": [
            "G1_REAL_OCR_NOT_RUN",
            "G4_LIVE_PROVIDER_NOT_RUN",
            "G5_PUBLIC_BROWSER_PERFORMANCE_NOT_RUN",
            "G6_RELEASE_MANIFEST_NOT_RUN",
            "HUMAN_EVIDENCE_NOT_RUN",
        ],
        "non_claims": [
            "Controlled P4 fixtures are not public or human evidence.",
            "A rejected CP-SAT admission is a valid experiment decision, not solver success.",
            "P4 phase PASS does not make the candidate release-ready.",
        ],
    }
    manifest["artifact_index"] = _artifact_index(args.output)
    manifest["manifest_hash"] = sha256_canonical({
        key: value for key, value in manifest.items() if key != "manifest_hash"
    })
    _write_json(args.output / "p4_gate_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if phase_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
