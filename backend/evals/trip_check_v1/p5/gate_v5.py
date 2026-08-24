"""Fail-closed P5 v5 Evaluation Gate with same-subject artifact readback."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from evals.trip_check_v1.p5.data_contract import digest
from evals.trip_check_v1.p5.formal_receipts_v5 import (
    P5FormalReceiptErrorV5,
    validate_dataset_formal_validation_receipt_v5,
    validate_verification_receipt_v5,
)
from evals.trip_check_v1.p5.judge_v5 import (
    judge_protocol_projection_hash_v5,
    judge_rubric_projection_hash_v5,
)


DATASET_ID_V5 = "trip-check-p5-360-v5"
ACTIVE_CONTRACT_V5 = "trip-check-p5-v5"
VARIANT_IDS_V5 = {"legacy_a", "core_b", "solver_c"}
PROMOTION_DECISIONS_V5 = {
    "KEEP_CORE_B",
    "PROMOTE_ADMITTED_CHALLENGER",
    "REJECT_ALL_CANDIDATES",
}
VERIFICATION_KINDS_V5 = (
    "p1",
    "p2",
    "p3",
    "p4",
    "backend_pytest",
    "ruff",
    "frontend_build",
    "dual_entry",
)


class P5GateErrorV5(RuntimeError):
    """Stable fail-closed P5 v5 gate error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5GateErrorV5(reason) from exc
    if not isinstance(value, dict):
        raise P5GateErrorV5(reason)
    return value


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5GateErrorV5("GATE_ARTIFACT_UNREADABLE") from exc


