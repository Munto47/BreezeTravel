"""Build the immutable, same-subject P6 CandidateRunSpec input bundle."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.trip_check.provider_integrity import DEFAULT_SNAPSHOT_PATH, provider_snapshot_sha256
from evals.trip_check_v1.p6.contracts_v1 import (
    P5_GATE_MANIFEST_HASH,
    P6ContractError,
    P6_EVIDENCE_ROOT_PARENT,
    P6_UPSTREAM_REF,
    canonical_bytes,
    digest,
    file_sha256,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_real_ocr_dataset_manifest,
)
from evals.trip_check_v1.p6.postgres_runner import migration_fingerprint


CONFIG_PATHS = (
    ".env.example",
    "docker-compose.yml",
    "backend/Dockerfile",
    "backend/app/config.py",
    "frontend/Dockerfile",
    "frontend/package-lock.json",
)
MODEL_PATHS = (
    "backend/app/importing/screenshots.py",
    "backend/evals/trip_check_v1/p6/real_ocr_runner.py",
    "backend/requirements.txt",
)
RULE_ROOTS = (
    "backend/app/audit",
    "backend/app/repairs",
)
RULE_FILES = (
    "backend/app/trip_check/advice.py",
    "backend/app/trip_check/provider_integrity.py",
)


def _load_json(path: Path, reason: str) -> dict[str, Any]:
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
        with path.open("xb") as stream:
            stream.write(canonical_bytes(value))
    except OSError as exc:
        raise P6ContractError("P6_CANDIDATE_INPUT_WRITE_FAILED") from exc


def _external_file(path: Path, repo_root: Path, reason: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve(strict=True))
    except ValueError:
        if resolved.is_file():
            return resolved
    except (OSError, RuntimeError) as exc:
        raise P6ContractError(reason) from exc
    raise P6ContractError(reason)


def _validate_p5_gate_manifest(path: Path) -> None:
    value = _load_json(path, "P6_P5_GATE_MANIFEST_INVALID")
    claimed_hash = value.get("manifest_hash")
    if not (
        claimed_hash == P5_GATE_MANIFEST_HASH
        and digest({key: item for key, item in value.items() if key != "manifest_hash"})
        == claimed_hash
        and value.get("schema_version") == "trip-check-p5-evaluation-gate-v5"
        and value.get("status") == "PASS"
        and value.get("dirty_tree") is False
        and value.get("promotion_decision")
        in {"KEEP_CORE_B", "PROMOTE_ADMITTED_CHALLENGER"}
    ):
        raise P6ContractError("P6_P5_GATE_BINDING_INVALID")


def _file_entries(repo_root: Path, relative_paths: Sequence[str]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        for relative in sorted(set(relative_paths)):
            path = repo_root / relative
            raw = path.read_bytes()
            entries.append({
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            })
    except OSError as exc:
        raise P6ContractError("P6_CANDIDATE_INPUT_FILE_UNREADABLE") from exc
    return entries


def _rule_paths(repo_root: Path) -> list[str]:
    values = list(RULE_FILES)
    for relative_root in RULE_ROOTS:
        root = repo_root / relative_root
        try:
            paths = sorted(path for path in root.rglob("*.py") if path.is_file())
        except OSError as exc:
            raise P6ContractError("P6_CANDIDATE_RULE_SCAN_FAILED") from exc
        values.extend(path.relative_to(repo_root).as_posix() for path in paths)
    return sorted(set(values))


def _manifest(schema_version: str, entries: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    if not entries:
        raise P6ContractError("P6_CANDIDATE_INPUT_MANIFEST_EMPTY")
    return {
        "schema_version": schema_version,
        **extra,
        "file_count": len(entries),
        "files": entries,
    }


def build_candidate_run_spec(
    *,
    ocr_dataset_manifest_path: Path,
    p5_gate_manifest_path: Path,
    output_root: Path,
    repo_root: Path,
    formal: bool = True,
    subject_commit: str | None = None,
    snapshot_hasher: Callable[[], str] = provider_snapshot_sha256,
    migration_builder: Callable[[Path], tuple[str, dict[str, Any]]] = migration_fingerprint,
    p5_validator: Callable[[Path], None] = _validate_p5_gate_manifest,
) -> tuple[dict[str, Any], Path]:
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_CANDIDATE_INPUT_EXTERNAL_ROOT_REQUIRED")

    if formal:
        if (
            subject_commit is not None
            or snapshot_hasher is not provider_snapshot_sha256
            or migration_builder is not migration_fingerprint
            or p5_validator is not _validate_p5_gate_manifest
        ):
            raise P6ContractError("P6_CANDIDATE_INPUT_FORMAL_INJECTION_FORBIDDEN")
        repo_state = read_actual_repo_state(repo_resolved)
        if repo_state["dirty_tree"] or repo_state["upstream_ref"] != P6_UPSTREAM_REF or repo_state["subject_commit"] != repo_state["upstream_commit"]:
            raise P6ContractError("P6_CANDIDATE_INPUT_REPO_BINDING_INVALID")
        subject = repo_state["subject_commit"]
        expected_root = Path(str(P6_EVIDENCE_ROOT_PARENT / subject)) / "inputs"
        if output_resolved != expected_root.resolve(strict=False):
            raise P6ContractError("P6_CANDIDATE_INPUT_OUTPUT_ROOT_INVALID")
    else:
        subject = subject_commit or "a" * 40
        repo_state = {
            "subject_commit": subject,
            "upstream_ref": P6_UPSTREAM_REF,
            "upstream_commit": subject,
            "dirty_tree": False,
        }
    if output_resolved.exists() and any(output_resolved.iterdir()):
        raise P6ContractError("P6_CANDIDATE_INPUT_OUTPUT_NOT_EMPTY")

    p5_manifest = _external_file(
        p5_gate_manifest_path, repo_resolved, "P6_P5_GATE_MANIFEST_INVALID"
    )
    p5_validator(p5_manifest)
    dataset_path = _external_file(
        ocr_dataset_manifest_path, repo_resolved, "P6_REAL_OCR_DATASET_INVALID"
    )
    dataset = validate_real_ocr_dataset_manifest(
        _load_json(dataset_path, "P6_REAL_OCR_DATASET_INVALID")
    )
    if dataset["subject_commit"] != subject:
        raise P6ContractError("P6_REAL_OCR_RUN_SPEC_BINDING_INVALID")

    config_manifest = _manifest(
        "trip-check-p6-config-input-manifest-v1",
        _file_entries(repo_resolved, CONFIG_PATHS),
        runtime_profile="candidate_controlled_snapshot",
        provider_mode="local_fixture",
        amap_mock=True,
        ft_router_enabled=False,
        demo_mode=False,
        dev_login_bypass=False,
        candidate_evidence_mode="external_read_only",
    )
    model_manifest = _manifest(
        "trip-check-p6-model-input-manifest-v1",
        _file_entries(repo_resolved, MODEL_PATHS),
        audit_runtime_llm=False,
        ocr_engine="paddleocr",
        ocr_engine_version="3.7.0",
        ocr_config_sha256=dataset["ocr_config_sha256"],
    )
    rule_manifest = _manifest(
        "trip-check-p6-rule-input-manifest-v1",
        _file_entries(repo_resolved, _rule_paths(repo_resolved)),
        audit_authority="AuditEngine",
    )
    migration_sha, migration_manifest = migration_builder(repo_resolved)
    snapshot_sha = snapshot_hasher()
    if snapshot_sha != file_sha256(DEFAULT_SNAPSHOT_PATH):
        raise P6ContractError("P6_CANDIDATE_SNAPSHOT_BINDING_INVALID")
    snapshot_manifest = {
        "schema_version": "trip-check-p6-snapshot-input-manifest-v1",
        "source_path": DEFAULT_SNAPSHOT_PATH.relative_to(repo_resolved).as_posix(),
        "source_sha256": snapshot_sha,
        "execution_mode": "snapshot",
        "network_required": False,
    }

    manifests = {
        "config_manifest.json": config_manifest,
        "model_manifest.json": model_manifest,
        "rule_manifest.json": rule_manifest,
        "migration_manifest.json": migration_manifest,
        "snapshot_manifest.json": snapshot_manifest,
    }
    for filename, value in manifests.items():
        _write_json_new(output_resolved / filename, value)
    manifest_shas = {
        filename: file_sha256(output_resolved / filename) for filename in manifests
    }
    if manifest_shas["migration_manifest.json"] != migration_sha:
        raise P6ContractError("P6_CANDIDATE_MIGRATION_BINDING_INVALID")

    evidence_root = str(P6_EVIDENCE_ROOT_PARENT / subject).replace("\\", "/")
    spec: dict[str, Any] = {
        "schema_version": "trip-check-p6-candidate-run-spec-v1",
        **repo_state,
        "p5_gate_manifest_hash": P5_GATE_MANIFEST_HASH,
        "scope": {
            "cities": ["北京", "上海", "杭州"],
            "single_city": True,
            "group_size": {"min": 2, "max": 5},
            "trip_days": {"min": 2, "max": 5},
            "input_types": ["TEXT", "SCREENSHOT"],
        },
        "bindings": {
            "config_sha256": manifest_shas["config_manifest.json"],
            "ocr_dataset_manifest_sha256": file_sha256(dataset_path),
            "model_manifest_sha256": manifest_shas["model_manifest.json"],
            "rule_manifest_sha256": manifest_shas["rule_manifest.json"],
            "snapshot_manifest_sha256": snapshot_sha,
            "migration_manifest_sha256": migration_sha,
        },
        "provider_live_matrix": {
            "max_calls": 18,
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "database": {
            "engine": "postgresql",
            "required_migration": "024_advice_bundles.sql",
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
    spec_path = output_resolved / "candidate_run_spec.json"
    _write_json_new(spec_path, spec)
    readback = _load_json(spec_path, "P6_CANDIDATE_RUN_SPEC_READBACK_FAILED")
    if readback != spec or file_sha256(spec_path) != hashlib.sha256(canonical_bytes(spec)).hexdigest():
        raise P6ContractError("P6_CANDIDATE_RUN_SPEC_READBACK_FAILED")
    return spec, spec_path
