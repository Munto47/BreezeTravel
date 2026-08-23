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
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p1"
PILOT_DATASET = BACKEND_ROOT / "evals" / "trip_check_v1" / "pilot.jsonl"
HIGH_CONFIDENCE_SECRET_PATTERNS = {
    "openai_key": re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    "github_token": re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),
    "aws_access_key": re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "windows_user_profile": re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE),
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
    ).strip()


def _portable_text(value: str) -> str:
    replacements = (
        (str(REPO_ROOT.resolve()), "<repo>"),
        (str(Path.home().resolve()), "<user-profile>"),
    )
    portable = value
    for source, target in replacements:
        portable = portable.replace(source, target)
        portable = portable.replace(source.replace("\\", "/"), target)
    return portable


def _portable_command(command: list[str]) -> list[str]:
    return [
        "python" if Path(item).resolve() == Path(sys.executable).resolve() else _portable_text(item)
        for item in command
    ]


def _portable_cwd(cwd: Path) -> str:
    try:
        relative = cwd.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError:
        return _portable_text(str(cwd.resolve()))
    return relative.as_posix() or "."


def _sanitize_text_file(path: Path) -> None:
    value = path.read_text(encoding="utf-8")
    portable = _portable_text(value)
    if portable != value:
        path.write_text(portable, encoding="utf-8")


