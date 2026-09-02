from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from time import perf_counter
from typing import Any

import asyncpg

from evals.g07_candidate.live_spec_builder import (
    G07_EVIDENCE_ROOT_PARENT,
    G07_RUN_SPEC_RELATIVE,
    _verify_g07_contract,
    read_actual_g07_repo_state,
)
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    file_sha256,
)
from scripts.smoke_g01_live_persistence import (
    _database_url,
    _migrate,
    _run as _run_live_chain,
)


EXPECTED_BROWSER_FILE_COUNTS = {
    "g02-product-delivery.spec.js": 1,
    "g03-product-delivery.spec.js": 1,
    "g03r-result-ui.spec.js": 29,
    "g04-screenshot-parity.spec.js": 5,
    "g05-knowledge.spec.js": 3,
    "g06-memory-share.spec.js": 3,
    "trip-understanding-v3.spec.js": 4,
}
EXPECTED_BROWSER_TEST_COUNT = sum(EXPECTED_BROWSER_FILE_COUNTS.values())
PERFORMANCE_CHAIN_COUNT = 50
PERFORMANCE_THRESHOLDS_MS = {
    "create_to_progress_p95_ms": 500.0,
    "create_to_editable_cards_p95_ms": 8_000.0,
}
BROWSER_COVERAGE = (
    "DEMO_AND_LOGIN",
    "TEXT_AND_SCREENSHOT",
    "DAY_CARDS_AND_EDIT",
    "MAP_STALE_AND_MANUAL_RERENDER",
    "STAY_AND_TOP3",
    "REFRESH_AND_CONCURRENT_EDIT",
    "SSE_RECONNECT",
    "PROVIDER_PARTIAL_FAILURE",
    "KNOWLEDGE_WITHDRAWAL",
    "MEMORY_AND_SHARE_REVOCATION",
    "PUBLIC_FIELD_REDACTION",
)


def _load_object(path: Path, reason: str) -> dict[str, Any]:
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
            stream.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    except OSError as exc:
        raise P6ContractError("G07_G5_ARTIFACT_WRITE_FAILED") from exc


