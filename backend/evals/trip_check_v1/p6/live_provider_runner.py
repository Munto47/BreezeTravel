"""Fail-closed P6 G4 fixed 18-call live Provider runner."""

from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.config import Settings
from app.trip_check.provider_integrity import ProviderCallReceipt
from evals.trip_check_v1.input_provider_integrity_runner import (
    live_credentials_ready,
    run_live_matrix,
)
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    file_sha256,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
)


CITIES = ("北京", "上海", "杭州")
EXPECTED_OPERATION_COUNTS = {
    "route.walking": 3,
    "route.transit": 3,
    "route.bicycling": 3,
    "route.driving": 3,
    "weather.daily": 3,
    "risk.weather_alert": 3,
}
PROVIDER_ENV_KEYS = (
    "AMAP_API_KEY",
    "QWEATHER_API_KEY",
    "QWEATHER_PRIVATE_KEY",
    "QWEATHER_KEY_ID",
    "QWEATHER_PROJECT_ID",
    "QWEATHER_AUTH_TYPE",
    "QWEATHER_API_HOST",
)


def _load_json(path: Path, reason: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc


def _write_json_new(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except OSError as exc:
        raise P6ContractError("P6_G4_ARTIFACT_WRITE_FAILED") from exc


def _external_env_file(path: Path | None, repo_root: Path) -> Path:
    if path is None:
        raise P6ContractError("P6_G4_LIVE_ENV_FILE_REQUIRED")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve(strict=True))
    except ValueError:
        if resolved.is_file():
            return resolved
    except (OSError, RuntimeError) as exc:
        raise P6ContractError("P6_G4_LIVE_ENV_FILE_INVALID") from exc
    raise P6ContractError("P6_G4_LIVE_ENV_FILE_INVALID")


@contextmanager
def _provider_env_isolation() -> Any:
    saved = {key: os.environ.get(key) for key in PROVIDER_ENV_KEYS}
    for key in PROVIDER_ENV_KEYS:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _settings_from_external_file(path: Path) -> Settings:
    try:
        with _provider_env_isolation():
            return Settings(_env_file=path)
    except Exception as exc:
        raise P6ContractError("P6_G4_LIVE_SETTINGS_INVALID") from exc


def _credential_values(settings: Settings) -> list[str]:
    return [
        settings.amap_api_key,
        settings.qweather_api_key,
        settings.qweather_private_key,
        settings.qweather_key_id,
        settings.qweather_project_id,
    ]


def _secret_leak_count(root: Path, settings: Settings) -> int:
    secrets = [value.encode("utf-8") for value in _credential_values(settings) if len(value) >= 6]
    leaks = 0
    try:
        for path in (item for item in root.rglob("*") if item.is_file()):
            content = path.read_bytes()
            leaks += sum(secret in content for secret in secrets)
    except OSError as exc:
        raise P6ContractError("P6_G4_SECRET_SCAN_FAILED") from exc
    return leaks


def _validate_provider_receipts(output_root: Path) -> tuple[Counter[str], int]:
    operations: Counter[str] = Counter()
    receipt_count = 0
    for city in CITIES:
        value = _load_json(
            output_root / "live" / city / "provider_receipts.json",
            "P6_G4_PROVIDER_RECEIPTS_INVALID",
        )
        if not isinstance(value, list) or len(value) != 6:
            raise P6ContractError("P6_G4_PROVIDER_RECEIPTS_INVALID")
        city_operations: set[str] = set()
        for raw in value:
            try:
                receipt = ProviderCallReceipt.model_validate(raw)
            except Exception as exc:
                raise P6ContractError("P6_G4_PROVIDER_RECEIPTS_INVALID") from exc
            expected_provider = (
                "amap"
                if receipt.operation.startswith("route.")
                else "qweather"
                if receipt.operation == "weather.daily"
                else "qweather_alert"
            )
            parsed = urlparse(receipt.source_url)
            if not (
                receipt.execution_mode == "live"
                and receipt.status == "SUCCEEDED"
                and receipt.failure_category is None
                and receipt.response_hash is not None
                and receipt.provider == expected_provider
                and parsed.scheme == "https"
                and parsed.hostname
                and not parsed.query
                and "fixture" not in receipt.source_url.casefold()
                and "snapshot" not in receipt.source_url.casefold()
            ):
                raise P6ContractError("P6_G4_PROVIDER_RECEIPTS_INVALID")
            operations[receipt.operation] += 1
            city_operations.add(receipt.operation)
            receipt_count += 1
        if city_operations != set(EXPECTED_OPERATION_COUNTS):
            raise P6ContractError("P6_G4_OPERATION_SET_INVALID")
    if dict(operations) != EXPECTED_OPERATION_COUNTS:
        raise P6ContractError("P6_G4_OPERATION_SET_INVALID")
    return operations, receipt_count


async def run_live_provider_gate(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    repo_root: Path,
    live_env_file: Path | None = None,
    formal: bool = True,
    settings: Settings | None = None,
    live_runner: Callable[..., Awaitable[dict[str, Any]]] = run_live_matrix,
) -> dict[str, Any]:
    raw_spec = _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    if not isinstance(raw_spec, Mapping):
        raise P6ContractError("P6_CANDIDATE_RUN_SPEC_INVALID")
    spec = validate_candidate_run_spec(raw_spec)
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_G4_EXTERNAL_ROOT_REQUIRED")
    if formal:
        if settings is not None or live_runner is not run_live_matrix:
            raise P6ContractError("P6_G4_FORMAL_INJECTION_FORBIDDEN")
        actual = read_actual_repo_state(repo_resolved)
        expected = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if actual != expected:
            raise P6ContractError("P6_G4_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g4").resolve(strict=False):
            raise P6ContractError("P6_G4_OUTPUT_ROOT_INVALID")
        if output_resolved.exists() and any(output_resolved.iterdir()):
            raise P6ContractError("P6_G4_OUTPUT_NOT_EMPTY")
        current_settings = _settings_from_external_file(
            _external_env_file(live_env_file, repo_resolved)
        )
    else:
        current_settings = settings or Settings(runtime_profile="test")
    ready, _ = live_credentials_ready(current_settings)
    if not ready:
        raise P6ContractError("P6_G4_CREDENTIALS_MISSING")
    matrix = spec["provider_live_matrix"]
    if matrix != {
        "amap_route_calls": 12,
        "qweather_forecast_calls": 3,
        "qweather_alert_calls": 3,
        "max_calls": 18,
        "retry_budget": 0,
        "fixture_fallback_required_zero": True,
    }:
        raise P6ContractError("P6_G4_MATRIX_BINDING_INVALID")
    try:
        manifest = await live_runner(
            commit_sha=spec["subject_commit"],
            output=output_resolved,
            settings=current_settings,
            max_live_calls=18,
        )
    except Exception as exc:
        raise P6ContractError("P6_G4_LIVE_EXECUTION_FAILED") from exc
    manifest_path = output_resolved / "live_provider_manifest.json"
    persisted_manifest = _load_json(manifest_path, "P6_G4_LIVE_MANIFEST_INVALID")
    if persisted_manifest != manifest:
        raise P6ContractError("P6_G4_LIVE_MANIFEST_READBACK_MISMATCH")
    if not (
        manifest.get("schema_version") == "trip-check-p3-live-provider-manifest-v1"
        and manifest.get("subject_commit") == spec["subject_commit"]
        and manifest.get("status") == "PASS"
        and manifest.get("query_budget") == 18
        and manifest.get("actual_network_call_count") == 18
        and manifest.get("actual_receipt_count") == 18
        and manifest.get("hidden_retry_count") == 0
        and isinstance(manifest.get("cases"), list)
        and len(manifest["cases"]) == 3
        and {item.get("city") for item in manifest["cases"]} == set(CITIES)
        and all(
            item.get("status") == "PASS"
            and item.get("receipt_count") == 6
            and item.get("failure_categories") == []
            for item in manifest["cases"]
        )
    ):
        raise P6ContractError("P6_G4_LIVE_MANIFEST_INVALID")
    operations, receipt_count = _validate_provider_receipts(output_resolved)
    secret_leaks = _secret_leak_count(output_resolved, current_settings)
    if secret_leaks:
        raise P6ContractError("P6_G4_SECRET_LEAK_DETECTED")
    binding_readback = {
        "schema_version": "trip-check-p6-g4-binding-readback-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "live_manifest_file_sha256": file_sha256(manifest_path),
        "operation_counts": dict(sorted(operations.items())),
        "provider_receipt_count": receipt_count,
        "fixture_fallback_count": 0,
        "secret_leak_count": 0,
    }
    binding_readback["receipt_hash"] = digest(binding_readback)
    _write_json_new(output_resolved / "g4_binding_readback.json", binding_readback)
    metrics = {
        "network_call_count": 18,
        "provider_receipt_count": 18,
        "amap_route_call_count": 12,
        "qweather_forecast_call_count": 3,
        "qweather_alert_call_count": 3,
        "fixture_fallback_count": 0,
        "provider_failure_count": 0,
        "hidden_retry_count": 0,
        "secret_leak_count": 0,
        "city_count": 3,
        "operation_count": 6,
    }
    receipt = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g4",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "live_provider",
        "checks_total": 14,
        "checks_passed": 14,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt = validate_gate_receipt(receipt, "g4", spec)
    _write_json_new(output_resolved / "g4_receipt.json", receipt)
    return receipt
