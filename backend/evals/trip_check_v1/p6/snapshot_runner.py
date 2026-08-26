"""Fail-closed P6 G3 controlled-snapshot replay runner."""

from __future__ import annotations

import hashlib
import json
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.trip_check.provider_integrity import provider_snapshot_sha256
from evals.trip_check_v1.input_provider_integrity_runner import run_snapshot_matrix
from evals.trip_check_v1.p6.contracts_v1 import (
    P6ContractError,
    digest,
    read_actual_repo_state,
    validate_candidate_run_spec,
    validate_gate_receipt,
)


SNAPSHOT_CASE_COUNT = 6
SNAPSHOT_ARTIFACTS_PER_CASE = 6
SNAPSHOT_PROVIDER_VERSION = "p3-provider-integrity-v1"


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
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    except OSError as exc:
        raise P6ContractError("P6_G3_ARTIFACT_WRITE_FAILED") from exc


@contextmanager
def _forbid_network() -> Any:
    attempts = {"count": 0}
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_create_connection = socket.create_connection

    def reject(*args: Any, **kwargs: Any) -> Any:
        attempts["count"] += 1
        raise P6ContractError("P6_G3_NETWORK_CALL_DETECTED")

    socket.socket.connect = reject
    socket.socket.connect_ex = reject
    socket.create_connection = reject
    try:
        yield attempts
    finally:
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex
        socket.create_connection = original_create_connection


def _artifact_map(root: Path, manifest: Mapping[str, Any]) -> dict[str, str]:
    index = manifest.get("artifact_index")
    if not isinstance(index, list) or len(index) != SNAPSHOT_CASE_COUNT * SNAPSHOT_ARTIFACTS_PER_CASE:
        raise P6ContractError("P6_G3_ARTIFACT_INDEX_INVALID")
    result: dict[str, str] = {}
    for item in index:
        if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "bytes"}:
            raise P6ContractError("P6_G3_ARTIFACT_INDEX_INVALID")
        relative = item["path"]
        if not isinstance(relative, str) or relative in result or ".." in Path(relative).parts:
            raise P6ContractError("P6_G3_ARTIFACT_INDEX_INVALID")
        path = root / relative
        try:
            actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            actual_bytes = path.stat().st_size
        except OSError as exc:
            raise P6ContractError("P6_G3_ARTIFACT_UNREADABLE") from exc
        if actual_sha != item["sha256"] or actual_bytes != item["bytes"]:
            raise P6ContractError("P6_G3_ARTIFACT_HASH_MISMATCH")
        result[relative] = actual_sha
    return result