def _write_text_new(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
    except OSError as exc:
        raise P6ContractError("G07_G5_ARTIFACT_WRITE_FAILED") from exc


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            stream.write("\n")
    except OSError as exc:
        raise P6ContractError("G07_G5_ARTIFACT_WRITE_FAILED") from exc


def _require_external_empty(path: Path, repo_root: Path, reason: str) -> Path:
    resolved = path.resolve(strict=False)
    repository = repo_root.resolve(strict=True)
    try:
        resolved.relative_to(repository)
    except ValueError:
        pass
    else:
        raise P6ContractError("G07_G5_EXTERNAL_ROOT_REQUIRED")
    if resolved.exists() and any(resolved.iterdir()):
        raise P6ContractError(reason)
    return resolved


def _execution_binding(
    *,
    repo_root: Path,
    formal: bool,
    subject_commit: str | None = None,
    candidate_tree: str | None = None,
) -> dict[str, Any]:
    repository = repo_root.resolve(strict=True)
    bindings = _verify_g07_contract(repository)
    if formal:
        if subject_commit is not None or candidate_tree is not None:
            raise P6ContractError("G07_G5_FORMAL_INJECTION_FORBIDDEN")
        state = read_actual_g07_repo_state(repository)
        if state["dirty_tree"] or state["subject_commit"] != state["upstream_commit"]:
            raise P6ContractError("G07_G5_REPO_BINDING_INVALID")
    else:
        state = {
            "subject_commit": subject_commit or "7" * 40,
            "candidate_tree": candidate_tree or "8" * 40,
            "upstream_ref": "origin/codex/g07-candidate-cycle-2",
            "upstream_commit": subject_commit or "7" * 40,
            "dirty_tree": False,
        }
    return {
        **state,
        "g07_run_spec_path": G07_RUN_SPEC_RELATIVE,
        "g07_run_spec_sha256": file_sha256(repository / G07_RUN_SPEC_RELATIVE),
        "verified_bindings": dict(sorted(bindings.items())),
    }


def _walk_specs(suites: object) -> list[Mapping[str, Any]]:
    if not isinstance(suites, list):
        raise P6ContractError("G07_G5_BROWSER_REPORT_INVALID")
    specs: list[Mapping[str, Any]] = []
    for suite in suites:
        if not isinstance(suite, Mapping):
            raise P6ContractError("G07_G5_BROWSER_REPORT_INVALID")
        current = suite.get("specs", [])
        if not isinstance(current, list) or any(
            not isinstance(spec, Mapping) for spec in current
        ):
            raise P6ContractError("G07_G5_BROWSER_REPORT_INVALID")
        specs.extend(current)
        specs.extend(_walk_specs(suite.get("suites", [])))
    return specs


def validate_browser_report(
    report: Mapping[str, Any], subject_commit: str
) -> dict[str, Any]:
    config = report.get("config")
    stats = report.get("stats")
    errors = report.get("errors")
    if not (
        isinstance(config, Mapping)
        and isinstance(stats, Mapping)
        and isinstance(errors, list)
        and not errors
    ):
        raise P6ContractError("G07_G5_BROWSER_REPORT_INVALID")
    metadata = config.get("metadata")
    expected_metadata = {
        "commit_sha": subject_commit,
        "evidence_class": "CONTROLLED_BROWSER_FIXTURE",
        "evidence_scope": "G07_G5_FULL_PRODUCT_CHAIN",
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
        "human_evidence": False,
    }
    if not isinstance(metadata, Mapping) or any(
        metadata.get(key) != value for key, value in expected_metadata.items()
    ):
        raise P6ContractError("G07_G5_BROWSER_BINDING_INVALID")
    expected_stats = {
        "expected": EXPECTED_BROWSER_TEST_COUNT,
        "unexpected": 0,
        "flaky": 0,
        "skipped": 0,
    }
    if any(stats.get(key) != value for key, value in expected_stats.items()):
        raise P6ContractError("G07_G5_BROWSER_MATRIX_FAILED")
    specs = _walk_specs(report.get("suites"))
    file_counts: Counter[str] = Counter()
    titles: list[str] = []
    for spec in specs:
        title = spec.get("title")
        file_name = spec.get("file")
        tests = spec.get("tests")
        if not (
            isinstance(title, str)
            and isinstance(file_name, str)
            and isinstance(tests, list)
            and tests
        ):
            raise P6ContractError("G07_G5_BROWSER_REPORT_INVALID")
        for test in tests:
            if not isinstance(test, Mapping) or test.get("status") != "expected":
                raise P6ContractError("G07_G5_BROWSER_MATRIX_FAILED")
            results = test.get("results")
            if not isinstance(results, list) or not results or any(
                not isinstance(result, Mapping) or result.get("status") != "passed"
                for result in results
            ):
                raise P6ContractError("G07_G5_BROWSER_MATRIX_FAILED")
        file_counts[Path(file_name).name] += 1
        titles.append(title)
    if dict(sorted(file_counts.items())) != EXPECTED_BROWSER_FILE_COUNTS:
        raise P6ContractError("G07_G5_BROWSER_COVERAGE_INVALID")
    if len(specs) != EXPECTED_BROWSER_TEST_COUNT or len(set(titles)) != len(titles):
        raise P6ContractError("G07_G5_BROWSER_COVERAGE_INVALID")
    return {
        "test_count": len(specs),
        "file_counts": dict(sorted(file_counts.items())),
        "title_set_sha256": digest(sorted(titles)),
    }


def _run_browser_command(
    *,
    repo_root: Path,
    report_path: Path,
    test_output: Path,
    subject_commit: str,
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    npm = "npm.cmd" if os.name == "nt" else "npm"
    environment = os.environ.copy()
    environment.update(
        {
            "G07_G5_PLAYWRIGHT_JSON": str(report_path),
            "G07_G5_PLAYWRIGHT_OUTPUT": str(test_output),
            "G07_CANDIDATE_COMMIT": subject_commit,
        }
    )
    for key in (
        "AMAP_API_KEY",
        "QWEN_API_KEY",
        "QWEN_API_URL",
        "QWEATHER_API_KEY",
        "QWEATHER_PRIVATE_KEY",
        "DATABASE_URL",
        "TEST_DATABASE_ADMIN_URL",
    ):
        environment.pop(key, None)
    try:
        return command_runner(
            [npm, "run", "test:e2e:g07"],
            cwd=repo_root / "backend/eval_data/g07_candidate/browser_runner",
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2_400,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P6ContractError("G07_G5_BROWSER_EXECUTION_FAILED") from exc


def _browser_database_name(subject_commit: str) -> str:
    return f"breezetravel_g07_browser_{subject_commit[:12]}"


async def _create_browser_database(admin_url: str, database_name: str) -> str:
    connection = await asyncpg.connect(admin_url)
    try:
        exists = await connection.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", database_name
        )
        if exists:
            raise P6ContractError("G07_G5_BROWSER_DATABASE_ALREADY_EXISTS")
        await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()
    database_url = _database_url(admin_url, database_name)
    try:
        await _migrate(database_url)
    except Exception:
        await _drop_browser_database(admin_url, database_name)
        raise
    return database_url


async def _drop_browser_database(admin_url: str, database_name: str) -> None:
    connection = await asyncpg.connect(admin_url)
    try:
        await connection.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await connection.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
    finally:
        await connection.close()


def _service_environment(
    *, database_url: str, database_admin_url: str, redis_url: str
) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "DATABASE_URL": database_url.replace(
                "postgresql://", "postgresql+asyncpg://", 1
            ),
            "TEST_DATABASE_ADMIN_URL": database_admin_url,
            "REDIS_URL": redis_url,
            "RUNTIME_PROFILE": "local_fixture",
            "TRIP_UNDERSTANDING_PROVIDER_MODE": "fixture",
            "SCREENSHOT_OCR_MODE": "fixture",
            "AMAP_MOCK": "true",
            "DEV_LOGIN_BYPASS": "true",
            "JWT_SECRET_KEY": "g07-browser-local-only-secret-2026",
            "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY": (
                "g07-browser-source-encryption-key-2026"
            ),
            "DEEPSEEK_API_KEY": "",
            "OPENAI_API_KEY": "",
            "QWEN_API_KEY": "",
            "AMAP_API_KEY": "",
            "QWEATHER_API_KEY": "",
            "QWEATHER_PRIVATE_KEY": "",
            "QWEATHER_KEY_ID": "",
            "QWEATHER_PROJECT_ID": "",
            "LANGCHAIN_TRACING_V2": "false",
            "LANGSMITH_TRACING": "false",
            "RUN_SERVICE_INTEGRATION": "1",
        }
    )
    return environment


