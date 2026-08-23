from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
FRONTEND_ROOT = REPO_ROOT / "frontend"
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p2"
PILOT_DATASET = BACKEND_ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
HIGH_CONFIDENCE_SECRET_PATTERNS = {
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "windows_user_profile": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
    "escaped_windows_user_profile": re.compile(
        r"[A-Za-z]:\\\\Users\\\\[^\\\s]+",
        re.IGNORECASE,
    ),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True, encoding="utf-8").strip()


def _portable_text(value: str) -> str:
    portable = value
    for source, replacement in (
        (str(REPO_ROOT.resolve()), "<repo>"),
        (str(Path.home().resolve()), "<user-profile>"),
    ):
        portable = portable.replace(source, replacement)
        portable = portable.replace(source.replace("\\", "/"), replacement)
        portable = portable.replace(source.replace("\\", "\\\\"), replacement)
    return portable


def _normalized_log(value: str) -> str:
    return "\n".join(line.rstrip() for line in _portable_text(value).splitlines()).rstrip() + "\n"


def _portable_command(command: list[str]) -> list[str]:
    return [
        "python" if Path(item).resolve() == Path(sys.executable).resolve() else _portable_text(item) for item in command
    ]


def _portable_cwd(cwd: Path) -> str:
    try:
        return cwd.resolve().relative_to(REPO_ROOT.resolve()).as_posix() or "."
    except ValueError:
        return _portable_text(str(cwd.resolve()))


def _run_gate(
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
        exit_code = completed.returncode
        output = completed.stdout + completed.stderr
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        output = (exc.stdout or "") + (exc.stderr or "")
        timed_out = True
    finished = datetime.now(timezone.utc)
    log_path = log_dir / f"{name}.log"
    log_path.write_text(_normalized_log(output), encoding="utf-8")
    return {
        "name": name,
        "status": "PASS" if exit_code == 0 else "FAIL",
        "exit_code": exit_code,
        "timed_out": timed_out,
        "command": _portable_command(command),
        "cwd": _portable_cwd(cwd),
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "log_path": log_path.relative_to(BACKEND_ROOT).as_posix(),
        "log_sha256": _sha256(log_path),
    }


def _safe_reset_output(output: Path) -> None:
    if output.resolve() != DEFAULT_OUTPUT.resolve():
        raise ValueError(f"P2 output must be exactly {DEFAULT_OUTPUT.resolve()}")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)


def _sanitize_text_file(path: Path) -> None:
    value = path.read_text(encoding="utf-8")
    portable = _portable_text(value)
    if portable != value:
        path.write_text(portable, encoding="utf-8")


