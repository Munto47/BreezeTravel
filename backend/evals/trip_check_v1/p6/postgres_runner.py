"""Fail-closed P6 G2 PostgreSQL evidence runner."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncpg

from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
)


G2_TEST_NODES = (
    "tests/test_migrations_integration.py::test_fresh_and_existing_database_migrations",
    "tests/test_creation_idempotency_postgres.py::test_postgres_creation_command_race_rollback_replay_and_import_pointer_delete",
    "tests/test_trip_check_postgres_integration.py::test_postgres_trip_check_termination_restart_replay_and_lease_cas",
    "tests/test_dual_entry_postgres_integration.py::test_postgres_import_audit_repair_apply_and_restart_readback",
    "tests/test_import_mobile_postgres.py::test_postgres_import_compare_and_set_and_apply_replay",
    "tests/test_repair_concurrency_postgres.py::test_postgres_apply_and_reject_choose_exactly_one_terminal_state",
    "tests/test_screenshot_postgres_integration.py::test_postgres_screenshot_cleanup_and_ocr_artifact_readback",
)
LATEST_MIGRATION = "024_advice_bundles.sql"


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def migration_fingerprint(repo_root: Path) -> tuple[str, dict[str, Any]]:
    backend_root = repo_root.resolve(strict=True) / "backend"
    init_path = backend_root / "app" / "db" / "init.sql"
    migration_root = backend_root / "app" / "db" / "migrations"
    try:
        migrations = sorted(migration_root.glob("*.sql"))
        if not init_path.is_file() or not migrations or migrations[-1].name != LATEST_MIGRATION:
            raise OSError("migration set is incomplete")
        manifest = {
            "schema_version": "trip-check-p6-migration-fingerprint-v1",
            "init_sql_sha256": hashlib.sha256(init_path.read_bytes()).hexdigest(),
            "migrations": [
                {
                    "filename": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in migrations
            ],
            "required_migration": LATEST_MIGRATION,
        }
    except OSError as exc:
        raise P6ContractError("P6_G2_MIGRATION_FINGERPRINT_FAILED") from exc
    return digest(manifest), manifest


def _validate_admin_url(value: str | None) -> str:
    if not value:
        raise P6ContractError("P6_G2_DATABASE_URL_MISSING")
    parsed = urlparse(value)
    if (
        parsed.scheme != "postgresql"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.path != "/postgres"
        or parsed.query
        or parsed.fragment
        or not parsed.username
        or not parsed.password
    ):
        raise P6ContractError("P6_G2_DATABASE_NOT_ISOLATED")
    return value


async def _database_readback(admin_url: str) -> dict[str, Any]:
    try:
        connection = await asyncpg.connect(admin_url, timeout=10)
        try:
            row = await connection.fetchrow(
                "SELECT current_database() AS database_name, "
                "current_setting('server_version_num') AS server_version_num, "
                "inet_server_addr()::text AS server_address"
            )
        finally:
            await connection.close()
    except Exception as exc:
        raise P6ContractError("P6_G2_DATABASE_READBACK_FAILED") from exc
    if (
        row is None
        or row["database_name"] != "postgres"
        or row["server_address"] not in {"127.0.0.1", "::1"}
        or not str(row["server_version_num"]).isdigit()
    ):
        raise P6ContractError("P6_G2_DATABASE_NOT_ISOLATED")
    return {
        "database_name": "postgres",
        "server_address_class": "LOOPBACK",
        "server_version_num": str(row["server_version_num"]),
    }


def _redact(value: str, secrets: Sequence[str]) -> str:
    result = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result


def _write_new(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
    except OSError as exc:
        raise P6ContractError("P6_G2_ARTIFACT_WRITE_FAILED") from exc


def _junit_counts(path: Path) -> dict[str, int]:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ET.ParseError) as exc:
        raise P6ContractError("P6_G2_JUNIT_INVALID") from exc
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        raise P6ContractError("P6_G2_JUNIT_INVALID")
    counts = {
        key: sum(int(suite.attrib.get(key, "0")) for suite in suites)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if counts["tests"] != len(G2_TEST_NODES):
        raise P6ContractError("P6_G2_TEST_SET_MISMATCH")
    return counts


async def run_postgres_gate(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    log_root: Path,
    repo_root: Path,
    database_admin_url: str | None,
    formal: bool = True,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    database_readback: Mapping[str, Any] | None = None,
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
            raise P6ContractError("P6_G2_EXTERNAL_ROOT_REQUIRED")
    if output_resolved == log_resolved:
        raise P6ContractError("P6_G2_ROOTS_MUST_BE_DISTINCT")
    if formal:
        actual = read_actual_repo_state(repo_resolved)
        expected = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if actual != expected:
            raise P6ContractError("P6_G2_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g2").resolve(strict=False):
            raise P6ContractError("P6_G2_OUTPUT_ROOT_INVALID")
        if output_resolved.exists() and any(output_resolved.iterdir()):
            raise P6ContractError("P6_G2_OUTPUT_NOT_EMPTY")
        if log_resolved.exists() and any(log_resolved.iterdir()):
            raise P6ContractError("P6_G2_LOG_ROOT_NOT_EMPTY")
    admin_url = _validate_admin_url(database_admin_url)
    parsed = urlparse(admin_url)
    readback = dict(database_readback or await _database_readback(admin_url))
    if (
        readback.get("database_name") != "postgres"
        or readback.get("server_address_class") != "LOOPBACK"
        or not str(readback.get("server_version_num", "")).isdigit()
    ):
        raise P6ContractError("P6_G2_DATABASE_NOT_ISOLATED")
    migration_sha, migration_manifest = migration_fingerprint(repo_resolved)
    if (
        migration_sha != spec["bindings"]["migration_manifest_sha256"]
        or spec["database"]["required_migration"] != LATEST_MIGRATION
    ):
        raise P6ContractError("P6_G2_MIGRATION_BINDING_INVALID")

    log_resolved.mkdir(parents=True, exist_ok=True)
    junit_path = log_resolved / "g2-junit.xml"
    environment = os.environ.copy()
    environment.update(
        {
            "RUN_SERVICE_INTEGRATION": "1",
            "TEST_DATABASE_ADMIN_URL": admin_url,
            "RUNTIME_PROFILE": "test",
            "AMAP_MOCK": "true",
            "FT_ROUTER_ENABLED": "false",
        }
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        *G2_TEST_NODES,
        "-q",
        "--disable-warnings",
        f"--junitxml={junit_path}",
    ]
    try:
        completed = command_runner(
            command,
            cwd=repo_resolved / "backend",
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=900,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise P6ContractError("P6_G2_TEST_EXECUTION_FAILED") from exc
    secrets = [admin_url, parsed.password or ""]
    stdout = _redact(completed.stdout or "", secrets)
    stderr = _redact(completed.stderr or "", secrets)
    _write_new(log_resolved / "pytest.stdout.log", stdout)
    _write_new(log_resolved / "pytest.stderr.log", stderr)
    if junit_path.exists():
        sanitized = _redact(junit_path.read_text(encoding="utf-8"), secrets)
        junit_path.write_text(sanitized, encoding="utf-8", newline="\n")
    if any(secret and secret in text for secret in secrets for text in (stdout, stderr)):
        raise P6ContractError("P6_G2_SECRET_REDACTION_FAILED")
    counts = _junit_counts(junit_path)
    if completed.returncode != 0 or any(counts[key] for key in ("failures", "errors", "skipped")):
        raise P6ContractError("P6_G2_TEST_MATRIX_FAILED")

    metrics = {
        "migration_failure_count": 0,
        "transaction_failure_count": 0,
        "restart_readback_failure_count": 0,
        "concurrency_failure_count": 0,
        "lease_takeover_failure_count": 0,
        "old_data_compatibility_failure_count": 0,
        "screenshot_cleanup_failure_count": 0,
        "idempotency_replay_failure_count": 0,
        "cas_conflict_failure_count": 0,
        "postgres_test_count": counts["tests"],
        "postgres_server_version_num": int(readback["server_version_num"]),
        "database_isolated": 1,
        "migration_file_count": len(migration_manifest["migrations"]),
    }
    receipt = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g2",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "postgresql_integration",
        "checks_total": len(G2_TEST_NODES) + 6,
        "checks_passed": len(G2_TEST_NODES) + 6,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt = validate_gate_receipt(receipt, "g2", spec)
    output_resolved.mkdir(parents=True, exist_ok=True)
    _write_new(
        output_resolved / "g2_receipt.json",
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
    )
    return receipt