def _start_fixture_services(
    *,
    repo_root: Path,
    log_root: Path,
    environment: Mapping[str, str],
) -> tuple[list[subprocess.Popen[bytes]], list[Any]]:
    backend = repo_root / "backend"
    service_command = [
        sys.executable,
        "-m",
        "scripts.run_g07_g5_browser_performance",
        "service",
    ]
    commands = {
        "backend": [*service_command, "backend"],
        "understanding-worker": [
            *service_command,
            "understanding-worker",
        ],
        "map-worker": [
            *service_command,
            "map-worker",
        ],
    }
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    processes: list[subprocess.Popen[bytes]] = []
    streams: list[Any] = []
    try:
        for name, command in commands.items():
            stream = (log_root / f"{name}.log").open("xb")
            streams.append(stream)
            processes.append(
                subprocess.Popen(
                    command,
                    cwd=backend,
                    env=dict(environment),
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    creationflags=creation_flags,
                )
            )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in processes):
                raise P6ContractError("G07_G5_FIXTURE_SERVICE_EXITED")
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8999/health", timeout=1
                ) as response:
                    if response.status == 200:
                        return processes, streams
            except (OSError, urllib.error.URLError):
                time.sleep(0.25)
        raise P6ContractError("G07_G5_FIXTURE_SERVICE_NOT_READY")
    except Exception:
        _stop_fixture_services(processes, streams)
        raise


