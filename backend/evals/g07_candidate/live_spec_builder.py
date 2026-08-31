from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

from app.trip_check.provider_integrity import provider_snapshot_sha256
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest, file_sha256


G07_RUN_SPEC_RELATIVE = "backend/eval_data/g07_candidate/run_spec_v1.json"
G07_UPSTREAM_REF = "origin/codex/g07-candidate"
G07_REMOTE_REF = "refs/heads/codex/g07-candidate"
G07_LATEST_MIGRATION = "034_trip_understanding_screenshot_batches.sql"
G07_EVIDENCE_ROOT_PARENT = PureWindowsPath(
    "D:/munto/code/claudeProject/agentTravel-g07-evidence/g07-candidate"
)
_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SPEC_KEYS = {
    "schema_version",
    "goal_id",
    "subject_commit",
    "candidate_tree",
    "upstream_ref",
    "upstream_commit",
    "dirty_tree",
    "g07_run_spec_path",
    "g07_run_spec_sha256",
    "verified_bindings",
    "provider_live_matrix",
    "migration",
    "evidence_root",
    "human_evidence",
    "run_spec_hash",
}


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
        raise P6ContractError("G07_LIVE_SPEC_WRITE_FAILED") from exc


def _git(repo_root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P6ContractError("G07_LIVE_SPEC_GIT_READBACK_FAILED") from exc


def read_actual_g07_repo_state(repo_root: Path) -> dict[str, Any]:
    upstream = _git(
        repo_root,
        "rev-parse",
        "--abbrev-ref",
        "--symbolic-full-name",
        "@{upstream}",
    )
    if upstream != G07_UPSTREAM_REF:
        raise P6ContractError("G07_LIVE_SPEC_UPSTREAM_INVALID")
    remote_line = _git(repo_root, "ls-remote", "--heads", "origin", G07_REMOTE_REF)
    parts = remote_line.split()
    if len(parts) != 2 or _COMMIT.fullmatch(parts[0]) is None:
        raise P6ContractError("G07_LIVE_SPEC_REMOTE_READBACK_FAILED")
    upstream_commit = _git(repo_root, "rev-parse", "@{upstream}")
    if upstream_commit != parts[0]:
        raise P6ContractError("G07_LIVE_SPEC_REMOTE_READBACK_MISMATCH")
    return {
        "subject_commit": _git(repo_root, "rev-parse", "HEAD"),
        "candidate_tree": _git(repo_root, "show", "-s", "--format=%T", "HEAD"),
        "upstream_ref": upstream,
        "upstream_commit": parts[0],
        "dirty_tree": bool(
            _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all")
        ),
    }


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


def _verify_bound_path(repo_root: Path, binding: object) -> str:
    if not isinstance(binding, Mapping):
        raise P6ContractError("G07_LIVE_SPEC_BINDING_INVALID")
    relative = binding.get("path")
    expected = binding.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise P6ContractError("G07_LIVE_SPEC_BINDING_INVALID")
    if file_sha256(repo_root / relative) != expected:
        raise P6ContractError("G07_LIVE_SPEC_BINDING_DRIFT")
    return expected


def _verify_g07_contract(repo_root: Path) -> dict[str, str]:
    run_spec_path = repo_root / G07_RUN_SPEC_RELATIVE
    run_spec = _load_object(run_spec_path, "G07_LIVE_SPEC_CONTRACT_INVALID")
    candidate = run_spec.get("candidate_subject")
    if not (
        run_spec.get("schema_version") == "g07-candidate-run-spec-v1"
        and run_spec.get("goal_id") == "TC-VNEXT-G07-CANDIDATE"
        and run_spec.get("freeze_status") == "FROZEN_INPUT_CONTRACT"
        and isinstance(candidate, Mapping)
        and candidate.get("branch") == "codex/g07-candidate"
        and candidate.get("remote_ref") == G07_REMOTE_REF
        and candidate.get("cross_commit_evidence_reuse") is False
    ):
        raise P6ContractError("G07_LIVE_SPEC_CONTRACT_INVALID")
    contracts = run_spec.get("contract_bindings")
    evaluations = run_spec.get("evaluation_bindings")
    if not isinstance(contracts, Mapping) or not isinstance(evaluations, Mapping):
        raise P6ContractError("G07_LIVE_SPEC_CONTRACT_INVALID")
    bindings = {
        "candidate_gate": _verify_bound_path(
            repo_root, contracts.get("automated_candidate_gate")
        ),
        "product_policy": _verify_bound_path(
            repo_root, contracts.get("product_delivery_policy")
        ),
        "latest_migration": _verify_bound_path(
            repo_root, contracts.get("latest_migration")
        ),
        "text_card_dataset": _verify_bound_path(
            repo_root, evaluations.get("text_card_90_case_contract")
        ),
        "trip_nlu_candidate": _verify_bound_path(
            repo_root, evaluations.get("trip_nlu_120_case_candidate")
        ),
        "provider_binding": _verify_bound_path(
            repo_root, evaluations.get("provider_binding")
        ),
        "model_panel": _verify_bound_path(repo_root, evaluations.get("model_panel")),
        "model_runtime": _verify_bound_path(
            repo_root, evaluations.get("model_runtime")
        ),
        "provider_snapshot": provider_snapshot_sha256(),
    }
    latest = contracts.get("latest_migration")
    if not isinstance(latest, Mapping) or not (
        latest.get("path")
        == f"backend/app/db/migrations/{G07_LATEST_MIGRATION}"
        and latest.get("append_only_range") == "001-034"
    ):
        raise P6ContractError("G07_LIVE_SPEC_MIGRATION_BINDING_INVALID")
    return bindings


def _migration_manifest(repo_root: Path) -> dict[str, Any]:
    files = sorted((repo_root / "backend/app/db/migrations").glob("*.sql"))
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


def validate_g07_live_provider_spec(value: Mapping[str, Any]) -> dict[str, Any]:
    spec = dict(value)
    if set(spec) != _SPEC_KEYS:
        raise P6ContractError("G07_LIVE_SPEC_FIELDS_INVALID")
    if not (
        spec["schema_version"] == "g07-live-provider-run-spec-v1"
        and spec["goal_id"] == "TC-VNEXT-G07-CANDIDATE"
        and isinstance(spec["subject_commit"], str)
        and _COMMIT.fullmatch(spec["subject_commit"])
        and isinstance(spec["candidate_tree"], str)
        and _COMMIT.fullmatch(spec["candidate_tree"])
        and spec["upstream_ref"] == G07_UPSTREAM_REF
        and spec["upstream_commit"] == spec["subject_commit"]
        and spec["dirty_tree"] is False
        and spec["g07_run_spec_path"] == G07_RUN_SPEC_RELATIVE
        and isinstance(spec["g07_run_spec_sha256"], str)
        and _HASH.fullmatch(spec["g07_run_spec_sha256"])
        and spec["human_evidence"] is False
    ):
        raise P6ContractError("G07_LIVE_SPEC_BINDING_INVALID")
    bindings = spec["verified_bindings"]
    if not isinstance(bindings, dict) or not bindings or any(
        not isinstance(item, str) or _HASH.fullmatch(item) is None
        for item in bindings.values()
    ):
        raise P6ContractError("G07_LIVE_SPEC_BINDING_INVALID")
    if spec["provider_live_matrix"] != {
        "amap_route_calls": 12,
        "qweather_forecast_calls": 3,
        "qweather_alert_calls": 3,
        "max_calls": 18,
        "retry_budget": 0,
        "fixture_fallback_required_zero": True,
    }:
        raise P6ContractError("G07_LIVE_SPEC_MATRIX_INVALID")
    migration = spec["migration"]
    if not isinstance(migration, dict) or not (
        migration.get("latest") == G07_LATEST_MIGRATION
        and migration.get("file_count") == 34
        and isinstance(migration.get("manifest_sha256"), str)
        and _HASH.fullmatch(migration["manifest_sha256"])
    ):
        raise P6ContractError("G07_LIVE_SPEC_MIGRATION_BINDING_INVALID")
    evidence_root = spec["evidence_root"]
    if not isinstance(evidence_root, str):
        raise P6ContractError("G07_LIVE_SPEC_EVIDENCE_ROOT_INVALID")
    normalized = PureWindowsPath(evidence_root)
    if (
        normalized.parent != G07_EVIDENCE_ROOT_PARENT
        or normalized.name != spec["subject_commit"]
    ):
        raise P6ContractError("G07_LIVE_SPEC_EVIDENCE_ROOT_INVALID")
    if spec["run_spec_hash"] != digest(
        {key: item for key, item in spec.items() if key != "run_spec_hash"}
    ):
        raise P6ContractError("G07_LIVE_SPEC_HASH_MISMATCH")
    return spec


def build_g07_live_provider_spec(
    *,
    output_root: Path,
    repo_root: Path,
    formal: bool = True,
    subject_commit: str | None = None,
    candidate_tree: str | None = None,
) -> tuple[dict[str, Any], Path]:
    repository = repo_root.resolve(strict=True)
    output = _require_external_empty_output(output_root, repository)
    bindings = _verify_g07_contract(repository)
    if formal:
        if subject_commit is not None or candidate_tree is not None:
            raise P6ContractError("G07_LIVE_SPEC_FORMAL_INJECTION_FORBIDDEN")
        state = read_actual_g07_repo_state(repository)
        if state["dirty_tree"] or state["subject_commit"] != state["upstream_commit"]:
            raise P6ContractError("G07_LIVE_SPEC_REPO_BINDING_INVALID")
        subject = state["subject_commit"]
        expected = Path(str(G07_EVIDENCE_ROOT_PARENT / subject)) / "inputs"
        if output != expected.resolve(strict=False):
            raise P6ContractError("G07_LIVE_SPEC_OUTPUT_ROOT_INVALID")
    else:
        subject = subject_commit or "a" * 40
        state = {
            "subject_commit": subject,
            "candidate_tree": candidate_tree or "b" * 40,
            "upstream_ref": G07_UPSTREAM_REF,
            "upstream_commit": subject,
            "dirty_tree": False,
        }

    migration = _migration_manifest(repository)
    migration_path = output / "migration_manifest.json"
    _write_json_new(migration_path, migration)
    evidence_root = str(G07_EVIDENCE_ROOT_PARENT / subject).replace("\\", "/")
    spec: dict[str, Any] = {
        "schema_version": "g07-live-provider-run-spec-v1",
        "goal_id": "TC-VNEXT-G07-CANDIDATE",
        **state,
        "g07_run_spec_path": G07_RUN_SPEC_RELATIVE,
        "g07_run_spec_sha256": file_sha256(repository / G07_RUN_SPEC_RELATIVE),
        "verified_bindings": dict(sorted(bindings.items())),
        "provider_live_matrix": {
            "amap_route_calls": 12,
            "qweather_forecast_calls": 3,
            "qweather_alert_calls": 3,
            "max_calls": 18,
            "retry_budget": 0,
            "fixture_fallback_required_zero": True,
        },
        "migration": {
            "latest": G07_LATEST_MIGRATION,
            "file_count": 34,
            "manifest_sha256": file_sha256(migration_path),
        },
        "evidence_root": evidence_root,
        "human_evidence": False,
    }
    spec["run_spec_hash"] = digest(spec)
    spec = validate_g07_live_provider_spec(spec)
    spec_path = output / "g07_live_provider_spec.json"
    _write_json_new(spec_path, spec)
    if _load_object(spec_path, "G07_LIVE_SPEC_READBACK_FAILED") != spec:
        raise P6ContractError("G07_LIVE_SPEC_READBACK_FAILED")
    return spec, spec_path
