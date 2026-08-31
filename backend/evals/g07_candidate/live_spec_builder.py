from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.trip_check.provider_integrity import provider_snapshot_sha256
from evals.trip_check_v1.p6.contracts_v1 import (
    G07_EVIDENCE_ROOT_PARENT,
    G07_UPSTREAM_REF,
    P5_GATE_MANIFEST_HASH,
    P6ContractError,
    digest,
    file_sha256,
    read_actual_repo_state,
    validate_candidate_run_spec,
)


G07_RUN_SPEC_RELATIVE = "backend/eval_data/g07_candidate/run_spec_v1.json"
G07_REMOTE_REF = "refs/heads/codex/g07-candidate"
G07_LATEST_MIGRATION = "034_trip_understanding_screenshot_batches.sql"


def _load_object(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P6ContractError(reason) from exc
    if not isinstance(value, dict):
        raise P6ContractError(reason)
    return value


def _write_json_new(path: Path, value: dict[str, Any]) -> None:
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
        raise P6ContractError("G07_LIVE_SPEC_WRITE_FAILED") from exc


def _require_external_empty_output(output_root: Path, repo_root: Path) -> Path:
    output = output_root.resolve(strict=False)
    repository = repo_root.resolve(strict=True)
    try:
        output.relative_to(repository)
    except ValueError:
        pass
    else:
        raise P6ContractError("G07_LIVE_SPEC_EXTERNAL_ROOT_REQUIRED")
    if output.exists() and any(output.iterdir()):
        raise P6ContractError("G07_LIVE_SPEC_OUTPUT_NOT_EMPTY")
    return output


def _verify_bound_path(repo_root: Path, binding: dict[str, Any]) -> str:
    relative = binding.get("path")
    expected = binding.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise P6ContractError("G07_LIVE_SPEC_BINDING_INVALID")
    path = repo_root / relative
    if file_sha256(path) != expected:
        raise P6ContractError("G07_LIVE_SPEC_BINDING_DRIFT")
    return expected


def _verify_g07_contract(repo_root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    run_spec_path = repo_root / G07_RUN_SPEC_RELATIVE
    run_spec = _load_object(run_spec_path, "G07_LIVE_SPEC_CONTRACT_INVALID")
    candidate = run_spec.get("candidate_subject")
    if not (
        run_spec.get("schema_version") == "g07-candidate-run-spec-v1"
        and run_spec.get("goal_id") == "TC-VNEXT-G07-CANDIDATE"
        and run_spec.get("freeze_status") == "FROZEN_INPUT_CONTRACT"
        and isinstance(candidate, dict)
        and candidate.get("branch") == "codex/g07-candidate"
        and candidate.get("remote_ref") == G07_REMOTE_REF
        and candidate.get("cross_commit_evidence_reuse") is False
    ):
        raise P6ContractError("G07_LIVE_SPEC_CONTRACT_INVALID")

    contracts = run_spec.get("contract_bindings")
    evaluations = run_spec.get("evaluation_bindings")
    if not isinstance(contracts, dict) or not isinstance(evaluations, dict):
        raise P6ContractError("G07_LIVE_SPEC_CONTRACT_INVALID")
    required = {
        "candidate_gate": _verify_bound_path(
            repo_root, contracts["automated_candidate_gate"]
        ),
        "product_policy": _verify_bound_path(
            repo_root, contracts["product_delivery_policy"]
        ),
        "latest_migration": _verify_bound_path(
            repo_root, contracts["latest_migration"]
        ),
        "text_card_dataset": _verify_bound_path(
            repo_root, evaluations["text_card_90_case_contract"]
        ),
        "trip_nlu_candidate": _verify_bound_path(
            repo_root, evaluations["trip_nlu_120_case_candidate"]
        ),
        "provider_binding": _verify_bound_path(
            repo_root, evaluations["provider_binding"]
        ),
        "model_panel": _verify_bound_path(repo_root, evaluations["model_panel"]),
        "model_runtime": _verify_bound_path(
            repo_root, evaluations["model_runtime"]
        ),
    }
    latest = contracts["latest_migration"]
    if (
        latest.get("path")
        != f"backend/app/db/migrations/{G07_LATEST_MIGRATION}"
        or latest.get("append_only_range") != "001-034"
    ):
        raise P6ContractError("G07_LIVE_SPEC_MIGRATION_BINDING_INVALID")
    return run_spec, required


def _migration_manifest(repo_root: Path) -> dict[str, Any]:
    migration_root = repo_root / "backend/app/db/migrations"
    files = sorted(migration_root.glob("*.sql"))
    if len(files) != 34 or files[-1].name != G07_LATEST_MIGRATION:
        raise P6ContractError("G07_LIVE_SPEC_MIGRATION_BINDING_INVALID")
    return {
        "schema_version": "g07-live-migration-manifest-v1",
        "file_count": len(files),
        "files": [
            {
                "path": path.relative_to(repo_root).as_posix(),
                "sha256": file_sha256(path),
            }
            for path in files
        ],
    }


def build_g07_live_provider_spec(
    *,
    output_root: Path,
    repo_root: Path,
    formal: bool = True,
    subject_commit: str | None = None,
) -> tuple[dict[str, Any], Path]:
    repository = repo_root.resolve(strict=True)
    output = _require_external_empty_output(output_root, repository)
    run_spec, bindings = _verify_g07_contract(repository)

    if formal:
        if subject_commit is not None:
            raise P6ContractError("G07_LIVE_SPEC_FORMAL_INJECTION_FORBIDDEN")
        state = read_actual_repo_state(repository)
        if (
            state["dirty_tree"]
            or state["upstream_ref"] != G07_UPSTREAM_REF
            or state["subject_commit"] != state["upstream_commit"]
        ):
            raise P6ContractError("G07_LIVE_SPEC_REPO_BINDING_INVALID")
        subject = state["subject_commit"]
        expected = Path(str(G07_EVIDENCE_ROOT_PARENT / subject)) / "inputs"
        if output != expected.resolve(strict=False):
            raise P6ContractError("G07_LIVE_SPEC_OUTPUT_ROOT_INVALID")
    else:
        subject = subject_commit or "a" * 40
        state = {
            "subject_commit": subject,
            "upstream_ref": G07_UPSTREAM_REF,
            "upstream_commit": subject,
            "dirty_tree": False,
        }

    migration_manifest = _migration_manifest(repository)
    migration_path = output / "migration_manifest.json"
    adapter_manifest = {
        "schema_version": "g07-live-provider-adapter-manifest-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        "subject_commit": subject,
        "g07_run_spec_path": G07_RUN_SPEC_RELATIVE,
        "g07_run_spec_sha256": file_sha256(repository / G07_RUN_SPEC_RELATIVE),
        "verified_bindings": dict(sorted(bindings.items())),
        "provider_snapshot_sha256": provider_snapshot_sha256(),
        "claim_boundary": "LIVE_PROVIDER_INPUT_ADAPTER_NOT_A_PROVIDER_RECEIPT",
    }
    adapter_path = output / "g07_live_adapter_manifest.json"
    _write_json_new(migration_path, migration_manifest)
    _write_json_new(adapter_path, adapter_manifest)

    evidence_root = str(G07_EVIDENCE_ROOT_PARENT / subject).replace("\\", "/")
    spec: dict[str, Any] = {
        "schema_version": "trip-check-p6-candidate-run-spec-v1",
        **state,
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "single_city": True,
            "group_size": {"min": 2, "max": 5},
            "trip_days": {"min": 2, "max": 5},
            "input_types": ["TEXT", "SCREENSHOT"],
        },
        "bindings": {
            "config_sha256": adapter_manifest["g07_run_spec_sha256"],
            "ocr_dataset_manifest_sha256": bindings["text_card_dataset"],
            "model_manifest_sha256": bindings["model_panel"],
            "rule_manifest_sha256": bindings["product_policy"],
            "snapshot_manifest_sha256": adapter_manifest[
                "provider_snapshot_sha256"
            ],
            "migration_manifest_sha256": file_sha256(migration_path),
        },
        "provider_live_matrix": {
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "max_calls": 18,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "database": {
            "engine": "postgresql",
            "required_migration": G07_LATEST_MIGRATION,
            "isolated": True,
            "migration_hash_readback_required": True,
        },
        "public_candidate": {
            "base_url": "https://www.breezetravel.cn",
            "controlled_snapshot_only": True,
            "health_path": "/health",
            "evidence_path": "/api/evidence/latest",
        },
        "evidence_root": evidence_root,
        "human_evidence": False,
    }
    spec["run_spec_hash"] = digest(spec)
    spec = validate_candidate_run_spec(spec)
    spec_path = output / "candidate_run_spec.json"
    _write_json_new(spec_path, spec)
    if _load_object(spec_path, "G07_LIVE_SPEC_READBACK_FAILED") != spec:
        raise P6ContractError("G07_LIVE_SPEC_READBACK_FAILED")
    return spec, spec_path