def _stop_fixture_services(
    processes: Sequence[subprocess.Popen[bytes]], streams: Sequence[Any]
) -> None:
    for process in reversed(processes):
        if process.poll() is None:
            process.terminate()
    for process in reversed(processes):
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
    for stream in streams:
        stream.close()


def _prepare_fixture_schema(
    *, repo_root: Path, log_root: Path, environment: Mapping[str, str]
) -> None:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "scripts.migrate"],
            cwd=repo_root / "backend",
            env=dict(environment),
            capture_output=True,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P6ContractError("G07_G5_FIXTURE_SCHEMA_SETUP_FAILED") from exc
    _write_text_new(
        log_root / "schema-migrate.stdout.log",
        result.stdout.decode("utf-8", errors="replace"),
    )
    _write_text_new(
        log_root / "schema-migrate.stderr.log",
        result.stderr.decode("utf-8", errors="replace"),
    )
    if result.returncode != 0:
        raise P6ContractError("G07_G5_FIXTURE_SCHEMA_SETUP_FAILED")


def run_browser_evidence(
    *,
    output_root: Path,
    log_root: Path,
    repo_root: Path,
    database_admin_url: str,
    redis_url: str = "redis://127.0.0.1:6379",
    formal: bool = True,
    subject_commit: str | None = None,
    candidate_tree: str | None = None,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    repository = repo_root.resolve(strict=True)
    binding = _execution_binding(
        repo_root=repository,
        formal=formal,
        subject_commit=subject_commit,
        candidate_tree=candidate_tree,
    )
    if formal and command_runner is not subprocess.run:
        raise P6ContractError("G07_G5_FORMAL_INJECTION_FORBIDDEN")
    output = _require_external_empty(
        output_root, repository, "G07_G5_BROWSER_OUTPUT_NOT_EMPTY"
    )
    logs = _require_external_empty(log_root, repository, "G07_G5_BROWSER_LOG_NOT_EMPTY")
    if output == logs:
        raise P6ContractError("G07_G5_BROWSER_ROOTS_MUST_BE_DISTINCT")
    if formal:
        expected = (
            Path(str(G07_EVIDENCE_ROOT_PARENT / binding["subject_commit"]))
            / "g5/browser"
        ).resolve(strict=False)
        if output != expected:
            raise P6ContractError("G07_G5_BROWSER_OUTPUT_ROOT_INVALID")
    output.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    report_path = output / "playwright-report.json"
    database_name = _browser_database_name(binding["subject_commit"])
    database_url = asyncio.run(
        _create_browser_database(database_admin_url, database_name)
    )
    processes: list[subprocess.Popen[bytes]] = []
    streams: list[Any] = []
    try:
        service_environment = _service_environment(
            database_url=database_url,
            database_admin_url=database_admin_url,
            redis_url=redis_url,
        )
        _prepare_fixture_schema(
            repo_root=repository,
            log_root=logs,
            environment=service_environment,
        )
        processes, streams = _start_fixture_services(
            repo_root=repository,
            log_root=logs,
            environment=service_environment,
        )
        result = _run_browser_command(
            repo_root=repository,
            report_path=report_path,
            test_output=output / "test-results",
            subject_commit=binding["subject_commit"],
            command_runner=command_runner,
        )
    finally:
        _stop_fixture_services(processes, streams)
        asyncio.run(_drop_browser_database(database_admin_url, database_name))
    _write_text_new(logs / "playwright.stdout.log", result.stdout or "")
    _write_text_new(logs / "playwright.stderr.log", result.stderr or "")
    if result.returncode != 0:
        raise P6ContractError("G07_G5_BROWSER_MATRIX_FAILED")
    report = _load_object(report_path, "G07_G5_BROWSER_REPORT_INVALID")
    proof = validate_browser_report(report, binding["subject_commit"])
    receipt: dict[str, Any] = {
        "schema_version": "g07-browser-receipt-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "gate": "G5_BROWSER",
        "subject_commit": binding["subject_commit"],
        "candidate_tree": binding["candidate_tree"],
        "g07_run_spec_sha256": binding["g07_run_spec_sha256"],
        "verified_bindings": binding["verified_bindings"],
        "status": "PASS",
        "evidence_level": "LOCAL_CONTROLLED_BROWSER_FIXTURE",
        "browser_report_sha256": file_sha256(report_path),
        "stdout_sha256": hashlib.sha256((result.stdout or "").encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256((result.stderr or "").encode()).hexdigest(),
        **proof,
        "coverage": list(BROWSER_COVERAGE),
        "provider_credentials_available_to_browser": False,
        "database_source": "ISOLATED_POSTGRESQL_APPLICATION_TABLES",
        "isolated_database_destroyed_after_receipt": True,
        "fixture_service_process_count": 3,
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
        "human_evidence": False,
    }
    receipt["receipt_hash"] = digest(receipt)
    _write_json_new(output / "browser_receipt.json", receipt)
    return receipt


def _p95(values: Sequence[float]) -> float:
    if not values:
        raise P6ContractError("G07_G5_PERFORMANCE_SAMPLES_MISSING")
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)], 3)