def _load_jsonl(path: Path, reason: str) -> list[Any]:
    rows: list[Any] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for line in handle:
                if line.strip():
                    rows.append(json.loads(line))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5GateErrorV5(reason) from exc
    return rows


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _is_commit(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _require_fields(value: Mapping[str, Any], required: set[str], reason: str) -> None:
    if required - set(value):
        raise P5GateErrorV5(reason)


def _validate_self_hash(value: Mapping[str, Any], field: str, reason: str) -> None:
    if value.get(field) != digest({key: item for key, item in value.items() if key != field}):
        raise P5GateErrorV5(reason)


def _repo_state_v5(repo_root: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise P5GateErrorV5("GATE_REPOSITORY_STATE_UNAVAILABLE") from exc

    return {
        "subject_commit": run("rev-parse", "HEAD"),
        "upstream_ref": run("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"),
        "upstream_commit": run("rev-parse", "@{upstream}"),
        "dirty_tree": bool(run("status", "--short")),
    }


def _resolve_receipt_path(repo_root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise P5GateErrorV5("FORMAL_VERIFICATION_RECEIPT_PATH_INVALID")
    raw = Path(value)
    if ".." in raw.parts:
        raise P5GateErrorV5("FORMAL_VERIFICATION_RECEIPT_PATH_INVALID")
    path = raw if raw.is_absolute() else repo_root / raw
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise P5GateErrorV5("FORMAL_VERIFICATION_RECEIPT_UNREADABLE") from exc
    if not resolved.is_file():
        raise P5GateErrorV5("FORMAL_VERIFICATION_RECEIPT_PATH_INVALID")
    return resolved


def _require_safe_gate_output(repo_root: Path, output_path: Path) -> Path:
    resolved = output_path.absolute()
    root = repo_root.resolve()
    try:
        resolved.resolve().relative_to(root)
    except ValueError:
        return resolved
    local_artifacts = (root / ".local-artifacts").resolve()
    try:
        resolved.resolve().relative_to(local_artifacts)
    except ValueError as exc:
        raise P5GateErrorV5("V5_GATE_OUTPUT_PATH_FORBIDDEN") from exc
    return resolved


def _artifact(name: str, path: Path, repo_root: Path) -> dict[str, Any]:
    resolved = path.resolve()
    try:
        display = resolved.relative_to(repo_root.resolve()).as_posix()
        storage = "repository"
    except ValueError:
        display = resolved.name
        storage = "external"
    return {
        "logical_name": name,
        "storage": storage,
        "path": display,
        "sha256": _sha256(resolved),
        "size_bytes": resolved.stat().st_size,
    }


def _parse_dataset_v5(*, repo_root: Path, path: Path, run_spec_path: Path, rubric_path: Path) -> dict[str, Any]:
    value = _load_json(path, "V5_DATASET_MANIFEST_INVALID")
    _validate_self_hash(value, "manifest_hash", "V5_DATASET_MANIFEST_HASH_MISMATCH")
    required = {
        "schema_version",
        "dataset_id",
        "manifest_hash",
        "counts",
        "files",
        "lanes",
        "contract_hashes",
        "frozen",
        "formal_validation_eligible",
        "seal_status",
        "sealing_commitment",
    }
    _require_fields(value, required, "V5_DATASET_MANIFEST_FIELDS_MISSING")
    counts = value.get("counts")
    lanes = value.get("lanes")
    files = value.get("files")
    contracts = value.get("contract_hashes")
    sealing = value.get("sealing_commitment")
    if (
        value.get("schema_version") != "trip-check-p5-dataset-manifest-v5"
        or value.get("dataset_id") != DATASET_ID_V5
        or value.get("frozen") is not True
        or value.get("formal_validation_eligible") is not True
        or value.get("seal_status") != "SEALED"
        or not isinstance(counts, Mapping)
        or counts.get("total") != 360
        or counts.get("by_split") != {"pilot": 18, "dev": 180, "regression": 72, "frozen_blind": 90}
        or not isinstance(lanes, Mapping)
        or not isinstance(lanes.get("nonblind"), Mapping)
        or not isinstance(lanes.get("frozen_blind"), Mapping)
        or lanes["nonblind"].get("case_count") != 270
        or lanes["frozen_blind"].get("case_count") != 90
        or not isinstance(files, Mapping)
        or set(files)
        != {
            "nonblind_cases",
            "blind_cases",
            "nonblind_materializations",
            "blind_materializations",
        }
        or not isinstance(contracts, Mapping)
        or not isinstance(sealing, Mapping)
        or sealing.get("status") != "SEALED"
        or not _is_sha256(sealing.get("blind_seal_file_sha256"))
        or not _is_sha256(sealing.get("candidate_dataset_manifest_hash"))
        or contracts.get("run_spec_template_sha256") != _sha256(run_spec_path)
        or contracts.get("judge_rubric_sha256") != _sha256(rubric_path)
        or contracts.get("judge_rubric_semantics_changed") is not False
        or not _is_sha256(contracts.get("contracts_v3_sha256"))
        or not _is_sha256(contracts.get("dataset_contracts_v5_sha256"))
    ):
        raise P5GateErrorV5("V5_DATASET_FORMAL_CONTRACT_INVALID")
    expected_rows = {
        "nonblind_cases": 270,
        "blind_cases": 90,
        "nonblind_materializations": 270,
        "blind_materializations": 90,
    }
    backend_root = repo_root / "backend"
    for key, row_count in expected_rows.items():
        entry = files[key]
        if not isinstance(entry, Mapping):
            raise P5GateErrorV5("V5_DATASET_FILE_BINDING_INVALID")
        relative_value = entry.get("path")
        if not isinstance(relative_value, str):
            raise P5GateErrorV5("V5_DATASET_FILE_BINDING_INVALID")
        relative = Path(relative_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise P5GateErrorV5("V5_DATASET_FILE_BINDING_INVALID")
        source = (backend_root / relative).resolve()
        try:
            source.relative_to(backend_root.resolve())
        except ValueError as exc:
            raise P5GateErrorV5("V5_DATASET_FILE_BINDING_INVALID") from exc
        if (
            not source.is_file()
            or entry.get("row_count") != row_count
            or entry.get("file_sha256") != _sha256(source)
            or not _is_sha256(entry.get("content_sha256"))
        ):
            raise P5GateErrorV5("V5_DATASET_FILE_BINDING_INVALID")
    return value


def _parse_active_and_seal_v5(
    *,
    active_path: Path,
    seal_path: Path,
    dataset: Mapping[str, Any],
    run_spec_path: Path,
    rubric_path: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    active = _load_json(active_path, "V5_ACTIVE_CONTRACT_INVALID")
    seal = _load_json(seal_path, "V5_BLIND_SEAL_INVALID")
    sealing = dataset.get("sealing_commitment")
    if (
        active.get("active_contract") != ACTIVE_CONTRACT_V5
        or active.get("formal_evidence_status") != "READY"
        or active.get("dataset_manifest_hash") != dataset["manifest_hash"]
        or active.get("blind_seal_v5_sha256") != _sha256(seal_path)
        or not _is_commit(active.get("candidate_freeze_commit"))
        or seal.get("schema_version") != "trip-check-p5-blind-seal-v5"
        or not isinstance(sealing, Mapping)
        or sealing.get("blind_seal_file_sha256") != _sha256(seal_path)
        or seal.get("candidate_dataset_manifest_hash") != sealing.get("candidate_dataset_manifest_hash")
        or seal.get("candidate_freeze_commit") != active.get("candidate_freeze_commit")
        or seal.get("case_count") != 90
        or seal.get("split") != "frozen_blind"
        or any(
            not _is_sha256(seal.get(field))
            for field in (
                "external_bundle_sha256",
                "labels_canonical_sha256",
                "review_receipt_sha256",
            )
        )
        or any(
            sealing.get(field) != seal.get(field)
            for field in (
                "external_bundle_sha256",
                "labels_canonical_sha256",
                "review_receipt_sha256",
            )
        )
        or seal.get("scoring_payload_present") is not False
        or seal.get("human_evidence") is not False
        or seal.get("run_spec_template_sha256") != _sha256(run_spec_path)
        or seal.get("rubric_sha256") != _sha256(rubric_path)
        or seal.get("contracts_v3_sha256") != dataset["contract_hashes"]["contracts_v3_sha256"]
        or seal.get("dataset_contracts_v5_sha256") != dataset["contract_hashes"]["dataset_contracts_v5_sha256"]
    ):
        raise P5GateErrorV5("V5_ACTIVE_SEAL_BINDING_INVALID")
    return active, seal


def _parse_run_manifest_v5(value: Mapping[str, Any], *, lane: str, dataset_hash: str) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "formal_evidence",
        "lane",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "dataset_manifest_hash",
        "artifact_index_hash",
        "terminal_outputs_file_sha256",
        "terminal_outputs_content_sha256",
        "terminal_outputs_path",
        "replay_outputs_path",
        "replay_outputs_file_sha256",
        "replay_outputs_content_sha256",
        "artifact_index_path",
        "rubric_sha256",
        "run_spec_template_sha256",
        "case_count",
        "terminal_count",
        "replay_executed",
        "replay_readback_count",
        "replay_mismatches",
        "blind_labels_read",
        "manifest_hash",
    }
    _require_fields(value, required, "V5_RUN_MANIFEST_FIELDS_MISSING")
    _validate_self_hash(value, "manifest_hash", "V5_RUN_MANIFEST_HASH_MISMATCH")
    expected_cases, expected_terminals = (270, 810) if lane == "nonblind" else (90, 270)
    expected_schema = (
        "trip-check-p5-run-group-v5"
        if lane == "nonblind"
        else "trip-check-p5-blind-run-group-v5"
    )
    if (
        value.get("schema_version") != expected_schema
        or value.get("status") != "PASS"
        or value.get("formal_evidence") is not True
        or value.get("lane") != lane
        or not _is_commit(value.get("subject_commit"))
        or not isinstance(value.get("upstream_ref"), str)
        or not value["upstream_ref"]
        or value.get("upstream_commit") != value.get("subject_commit")
        or value.get("dirty_tree") is not False
        or value.get("dataset_manifest_hash") != dataset_hash
        or value.get("case_count") != expected_cases
        or value.get("terminal_count") != expected_terminals
        or value.get("replay_executed") is not True
        or value.get("replay_readback_count") != expected_terminals
        or value.get("replay_mismatches") != []
        or value.get("blind_labels_read") is not False
        or any(
            not _is_sha256(value.get(field))
            for field in (
                "artifact_index_hash",
                "terminal_outputs_file_sha256",
                "terminal_outputs_content_sha256",
                "replay_outputs_file_sha256",
                "replay_outputs_content_sha256",
                "run_spec_template_sha256",
                "rubric_sha256",
            )
        )
    ):
        raise P5GateErrorV5("V5_RUN_MANIFEST_CONTRACT_INVALID")
    if lane == "nonblind" and value.get("replay_match_count") != expected_terminals:
        raise P5GateErrorV5("V5_RUN_MANIFEST_CONTRACT_INVALID")
    if lane == "frozen_blind" and (
        not _is_sha256(value.get("nonce_sha256"))
        or not _is_sha256(value.get("run_binding_hash"))
        or not _is_sha256(value.get("nonce_consumption_receipt_sha256"))
    ):
        raise P5GateErrorV5("V5_BLIND_NONCE_BINDING_INVALID")
    return dict(value)


def _resolve_run_artifact_v5(run_manifest_path: Path, value: object, expected: str) -> Path:
    if value != expected:
        raise P5GateErrorV5("V5_RUN_ARTIFACT_PATH_INVALID")
    candidate = run_manifest_path.parent / expected
    if candidate.is_symlink():
        raise P5GateErrorV5("V5_RUN_ARTIFACT_LINK_FORBIDDEN")
    try:
        resolved = candidate.resolve(strict=True)
        run_dir = run_manifest_path.parent.resolve(strict=True)
    except OSError as exc:
        raise P5GateErrorV5("V5_RUN_ARTIFACT_UNREADABLE") from exc
    if resolved.parent != run_dir:
        raise P5GateErrorV5("V5_RUN_ARTIFACT_PATH_ESCAPE")
    return resolved


def _validate_run_artifacts_v5(
    *, run: Mapping[str, Any], run_manifest_path: Path, expected_count: int
) -> dict[str, Path]:
    terminal = _resolve_run_artifact_v5(run_manifest_path, run.get("terminal_outputs_path"), "terminal_outputs.jsonl")
    replay = _resolve_run_artifact_v5(run_manifest_path, run.get("replay_outputs_path"), "replay_readback.jsonl")
    index_path = _resolve_run_artifact_v5(run_manifest_path, run.get("artifact_index_path"), "artifact_index.json")
    terminal_rows = _load_jsonl(terminal, "V5_RUN_TERMINAL_OUTPUTS_INVALID")
    replay_rows = _load_jsonl(replay, "V5_RUN_REPLAY_OUTPUTS_INVALID")
    if (
        len(terminal_rows) != expected_count
        or len(replay_rows) != expected_count
        or _sha256(terminal) != run["terminal_outputs_file_sha256"]
        or digest(terminal_rows) != run["terminal_outputs_content_sha256"]
        or _sha256(replay) != run["replay_outputs_file_sha256"]
        or digest(replay_rows) != run["replay_outputs_content_sha256"]
    ):
        raise P5GateErrorV5("V5_RUN_OUTPUT_READBACK_MISMATCH")
    index = _load_json(index_path, "V5_RUN_ARTIFACT_INDEX_INVALID")
    if (
        index.get("artifact_index_hash")
        != digest({key: item for key, item in index.items() if key != "artifact_index_hash"})
        or index.get("artifact_index_hash") != run["artifact_index_hash"]
        or index.get("entries")
        != [
            {
                "path": terminal.name,
                "byte_size": terminal.stat().st_size,
                "sha256": _sha256(terminal),
                "content_sha256": digest(terminal_rows),
            },
            {
                "path": replay.name,
                "byte_size": replay.stat().st_size,
                "sha256": _sha256(replay),
                "content_sha256": digest(replay_rows),
            },
        ]
    ):
        raise P5GateErrorV5("V5_RUN_ARTIFACT_INDEX_MISMATCH")
    return {"terminal_outputs": terminal, "replay_outputs": replay, "artifact_index": index_path}


def _require_external_file_v5(repo_root: Path, path: Path) -> Path:
    if path.is_symlink():
        raise P5GateErrorV5("V5_FORMAL_ARTIFACT_LINK_FORBIDDEN")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        if not resolved.is_file():
            raise P5GateErrorV5("V5_FORMAL_ARTIFACT_PATH_INVALID")
        return resolved
    except OSError as exc:
        raise P5GateErrorV5("V5_FORMAL_ARTIFACT_UNREADABLE") from exc
    raise P5GateErrorV5("V5_FORMAL_ARTIFACT_MUST_BE_EXTERNAL")


def _validate_blind_nonce_chain_v5(
    *,
    run: Mapping[str, Any],
    nonce_path: Path,
    mint_receipt_path: Path,
    consumption_receipt_path: Path,
) -> None:
    nonce = _load_json(nonce_path, "V5_BLIND_NONCE_INVALID")
    mint = _load_json(mint_receipt_path, "V5_BLIND_NONCE_MINT_RECEIPT_INVALID")
    consumed = _load_json(consumption_receipt_path, "V5_BLIND_NONCE_CONSUMPTION_RECEIPT_INVALID")
    nonce_sha256 = digest(nonce.get("nonce"))
    expected_mint_fields = {
        "schema_version",
        "status",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "nonce_file_path",
        "nonce_file_sha256",
        "nonce_sha256",
        "label_payload_present",
        "receipt_hash",
    }
    if (
        set(nonce) != {"schema_version", "purpose", "dataset_id", "active_contract", "nonce"}
        or nonce.get("schema_version") != "trip-check-p5-blind-run-nonce-v5"
        or nonce.get("purpose") != "execute_frozen_blind_once"
        or nonce.get("dataset_id") != DATASET_ID_V5
        or nonce.get("active_contract") != ACTIVE_CONTRACT_V5
        or not _is_sha256(nonce.get("nonce"))
        or set(mint) != expected_mint_fields
        or mint.get("schema_version") != "trip-check-p5-blind-run-nonce-mint-receipt-v5"
        or mint.get("status") != "MINTED_NOT_CONSUMED"
        or mint.get("subject_commit") != run["subject_commit"]
        or mint.get("upstream_ref") != run["upstream_ref"]
        or mint.get("upstream_commit") != run["upstream_commit"]
        or mint.get("dirty_tree") is not False
        or Path(str(mint.get("nonce_file_path"))).resolve() != nonce_path.resolve()
        or mint.get("nonce_file_sha256") != _sha256(nonce_path)
        or mint.get("nonce_sha256") != nonce_sha256
        or mint.get("label_payload_present") is not False
        or mint.get("receipt_hash") != digest({key: item for key, item in mint.items() if key != "receipt_hash"})
    ):
        raise P5GateErrorV5("V5_BLIND_NONCE_MINT_BINDING_INVALID")
    run_core = {
        key: item
        for key, item in run.items()
        if key not in {"run_binding_hash", "nonce_consumption_receipt_sha256", "manifest_hash"}
    }
    expected_consumption_fields = {
        "schema_version",
        "status",
        "dataset_id",
        "dataset_manifest_hash",
        "nonce_sha256",
        "claimed_at",
        "completed_at",
        "run_id",
        "run_binding_hash",
        "artifact_index_hash",
        "failure_reason_code",
    }
    if (
        set(consumed) != expected_consumption_fields
        or consumed.get("schema_version") != "trip-check-p5-blind-run-consumption-receipt-v5"
        or consumed.get("status") != "CONSUMED"
        or consumed.get("dataset_id") != DATASET_ID_V5
        or consumed.get("dataset_manifest_hash") != run["dataset_manifest_hash"]
        or consumed.get("nonce_sha256") != nonce_sha256
        or consumed.get("run_id") != run.get("run_id")
        or consumed.get("run_binding_hash") != run["run_binding_hash"]
        or consumed.get("run_binding_hash") != digest(run_core)
        or consumed.get("artifact_index_hash") != run["artifact_index_hash"]
        or consumed.get("failure_reason_code") is not None
        or not isinstance(consumed.get("claimed_at"), str)
        or not consumed["claimed_at"]
        or not isinstance(consumed.get("completed_at"), str)
        or not consumed["completed_at"]
        or _sha256(consumption_receipt_path) != run["nonce_consumption_receipt_sha256"]
        or any(
            token in json.dumps(payload, sort_keys=True).lower()
            for payload in (nonce, mint, consumed)
            for token in ("label", "oracle", "answer", "expected")
            if payload is not mint or token != "label"
        )
    ):
        raise P5GateErrorV5("V5_BLIND_NONCE_CONSUMPTION_BINDING_INVALID")


def parse_nonblind_score_v5(value: Mapping[str, Any], *, run: Mapping[str, Any]) -> dict[str, Any]:
    """Strict, isolated parser for the independently-integrated v5 scorer."""

    required = {
        "schema_version",
        "status",
        "subject_commit",
        "dataset_manifest_hash",
        "run_group_manifest_hash",
        "artifact_index_hash",
        "case_count",
        "terminal_count",
        "replay_readback_count",
        "variant_metrics",
        "paired_comparisons",
        "zero_tolerance_checks",
        "stage_gate_checks",
        "promotion_decision",
        "solver_admission_inherited",
        "solver_may_promote_from_p5_score",
        "evidence_boundary",
        "report_hash",
    }
    _require_fields(value, required, "V5_NONBLIND_SCORE_FIELDS_MISSING")
    _validate_self_hash(value, "report_hash", "V5_NONBLIND_SCORE_HASH_MISMATCH")
    variants = value.get("variant_metrics")
    zero_checks = value.get("zero_tolerance_checks")
    stage_checks = value.get("stage_gate_checks")
    decision = value.get("promotion_decision")
    if (
        value.get("schema_version") != "trip-check-p5-nonblind-score-report-v5"
        or value.get("status") not in {"PASS", "REJECT"}
        or value.get("subject_commit") != run["subject_commit"]
        or value.get("dataset_manifest_hash") != run["dataset_manifest_hash"]
        or value.get("run_group_manifest_hash") != run["manifest_hash"]
        or value.get("artifact_index_hash") != run["artifact_index_hash"]
        or value.get("case_count") != 270
        or value.get("terminal_count") != 810
        or value.get("replay_readback_count") != 810
        or not isinstance(variants, Mapping)
        or set(variants) != VARIANT_IDS_V5
        or not isinstance(zero_checks, Mapping)
        or not zero_checks
        or any(type(item) is not bool for item in zero_checks.values())
        or not isinstance(stage_checks, Mapping)
        or not stage_checks
        or any(type(item) is not bool for item in stage_checks.values())
        or decision not in PROMOTION_DECISIONS_V5
        or value.get("solver_admission_inherited") != "REJECT"
        or value.get("solver_may_promote_from_p5_score") is not False
        or not isinstance(value.get("paired_comparisons"), Mapping)
        or not isinstance(value.get("evidence_boundary"), Mapping)
    ):
        raise P5GateErrorV5("V5_NONBLIND_SCORE_CONTRACT_INVALID")
    if value["status"] == "PASS" and (not all(zero_checks.values()) or not all(stage_checks.values())):
        raise P5GateErrorV5("V5_NONBLIND_SCORE_STATUS_INVALID")
    if decision == "PROMOTE_ADMITTED_CHALLENGER":
        challenger = value.get("admitted_challenger_variant_id")
        admission = value.get("challenger_admission")
        if challenger == "solver_c":
            raise P5GateErrorV5("V5_SOLVER_PROMOTION_FORBIDDEN")
        if (
            challenger not in VARIANT_IDS_V5 - {"core_b", "solver_c"}
            or not isinstance(admission, Mapping)
            or admission.get("status") != "ADMITTED"
            or admission.get("variant_id") != challenger
        ):
            raise P5GateErrorV5("V5_CHALLENGER_ADMISSION_INVALID")
    return dict(value)


def parse_blind_score_v5(value: Mapping[str, Any], *, run: Mapping[str, Any], seal_sha256: str) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "bindings",
        "case_count",
        "terminal_count",
        "replay_readback_count",
        "variant_metrics",
        "zero_tolerance_checks",
        "human_calibration_performed",
        "human_evidence",
        "live_provider_evidence",
        "public_e2e_evidence",
        "report_hash",
    }
    _require_fields(value, required, "V5_BLIND_SCORE_FIELDS_MISSING")
    _validate_self_hash(value, "report_hash", "V5_BLIND_SCORE_HASH_MISMATCH")
    bindings = value.get("bindings")
    variants = value.get("variant_metrics")
    zero_checks = value.get("zero_tolerance_checks")
    if (
        value.get("schema_version") != "trip-check-p5-isolated-blind-score-v5"
        or value.get("status") not in {"PASS", "REJECT"}
        or not isinstance(bindings, Mapping)
        or bindings.get("subject_commit") != run["subject_commit"]
        or bindings.get("dataset_manifest_hash") != run["dataset_manifest_hash"]
        or bindings.get("run_group_manifest_hash") != run["manifest_hash"]
        or bindings.get("terminal_outputs_file_sha256") != run["terminal_outputs_file_sha256"]
        or bindings.get("terminal_outputs_content_sha256") != run["terminal_outputs_content_sha256"]
        or bindings.get("artifact_index_hash") != run["artifact_index_hash"]
        or bindings.get("blind_seal_sha256") != seal_sha256
        or bindings.get("run_spec_template_sha256") != run["run_spec_template_sha256"]
        or value.get("case_count") != 90
        or value.get("terminal_count") != 270
        or value.get("replay_readback_count") != 270
        or not isinstance(variants, Mapping)
        or set(variants) != VARIANT_IDS_V5
        or not isinstance(zero_checks, Mapping)
        or not zero_checks
        or any(type(item) is not bool for item in zero_checks.values())
        or value.get("human_calibration_performed") is not False
        or value.get("human_evidence") is not False
        or value.get("live_provider_evidence") is not False
        or value.get("public_e2e_evidence") is not False
    ):
        raise P5GateErrorV5("V5_BLIND_SCORE_CONTRACT_INVALID")
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    if '"case_id"' in serialized or "p5.blind." in serialized or '"by_' in serialized:
        raise P5GateErrorV5("V5_BLIND_AGGREGATE_DETAIL_LEAK")
    if value["status"] == "PASS" and not all(zero_checks.values()):
        raise P5GateErrorV5("V5_BLIND_SCORE_STATUS_INVALID")
    return dict(value)


def _parse_judge_panel_v5(value: Mapping[str, Any], *, run: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "status",
        "evidence_class",
        "automated_proxy_judge",
        "human_calibration_performed",
        "round_count",
        "candidate_count",
        "calibration_panel_sha256",
        "calibration_panel_report_hash",
        "agreement_threshold",
        "verdict_agreement_rate",
        "per_dimension_agreement_rate",
        "variant_metrics",
        "provenance",
        "subject_commit",
        "dataset_manifest_hash",
        "run_group_manifest_hash",
        "artifact_index_hash",
        "terminal_outputs_content_sha256",
        "deterministic_scorer_priority",
        "judge_may_override_deterministic_failure",
        "unsupported_claim_candidate_count",
        "report_hash",
    }
    _require_fields(value, required, "V5_JUDGE_PANEL_FIELDS_MISSING")
    _validate_self_hash(value, "report_hash", "V5_JUDGE_PANEL_HASH_MISMATCH")
    provenance = value.get("provenance")
    dimension_agreement = value.get("per_dimension_agreement_rate")
    if (
        value.get("schema_version") != "trip-check-p5-judge-panel-v5"
        or value.get("status") not in {"PASS", "BLOCKED"}
        or value.get("evidence_class") != "automated_proxy_judge"
        or value.get("automated_proxy_judge") is not True
        or value.get("human_calibration_performed") is not False
        or value.get("round_count") != 3
        or value.get("candidate_count") != 270
        or not _is_sha256(value.get("calibration_panel_sha256"))
        or not _is_sha256(value.get("calibration_panel_report_hash"))
        or value.get("agreement_threshold") != 0.85
        or not isinstance(provenance, list)
        or len(provenance) != 3
        or len({item.get("round_index") for item in provenance if isinstance(item, Mapping)}) != 3
        or any(not isinstance(item, Mapping) for item in provenance)
        or any(
            len({item.get(field) for item in provenance}) != 3
            for field in ("evaluator_id", "agent_task_id", "agent_id", "context_id")
        )
        or not isinstance(dimension_agreement, Mapping)
        or set(dimension_agreement) != {"clarity", "actionability", "evidence_boundary_expression"}
        or value.get("subject_commit") != run["subject_commit"]
        or value.get("dataset_manifest_hash") != run["dataset_manifest_hash"]
        or value.get("run_group_manifest_hash") != run["manifest_hash"]
        or value.get("artifact_index_hash") != run["artifact_index_hash"]
        or value.get("terminal_outputs_content_sha256") != run["terminal_outputs_content_sha256"]
        or value.get("deterministic_scorer_priority") is not True
        or value.get("judge_may_override_deterministic_failure") is not False
    ):
        raise P5GateErrorV5("V5_JUDGE_PANEL_CONTRACT_INVALID")
    agreement_pass = value.get("verdict_agreement_rate", 0) >= 0.85 and all(
        item >= 0.85 for item in dimension_agreement.values()
    )
    semantic_pass = agreement_pass and value.get("unsupported_claim_candidate_count") == 0
    if (value["status"] == "PASS") != semantic_pass:
        raise P5GateErrorV5("V5_JUDGE_PANEL_STATUS_INVALID")
    return dict(value)


def _parse_formal_receipt_v5(
    *,
    repo_root: Path,
    value: Mapping[str, Any],
    subject_commit: str,
    upstream_ref: str,
    upstream_commit: str,
    dataset_hash: str,
    expected_bindings: Mapping[str, str],
) -> tuple[dict[str, Any], list[tuple[str, Path]]]:
    required = {
        "schema_version",
        "status",
        "formal",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "dataset_id",
        "dataset_manifest_hash",
        "config_hash",
        "dataset_validation_receipt",
        "bindings",
        "counts",
        "verification_receipts",
        "errors",
        "receipt_hash",
    }
    _require_fields(value, required, "V5_FORMAL_RECEIPT_FIELDS_MISSING")
    _validate_self_hash(value, "receipt_hash", "V5_FORMAL_RECEIPT_HASH_MISMATCH")
    bindings = value.get("bindings")
    counts = value.get("counts")
    receipts = value.get("verification_receipts")
    dataset_receipt_entry = value.get("dataset_validation_receipt")
    if (
        value.get("schema_version") != "trip-check-p5-formal-validation-receipt-v5"
        or value.get("status") != "PASS"
        or value.get("formal") is not True
        or value.get("subject_commit") != subject_commit
        or value.get("upstream_ref") != upstream_ref
        or value.get("upstream_commit") != upstream_commit
        or value.get("dirty_tree") is not False
        or value.get("dataset_id") != DATASET_ID_V5
        or value.get("dataset_manifest_hash") != dataset_hash
        or not _is_sha256(value.get("config_hash"))
        or value.get("errors") != []
        or bindings != expected_bindings
        or counts
        != {
            "nonblind_cases": 270,
            "blind_cases": 90,
            "nonblind_terminals": 810,
            "blind_terminals": 270,
            "replay_readback": 1080,
            "judge_rounds": 3,
            "judge_provenance": 3,
        }
        or not isinstance(receipts, Mapping)
        or set(receipts) != set(VERIFICATION_KINDS_V5)
        or not isinstance(dataset_receipt_entry, Mapping)
        or set(dataset_receipt_entry) != {"path", "sha256", "receipt_hash", "status"}
    ):
        raise P5GateErrorV5("V5_FORMAL_RECEIPT_CONTRACT_INVALID")
    artifact_paths: list[tuple[str, Path]] = []
    dataset_receipt_path = _resolve_receipt_path(repo_root, dataset_receipt_entry["path"])
    try:
        dataset_receipt = validate_dataset_formal_validation_receipt_v5(dataset_receipt_path)
    except P5FormalReceiptErrorV5 as exc:
        raise P5GateErrorV5("V5_DATASET_FORMAL_RECEIPT_INVALID") from exc
    if (
        dataset_receipt_entry["status"] != "PASS"
        or dataset_receipt["status"] != "PASS"
        or _sha256(dataset_receipt_path) != dataset_receipt_entry["sha256"]
        or dataset_receipt["receipt_hash"] != dataset_receipt_entry["receipt_hash"]
        or dataset_receipt["subject_commit"] != subject_commit
        or dataset_receipt["upstream_ref"] != upstream_ref
        or dataset_receipt["upstream_commit"] != upstream_commit
        or dataset_receipt["dataset_manifest"]["manifest_hash"] != dataset_hash
    ):
        raise P5GateErrorV5("V5_DATASET_FORMAL_RECEIPT_BINDING_INVALID")
    artifact_paths.append(("dataset_formal_validation_receipt", dataset_receipt_path))
    expected_entry_fields = {
        "path",
        "sha256",
        "status",
        "subject_commit",
        "upstream_ref",
        "upstream_commit",
        "dirty_tree",
        "config_hash",
        "artifact_set_hash",
        "readback_verified",
    }
    for kind in VERIFICATION_KINDS_V5:
        entry = receipts[kind]
        if (
            not isinstance(entry, Mapping)
            or set(entry) != expected_entry_fields
            or entry.get("status") != "PASS"
            or entry.get("subject_commit") != subject_commit
            or entry.get("upstream_ref") != upstream_ref
            or entry.get("upstream_commit") != upstream_commit
            or entry.get("dirty_tree") is not False
            or entry.get("readback_verified") is not True
            or not _is_sha256(entry.get("sha256"))
            or not _is_sha256(entry.get("config_hash"))
            or not _is_sha256(entry.get("artifact_set_hash"))
        ):
            raise P5GateErrorV5("V5_VERIFICATION_RECEIPT_ENTRY_INVALID")
        receipt_path = _resolve_receipt_path(repo_root, entry["path"])
        try:
            actual = validate_verification_receipt_v5(receipt_path)
        except P5FormalReceiptErrorV5 as exc:
            raise P5GateErrorV5("V5_VERIFICATION_RECEIPT_UNREADABLE") from exc
        if (
            _sha256(receipt_path) != entry["sha256"]
            or actual.get("schema_version") != "trip-check-p5-verification-receipt-v5"
            or actual.get("receipt_kind") != kind
            or actual.get("status") != "PASS"
            or actual.get("subject_commit") != subject_commit
            or actual.get("upstream_ref") != upstream_ref
            or actual.get("upstream_commit") != upstream_commit
            or actual.get("dirty_tree") is not False
            or actual.get("readback_verified") is not True
            or actual.get("config_hash") != entry["config_hash"]
            or actual.get("artifact_set_hash") != entry["artifact_set_hash"]
        ):
            raise P5GateErrorV5("V5_VERIFICATION_RECEIPT_BINDING_INVALID")
        if kind == "p4" and (
            not isinstance(actual.get("solver_admission"), Mapping)
            or actual["solver_admission"].get("status") != "REJECT"
            or actual["solver_admission"].get("default_strategy") != "bounded_repair_v1"
        ):
            raise P5GateErrorV5("V5_P4_SOLVER_INHERITANCE_INVALID")
        artifact_paths.append((f"{kind}_verification_receipt", receipt_path))
    return dict(value), artifact_paths


def build_p5_gate_manifest_v5(
    *,
    repo_root: Path,
    dataset_manifest_path: Path,
    active_contract_path: Path,
    blind_seal_path: Path,
    run_spec_path: Path,
    rubric_path: Path,
    judge_protocol_path: Path,
    calibration_panel_path: Path,
    nonblind_run_manifest_path: Path,
    nonblind_score_path: Path,
    blind_run_manifest_path: Path,
    blind_score_path: Path,
    judge_panel_path: Path,
    blind_nonce_path: Path,
    blind_nonce_mint_receipt_path: Path,
    blind_nonce_consumption_receipt_path: Path,
    formal_receipt_path: Path,
    output_path: Path,
    gate_schema_path: Path,
    require_current_subject: bool = True,
    calibration_panel_validator: Callable[..., object] | None = None,
) -> dict[str, Any]:
    root = repo_root.resolve()
    safe_output = _require_safe_gate_output(root, output_path)
    for formal_path in (
        nonblind_run_manifest_path,
        nonblind_score_path,
        blind_run_manifest_path,
        blind_score_path,
        judge_panel_path,
        calibration_panel_path,
        formal_receipt_path,
        blind_nonce_path,
        blind_nonce_mint_receipt_path,
        blind_nonce_consumption_receipt_path,
    ):
        _require_external_file_v5(root, formal_path)
    dataset = _parse_dataset_v5(
        repo_root=root,
        path=dataset_manifest_path,
        run_spec_path=run_spec_path,
        rubric_path=rubric_path,
    )
    active, _seal = _parse_active_and_seal_v5(
        active_path=active_contract_path,
        seal_path=blind_seal_path,
        dataset=dataset,
        run_spec_path=run_spec_path,
        rubric_path=rubric_path,
    )
    nonblind_run = _parse_run_manifest_v5(
        _load_json(nonblind_run_manifest_path, "V5_NONBLIND_RUN_INVALID"),
        lane="nonblind",
        dataset_hash=dataset["manifest_hash"],
    )
    blind_run = _parse_run_manifest_v5(
        _load_json(blind_run_manifest_path, "V5_BLIND_RUN_INVALID"),
        lane="frozen_blind",
        dataset_hash=dataset["manifest_hash"],
    )
    binding_fields = ("subject_commit", "upstream_ref", "upstream_commit")
    if any(nonblind_run[field] != blind_run[field] for field in binding_fields):
        raise P5GateErrorV5("V5_RUN_SUBJECT_OR_UPSTREAM_MISMATCH")
    if (
        nonblind_run["run_spec_template_sha256"] != _sha256(run_spec_path)
        or blind_run["run_spec_template_sha256"] != _sha256(run_spec_path)
        or nonblind_run["rubric_sha256"] != _sha256(rubric_path)
        or blind_run["rubric_sha256"] != _sha256(rubric_path)
    ):
        raise P5GateErrorV5("V5_RUN_SPEC_BINDING_INVALID")
    subject_commit = nonblind_run["subject_commit"]
    upstream_ref = nonblind_run["upstream_ref"]
    upstream_commit = nonblind_run["upstream_commit"]
    nonblind_score = parse_nonblind_score_v5(
        _load_json(nonblind_score_path, "V5_NONBLIND_SCORE_INVALID"),
        run=nonblind_run,
    )
    blind_score = parse_blind_score_v5(
        _load_json(blind_score_path, "V5_BLIND_SCORE_INVALID"),
        run=blind_run,
        seal_sha256=_sha256(blind_seal_path),
    )
    panel = _parse_judge_panel_v5(_load_json(judge_panel_path, "V5_JUDGE_PANEL_INVALID"), run=blind_run)
    rubric = _load_json(rubric_path, "V5_JUDGE_RUBRIC_INVALID")
    protocol = _load_json(judge_protocol_path, "V5_JUDGE_PROTOCOL_INVALID")
    if calibration_panel_validator is None:
        from evals.trip_check_v1.p5.judge_calibration_v1 import (
            validate_judge_calibration_panel_v1,
        )

        calibration_panel_validator = validate_judge_calibration_panel_v1
    calibration_panel = calibration_panel_validator(
        repo_root=root,
        panel_path=calibration_panel_path,
        rubric_path=rubric_path,
        protocol_path=judge_protocol_path,
    )
    if (
        not isinstance(calibration_panel, Mapping)
        or panel.get("calibration_panel_sha256") != _sha256(calibration_panel_path)
        or panel.get("calibration_panel_report_hash")
        != calibration_panel.get("report_hash")
        or any(
            item.get("calibration_panel_sha256")
            != panel["calibration_panel_sha256"]
            or item.get("calibration_panel_report_hash")
            != panel["calibration_panel_report_hash"]
            for item in panel["provenance"]
        )
    ):
        raise P5GateErrorV5("V5_JUDGE_CALIBRATION_BINDING_INVALID")
    if any(
        item.get("source_rubric_sha256") != _sha256(rubric_path)
        or item.get("judge_input_rubric_sha256")
        != judge_rubric_projection_hash_v5(rubric)
        for item in panel["provenance"]
    ):
        raise P5GateErrorV5("V5_JUDGE_RUBRIC_PROVENANCE_INVALID")
    if any(
        item.get("source_protocol_sha256") != _sha256(judge_protocol_path)
        or item.get("judge_input_protocol_sha256")
        != judge_protocol_projection_hash_v5(rubric, protocol)
        for item in panel["provenance"]
    ):
        raise P5GateErrorV5("V5_JUDGE_PROTOCOL_PROVENANCE_INVALID")
    if any(
        item.get("terminal_outputs_content_sha256")
        != blind_run["terminal_outputs_content_sha256"]
        for item in panel["provenance"]
    ):
        raise P5GateErrorV5("V5_JUDGE_RUN_PROVENANCE_INVALID")
    nonblind_artifacts = _validate_run_artifacts_v5(
        run=nonblind_run,
        run_manifest_path=nonblind_run_manifest_path,
        expected_count=810,
    )
    blind_artifacts = _validate_run_artifacts_v5(
        run=blind_run,
        run_manifest_path=blind_run_manifest_path,
        expected_count=270,
    )
    _validate_blind_nonce_chain_v5(
        run=blind_run,
        nonce_path=blind_nonce_path,
        mint_receipt_path=blind_nonce_mint_receipt_path,
        consumption_receipt_path=blind_nonce_consumption_receipt_path,
    )

    artifact_paths = {
        "dataset_manifest": dataset_manifest_path,
        "active_contract": active_contract_path,
        "blind_seal": blind_seal_path,
        "run_spec": run_spec_path,
        "judge_rubric": rubric_path,
        "judge_protocol": judge_protocol_path,
        "judge_calibration_panel": calibration_panel_path,
        "nonblind_run_manifest": nonblind_run_manifest_path,
        "nonblind_score": nonblind_score_path,
        "blind_run_manifest": blind_run_manifest_path,
        "blind_score": blind_score_path,
        "judge_panel": judge_panel_path,
        "nonblind_terminal_outputs": nonblind_artifacts["terminal_outputs"],
        "nonblind_replay_outputs": nonblind_artifacts["replay_outputs"],
        "nonblind_artifact_index": nonblind_artifacts["artifact_index"],
        "blind_terminal_outputs": blind_artifacts["terminal_outputs"],
        "blind_replay_outputs": blind_artifacts["replay_outputs"],
        "blind_artifact_index": blind_artifacts["artifact_index"],
        "blind_nonce": blind_nonce_path,
        "blind_nonce_mint_receipt": blind_nonce_mint_receipt_path,
        "blind_nonce_consumption_receipt": blind_nonce_consumption_receipt_path,
    }
    expected_bindings = {f"{name}_sha256": _sha256(path) for name, path in artifact_paths.items()}
    formal, verification_paths = _parse_formal_receipt_v5(
        repo_root=root,
        value=_load_json(formal_receipt_path, "V5_FORMAL_RECEIPT_INVALID"),
        subject_commit=subject_commit,
        upstream_ref=upstream_ref,
        upstream_commit=upstream_commit,
        dataset_hash=dataset["manifest_hash"],
        expected_bindings=expected_bindings,
    )
    if require_current_subject:
        state = _repo_state_v5(root)
        if state != {
            "subject_commit": subject_commit,
            "upstream_ref": upstream_ref,
            "upstream_commit": upstream_commit,
            "dirty_tree": False,
        }:
            raise P5GateErrorV5("V5_CURRENT_SUBJECT_UPSTREAM_OR_TREE_INVALID")
    exact_counts = (
        nonblind_run["terminal_count"] == 810
        and blind_run["terminal_count"] == 270
        and nonblind_run["replay_readback_count"] + blind_run["replay_readback_count"] == 1080
    )
    quality_pass = nonblind_score["status"] == "PASS" and blind_score["status"] == "PASS" and panel["status"] == "PASS"
    proposed = nonblind_score["promotion_decision"]
    if not quality_pass:
        decision = "REJECT_ALL_CANDIDATES"
    elif proposed == "REJECT_ALL_CANDIDATES":
        decision = "REJECT_ALL_CANDIDATES"
    elif proposed == "PROMOTE_ADMITTED_CHALLENGER":
        if nonblind_score.get("admitted_challenger_variant_id") == "solver_c":
            raise P5GateErrorV5("V5_SOLVER_PROMOTION_FORBIDDEN")
        decision = "PROMOTE_ADMITTED_CHALLENGER"
    else:
        decision = "KEEP_CORE_B"
    if decision not in PROMOTION_DECISIONS_V5:
        raise P5GateErrorV5("V5_PROMOTION_DECISION_INVALID")

    checks = {
        "same_subject_commit": True,
        "same_upstream_ref_and_commit": upstream_commit == subject_commit,
        "clean_tree": True,
        "dataset_active_seal_bound": True,
        "run_spec_and_rubric_hash_bound": True,
        "run_artifacts_read_back": True,
        "blind_nonce_single_use_bound": True,
        "nonblind_810": nonblind_run["terminal_count"] == 810,
        "blind_270": blind_run["terminal_count"] == 270,
        "replay_readback_1080": exact_counts,
        "nonblind_score_bound": True,
        "blind_aggregate_bound": True,
        "three_independent_judge_provenance": len(panel["provenance"]) == 3,
        "judge_panel_agreement_gte_85": panel["verdict_agreement_rate"] >= 0.85
        and all(value >= 0.85 for value in panel["per_dimension_agreement_rate"].values()),
        "automated_proxy_judge_explicit": panel["automated_proxy_judge"] is True,
        "human_calibration_not_performed": panel["human_calibration_performed"] is False,
        "formal_receipt_readback": formal["status"] == "PASS",
        "p1_p4_receipts_bound": True,
        "backend_ruff_frontend_dual_entry_receipts_bound": True,
        "p4_solver_admission_inherited_reject": True,
        "solver_not_promoted": decision != "PROMOTE_ADMITTED_CHALLENGER"
        or nonblind_score.get("admitted_challenger_variant_id") != "solver_c",
    }
    artifacts = [_artifact(name, path, root) for name, path in artifact_paths.items()]
    artifacts.append(_artifact("formal_validation_receipt", formal_receipt_path, root))
    artifacts.extend(_artifact(name, path, root) for name, path in verification_paths)
    gate_accepted = quality_pass and decision != "REJECT_ALL_CANDIDATES" and all(checks.values())
    manifest = {
        "schema_version": "trip-check-p5-evaluation-gate-v5",
        "goal_id": "TC-P5-G01-evaluation-ablation",
        "status": "PASS" if gate_accepted else "REJECT",
        "subject_commit": subject_commit,
        "upstream_ref": upstream_ref,
        "upstream_commit": upstream_commit,
        "dirty_tree": False,
        "dataset_id": DATASET_ID_V5,
        "dataset_manifest_hash": dataset["manifest_hash"],
        "counts": {
            "nonblind_cases": 270,
            "blind_cases": 90,
            "nonblind_terminals": 810,
            "blind_terminals": 270,
            "total_terminals": 1080,
            "replay_readback": 1080,
            "judge_rounds": 3,
            "judge_provenance": 3,
        },
        "checks": checks,
        "promotion_decision": decision,
        "default_runtime_strategy": "bounded_repair_v1",
        "solver_admission": {
            "inherited_from_p4": "REJECT",
            "may_be_overridden_by_p5_score": False,
            "promotion_eligible": False,
        },
        "evidence_boundaries": {
            "controlled_snapshot": "EVALUATED",
            "replay_readback": "PASS" if exact_counts else "REJECT",
            "automated_proxy_judge": panel["status"],
            "human_calibration_performed": False,
            "human_evidence": "NOT_RUN",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "release": "NOT_RUN",
        },
        "artifact_index": artifacts,
    }
    manifest["manifest_hash"] = digest(manifest)
    schema = _load_json(gate_schema_path, "V5_GATE_SCHEMA_INVALID")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(manifest),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        raise P5GateErrorV5(f"V5_GATE_SCHEMA_REJECTED:{errors[0].message}")
    state_before_write = _repo_state_v5(root) if require_current_subject else None
    safe_output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    safe_output.write_text(payload, encoding="utf-8", newline="\n")
    if json.loads(safe_output.read_text(encoding="utf-8")) != manifest:
        raise P5GateErrorV5("V5_GATE_READBACK_FAILED")
    if require_current_subject and _repo_state_v5(root) != state_before_write:
        raise P5GateErrorV5("V5_GATE_POST_WRITE_REPOSITORY_STATE_CHANGED")
    return manifest


__all__ = [
    "P5GateErrorV5",
    "build_p5_gate_manifest_v5",
    "parse_blind_score_v5",
    "parse_nonblind_score_v5",
]