def _scan(*, baseline: str, subject: str, output: Path) -> dict[str, Any]:
    changed = subprocess.check_output(
        ["git", "diff", "--unified=0", baseline, subject, "--", "."],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    findings: list[dict[str, str]] = []
    for label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS.items():
        if pattern.search(changed):
            findings.append({"scope": "git_diff", "pattern": label})
    for path in sorted(item for item in output.rglob("*") if item.is_file()):
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS.items():
            if pattern.search(value):
                findings.append({"scope": path.relative_to(BACKEND_ROOT).as_posix(), "pattern": label})
    return {
        "status": "PASS" if not findings else "FAIL",
        "baseline_commit": baseline,
        "subject_commit": subject,
        "finding_count": len(findings),
        "findings": findings,
    }


def _artifact_index(output: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(output).as_posix(),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(item for item in output.rglob("*") if item.is_file())
        if path.name != "reliability_gate_manifest.json"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute the Trip Check P2 Reliability Gate")
    parser.add_argument("--subject-commit", default=None)
    parser.add_argument("--baseline-commit", default="8816975f2abf417d21c3aa7dc977576d64347502")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    subject = args.subject_commit or _git("rev-parse", "HEAD")
    if _git("rev-parse", "HEAD") != subject:
        raise RuntimeError("P2 subject must be the checked-out HEAD")
    upstream = _git("rev-parse", "@{upstream}")
    if upstream != subject:
        raise RuntimeError("P2 subject must already be pushed to upstream")
    dirty = [
        line
        for line in _git("status", "--porcelain=v1").splitlines()
        if line and "backend/evidence/trip_check_v1/p2/" not in line.replace("\\", "/")
    ]
    if dirty:
        raise RuntimeError(f"implementation tree is not clean: {dirty}")

    output = args.output.resolve()
    _safe_reset_output(output)
    log_dir = output / "logs"
    log_dir.mkdir(parents=True)
    python = sys.executable
    npm = "npm.cmd" if os.name == "nt" else "npm"
    base_env = os.environ.copy()
    base_env.pop("RUN_SERVICE_INTEGRATION", None)
    pg_env = {**base_env, "RUN_SERVICE_INTEGRATION": "1"}

    gates = [
        _run_gate(
            name="backend_full_pytest",
            command=[python, "-m", "pytest", "tests/", "-q"],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=900,
        ),
        _run_gate(
            name="backend_ruff",
            command=[python, "-m", "ruff", "check", "app", "evals", "scripts", "tests"],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=180,
        ),
        _run_gate(
            name="frontend_build",
            command=[npm, "run", "build"],
            cwd=FRONTEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=420,
        ),
        _run_gate(
            name="browser_recovery",
            command=[npm, "run", "test:e2e:trip-check-p2"],
            cwd=FRONTEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=300,
        ),
        _run_gate(
            name="dual_entry_testset_validation",
            command=[python, "backend/scripts/validate_dual_entry_testset.py"],
            cwd=REPO_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=180,
        ),
        _run_gate(
            name="postgres_integration",
            command=[
                python,
                "-m",
                "pytest",
                "tests/test_migrations_integration.py",
                "tests/test_trip_check_postgres_integration.py",
                "tests/test_repair_concurrency_postgres.py",
                "tests/test_creation_idempotency_postgres.py",
                "tests/test_workspace_resume_postgres.py",
                "tests/test_trip_check_reliability_runner.py",
                "-q",
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=pg_env,
            timeout=600,
        ),
        _run_gate(
            name="reliability_six_cases",
            command=[
                python,
                "-m",
                "scripts.run_trip_check_reliability",
                "--commit-sha",
                subject,
                "--output",
                str(output / "reliability"),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=300,
        ),
        _run_gate(
            name="p1_pilot_regression",
            command=[
                python,
                "-m",
                "scripts.run_trip_check_pilot",
                "--commit-sha",
                subject,
                "--output",
                str(output / "pilot_regression"),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=300,
        ),
    ]

    browser_path = output / "browser-playwright.json"
    if browser_path.exists():
        _sanitize_text_file(browser_path)
    reliability_path = output / "reliability" / "reliability_manifest.json"
    pilot_path = output / "pilot_regression" / "pilot_manifest.json"
    browser = json.loads(browser_path.read_text(encoding="utf-8")) if browser_path.exists() else None
    reliability = json.loads(reliability_path.read_text(encoding="utf-8")) if reliability_path.exists() else None
    pilot = json.loads(pilot_path.read_text(encoding="utf-8")) if pilot_path.exists() else None
    secret_scan = _scan(baseline=args.baseline_commit, subject=subject, output=output)
    _write_json(output / "sensitive_scan.json", secret_scan)
    gate_by_name = {item["name"]: item for item in gates}
    browser_stats = browser.get("stats", {}) if browser else {}
    artifact_contract = (
        reliability is not None
        and reliability.get("status") == "PASS"
        and reliability.get("subject_commit") == subject
        and reliability.get("canonical_cases_passed") == 6
        and reliability.get("domain_required_field_coverage") == 1.0
        and reliability.get("domain_otel_association_rate") == 1.0
        and reliability.get("sensitive_attribute_hit_count") == 0
        and pilot is not None
        and pilot.get("status") == "PASS"
        and pilot.get("commit_sha") == subject
        and pilot.get("metrics", {}).get("case_count") == 18
        and browser is not None
        and browser_stats.get("expected") == 4
        and browser_stats.get("unexpected") == 0
    )
    manifest = {
        "schema_version": "trip-check-p2-reliability-gate-manifest-v1",
        "goal_id": "TC-P2-G01-reliable-run-and-trace",
        "subject_commit": subject,
        "upstream_commit_at_start": upstream,
        "baseline_commit": args.baseline_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "PASS"
            if all(item["status"] == "PASS" for item in gates) and artifact_contract and secret_scan["status"] == "PASS"
            else "REJECT"
        ),
        "proof_classes": {
            "controlled_fixture": "PASS",
            "postgresql_integration": gate_by_name["postgres_integration"]["status"],
            "controlled_browser_fixture": gate_by_name["browser_recovery"]["status"],
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "human_evidence": "NOT_RUN",
        },
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "traveler_count": "2-5",
            "days": "2-5",
            "input": "TEXT_ONLY",
        },
        "candidate_release_gates": {f"G{index}": "NOT_RUN" for index in range(7)},
        "reliability": {
            "manifest_path": reliability_path.relative_to(BACKEND_ROOT).as_posix(),
            "manifest_sha256": _sha256(reliability_path) if reliability_path.exists() else None,
            "summary": {
                key: reliability.get(key) if reliability else None
                for key in (
                    "canonical_case_count",
                    "canonical_cases_passed",
                    "domain_required_field_coverage",
                    "domain_otel_association_rate",
                    "sensitive_attribute_hit_count",
                    "dependencies",
                    "dataset_hash",
                    "fixture_snapshot_hash",
                )
            },
        },
        "p1_pilot_regression": {
            "manifest_path": pilot_path.relative_to(BACKEND_ROOT).as_posix(),
            "manifest_sha256": _sha256(pilot_path) if pilot_path.exists() else None,
            "dataset_path": PILOT_DATASET.relative_to(BACKEND_ROOT).as_posix(),
            "dataset_sha256": _sha256(PILOT_DATASET),
            "metrics": pilot.get("metrics") if pilot else None,
        },
        "browser": {
            "report_path": browser_path.relative_to(BACKEND_ROOT).as_posix(),
            "report_sha256": _sha256(browser_path) if browser_path.exists() else None,
            "stats": browser_stats,
        },
        "verification_gates": gates,
        "sensitive_scan": secret_scan,
        "non_claims": [
            "No live or paid Provider was called.",
            "No public deployment or public E2E was run.",
            "No human evaluation was run.",
            "Controlled fixture, PostgreSQL, browser, live Provider, public and human evidence are not interchangeable.",
        ],
        "artifact_index": _artifact_index(output),
    }
    manifest_path = output / "reliability_gate_manifest.json"
    _write_json(manifest_path, manifest)
    readback = json.loads(manifest_path.read_text(encoding="utf-8"))
    if readback != manifest:
        raise RuntimeError("Reliability manifest readback mismatch")
    print(json.dumps({"status": manifest["status"], "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
