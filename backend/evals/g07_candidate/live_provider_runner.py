from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from app.config import Settings
from evals.g07_candidate.live_spec_builder import (
    read_actual_g07_repo_state,
    validate_g07_live_provider_spec,
)
from evals.trip_check_v1.input_provider_integrity_runner import (
    live_credentials_ready,
    run_live_matrix,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest, file_sha256
from evals.trip_check_v1.p6.live_provider_runner import (
    EXPECTED_OPERATION_COUNTS,
    _external_env_file,
    _secret_leak_count,
    _settings_from_external_file,
    _validate_provider_receipts,
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
        raise P6ContractError("G07_G4_ARTIFACT_WRITE_FAILED") from exc


async def run_g07_live_provider_gate(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    repo_root: Path,
    live_env_file: Path | None = None,
    formal: bool = True,
    settings: Settings | None = None,
    live_runner: Callable[..., Awaitable[dict[str, Any]]] = run_live_matrix,
) -> dict[str, Any]:
    spec = validate_g07_live_provider_spec(
        _load_object(candidate_run_spec_path, "G07_LIVE_SPEC_INVALID")
    )
    repository = repo_root.resolve(strict=True)
    output = output_root.resolve(strict=False)
    try:
        output.relative_to(repository)
    except ValueError:
        pass
    else:
        raise P6ContractError("G07_G4_EXTERNAL_ROOT_REQUIRED")
    if formal:
        if settings is not None or live_runner is not run_live_matrix:
            raise P6ContractError("G07_G4_FORMAL_INJECTION_FORBIDDEN")
        actual = read_actual_g07_repo_state(repository)
        expected = {
            key: spec[key]
            for key in (
                "subject_commit",
                "candidate_tree",
                "upstream_ref",
                "upstream_commit",
                "dirty_tree",
            )
        }
        if actual != expected:
            raise P6ContractError("G07_G4_REPO_BINDING_INVALID")
        expected_output = (Path(spec["evidence_root"]) / "g4").resolve(
            strict=False
        )
        if output != expected_output:
            raise P6ContractError("G07_G4_OUTPUT_ROOT_INVALID")
        if output.exists() and any(output.iterdir()):
            raise P6ContractError("G07_G4_OUTPUT_NOT_EMPTY")
        current_settings = _settings_from_external_file(
            _external_env_file(live_env_file, repository)
        )
    else:
        current_settings = settings or Settings(runtime_profile="test")
    ready, _reason = live_credentials_ready(current_settings)
    if not ready:
        raise P6ContractError("G07_G4_CREDENTIALS_MISSING")
    try:
        manifest = await live_runner(
            commit_sha=spec["subject_commit"],
            output=output,
            settings=current_settings,
            max_live_calls=18,
        )
    except Exception as exc:
        raise P6ContractError("G07_G4_LIVE_EXECUTION_FAILED") from exc
    manifest_path = output / "live_provider_manifest.json"
    if _load_object(manifest_path, "G07_G4_LIVE_MANIFEST_INVALID") != manifest:
        raise P6ContractError("G07_G4_LIVE_MANIFEST_READBACK_MISMATCH")
    if not (
        manifest.get("schema_version")
        == "trip-check-p3-live-provider-manifest-v1"
        and manifest.get("subject_commit") == spec["subject_commit"]
        and manifest.get("status") == "PASS"
        and manifest.get("query_budget") == 18
        and manifest.get("actual_network_call_count") == 18
        and manifest.get("actual_receipt_count") == 18
        and manifest.get("hidden_retry_count") == 0
        and isinstance(manifest.get("cases"), list)
        and len(manifest["cases"]) == 3
        and all(
            item.get("status") == "PASS"
            and item.get("receipt_count") == 6
            and item.get("failure_categories") == []
            for item in manifest["cases"]
        )
    ):
        raise P6ContractError("G07_G4_LIVE_MANIFEST_INVALID")
    operations, receipt_count = _validate_provider_receipts(output)
    if dict(operations) != EXPECTED_OPERATION_COUNTS or receipt_count != 18:
        raise P6ContractError("G07_G4_OPERATION_SET_INVALID")
    if _secret_leak_count(output, current_settings):
        raise P6ContractError("G07_G4_SECRET_LEAK_DETECTED")
    binding_readback = {
        "schema_version": "g07-live-provider-binding-readback-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "subject_commit": spec["subject_commit"],
        "candidate_tree": spec["candidate_tree"],
        "run_spec_hash": spec["run_spec_hash"],
        "g07_run_spec_sha256": spec["g07_run_spec_sha256"],
        "live_manifest_file_sha256": file_sha256(manifest_path),
        "operation_counts": dict(sorted(operations.items())),
        "provider_receipt_count": receipt_count,
        "fixture_fallback_count": 0,
        "secret_leak_count": 0,
    }
    binding_readback["receipt_hash"] = digest(binding_readback)
    _write_json_new(output / "g4_binding_readback.json", binding_readback)
    receipt = {
        "schema_version": "g07-live-provider-gate-receipt-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "gate": "G4_LIVE_PROVIDER_MAP_WEATHER",
        "subject_commit": spec["subject_commit"],
        "candidate_tree": spec["candidate_tree"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "LIVE_PROVIDER_EVIDENCE",
        "metrics": {
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
        },
        "claim_boundary": "MAP_ROUTE_AND_WEATHER_ONLY_QWEN_IS_SEPARATE",
    }
    receipt["receipt_hash"] = digest(receipt)
    _write_json_new(output / "g4_receipt.json", receipt)
    return receipt
