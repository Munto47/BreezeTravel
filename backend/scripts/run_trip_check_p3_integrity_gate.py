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
import time
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
    "private_key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{64,}\s+-----END [A-Z ]*PRIVATE KEY-----"
    ),
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


def _normalized_log(value: str) -> str:
    return "\n".join(line.rstrip() for line in _portable(value).splitlines()).rstrip() + "\n"


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
    log_path.write_text(_normalized_log(output), encoding="utf-8", newline="\n")
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


def _ensure_postgres(mode: str, *, timeout_seconds: int = 60) -> dict[str, Any]:
    ready, reason = _postgres_preflight()
    if ready:
        return {
            "status": "READY",
            "mode": mode,
            "endpoint": reason,
            "started_by_gate": False,
        }
    if mode == "skip":
        return {
            "status": "NOT_RUN",
            "mode": mode,
            "reason": "PostgreSQL startup was explicitly skipped",
            "started_by_gate": False,
        }
    if mode == "external":
        return {
            "status": "UNAVAILABLE",
            "mode": mode,
            "reason": reason,
            "started_by_gate": False,
        }
    try:
        completed = subprocess.run(
            ["docker", "compose", "up", "-d", "postgres"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "UNAVAILABLE",
            "mode": mode,
            "reason": _portable(str(exc)),
            "started_by_gate": False,
        }
    if completed.returncode != 0:
        return {
            "status": "UNAVAILABLE",
            "mode": mode,
            "reason": _normalized_log(completed.stdout + completed.stderr).strip(),
            "started_by_gate": False,
        }
    deadline = time.monotonic() + timeout_seconds
    last_reason = reason
    while time.monotonic() < deadline:
        ready, last_reason = _postgres_preflight()
        if ready:
            return {
                "status": "READY",
                "mode": mode,
                "endpoint": last_reason,
                "started_by_gate": True,
            }
        time.sleep(1)
    return {
        "status": "UNAVAILABLE",
        "mode": mode,
        "reason": f"PostgreSQL did not become ready: {last_reason}",
        "started_by_gate": True,
    }


def _stop_gate_postgres() -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["docker", "compose", "stop", "postgres"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"status": "FAIL", "reason": _portable(str(exc))}
    return {
        "status": "PASS" if completed.returncode == 0 else "FAIL",
        "exit_code": completed.returncode,
        "output": _normalized_log(completed.stdout + completed.stderr).strip(),
    }


def _evaluate_phase_and_candidate(
    *,
    required_checks_pass: bool,
    synthetic_phase_gate: str,
    g2: str,
    g3: str,
    candidate_gates: dict[str, str],
    phase_contracts: dict[str, bool],
    candidate_contracts: dict[str, bool],
    sensitive_scan_status: str,
) -> tuple[str, str]:
    phase_status = (
        "PASS"
        if required_checks_pass
        and all(value == "PASS" for value in (synthetic_phase_gate, g2, g3))
        and all(phase_contracts.values())
        and sensitive_scan_status == "PASS"
        else "REJECT"
    )
    candidate_status = (
        "PASS"
        if phase_status == "PASS"
        and all(value == "PASS" for value in candidate_gates.values())
        and all(candidate_contracts.values())
        else "REJECT"
    )
    return phase_status, candidate_status


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


def _synthetic_ocr_evidence(path: Path, *, subject: str) -> dict[str, Any]:
    if not path.is_file():
        return {"name": "synthetic_ocr_stress", "status": "FAIL", "reason": "manifest missing"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    metrics = payload.get("metrics", {})
    cleanup = payload.get("cleanup_receipt", {})
    accepted = (
        payload.get("schema_version") == "trip-check-p3-synthetic-ocr-manifest-v2"
        and payload.get("evidence_class") == "synthetic_stress"
        and payload.get("status") == "PASS"
        and payload.get("subject_commit") == subject
        and metrics.get("case_count") == 12
        and float(metrics.get("key_field_f1", 0)) >= 0.95
        and float(metrics.get("low_confidence_confirmation_recall", 0)) == 1.0
        and metrics.get("original_image_leak_hits") == 0
        and cleanup.get("status") == "DELETED"
        and cleanup.get("run_dir_removed") is True
        and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("spec_sha256") or "")) is not None
        and re.fullmatch(r"[0-9a-f]{64}", str(payload.get("render_set_sha256") or "")) is not None
    )
    try:
        manifest_path = path.relative_to(BACKEND_ROOT).as_posix()
    except ValueError:
        manifest_path = "<external-synthetic-ocr-manifest>"
    return {
        "name": "synthetic_ocr_stress",
        "status": "PASS" if accepted else "FAIL",
        "manifest_sha256": _sha256(path),
        "manifest_path": manifest_path,
        "metrics": metrics,
        "cleanup_receipt": cleanup,
        "spec_sha256": payload.get("spec_sha256"),
        "render_set_sha256": payload.get("render_set_sha256"),
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
    parser.add_argument("--ocr-python", type=Path, default=Path(sys.executable))
    parser.add_argument("--synthetic-ocr-visual-review-approved", action="store_true")
    parser.add_argument("--postgres-mode", choices=("auto", "external", "skip"), default="auto")
    parser.add_argument("--allow-live-provider", action="store_true")
    parser.add_argument("--live-env-file", type=Path, default=None)
    args = parser.parse_args()
    if args.allow_live_provider and (args.live_env_file is None or not args.live_env_file.resolve().is_file()):
        parser.error("--allow-live-provider requires an existing --live-env-file")
    if not args.ocr_python.resolve().is_file():
        parser.error("--ocr-python must point to an existing interpreter")
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
    ocr_env = {
        **env,
        "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
        "PADDLE_PDX_MODEL_SOURCE": "BOS",
    }
    synthetic_path = output / "synthetic_ocr_manifest.json"
    synthetic_command = [
        str(args.ocr_python.resolve()),
        "-m",
        "scripts.run_trip_check_p3_synthetic_ocr",
        "--subject-commit",
        subject,
        "--output",
        str(synthetic_path),
    ]
    if args.synthetic_ocr_visual_review_approved:
        synthetic_command.append("--visual-review-approved")

    checks = [
        _run(
            name="synthetic_ocr_stress",
            command=synthetic_command,
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=ocr_env,
            timeout=900,
        ),
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
    synthetic_ocr = _synthetic_ocr_evidence(synthetic_path, subject=subject)
    real_ocr = _not_run(
        "real_ocr_dataset",
        "candidate G1 real OCR evidence remains required before P6 and was not run for the P3 phase gate",
    )
    postgres_service = _ensure_postgres(args.postgres_mode)
    if postgres_service["status"] != "READY" and postgres_service["started_by_gate"]:
        postgres_service["stop_receipt"] = _stop_gate_postgres()
    if postgres_service["status"] == "READY":
        pg_env = {**env, "RUN_SERVICE_INTEGRATION": "1"}
        try:
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
        finally:
            if postgres_service["started_by_gate"]:
                postgres_service["stop_receipt"] = _stop_gate_postgres()
    else:
        pg_reason = postgres_service.get("reason", "PostgreSQL was not started")
        postgres = _not_run("postgres_p3_integration", pg_reason)
        p2_reliability = _not_run("p2_reliability_regression", pg_reason)
    if args.allow_live_provider:
        live = _run(
            name="live_provider_matrix",
            command=[
                python,
                "-m",
                "scripts.run_trip_check_p3_integrity",
                "--live",
                "--allow-live-provider",
                "--live-env-file",
                str(args.live_env_file.resolve()),
                "--max-live-calls",
                "18",
                "--commit-sha",
                subject,
                "--output",
                str(output),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=env,
            timeout=300,
        )
    else:
        live = _not_run("live_provider_matrix", "explicit --allow-live-provider was not supplied")
    live_manifest_path = output / "live_provider_manifest.json"
    live_manifest = json.loads(live_manifest_path.read_text("utf-8")) if live_manifest_path.exists() else None
    live_status = live_manifest.get("status") if live_manifest else live["status"]
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
    synthetic_phase_gate = (
        "PASS"
        if check_by_name["synthetic_ocr_stress"]["status"] == "PASS" and synthetic_ocr["status"] == "PASS"
        else "FAIL"
    )
    g1 = "NOT_RUN"
    postgres_cleanup_ok = (
        not postgres_service["started_by_gate"]
        or postgres_service.get("stop_receipt", {}).get("status") == "PASS"
    )
    g2 = (
        "PASS" if postgres["status"] == "PASS" and p2_reliability["status"] == "PASS" and postgres_cleanup_ok
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
    g4 = live_status if live_status in {"PASS", "NOT_RUN"} else "FAIL"
    browser_stats = browser.get("stats", {}) if browser else {}
    pilot_metrics = pilot.get("metrics", {}) if pilot else {}
    phase_contracts = {
        "browser_cases_2_of_2": browser_stats.get("expected") == 2 and browser_stats.get("unexpected") == 0,
        "p1_pilot_18_of_18": pilot_metrics.get("case_count") == 18 and pilot_metrics.get("passed_count") == 18,
        "p1_city_distribution": pilot_metrics.get("city_counts") == {"北京": 6, "上海": 6, "杭州": 6},
        "p2_reliability_6_of_6": (
            p2_manifest is not None
            and p2_manifest.get("status") == "PASS"
            and p2_manifest.get("canonical_cases_passed") == 6
        ),
        "snapshot_six_of_six": g3 == "PASS",
    }
    candidate_contracts = {
        "live_receipts_18": (
            live_status == "PASS"
            and live_manifest is not None
            and live_manifest.get("actual_receipt_count") == 18
            and live_manifest.get("actual_network_call_count") == 18
            and live_manifest.get("hidden_retry_count") == 0
        ),
    }
    sensitive_scan = _scan(baseline=args.baseline_commit, subject=subject, output=output)
    _write_json(output / "sensitive_scan.json", sensitive_scan)
    required_checks_pass = all(item["status"] == "PASS" for item in checks)
    candidate_gates = {
        "G1_REAL_OCR_CANDIDATE_GATE": g1,
        "G4_LIVE_PROVIDER": g4,
        "G5_PUBLIC_BROWSER_PERFORMANCE": "NOT_RUN",
        "G6_RELEASE_MANIFEST": "NOT_RUN",
    }
    phase_status, candidate_status = _evaluate_phase_and_candidate(
        required_checks_pass=required_checks_pass,
        synthetic_phase_gate=synthetic_phase_gate,
        g2=g2,
        g3=g3,
        candidate_gates=candidate_gates,
        phase_contracts=phase_contracts,
        candidate_contracts=candidate_contracts,
        sensitive_scan_status=sensitive_scan["status"],
    )
    manifest = {
        "schema_version": "trip-check-p3-integrity-gate-manifest-v2",
        "goal_id": "TC-P3-G01-input-provider-integrity",
        "subject_commit": subject,
        "upstream_commit_at_start": upstream,
        "baseline_commit": args.baseline_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": phase_status,
        "p3_phase_status": phase_status,
        "candidate_readiness_status": candidate_status,
        "goal_status": "COMPLETE" if phase_status == "PASS" else "IN_PROGRESS",
        "gates": {"G1": g1, "G2": g2, "G3": g3, "G4": g4, "G5": "NOT_RUN", "G6": "NOT_RUN"},
        "phase_gates": {
            "P3_SYNTHETIC_OCR_PHASE_GATE": synthetic_phase_gate,
            "G2_POSTGRES_RELIABILITY": g2,
            "G3_SNAPSHOT_REPLAY": g3,
        },
        "candidate_gates": candidate_gates,
        "proof_classes": {
            "controlled_fixture": "PASS" if required_checks_pass else "FAIL",
            "synthetic_ocr_stress": synthetic_ocr["status"],
            "real_ocr_dataset": real_ocr["status"],
            "postgresql_integration": postgres["status"],
            "provider_snapshot": g3,
            "live_provider": g4,
            "controlled_browser_fixture": check_by_name["browser_screenshot_flow"]["status"],
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
        "scope": {"cities": list(("北京", "上海", "杭州")), "traveler_count": "2-5", "days": "2-5"},
        "input": {
            "synthetic_ocr": synthetic_ocr,
            "real_ocr": real_ocr,
            "original_image_artifacts_in_git": 0,
        },
        "postgresql": {"service": postgres_service, "verification": postgres},
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
        "phase_required_contracts": phase_contracts,
        "candidate_required_contracts": candidate_contracts,
        "verification_checks": checks + [postgres, p2_reliability, live],
        "sensitive_scan": sensitive_scan,
        "blockers": [
            item for item, value in {
                "TRIP_CHECK_V1_P3_SYNTHETIC_OCR_NOT_PASSED": synthetic_phase_gate,
                "TRIP_CHECK_V1_P3_POSTGRES_NOT_PASSED": g2,
                "TRIP_CHECK_V1_P3_SNAPSHOT_NOT_PASSED": g3,
            }.items() if value != "PASS"
        ],
        "candidate_evidence_debt": [
            item for item, value in {
                "G1_REAL_OCR_NOT_RUN": g1,
                "G4_LIVE_PROVIDER_NOT_PASSED": g4,
                "G5_PUBLIC_BROWSER_PERFORMANCE_NOT_RUN": "NOT_RUN",
                "G6_RELEASE_MANIFEST_NOT_RUN": "NOT_RUN",
            }.items() if value != "PASS"
        ],
        "non_claims": [
            "Synthetic OCR stress evidence is not real OCR candidate evidence.",
            "Candidate G1 remains NOT_RUN and must be satisfied with real OCR evidence before P6.",
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
    print(json.dumps({
        "status": phase_status,
        "candidate_readiness_status": candidate_status,
        "gates": manifest["gates"],
        "manifest": str(manifest_path),
    }, ensure_ascii=False))
    return 0 if phase_status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