def _number(value: object, reason: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise P6ContractError(reason)
    return float(value)


def _sanitize_chain(
    value: Mapping[str, Any], *, index: int, elapsed_ms: float
) -> dict[str, Any]:
    return {
        "chain_id": f"g07-live-chain-{index:03d}",
        "candidate_commit": value.get("candidate_commit"),
        "candidate_tree": value.get("candidate_tree"),
        "exact_model_id": value.get("exact_model_id"),
        "source_sha256": value.get("source_sha256"),
        "create_to_progress_ms": value.get("create_to_progress_ms"),
        "create_to_editable_cards_ms": value.get("create_to_editable_cards_ms"),
        "total_isolated_chain_ms": round(elapsed_ms, 3),
        "public_result_status": value.get("public_result_status"),
        "public_card_count": value.get("public_card_count"),
        "public_forbidden_key_count": value.get("public_forbidden_key_count"),
        "qwen_external_calls": value.get("qwen_external_calls"),
        "qwen_repair_calls": value.get("qwen_repair_calls"),
        "qwen_input_tokens": value.get("qwen_input_tokens"),
        "qwen_output_tokens": value.get("qwen_output_tokens"),
        "qwen_estimated_cost_cny": value.get("qwen_estimated_cost_cny"),
        "persisted_activity_count": value.get("persisted_activity_count"),
        "persisted_auto_match_count": value.get("persisted_auto_match_count"),
        "persisted_coordinate_receipt_count": value.get(
            "persisted_coordinate_receipt_count"
        ),
        "initial_map_job_count": value.get("initial_map_job_count"),
        "initial_map_terminal_status": value.get("initial_map_terminal_status"),
        "route_effect_count": value.get("route_effect_count"),
        "route_external_call_count": value.get("route_external_call_count"),
        "edit_status": value.get("edit_status"),
        "map_status_after_edit": value.get("map_status_after_edit"),
        "automatic_route_calls_after_edit": value.get(
            "automatic_route_calls_after_edit"
        ),
        "raw_request_or_response_retained": value.get(
            "raw_request_or_response_retained"
        ),
        "database_source": value.get("database_source"),
        "isolated_database_destroyed_after_receipt": value.get(
            "isolated_database_destroyed_after_receipt"
        ),
        "blind_inputs_read": value.get("blind_inputs_read"),
        "blind_truth_read": value.get("blind_truth_read"),
        "human_evidence": value.get("human_evidence"),
    }


def _validate_chain(sample: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    numeric_fields = (
        "create_to_progress_ms",
        "create_to_editable_cards_ms",
        "total_isolated_chain_ms",
        "qwen_external_calls",
        "qwen_repair_calls",
        "qwen_input_tokens",
        "qwen_output_tokens",
        "qwen_estimated_cost_cny",
        "public_card_count",
        "public_forbidden_key_count",
        "persisted_activity_count",
        "persisted_auto_match_count",
        "persisted_coordinate_receipt_count",
        "initial_map_job_count",
        "route_effect_count",
        "route_external_call_count",
        "automatic_route_calls_after_edit",
        "blind_inputs_read",
        "blind_truth_read",
    )
    for field in numeric_fields:
        _number(sample.get(field), "G07_G5_PERFORMANCE_SAMPLE_INVALID")
    if not (
        sample.get("candidate_commit") == binding["subject_commit"]
        and sample.get("candidate_tree") == binding["candidate_tree"]
        and sample.get("exact_model_id") == "qwen3.7-flash-2026-07-15"
        and isinstance(sample.get("source_sha256"), str)
        and len(str(sample["source_sha256"])) == 64
        and sample.get("public_result_status") in {"READY", "PARTIAL_RESULT"}
        and sample.get("public_card_count", 0) >= 1
        and sample.get("public_forbidden_key_count") == 0
        and 1 <= sample.get("qwen_external_calls", 0) <= 2
        and sample.get("qwen_repair_calls", 0) <= 1
        and sample.get("persisted_auto_match_count", 0) >= 2
        and sample.get("persisted_coordinate_receipt_count")
        == sample.get("persisted_auto_match_count")
        and sample.get("initial_map_job_count") == 1
        and sample.get("initial_map_terminal_status") in {"AVAILABLE", "LIMITED"}
        and sample.get("route_effect_count", 0) >= 2
        and sample.get("route_external_call_count", 0) >= 2
        and sample.get("automatic_route_calls_after_edit") == 0
        and sample.get("edit_status") == "APPLIED"
        and sample.get("map_status_after_edit") == "NEEDS_UPDATE"
        and sample.get("raw_request_or_response_retained") is False
        and sample.get("database_source")
        == "ISOLATED_POSTGRESQL_APPLICATION_TABLES"
        and sample.get("isolated_database_destroyed_after_receipt") is True
        and sample.get("blind_inputs_read") == 0
        and sample.get("blind_truth_read") == 0
        and sample.get("human_evidence") is False
    ):
        raise P6ContractError("G07_G5_PERFORMANCE_SAMPLE_INVALID")


async def _default_chain_runner(args: argparse.Namespace) -> dict[str, object]:
    return await _run_live_chain(args)


async def run_live_performance_evidence(
    *,
    output_root: Path,
    repo_root: Path,
    database_admin_url: str,
    model_role: str = "LOW_LATENCY_CANDIDATE",
    formal: bool = True,
    subject_commit: str | None = None,
    candidate_tree: str | None = None,
    chain_runner: Callable[
        [argparse.Namespace], Awaitable[dict[str, object]]
    ] = _default_chain_runner,
) -> dict[str, Any]:
    repository = repo_root.resolve(strict=True)
    binding = _execution_binding(
        repo_root=repository,
        formal=formal,
        subject_commit=subject_commit,
        candidate_tree=candidate_tree,
    )
    if formal and chain_runner is not _default_chain_runner:
        raise P6ContractError("G07_G5_FORMAL_INJECTION_FORBIDDEN")
    output = _require_external_empty(
        output_root, repository, "G07_G5_PERFORMANCE_OUTPUT_NOT_EMPTY"
    )
    if formal:
        expected = (
            Path(str(G07_EVIDENCE_ROOT_PARENT / binding["subject_commit"]))
            / "g5/live-performance"
        ).resolve(strict=False)
        if output != expected:
            raise P6ContractError("G07_G5_PERFORMANCE_OUTPUT_ROOT_INVALID")
    output.mkdir(parents=True, exist_ok=True)
    progress_path = output / "live-chain-samples.jsonl"
    runner_args = argparse.Namespace(
        database_admin_url=database_admin_url,
        model_role=model_role,
    )
    samples: list[dict[str, Any]] = []
    for index in range(1, PERFORMANCE_CHAIN_COUNT + 1):
        started = perf_counter()
        try:
            result = await chain_runner(runner_args)
            sample = _sanitize_chain(
                result,
                index=index,
                elapsed_ms=(perf_counter() - started) * 1_000,
            )
            _validate_chain(sample, binding)
        except Exception as exc:
            _append_jsonl(
                progress_path,
                {
                    "chain_id": f"g07-live-chain-{index:03d}",
                    "status": "FAILED",
                    "error_type": type(exc).__name__,
                },
            )
            if isinstance(exc, P6ContractError):
                raise
            raise P6ContractError("G07_G5_LIVE_CHAIN_FAILED") from exc
        _append_jsonl(progress_path, sample)
        samples.append(sample)
        if index % 5 == 0 or index == PERFORMANCE_CHAIN_COUNT:
            print(
                json.dumps(
                    {"completed": index, "total": PERFORMANCE_CHAIN_COUNT},
                    sort_keys=True,
                ),
                flush=True,
            )
    progress_sha256 = file_sha256(progress_path)
    progress_rows = [
        json.loads(line)
        for line in progress_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if progress_rows != samples or len(samples) != PERFORMANCE_CHAIN_COUNT:
        raise P6ContractError("G07_G5_PERFORMANCE_READBACK_MISMATCH")
    metrics = {
        "create_to_progress_p95_ms": _p95(
            [float(sample["create_to_progress_ms"]) for sample in samples]
        ),
        "create_to_editable_cards_p95_ms": _p95(
            [float(sample["create_to_editable_cards_ms"]) for sample in samples]
        ),
        "total_isolated_chain_p95_ms": _p95(
            [float(sample["total_isolated_chain_ms"]) for sample in samples]
        ),
    }
    threshold_failures = [
        key
        for key, threshold in PERFORMANCE_THRESHOLDS_MS.items()
        if metrics[key] > threshold
    ]
    receipt: dict[str, Any] = {
        "schema_version": "g07-live-performance-receipt-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "gate": "G5_LIVE_PERFORMANCE",
        "subject_commit": binding["subject_commit"],
        "candidate_tree": binding["candidate_tree"],
        "g07_run_spec_sha256": binding["g07_run_spec_sha256"],
        "verified_bindings": binding["verified_bindings"],
        "status": "PASS" if not threshold_failures else "FAIL",
        "evidence_level": "DEV_LIVE_QWEN_AMAP_POSTGRESQL_APPLICATION_CHAIN",
        "sample_count": len(samples),
        "metrics": metrics,
        "thresholds_ms": PERFORMANCE_THRESHOLDS_MS,
        "threshold_failures": threshold_failures,
        "qwen_external_call_count": sum(
            int(sample["qwen_external_calls"]) for sample in samples
        ),
        "route_external_call_count": sum(
            int(sample["route_external_call_count"]) for sample in samples
        ),
        "qwen_repair_call_count": sum(
            int(sample["qwen_repair_calls"]) for sample in samples
        ),
        "qwen_estimated_list_price_usage_cny": round(
            sum(float(sample["qwen_estimated_cost_cny"]) for sample in samples),
            8,
        ),
        "actual_incremental_billing_status": "NOT_EXPOSED_BY_PROVIDER",
        "usable_map_count": sum(
            sample["initial_map_terminal_status"] in {"AVAILABLE", "LIMITED"}
            for sample in samples
        ),
        "editable_partial_result_count": sum(
            sample["public_result_status"] == "PARTIAL_RESULT" for sample in samples
        ),
        "edit_triggered_route_call_count": sum(
            int(sample["automatic_route_calls_after_edit"]) for sample in samples
        ),
        "public_forbidden_key_count": sum(
            int(sample["public_forbidden_key_count"]) for sample in samples
        ),
        "orphan_database_count": 0,
        "sample_file_sha256": progress_sha256,
        "raw_request_or_response_retained": False,
        "blind_inputs_read": 0,
        "blind_truth_read": 0,
        "human_evidence": False,
    }
    receipt["receipt_hash"] = digest(receipt)
    _write_json_new(output / "live_performance_receipt.json", receipt)
    if threshold_failures and formal:
        raise P6ContractError("G07_G5_PERFORMANCE_THRESHOLDS_FAILED")
    return receipt
