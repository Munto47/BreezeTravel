"""Fail-closed P6 G5 controlled local-browser evidence runner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    read_actual_repo_state,
    validate_candidate_run_spec,
)


EXPECTED_TITLES = {
    "北京文本主链完成 Repair、新 Revision、postcheck 与 SSE 断点恢复",
    "上海文本主链完成 Repair、新 Revision、postcheck 与 SSE 断点恢复，局部 Provider 失败保留成功事实",
    "杭州文本主链完成 Repair、新 Revision、postcheck 与 SSE 断点恢复",
    "BJ-02 歧义地点未经人工选择不能创建权威 revision 或 Run",
    "截图 OCR 显示低置信度和原图删除回执，刷新恢复导入草稿",
    "原图删除失败时页面显示 PRIVACY_BLOCKED 且不创建导入草稿",
}
REQUIRED_COVERAGE = (
    "TEXT_ENTRY",
    "SCREENSHOT_ENTRY",
    "TRIP_BRIEF_CONFIRMATION",
    "PLACE_DISAMBIGUATION",
    "REFRESH_RESUME",
    "SSE_RECONNECT",
    "PROVIDER_PARTIAL_FAILURE",
    "ADVICE_ADOPTION",
    "NEW_REVISION",
    "FULL_POSTCHECK",
    "PRIVACY_FAIL_CLOSED",
)


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _write_new(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
    except OSError as exc:
        raise P6ContractError("P6_G5_LOCAL_ARTIFACT_WRITE_FAILED") from exc


def _titles(suites: object) -> set[str]:
    result: set[str] = set()
    if not isinstance(suites, list):
        raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
    for suite in suites:
        if not isinstance(suite, Mapping):
            raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
        specs = suite.get("specs", [])
        if not isinstance(specs, list):
            raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
        for spec in specs:
            if not isinstance(spec, Mapping) or not isinstance(spec.get("title"), str):
                raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
            result.add(spec["title"])
        result.update(_titles(suite.get("suites", [])))
    return result


def _validate_report(report: Mapping[str, Any], subject_commit: str) -> dict[str, int]:
    config = report.get("config")
    stats = report.get("stats")
    errors = report.get("errors")
    if not isinstance(config, Mapping) or not isinstance(stats, Mapping) or not isinstance(errors, list):
        raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
    metadata = config.get("metadata")
    if not isinstance(metadata, Mapping) or any(
        (
            metadata.get("commit_sha") != subject_commit,
            metadata.get("evidence_class") != "CONTROLLED_BROWSER_FIXTURE",
            metadata.get("evidence_scope") != "P6_G5_LOCAL_CHAIN",
            metadata.get("live_provider_evidence") is not False,
            metadata.get("public_e2e_evidence") is not False,
            metadata.get("human_evidence") is not False,
        )
    ):
        raise P6ContractError("P6_G5_LOCAL_REPORT_BINDING_INVALID")
    counts: dict[str, int] = {}
    for key in ("expected", "unexpected", "flaky", "skipped"):
        value = stats.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise P6ContractError("P6_G5_LOCAL_REPORT_INVALID")
        counts[key] = value
    if (
        counts != {"expected": len(EXPECTED_TITLES), "unexpected": 0, "flaky": 0, "skipped": 0}
        or errors
        or _titles(report.get("suites")) != EXPECTED_TITLES
    ):
        raise P6ContractError("P6_G5_LOCAL_BROWSER_MATRIX_FAILED")
    return counts


def _run_browser(
    *,
    frontend_root: Path,
    report_path: Path,
    log_root: Path,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    command: Sequence[str] = (npm, "run", "test:e2e:trip-check-p6")
    environment = os.environ.copy()
    environment["P6_G5_PLAYWRIGHT_JSON"] = str(report_path)
    for key in (
        "AMAP_API_KEY",
        "QWEATHER_API_KEY",
        "QWEATHER_PRIVATE_KEY",
        "DATABASE_URL",
        "TEST_DATABASE_ADMIN_URL",
    ):
        environment.pop(key, None)
    try:
        result = command_runner(
            list(command),
            cwd=frontend_root,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=360,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P6ContractError("P6_G5_LOCAL_BROWSER_EXECUTION_FAILED") from exc
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    _write_new(log_root / "playwright.stdout.log", stdout)
    _write_new(log_root / "playwright.stderr.log", stderr)
    if result.returncode != 0:
        raise P6ContractError("P6_G5_LOCAL_BROWSER_MATRIX_FAILED")
    return {
        "returncode": result.returncode,
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }


def run_local_browser_evidence(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    log_root: Path,
    repo_root: Path,
    formal: bool = True,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    log_resolved = log_root.resolve(strict=False)
    for path in (output_resolved, log_resolved):
        try:
            path.relative_to(repo_resolved)
        except ValueError:
            pass
        else:
            raise P6ContractError("P6_G5_LOCAL_EXTERNAL_ROOT_REQUIRED")
    if output_resolved == log_resolved:
        raise P6ContractError("P6_G5_LOCAL_ROOTS_MUST_BE_DISTINCT")
    if formal:
        expected_repo = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if read_actual_repo_state(repo_resolved) != expected_repo:
            raise P6ContractError("P6_G5_LOCAL_REPO_BINDING_INVALID")
        expected_root = (Path(spec["evidence_root"]) / "g5" / "local").resolve(strict=False)
        if output_resolved != expected_root:
            raise P6ContractError("P6_G5_LOCAL_OUTPUT_ROOT_INVALID")
        for path, reason in (
            (output_resolved, "P6_G5_LOCAL_OUTPUT_NOT_EMPTY"),
            (log_resolved, "P6_G5_LOCAL_LOG_ROOT_NOT_EMPTY"),
        ):
            if path.exists() and any(path.iterdir()):
                raise P6ContractError(reason)
    output_resolved.mkdir(parents=True, exist_ok=True)
    log_resolved.mkdir(parents=True, exist_ok=True)
    report_path = output_resolved / "playwright-report.json"
    command = _run_browser(
        frontend_root=repo_resolved / "frontend",
        report_path=report_path,
        log_root=log_resolved,
        command_runner=command_runner,
    )
    report = _load_json(report_path, "P6_G5_LOCAL_REPORT_INVALID")
    counts = _validate_report(report, spec["subject_commit"])
    try:
        report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P6ContractError("P6_G5_LOCAL_REPORT_UNREADABLE") from exc
    receipt: dict[str, Any] = {
        "schema_version": "trip-check-p6-local-browser-receipt-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "browser_local",
        "browser_report_sha256": report_sha,
        "test_counts": counts,
        "test_titles_sha256": digest(sorted(EXPECTED_TITLES)),
        "coverage": list(REQUIRED_COVERAGE),
        "command": command,
        "human_evidence": False,
    }
    receipt["receipt_hash"] = digest(receipt)
    _write_new(
        output_resolved / "local_browser_receipt.json",
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return receipt