def _case_map(manifest: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    cases = manifest.get("cases")
    if not isinstance(cases, list) or len(cases) != SNAPSHOT_CASE_COUNT:
        raise P6ContractError("P6_G3_CASE_SET_INVALID")
    result: dict[str, Mapping[str, Any]] = {}
    for item in cases:
        if not isinstance(item, Mapping) or not isinstance(item.get("case_id"), str):
            raise P6ContractError("P6_G3_CASE_SET_INVALID")
        result[item["case_id"]] = item
    if len(result) != SNAPSHOT_CASE_COUNT:
        raise P6ContractError("P6_G3_CASE_SET_INVALID")
    return result


def _validate_round(
    root: Path,
    manifest: Mapping[str, Any],
    subject_commit: str,
    snapshot_sha: str,
) -> tuple[dict[str, str], dict[str, Mapping[str, Any]]]:
    cases = _case_map(manifest)
    if not (
        manifest.get("schema_version") == "trip-check-p3-provider-integrity-manifest-v1"
        and manifest.get("subject_commit") == subject_commit
        and manifest.get("execution_mode") == "snapshot"
        and manifest.get("status") == "PASS"
        and manifest.get("canonical_case_count") == SNAPSHOT_CASE_COUNT
        and manifest.get("canonical_cases_passed") == SNAPSHOT_CASE_COUNT
        and manifest.get("snapshot_sha256") == snapshot_sha
        and manifest.get("network_call_count") == 0
        and all(
            item.get("status") == "PASS"
            and item.get("network_call_count") == 0
            and item.get("replay_equal") is True
            and item.get("receipt_count") == 6
            and item.get("observation_count") == 8
            for item in cases.values()
        )
    ):
        raise P6ContractError("P6_G3_ROUND_INVALID")
    for case_id in cases:
        run_spec = _load_json(root / "cases" / case_id / "run_spec.json", "P6_G3_RUN_SPEC_INVALID")
        if not (
            run_spec.get("commit_sha") == subject_commit
            and run_spec.get("execution_mode") == "snapshot"
            and run_spec.get("provider_version") == SNAPSHOT_PROVIDER_VERSION
            and run_spec.get("snapshot_hash") == snapshot_sha
            and run_spec.get("budget", {}).get("max_provider_queries") == 6
            and run_spec.get("budget", {}).get("max_retries") == 0
        ):
            raise P6ContractError("P6_G3_RUN_SPEC_INVALID")
    return _artifact_map(root, manifest), cases


async def run_snapshot_gate(
    *,
    candidate_run_spec_path: Path,
    output_root: Path,
    repo_root: Path,
    formal: bool = True,
    matrix_runner: Callable[..., Awaitable[dict[str, Any]]] = run_snapshot_matrix,
) -> dict[str, Any]:
    spec = validate_candidate_run_spec(
        _load_json(candidate_run_spec_path, "P6_CANDIDATE_RUN_SPEC_INVALID")
    )
    repo_resolved = repo_root.resolve(strict=True)
    output_resolved = output_root.resolve(strict=False)
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise P6ContractError("P6_G3_EXTERNAL_ROOT_REQUIRED")
    if formal:
        actual = read_actual_repo_state(repo_resolved)
        expected = {
            "subject_commit": spec["subject_commit"],
            "upstream_ref": spec["upstream_ref"],
            "upstream_commit": spec["upstream_commit"],
            "dirty_tree": False,
        }
        if actual != expected:
            raise P6ContractError("P6_G3_REPO_BINDING_INVALID")
        if output_resolved != (Path(spec["evidence_root"]) / "g3").resolve(strict=False):
            raise P6ContractError("P6_G3_OUTPUT_ROOT_INVALID")
        if output_resolved.exists() and any(output_resolved.iterdir()):
            raise P6ContractError("P6_G3_OUTPUT_NOT_EMPTY")
    snapshot_sha = provider_snapshot_sha256()
    if snapshot_sha != spec["bindings"]["snapshot_manifest_sha256"]:
        raise P6ContractError("P6_G3_SNAPSHOT_BINDING_INVALID")

    round_roots = [output_resolved / "round-1", output_resolved / "round-2"]
    try:
        with _forbid_network() as attempts:
            manifests = [
                await matrix_runner(
                    commit_sha=spec["subject_commit"],
                    output=round_root,
                )
                for round_root in round_roots
            ]
    except P6ContractError:
        raise
    except Exception as exc:
        raise P6ContractError("P6_G3_REPLAY_EXECUTION_FAILED") from exc
    if attempts["count"] != 0:
        raise P6ContractError("P6_G3_NETWORK_CALL_DETECTED")
    validated = [
        _validate_round(round_root, manifest, spec["subject_commit"], snapshot_sha)
        for round_root, manifest in zip(round_roots, manifests, strict=True)
    ]
    artifact_maps = [item[0] for item in validated]
    case_maps = [item[1] for item in validated]
    artifact_mismatches = sum(
        artifact_maps[0].get(path) != artifact_maps[1].get(path)
        for path in set(artifact_maps[0]) | set(artifact_maps[1])
    )
    case_ids = set(case_maps[0]) | set(case_maps[1])
    terminal_mismatches = sum(
        case_maps[0].get(case_id, {}).get("result_hash")
        != case_maps[1].get(case_id, {}).get("result_hash")
        for case_id in case_ids
    )
    receipt_paths = [path for path in artifact_maps[0] if path.endswith("provider_receipts.json")]
    fact_paths = [path for path in artifact_maps[0] if path.endswith("evidence_observations.json")]
    receipt_mismatches = sum(
        artifact_maps[0].get(path) != artifact_maps[1].get(path) for path in receipt_paths
    )
    fact_mismatches = sum(
        artifact_maps[0].get(path) != artifact_maps[1].get(path) for path in fact_paths
    )
    replay_mismatches = (
        artifact_mismatches + terminal_mismatches + receipt_mismatches + fact_mismatches
    )
    if replay_mismatches:
        raise P6ContractError("P6_G3_REPLAY_MISMATCH")

    binding_readback = {
        "schema_version": "trip-check-p6-g3-binding-readback-v1",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "candidate_config_sha256": spec["bindings"]["config_sha256"],
        "snapshot_sha256": snapshot_sha,
        "provider_version": SNAPSHOT_PROVIDER_VERSION,
        "round_artifact_set_sha256": [digest(item) for item in artifact_maps],
    }
    binding_readback["receipt_hash"] = digest(binding_readback)
    _write_json_new(output_resolved / "g3_binding_readback.json", binding_readback)
    metrics = {
        "network_call_count": 0,
        "replay_mismatch_count": 0,
        "socket_attempt_count": 0,
        "snapshot_round_count": 2,
        "snapshot_case_count": SNAPSHOT_CASE_COUNT,
        "snapshot_artifact_count": len(artifact_maps[0]),
        "receipt_replay_mismatch_count": 0,
        "fact_replay_mismatch_count": 0,
        "terminal_replay_mismatch_count": 0,
        "artifact_replay_mismatch_count": 0,
        "snapshot_hash_readback_count": SNAPSHOT_CASE_COUNT * 2,
        "provider_version_readback_count": SNAPSHOT_CASE_COUNT * 2,
        "config_hash_readback_count": 1,
    }
    receipt = {
        "schema_version": "trip-check-p6-gate-receipt-v1",
        "gate": "g3",
        "subject_commit": spec["subject_commit"],
        "run_spec_hash": spec["run_spec_hash"],
        "status": "PASS",
        "evidence_level": "controlled_snapshot",
        "checks_total": 12,
        "checks_passed": 12,
        "failure_count": 0,
        "metrics": metrics,
    }
    receipt["receipt_hash"] = digest(receipt)
    receipt = validate_gate_receipt(receipt, "g3", spec)
    _write_json_new(output_resolved / "g3_receipt.json", receipt)
    return receipt
