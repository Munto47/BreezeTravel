"""Fail-closed P6 G0 repository and authority gate runner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
)


AUTHORITY_FILES = (
    "AGENTS.md",
    "docs/product/PROJECT_CHARTER.md",
    "docs/product/TRIP_CHECK_SPEC.md",
    "docs/governance/PROGRAM.md",
    "docs/governance/CURRENT_GOAL.md",
    "docs/governance/RELEASE_GATES.md",
)
CANDIDATE_COMPOSE = "deploy/p6/docker-compose.candidate.yml"
CANDIDATE_NGINX = "deploy/p6/nginx-breezetravel-candidate.conf"


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
        raise P6ContractError("P6_G0_ARTIFACT_WRITE_FAILED") from exc


def _authority_fingerprint(repo_root: Path) -> tuple[str, int]:
    values: list[dict[str, Any]] = []
    try:
        for relative in AUTHORITY_FILES:
            path = repo_root / relative
            content = path.read_bytes()
            values.append(
                {
                    "path": relative,
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": len(content),
                }
            )
    except OSError as exc:
        raise P6ContractError("P6_G0_AUTHORITY_FILE_UNREADABLE") from exc
    return digest(values), len(values)


def _candidate_deployment_fingerprint(repo_root: Path) -> str:
    try:
        compose = (repo_root / CANDIDATE_COMPOSE).read_text(encoding="utf-8")
        nginx = (repo_root / CANDIDATE_NGINX).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise P6ContractError("P6_G0_CANDIDATE_DEPLOYMENT_CONFIG_INVALID") from exc
    compose_required = (
        'RUNTIME_PROFILE: local_fixture',
        'AMAP_MOCK: "true"',
        'DEV_LOGIN_BYPASS: "false"',
        'NEXT_PUBLIC_SHOW_TEST_LOGIN: "false"',
        'PUBLIC_DEMO_MODE: "true"',
        'CORS_ORIGIN_REGEX:',
        ':/run/breezetravel-evidence:ro',
        '127.0.0.1:8000:8000',
        '127.0.0.1:3000:3000',
    )
    nginx_required = (
        'server_name breezetravel.cn www.breezetravel.cn;',
        'location = /health',
        'proxy_pass http://127.0.0.1:8000/health;',
        'location /api/',
        'limit_req zone=breezetravel_api',
        'X-Content-Type-Options nosniff',
    )
    if (
        any(token not in compose for token in compose_required)
        or 'DEV_LOGIN_BYPASS: "true"' in compose
        or 'NEXT_PUBLIC_SHOW_TEST_LOGIN: "true"' in compose
        or any(token not in nginx for token in nginx_required)
    ):
        raise P6ContractError("P6_G0_CANDIDATE_DEPLOYMENT_CONFIG_INVALID")
    return digest({"compose": compose, "nginx": nginx})


def _junit_counts(path: Path) -> dict[str, int]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError) as exc:
        raise P6ContractError("P6_G0_BACKEND_JUNIT_INVALID") from exc
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise P6ContractError("P6_G0_BACKEND_JUNIT_INVALID")
    counts = {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] < 1:
        raise P6ContractError("P6_G0_BACKEND_JUNIT_INVALID")
    return counts


def _commands(repo_root: Path, junit_path: Path) -> list[tuple[str, list[str], Path, int]]:
    backend = repo_root / "backend"
    frontend = repo_root / "frontend"
    npm = "npm.cmd" if os.name == "nt" else "npm"
    return [
        (
            "p6_schema_validation",
            [sys.executable, "scripts/validate_trip_check_p6_contracts.py", "--schemas-only"],
            backend,
            120,
        ),
        (
            "backend_full_pytest",
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/",
                "-q",
                "--disable-warnings",
                f"--junitxml={junit_path}",
            ],
            backend,
            1200,
        ),
        (
            "backend_ruff",
            [sys.executable, "-m", "ruff", "check", "app", "evals", "scripts", "tests"],
            backend,
            240,
        ),
        ("frontend_build", [npm, "run", "build"], frontend, 600),
        (
            "dual_entry_validation",
            [sys.executable, "backend/scripts/validate_dual_entry_testset.py"],
            repo_root,
            240,
        ),
    ]


def _run_command(
    *,
    name: str,
    command: Sequence[str],
    cwd: Path,
    timeout: int,
    log_root: Path,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    environment = os.environ.copy()
    for key in (
        "RUN_EXTERNAL_TESTS",
        "RUN_SERVICE_INTEGRATION",
        "AMAP_API_KEY",
        "QWEATHER_API_KEY",
        "QWEATHER_PRIVATE_KEY",
    ):
        environment.pop(key, None)
    environment.update(
        {
            "RUNTIME_PROFILE": "test",
            "DEMO_MODE": "true",
            "AMAP_MOCK": "true",
            "FT_ROUTER_ENABLED": "false",
            "LANGCHAIN_TRACING_V2": "false",
        }
    )
    try:
        result = command_runner(
            list(command),
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P6ContractError("P6_G0_COMMAND_EXECUTION_FAILED") from exc
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    _write_new(log_root / f"{name}.stdout.log", stdout)
    _write_new(log_root / f"{name}.stderr.log", stderr)
    return {
        "name": name,
        "returncode": result.returncode,
        "stdout_sha256": hashlib.sha256(stdout.encode("utf-8")).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr.encode("utf-8")).hexdigest(),
    }


def run_repository_gate(
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
            raise P6ContractError("P6_G0_EXTERNAL_ROOT_REQUIRED")
    if output_resolved == log_resolved:
        raise P6ContractError("P6_G0_ROOTS_MUST_BE_DISTINCT")
    if formal:
        actual = read_actual_repo_state(repo_resolved)
        expected = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if actual != expected:
            raise P6ContractError("P6_G0_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g0").resolve(strict=False):
            raise P6ContractError("P6_G0_OUTPUT_ROOT_INVALID")
        if output_resolved.exists() and any(output_resolved.iterdir()):
            raise P6ContractError("P6_G0_OUTPUT_NOT_EMPTY")
        if log_resolved.exists() and any(log_resolved.iterdir()):
            raise P6ContractError("P6_G0_LOG_ROOT_NOT_EMPTY")
    authority_sha, authority_count = _authority_fingerprint(repo_resolved)
    candidate_deployment_sha = _candidate_deployment_fingerprint(repo_resolved)
    log_resolved.mkdir(parents=True, exist_ok=True)
    junit_path = log_resolved / "backend-full-junit.xml"
    results = [
        _run_command(
            name=name,
            command=command,
            cwd=cwd,
            timeout=timeout,
            log_root=log_resolved,
            command_runner=command_runner,
        )
        for name, command, cwd, timeout in _commands(repo_resolved, junit_path)
    ]
    if any(item["returncode"] != 0 for item in results):
        raise P6ContractError("P6_G0_COMMAND_MATRIX_FAILED")
    counts = _junit_counts(junit_path)
    if counts["failures"] or counts["errors"]:
        raise P6ContractError("P6_G0_BACKEND_TESTS_FAILED")
    readback = {
        "schema_version": "trip-check-p6-g0-repository-readback-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "authority_fingerprint": authority_sha,
        "authority_file_count": authority_count,
        "candidate_deployment_fingerprint": candidate_deployment_sha,
        "commands": results,
        "backend_junit": counts,
    }
    readback["receipt_hash"] = digest(readback)
    _write_new(
        output_resolved / "g0_repository_readback.json",
        json.dumps(readback, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    metrics = {
        "authority_conflict_count": 0,
        "schema_failure_count": 0,
        "backend_failure_count": 0,
        "ruff_failure_count": 0,
        "frontend_build_failure_count": 0,
        "dual_entry_failure_count": 0,
        "capability_claim_failure_count": 0,
        "authority_file_count": authority_count,
        "command_count": len(results),
        "backend_test_count": counts["tests"],
        "backend_skipped_count": counts["skipped"],
    }
    receipt = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g0",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "repository_contract",
        "checks_total": 12,
        "checks_passed": 12,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt = validate_gate_receipt(receipt, "g0", spec)
    _write_new(
        output_resolved / "g0_receipt.json",
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return receipt