def _run_gate(
    *,
    name: str,
    command: list[str],
    cwd: Path,
    log_dir: Path,
    env: dict[str, str] | None = None,
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
    log_path.write_text(_portable_text(output), encoding="utf-8")
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


def _safe_reset_output(output_dir: Path) -> None:
    resolved = output_dir.resolve()
    expected = DEFAULT_OUTPUT.resolve()
    if resolved != expected:
        raise ValueError(f"D1 output must be exactly {expected}")
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def _scan_subject_and_artifacts(*, baseline: str, subject: str, output_dir: Path) -> dict[str, Any]:
    changed_text = subprocess.check_output(
        ["git", "diff", "--unified=0", baseline, subject, "--", "."],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    findings: list[dict[str, str]] = []
    for label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS.items():
        if pattern.search(changed_text):
            findings.append({"scope": "git_diff", "pattern": label})
    for path in sorted(item for item in output_dir.rglob("*") if item.is_file()):
        try:
            value = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in HIGH_CONFIDENCE_SECRET_PATTERNS.items():
            if pattern.search(value):
                findings.append(
                    {
                        "scope": path.relative_to(BACKEND_ROOT).as_posix(),
                        "pattern": label,
                    }
                )
    return {
        "status": "PASS" if not findings else "FAIL",
        "baseline_commit": baseline,
        "subject_commit": subject,
        "patterns": sorted(HIGH_CONFIDENCE_SECRET_PATTERNS),
        "finding_count": len(findings),
        "findings": findings,
    }


def _demo_script(subject: str) -> str:
    return f"""# BreezeTravel P1 90 秒演示脚本

证据绑定 commit：`{subject}`。演示只使用受控 fixture，不表示 live Provider、公网或真人证据。

- **0–15 秒：文本与边界。** 在“行程查”的既有 Import 页面粘贴北京、上海或杭州 2 天文本。指出范围固定为单城市、2–5 人、2–5 天。
- **15–30 秒：Brief 与地点确认。** 展示 TripBrief 的人数、日期、偏好来源；未确认 Brief 不能运行。切到 BJ-02，展示歧义“博物馆”不会自动绑定，也不能创建权威 revision。
- **30–50 秒：Evidence → Audit → Advice。** 在三城任一 `01` 案例确认 Brief 并启动 Run，展示持久阶段事件、受控路线 receipt、`ROUTE_GAP_INSUFFICIENT` Finding，以及绑定 Evidence 与不确定性的 Advice。
- **50–70 秒：Repair 与新 Revision。** 对比“顺延后一站”和“缩短前一站”，采纳一个已有 EditCommand 方案；展示旧报告 stale、revision 1 → 2，以及新 revision 的完整 postcheck。
- **70–82 秒：恢复与去重。** 刷新页面，展示 revision 2、`SUCCEEDED · POSTCHECK` 和 SSE 从 `Last-Event-ID` 继续。打开故障矩阵，指出 Evidence 后终止恢复前后 snapshot/receipt/revision 数量不变。
- **82–90 秒：证据边界。** 展示 D1 manifest：18/18 pilot、三城浏览器、PostgreSQL 和自动回归分别记录；明确 fixture、浏览器、PostgreSQL、live Provider、公网和真人证据不能互相替代。
"""


def _artifact_index(output_dir: Path) -> list[dict[str, Any]]:
    return [
        {
            "path": path.relative_to(output_dir).as_posix(),
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
        }
        for path in sorted(item for item in output_dir.rglob("*") if item.is_file())
        if path.name != "d1_manifest.json"
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="Execute and bind the BreezeTravel P1 D1 gates")
    parser.add_argument("--subject-commit", default=None)
    parser.add_argument("--baseline-commit", default="acacc946")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    subject = args.subject_commit or _git("rev-parse", "HEAD")
    if _git("rev-parse", "HEAD") != subject:
        raise RuntimeError("D1 subject must be the checked-out HEAD")
    upstream = _git("rev-parse", "@{upstream}")
    if upstream != subject:
        raise RuntimeError("D1 subject must already be pushed to its upstream branch")
    dirty_before = [
        line
        for line in _git("status", "--porcelain=v1").splitlines()
        if line and "backend/evidence/trip_check_v1/" not in line.replace("\\", "/")
    ]
    if dirty_before:
        raise RuntimeError(f"tracked implementation tree is not clean: {dirty_before}")

    output_dir = args.output.resolve()
    _safe_reset_output(output_dir)
    log_dir = output_dir / "logs"
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
            name="dual_entry_testset_validation",
            command=[python, "backend/scripts/validate_dual_entry_testset.py"],
            cwd=REPO_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=180,
        ),
        _run_gate(
            name="postgres_fault_matrix",
            command=[
                python,
                "-m",
                "pytest",
                "tests/test_migrations_integration.py",
                "tests/test_trip_check_postgres_integration.py",
                "tests/test_repair_concurrency_postgres.py",
                "tests/test_creation_idempotency_postgres.py",
                "tests/test_import_mobile_postgres.py",
                "tests/test_workspace_resume_postgres.py",
                "-q",
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=pg_env,
            timeout=420,
        ),
        _run_gate(
            name="trip_check_fault_contracts",
            command=[
                python,
                "-m",
                "pytest",
                "tests/test_trip_check_executor.py",
                "tests/test_trip_check_runs_api.py",
                "tests/test_trip_check_advice_api.py",
                "tests/test_repairs.py",
                "-q",
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=240,
        ),
        _run_gate(
            name="pilot_18",
            command=[
                python,
                "-m",
                "scripts.run_trip_check_pilot",
                "--commit-sha",
                subject,
                "--output",
                str(output_dir / "pilot"),
            ],
            cwd=BACKEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=240,
        ),
        _run_gate(
            name="browser_three_city",
            command=[npm, "run", "test:e2e:trip-check-p1"],
            cwd=FRONTEND_ROOT,
            log_dir=log_dir,
            env=base_env,
            timeout=240,
        ),
    ]

    pilot_manifest_path = output_dir / "pilot" / "pilot_manifest.json"
    browser_report_path = output_dir / "browser-playwright.json"
    if browser_report_path.exists():
        _sanitize_text_file(browser_report_path)
    pilot = json.loads(pilot_manifest_path.read_text(encoding="utf-8")) if pilot_manifest_path.exists() else None
    browser = json.loads(browser_report_path.read_text(encoding="utf-8")) if browser_report_path.exists() else None
    secret_scan = _scan_subject_and_artifacts(
        baseline=args.baseline_commit,
        subject=subject,
        output_dir=output_dir,
    )
    _write_json(output_dir / "sensitive_scan.json", secret_scan)
    (output_dir / "DEMO_90_SECONDS.md").write_text(_demo_script(subject), encoding="utf-8")

    gate_by_name = {item["name"]: item for item in gates}
    fault_status = "PASS" if all(
        gate_by_name[name]["status"] == "PASS"
        for name in ("postgres_fault_matrix", "trip_check_fault_contracts", "browser_three_city")
    ) else "FAIL"
    fault_matrix = {
        "schema_version": "trip-check-p1-fault-matrix-v1",
        "subject_commit": subject,
        "status": fault_status,
        "evidence_class": ["POSTGRESQL_INTEGRATION", "CONTROLLED_FIXTURE", "CONTROLLED_BROWSER_FIXTURE"],
        "cases": [
            {"fault": "terminate_after_evidence", "proof": ["test_trip_check_executor.py", "test_trip_check_postgres_integration.py"]},
            {"fault": "same_idempotency_request_replay", "proof": ["test_trip_check_executor.py", "test_trip_check_postgres_integration.py"]},
            {"fault": "different_payload_reuses_idempotency_key", "proof": ["test_trip_check_runs_api.py", "test_trip_check_postgres_integration.py"]},
            {"fault": "concurrent_revision_or_repair", "proof": ["test_repair_concurrency_postgres.py", "test_import_mobile_postgres.py"]},
            {"fault": "missing_or_stale_if_match", "proof": ["test_trip_check_runs_api.py", "test_import_mobile_postgres.py"]},
            {"fault": "config_hash_drift", "proof": ["test_trip_check_runs_api.py", "test_trip_check_postgres_integration.py"]},
            {"fault": "expired_lease_takeover_and_competing_claim", "proof": ["test_trip_check_postgres_integration.py"]},
            {"fault": "sse_last_event_id_reconnect", "proof": ["test_trip_check_runs_api.py", "trip-check-p1.spec.js"]},
        ],
        "gate_logs": {
            name: gate_by_name[name]["log_path"]
            for name in ("postgres_fault_matrix", "trip_check_fault_contracts", "browser_three_city")
        },
    }
    _write_json(output_dir / "fault_matrix.json", fault_matrix)

    browser_stats = browser.get("stats", {}) if browser else {}
    all_gates_pass = all(item["status"] == "PASS" for item in gates)
    artifact_status = (
        pilot is not None
        and pilot.get("status") == "PASS"
        and pilot.get("commit_sha") == subject
        and browser is not None
        and browser_stats.get("expected") == 4
        and browser_stats.get("unexpected") == 0
    )
    manifest = {
        "schema_version": "trip-check-p1-d1-manifest-v1",
        "goal_id": "TC-P1-G01-text-vertical-slice",
        "subject_commit": subject,
        "upstream_commit_at_start": upstream,
        "baseline_commit": args.baseline_commit,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "PASS"
            if all_gates_pass and artifact_status and secret_scan["status"] == "PASS"
            else "REJECT"
        ),
        "proof_classes": {
            "controlled_fixture": "PASS" if pilot and pilot.get("status") == "PASS" else "FAIL",
            "postgresql_integration": gate_by_name["postgres_fault_matrix"]["status"],
            "controlled_browser_fixture": gate_by_name["browser_three_city"]["status"],
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
        "pilot": {
            "manifest_path": pilot_manifest_path.relative_to(BACKEND_ROOT).as_posix(),
            "manifest_sha256": _sha256(pilot_manifest_path) if pilot_manifest_path.exists() else None,
            "dataset_path": PILOT_DATASET.relative_to(BACKEND_ROOT).as_posix(),
            "dataset_sha256": _sha256(PILOT_DATASET),
            "metrics": pilot.get("metrics") if pilot else None,
        },
        "browser": {
            "report_path": browser_report_path.relative_to(BACKEND_ROOT).as_posix(),
            "report_sha256": _sha256(browser_report_path) if browser_report_path.exists() else None,
            "stats": browser_stats,
        },
        "fault_matrix": {
            "status": fault_status,
            "path": (output_dir / "fault_matrix.json").relative_to(BACKEND_ROOT).as_posix(),
            "sha256": _sha256(output_dir / "fault_matrix.json"),
        },
        "verification_gates": gates,
        "sensitive_scan": secret_scan,
        "non_claims": [
            "No live or paid Provider was called.",
            "No public deployment or public E2E was run.",
            "No human evaluation was run.",
            "Fixture, PostgreSQL, browser, live Provider, public and human evidence are not interchangeable.",
        ],
        "artifact_index": _artifact_index(output_dir),
    }
    _write_json(output_dir / "d1_manifest.json", manifest)
    print(json.dumps({"status": manifest["status"], "manifest": str(output_dir / "d1_manifest.json")}, ensure_ascii=False))
    return 0 if manifest["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
