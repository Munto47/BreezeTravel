from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = REPO_ROOT / "frontend"
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p3"
DEFAULT_BASELINE = "cd533cf81034cfdd11e9a3f3ea15d953202bb1a5"
PILOT_DATASET = BACKEND_ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
PROVIDER_SNAPSHOT = BACKEND_ROOT / "evals" / "fixtures" / "trip_check_provider_integrity_v1.json"
SECRET_PATTERNS = {
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "windows_user_profile": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    "escaped_windows_user_profile": re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\s]+", re.IGNORECASE),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8").strip()


def _portable(value: str) -> str:
    result = value
    for source, replacement in (
        (str(REPO_ROOT.resolve()), "<repo>"),
        (str(Path.home().resolve()), "<user-profile>"),
    ):
        result = result.replace(source, replacement)
        result = result.replace(source.replace("\\", "/"), replacement)
        result = result.replace(source.replace("\\", "\\\\"), replacement)
    return result


def _safe_reset_output(output: Path) -> None:
    if output.resolve() != DEFAULT_OUTPUT.resolve():
        raise ValueError(f"P3 output must be exactly {DEFAULT_OUTPUT.resolve()}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def _run(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    log_dir: Path,
    env: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        code = completed.returncode
        output = completed.stdout + completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        code = 124
        output = (exc.stdout or "") + (exc.stderr or "")
        timed_out = True
    finished = datetime.now(timezone.utc)
    log_path = log_dir / f"{name}.log"
    log_path.write_text(_portable(output).rstrip() + "\n", encoding="utf-8", newline="\n")
    return {
        "name": name,
        "status": "PASS" if code == 0 else "FAIL",
        "exit_code": code,
        "timed_out": timed_out,
        "command": ["python" if Path(item).resolve() == Path(sys.executable).resolve() else _portable(item) for item in command],
        "cwd": cwd.resolve().relative_to(REPO_ROOT.resolve()).as_posix() or ".",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "log_path": log_path.relative_to(BACKEND_ROOT).as_posix(),
        "log_sha256": _sha256(log_path),
    }


def _not_run(name: str, reason: str) -> dict[str, Any]:
    return {"name": name, "status": "NOT_RUN", "reason": reason}


def _postgres_preflight() -> tuple[bool, str]:
    raw = os.getenv("TEST_DATABASE_ADMIN_URL", "postgresql://postgres:postgres@127.0.0.1:5432/postgres")
    parsed = urlparse(raw.replace("postgresql+asyncpg://", "postgresql://"))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    try:
        with socket.create_connection((host, port), timeout=1):
            return True, f"{host}:{port}"
    except OSError:
        return False, f"PostgreSQL is unreachable at {host}:{port}"


def _real_ocr_evidence(path_value: str | None, *, subject: str) -> dict[str, Any]:
    if not path_value:
        return _not_run("real_ocr_dataset", "P3_REAL_OCR_MANIFEST is not configured")
    path = Path(path_value).resolve()
    if not path.is_file():
        return _not_run("real_ocr_dataset", "configured real OCR manifest does not exist")
    payload = json.loads(path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics", {})
    accepted = (
        payload.get("status") == "PASS"
        and payload.get("subject_commit") == subject
        and metrics.get("case_count") == 12
        and float(metrics.get("key_field_f1", 0)) >= 0.95
        and float(metrics.get("low_confidence_confirmation_recall", 0)) == 1.0
        and metrics.get("original_image_leak_hits") == 0
    )
    return {
        "name": "real_ocr_dataset",
        "status": "PASS" if accepted else "FAIL",
        "manifest_sha256": _sha256(path),
        "metrics": metrics,
        "source_path": "<external-real-ocr-manifest>",
    }


def _scan(*, baseline: str, subject: str, output: Path) -> dict[str, Any]:
    changed = subprocess.check_output(
        ["git", "diff", "--unified=0", baseline, subject, "--", "."],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    findings: list[dict[str, str]] = []
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(changed):
            findings.append({"scope": "git_diff", "pattern": label})
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append({"scope": path.relative_to(BACKEND_ROOT).as_posix(), "pattern": "binary_artifact"})
            continue
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(value):
                findings.append({"scope": path.relative_to(BACKEND_ROOT).as_posix(), "pattern": label})
    return {"status": "PASS" if not findings else "FAIL", "finding_count": len(findings), "findings": findings}


def _artifact_index(output: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(output).as_posix(), "sha256": _sha256(path), "bytes": path.stat().st_size}
        for path in sorted(item for item in output.rglob("*") if item.is_file())
        if path.name != "integrity_gate_manifest.json"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the Trip Check P3 input/provider integrity gate")
    parser.add_argument("--subject-commit", default=None)
    parser.add_argument("--baseline-commit", default=DEFAULT_BASELINE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    subject = args.subject_commit or _git("rev-parse", "HEAD")
    if _git("rev-parse", "HEAD") != subject:
        raise RuntimeError("P3 subject must be the checked-out HEAD")
    upstream = _git("rev-parse", "@{upstream}")
    if upstream != subject:
        raise RuntimeError("P3 subject must already be pushed to upstream")
    dirty = [
        line for line in _git("status", "--porcelain=v1").splitlines()
        if line and "backend/evidence/trip_check_v1/p3/" not in line.replace("\\", "/")
    ]
    if dirty:
        raise RuntimeError(f"implementation tree is not clean: {dirty}")
    output = args.output.resolve()
    _safe_reset_output(output)
    log_dir = output / "logs"
    log_dir.mkdir(parents=True)
    python = sys.executable
    npm = "npm.cmd" if os.name == "nt" else "npm"
    env = os.environ.copy()
    env.pop("RUN_SERVICE_INTEGRATION", None)

    checks = [
        _run(
            name="screenshot_controlled_contract",
            command=[python, "-m", "pytest", "tests/test_screenshot_imports.py", "-q"],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=180,
        ),
        _run(
            name="provider_snapshot_matrix",
            command=[
                python, "-m", "scripts.run_trip_check_p3_integrity", "--commit-sha", subject,
                "--output", str(output / "provider_integrity"),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=180,
        ),
        _run(
            name="browser_screenshot_flow",
            command=[npm, "run", "test:e2e:trip-check-p3"],
            cwd=FRONTEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=300,
        ),
        _run(
            name="p1_pilot_regression",
            command=[
                python, "-m", "scripts.run_trip_check_pilot", "--commit-sha", subject,
                "--output", str(output / "pilot_regression"),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=300,
        ),
        _run(
            name="backend_full_pytest",
            command=[python, "-m", "pytest", "tests/", "-q"],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=900,
        ),
        _run(
            name="backend_ruff",
            command=[python, "-m", "ruff", "check", "app", "evals", "scripts", "tests"],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=180,
        ),
        _run(
            name="frontend_build",
            command=[npm, "run", "build"],
            cwd=FRONTEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=420,
        ),
        _run(
            name="dual_entry_testset_validation",
            command=[python, "backend/scripts/validate_dual_entry_testset.py"],
            cwd=REPO_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=180,
        ),
    ]
    real_ocr = _real_ocr_evidence(os.getenv("P3_REAL_OCR_MANIFEST"), subject=subject)
    pg_ready, pg_reason = _postgres_preflight()
    if pg_ready:
        pg_env = {**env, "RUN_SERVICE_INTEGRATION": "1"}
        postgres = _run(
            name="postgres_p3_integration",
            command=[
                python, "-m", "pytest",
                "tests/test_migrations_integration.py",
                "tests/test_screenshot_postgres_integration.py",
                "tests/test_trip_check_postgres_integration.py",
                "tests/test_trip_check_reliability_runner.py",
                "-q",
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=pg_env,
            timeout=900,
        )
        p2_reliability = _run(
            name="p2_reliability_regression",
            command=[
                python, "-m", "scripts.run_trip_check_reliability", "--commit-sha", subject,
                "--output", str(output / "p2_reliability_regression"),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=pg_env,
            timeout=600,
        )
    else:
        postgres = _not_run("postgres_p3_integration", pg_reason)
        p2_reliability = _not_run("p2_reliability_regression", pg_reason)
    live = _run(
        name="live_provider_matrix",
        command=[
            python, "-m", "scripts.run_trip_check_p3_integrity", "--live", "--commit-sha", subject,
            "--output", str(output),
        ],
        cwd=BACKEND_ROOT,
        log_dir=log_dir,
        env=env,
        timeout=300,
    )
    live_manifest_path = output / "live_provider_manifest.json"
    live_manifest = json.loads(live_manifest_path.read_text("utf-8")) if live_manifest_path.exists() else None
    live_status = live_manifest.get("status") if live_manifest else "FAIL"
    snapshot_path = output / "provider_integrity" / "provider_integrity_manifest.json"
    snapshot = json.loads(snapshot_path.read_text("utf-8")) if snapshot_path.exists() else None
    browser_path = output / "browser-playwright.json"
    if browser_path.exists():
        browser_path.write_text(_portable(browser_path.read_text("utf-8")), encoding="utf-8", newline="\n")
    browser = json.loads(browser_path.read_text("utf-8")) if browser_path.exists() else None
    pilot_path = output / "pilot_regression" / "pilot_manifest.json"
    pilot = json.loads(pilot_path.read_text("utf-8")) if pilot_path.exists() else None
    p2_path = output / "p2_reliability_regression" / "reliability_manifest.json"
    p2_manifest = json.loads(p2_path.read_text("utf-8")) if p2_path.exists() else None
    check_by_name = {item["name"]: item for item in checks}
    g1 = (
        "PASS" if check_by_name["screenshot_controlled_contract"]["status"] == "PASS" and real_ocr["status"] == "PASS"
        else "NOT_RUN" if check_by_name["screenshot_controlled_contract"]["status"] == "PASS" and real_ocr["status"] == "NOT_RUN"
        else "FAIL"
    )
    g2 = (
        "PASS" if postgres["status"] == "PASS" and p2_reliability["status"] == "PASS"
        else "NOT_RUN" if "NOT_RUN" in {postgres["status"], p2_reliability["status"]}
        else "FAIL"
    )
    g3 = (
        "PASS"
        if check_by_name["provider_snapshot_matrix"]["status"] == "PASS"
        and snapshot is not None
        and snapshot.get("status") == "PASS"
        and snapshot.get("canonical_cases_passed") == 6
        and snapshot.get("network_call_count") == 0
        else "FAIL"
    )
    g4 = live_status if live["status"] == "PASS" else "FAIL"
    browser_stats = browser.get("stats", {}) if browser else {}
    pilot_metrics = pilot.get("metrics", {}) if pilot else {}
    contracts = {
        "browser_cases_2_of_2": browser_stats.get("expected") == 2 and browser_stats.get("unexpected") == 0,
        "p1_pilot_18_of_18": pilot_metrics.get("case_count") == 18 and pilot_metrics.get("passed_count") == 18,
        "p1_city_distribution": pilot_metrics.get("city_counts") == {"北京": 6, "上海": 6, "杭州": 6},
        "p2_reliability_6_of_6": (
            p2_manifest is not None
            and p2_manifest.get("status") == "PASS"
            and p2_manifest.get("canonical_cases_passed") == 6
        ),
        "snapshot_six_of_six": g3 == "PASS",
        "live_receipts_18": live_status == "PASS" and live_manifest.get("actual_receipt_count") == 18,
    }
    sensitive_scan = _scan(baseline=args.baseline_commit, subject=subject, output=output)
    _write_json(output / "sensitive_scan.json", sensitive_scan)
    required_checks_pass = all(item["status"] == "PASS" for item in checks)
    status = (
        "PASS"
        if required_checks_pass
        and all(value == "PASS" for value in (g1, g2, g3, g4))
        and all(contracts.values())
        and sensitive_scan["status"] == "PASS"
        else "REJECT"
    )
    manifest = {
        "schema_version": "trip-check-p3-integrity-gate-manifest-v1",
        "goal_id": "TC-P3-G01-input-provider-integrity",
        "subject_commit": subject,
        "upstream_commit_at_start": upstream,
        "baseline_commit": args.baseline_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "goal_status": "COMPLETE" if status == "PASS" else "IN_PROGRESS",
        "gates": {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": "NOT_RUN", "G6": "NOT_RUN"},
        "proof_classes": {
            "controlled_fixture": "PASS" if required_checks_pass else "FAIL",
            "real_ocr_dataset": real_ocr["status"],
            "postgresql_integration": postgres["status"],
            "provider_snapshot": g3,
            "live_provider": g4,
            "controlled_browser_fixture": check_by_name["browser_screenshot_flow"]["status"],
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
        "scope": {"cities": list(("北京", "上海", "杭州")), "traveler_count": "2-5", "days": "2-5"},
        "input": {"real_ocr": real_ocr, "original_image_artifacts_in_git": 0},
        "postgresql": postgres,
        "p2_reliability_regression": {
            "status": p2_reliability["status"],
            "manifest_path": p2_path.relative_to(BACKEND_ROOT).as_posix(),
            "manifest_sha256": _sha256(p2_path) if p2_path.exists() else None,
        },
        "provider": {
            "snapshot_manifest_path": snapshot_path.relative_to(BACKEND_ROOT).as_posix(),
            "snapshot_manifest_sha256": _sha256(snapshot_path) if snapshot_path.exists() else None,
            "snapshot_sha256": _sha256(PROVIDER_SNAPSHOT),
            "live_manifest_path": live_manifest_path.relative_to(BACKEND_ROOT).as_posix(),
            "live_manifest_sha256": _sha256(live_manifest_path) if live_manifest_path.exists() else None,
            "live_query_budget": 18,
        },
        "p1_pilot_regression": {
            "dataset_sha256": _sha256(PILOT_DATASET),
            "manifest_path": pilot_path.relative_to(BACKEND_ROOT).as_posix(),
            "manifest_sha256": _sha256(pilot_path) if pilot_path.exists() else None,
            "metrics": pilot_metrics,
        },
        "browser": {
            "report_path": browser_path.relative_to(BACKEND_ROOT).as_posix(),
            "report_sha256": _sha256(browser_path) if browser_path.exists() else None,
            "stats": browser_stats,
        },
        "contracts": contracts,
        "verification_checks": checks + [postgres, p2_reliability, live],
        "sensitive_scan": sensitive_scan,
        "blockers": [
            item for item, value in {
                "TRIP_CHECK_V1_P3_REAL_OCR_NOT_PASSED": g1,
                "TRIP_CHECK_V1_P3_POSTGRES_NOT_PASSED": g2,
                "TRIP_CHECK_V1_P3_SNAPSHOT_NOT_PASSED": g3,
                "TRIP_CHECK_V1_P3_LIVE_PROVIDER_NOT_PASSED": g4,
            }.items() if value != "PASS"
        ],
        "non_claims": [
            "Controlled OCR mocks are not real PaddleOCR accuracy evidence.",
            "Snapshot Provider facts are not live Provider receipts.",
            "Controlled browser fixtures are not public E2E or human evidence.",
            "G5, public deployment and human evaluation were not run.",
        ],
        "artifact_index": _artifact_index(output),
    }
    manifest_path = output / "integrity_gate_manifest.json"
    _write_json(manifest_path, manifest)
    if json.loads(manifest_path.read_text("utf-8")) != manifest:
        raise RuntimeError("P3 integrity manifest readback mismatch")
    print(json.dumps({"status": status, "gates": manifest["gates"], "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
