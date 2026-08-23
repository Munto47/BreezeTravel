"""Fail-closed deterministic scorer for P5 v3 non-blind run groups."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from statistics import mean, median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.trip_check_v1.p5.active_contract import require_v3_formal_ready
from evals.trip_check_v1.p5.adapters_v3 import validate_materialization_v3
from evals.trip_check_v1.p5.contracts_v3 import (
    P5ArtifactIndexV3,
    P5CaseResultV3,
    P5CaseV3,
    P5FailureRecordV3,
    P5TerminalOutputV3,
    P5VariantRunSpecV3,
    TerminalStatusV3,
    VARIANT_IDS_V3,
)
from evals.trip_check_v1.p5.data_contract import digest, file_sha256
from evals.trip_check_v1.p5.data_contract_v2 import JUDGE_RUBRIC_PATH_V2
from evals.trip_check_v1.p5.data_contract_v3 import (
    BLIND_SEAL_PATH_V3,
    MANIFEST_PATH_V3,
    NONBLIND_MATERIALIZATIONS_PATH_V3,
    NONBLIND_PATH_V3,
    RUN_SPEC_TEMPLATE_PATH_V3,
    case_set_hash_v3,
    materialization_set_hash_v3,
)
from evals.trip_check_v1.p5.runner_v3 import (
    build_failure_record_v3,
    revision_lineage_v3,
    validate_run_spec_whitelist_v3,
)
from evals.trip_check_v1.p5.scorer_v2 import (
    _concurrency_result,
    _contains_unknown_or_unavailable_status,
    _deterministic_unsupported_claim_count,
    _finding_reason_codes,
    _nonpass_finding_advice_coverage,
    _unknown_promoted_to_pass,
    _usage_measurement,
    aggregate_scores_v2,
)


RUN_GROUP_FIELDS_V3 = frozenset(
    {
        "schema_version",
        "run_id",
        "status",
        "formal_evidence",
        "lane",
        "subject_commit",
        "dirty_tree",
        "dataset_manifest_hash",
        "cases_file_sha256",
        "materializations_file_sha256",
        "case_count",
        "case_set_hash",
        "materialization_set_hash",
        "variant_ids",
        "variant_count",
        "terminal_count",
        "expected_terminal_count",
        "run_specs",
        "case_results_path",
        "case_results_file_sha256",
        "case_results_content_sha256",
        "failure_records_path",
        "failure_records_file_sha256",
        "failure_records_content_sha256",
        "failure_record_count",
        "artifact_index_path",
        "artifact_index_hash",
        "replay_executed",
        "replay_match_count",
        "replay_mismatches",
        "replay_hash_policy",
        "ocr_replay_provenance",
        "hidden_retry_count",
        "blind_labels_read",
        "external_api_calls",
        "fresh_ocr_model_inferences",
        "human_evidence",
        "active_contract_file_sha256",
        "blind_seal_file_sha256",
        "candidate_freeze_commit",
        "manifest_hash",
    }
)
RUN_SPEC_VARIANT_WHITELIST_V3 = frozenset(
    {"variant_id", "adapter_version", "repair_strategy"}
)


class P5V3ScoringError(ValueError):
    """Stable fail-closed validation error returned by the v3 scorer."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class P5CaseScoreV3(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["trip-check-p5-score-v3"] = "trip-check-p5-score-v3"
    case_id: str
    split: str
    city: str
    input_kind: str
    difficulty: str
    fault_profile_id: str
    variant_id: str
    terminal_status: TerminalStatusV3
    task_success: bool
    deterministic_pass: bool
    score: float = Field(ge=0, le=100)
    terminal_ok: bool
    resolution_match: bool
    required_reason_codes: list[str]
    missing_reason_codes: list[str]
    wrong_city_or_poi_count: int | None
    unknown_preservation: str
    advice_coverage: str
    nonpass_finding_count: int
    covered_nonpass_finding_count: int
    unsupported_claim_count: int
    candidate_receipt_coverage: str
    concurrency_result: str
    repair_postcheck: str
    replay_hash_match: bool
    strategy_outcome_match: bool
    ocr_receipt_result: str
    token_count: int | Literal["NOT_MEASURED"]
    cost_usd: float | Literal["NOT_MEASURED"]
    usage_measurement: str
    deterministic_failure_codes: list[str]


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V3ScoringError(reason) from exc
    if not isinstance(value, dict):
        raise P5V3ScoringError(reason)
    return value


def _load_jsonl(path: Path, reason: str) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V3ScoringError(reason) from exc
    if any(not isinstance(row, dict) for row in rows):
        raise P5V3ScoringError(reason)
    return rows


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5V3ScoringError("ARTIFACT_UNREADABLE") from exc


def _contains_symlink_or_junction(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink() or (
                hasattr(current, "is_junction") and current.is_junction()
            ):
                return True
        except OSError:
            return True
    return False


def _safe_artifact_path(run_dir: Path, name: object, *, expected: str) -> Path:
    if name != expected:
        raise P5V3ScoringError("RUN_ARTIFACT_PATH_INVALID")
    path = run_dir / expected
    try:
        path.resolve().relative_to(run_dir.resolve())
    except (ValueError, OSError) as exc:
        raise P5V3ScoringError("RUN_ARTIFACT_PATH_ESCAPE") from exc
    if _contains_symlink_or_junction(path.absolute()) or not path.is_file():
        raise P5V3ScoringError("RUN_ARTIFACT_PATH_UNSAFE")
    return path


def _git(repo_root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise P5V3ScoringError("GIT_READBACK_FAILED") from exc
    return result.stdout.strip()


def _git_is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise P5V3ScoringError("GIT_READBACK_FAILED") from exc
    if result.returncode not in {0, 1}:
        raise P5V3ScoringError("GIT_READBACK_FAILED")
    return result.returncode == 0


def _validate_run_manifest_types(manifest: Mapping[str, Any]) -> None:
    boolean_fields = {
        "formal_evidence",
        "dirty_tree",
        "replay_executed",
        "blind_labels_read",
    }
    integer_fields = {
        "case_count",
        "variant_count",
        "terminal_count",
        "expected_terminal_count",
        "failure_record_count",
        "replay_match_count",
        "hidden_retry_count",
        "external_api_calls",
        "fresh_ocr_model_inferences",
    }
    string_fields = RUN_GROUP_FIELDS_V3 - boolean_fields - integer_fields - {
        "variant_ids",
        "run_specs",
        "replay_mismatches",
        "ocr_replay_provenance",
    }
    if (
        any(type(manifest.get(field)) is not bool for field in boolean_fields)
        or any(
            not isinstance(manifest.get(field), int)
            or isinstance(manifest.get(field), bool)
            or manifest[field] < 0
            for field in integer_fields
        )
        or any(
            not isinstance(manifest.get(field), str) or not manifest[field]
            for field in string_fields
        )
        or not isinstance(manifest.get("variant_ids"), list)
        or not isinstance(manifest.get("run_specs"), dict)
        or not isinstance(manifest.get("replay_mismatches"), list)
        or not isinstance(manifest.get("ocr_replay_provenance"), dict)
    ):
        raise P5V3ScoringError("RUN_GROUP_MANIFEST_TYPES_INVALID")


def semantic_output_hash_v3(output: P5TerminalOutputV3) -> str:
    return digest(
        {
            "replay_hash_policy": "p5-semantic-projection-v3",
            "case_id": output.case_id,
            "input_hash": output.input_hash,
            "materialization_hash": output.materialization_hash,
            "run_spec_hash": output.run_spec_hash,
            "variant_id": output.variant_id,
            "adapter_version": output.adapter_version,
            "repair_strategy": output.repair_strategy,
            "terminal_status": output.terminal_status.value,
            "capability_outcomes": output.capability_outcomes,
            "native_output": output.native_output,
            "evaluation_projection": output.evaluation_projection,
            "findings": output.findings,
            "advice": output.advice,
            "postcheck": output.postcheck,
            "receipts": output.receipts,
            "token_count": output.token_count,
            "cost_usd": output.cost_usd,
            "error_category": output.error_category,
        }
    )


def _strategy_matches(output: P5TerminalOutputV3, expected: str) -> bool:
    if output.variant_id != "solver_c" and expected in {"UNSAT", "TIMEOUT", "FALLBACK"}:
        return True
    strategy = output.native_output.get("solver_strategy", {})
    if not isinstance(strategy, Mapping):
        strategy = {}
    primary_result = strategy.get("primary")
    effective_result = strategy.get("effective")
    receipt = strategy.get("receipt")
    primary = strategy.get("primary_status", strategy.get("status"))
    if primary is None and isinstance(primary_result, Mapping):
        primary = primary_result.get("status")
    effective = strategy.get("effective_status")
    if effective is None and isinstance(effective_result, Mapping):
        effective = effective_result.get("status")
    effective = effective or primary
    fallback_used = strategy.get("fallback_used") is True or (
        isinstance(receipt, Mapping) and receipt.get("fallback_strategy_id") is not None
    )
    if expected == "FEASIBLE":
        if output.variant_id == "solver_c" and primary is not None:
            return primary == "SUCCESS" or (fallback_used and effective == "SUCCESS")
        return output.terminal_status in {
            TerminalStatusV3.SUCCEEDED,
            TerminalStatusV3.NEEDS_USER_RESOLUTION,
        }
    if expected == "UNSAT":
        return primary == "UNSAT"
    if expected == "TIMEOUT":
        return primary == "TIMEOUT"
    if expected == "FALLBACK":
        return (
            primary in {"ERROR", "TIMEOUT", "UNSAT"}
            and fallback_used
            and effective == "SUCCESS"
        )
    return False


def _successful_receipt_ids(receipts: object) -> set[str]:
    if not isinstance(receipts, list):
        return set()
    return {
        str(receipt["receipt_id"])
        for receipt in receipts
        if isinstance(receipt, Mapping)
        and isinstance(receipt.get("receipt_id"), str)
        and receipt.get("status") in {"PASS", "SUCCEEDED"}
    }


def _materialized_candidates(
    materialization: Mapping[str, Any] | None,
) -> tuple[dict[str, Mapping[str, Any]], set[str]] | None:
    if materialization is None:
        return None
    raw_sets = materialization.get("candidate_sets")
    if not isinstance(raw_sets, list):
        return None
    candidates: dict[str, Mapping[str, Any]] = {}
    for artifact in raw_sets:
        if not isinstance(artifact, Mapping):
            return None
        candidate_set = artifact.get("candidate_set")
        raw_candidates = (
            candidate_set.get("candidates") if isinstance(candidate_set, Mapping) else None
        )
        if not isinstance(raw_candidates, list):
            return None
        for candidate in raw_candidates:
            if not isinstance(candidate, Mapping):
                return None
            candidate_id = candidate.get("canonical_place_id")
            if not isinstance(candidate_id, str) or candidate_id in candidates:
                return None
            candidates[candidate_id] = candidate
    return candidates, _successful_receipt_ids(materialization.get("receipts"))


def _candidate_receipts_complete(
    candidates: Iterable[Mapping[str, Any]], receipt_ids: set[str]
) -> bool:
    saw_candidate = False
    for candidate in candidates:
        saw_candidate = True
        place_receipt_id = candidate.get("place_receipt_id")
        route_receipt_ids = candidate.get("route_receipt_ids")
        if (
            not isinstance(place_receipt_id, str)
            or place_receipt_id not in receipt_ids
            or not isinstance(route_receipt_ids, list)
            or not route_receipt_ids
            or any(
                not isinstance(receipt_id, str) or receipt_id not in receipt_ids
                for receipt_id in route_receipt_ids
            )
        ):
            return False
    return saw_candidate


def _candidate_status(
    case: P5CaseV3,
    output: P5TerminalOutputV3,
    materialization: Mapping[str, Any] | None,
) -> str:
    assert case.oracle is not None
    oracle = case.oracle
    selected = output.evaluation_projection.get("selected_place_ids")
    selected_ids = selected if isinstance(selected, list) else None
    materialized_hashes = [item.content_sha256 for item in case.materialization.candidate_sets]
    if sorted(output.candidate_set_hashes) != sorted(materialized_hashes):
        return "FAIL"
    if not oracle.specific_place_allowed and selected_ids != []:
        return "FAIL"
    if oracle.candidate_receipt_mode == "FORBIDDEN":
        return "PASS" if selected_ids == [] and not materialized_hashes else "FAIL"
    if oracle.candidate_receipt_mode == "NOT_APPLICABLE":
        return "NOT_REQUIRED" if selected_ids is not None else "FAIL"
    if selected_ids is None or not materialized_hashes:
        return "FAIL"
    materialized = _materialized_candidates(materialization)
    if materialized is None:
        return "FAIL"
    candidates, _materialization_receipt_ids = materialized
    if selected_ids:
        selected_candidates = []
        for selected_id in selected_ids:
            candidate = candidates.get(selected_id) if isinstance(selected_id, str) else None
            if candidate is None:
                return "FAIL"
            selected_candidates.append(candidate)
        coverage = output.evaluation_projection.get("candidate_receipt_coverage")
        return (
            "PASS"
            if isinstance(coverage, (int, float))
            and not isinstance(coverage, bool)
            and float(coverage) == 1.0
            and _candidate_receipts_complete(
                selected_candidates, _successful_receipt_ids(output.receipts)
            )
            else "FAIL"
        )
    # User resolution is intentionally fail-closed: no candidate is auto-selected.
    # Materialization truth cannot substitute for product-terminal evidence.  All
    # displayed candidates must be covered by receipts carried by this terminal.
    coverage = output.evaluation_projection.get("candidate_receipt_coverage")
    if (
        case.oracle.requires_user_resolution
        and output.terminal_status is TerminalStatusV3.NEEDS_USER_RESOLUTION
        and output.evaluation_projection.get("requires_user_resolution") is True
        and isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        and float(coverage) == 1.0
        and _candidate_receipts_complete(
            candidates.values(), _successful_receipt_ids(output.receipts)
        )
    ):
        return "PASS"
    return "FAIL"


def _required_obligation_satisfied(
    code: str,
    *,
    output: P5TerminalOutputV3,
    resolution_match: bool,
    candidate_status: str,
    concurrency_status: str,
    strategy_match: bool,
) -> bool:
    reason_codes = _finding_reason_codes(output)
    if not code.startswith("P4_"):
        return code in reason_codes
    projection = output.evaluation_projection
    if code == "P4_ADVICE_COMPLETENESS":
        return bool(output.advice)
    if code == "P4_EMPTY_CANDIDATE_SET":
        return (
            resolution_match
            and projection.get("requires_user_resolution") is True
            and projection.get("selected_place_ids") == []
            and candidate_status == "PASS"
        )
    if code == "P4_CANDIDATE_RECEIPT_MISSING":
        detected = output.capability_outcomes.get("candidate_receipt_missing_detection")
        projected = projection.get("candidate_receipt_integrity")
        return detected in {"DETECTED", "PASS"} or projected == "MISSING_RECEIPT" or (
            resolution_match
            and projection.get("requires_user_resolution") is True
            and projection.get("selected_place_ids") == []
            and candidate_status == "PASS"
        )
    if code == "P4_ROUTE_CONFLICT":
        return bool(
            reason_codes
            & {
                "ROUTE_GAP_INSUFFICIENT",
                "ROUTE_GAP_TIME_UNKNOWN",
                "TIME_CHAIN_CONFLICT",
                "TIME_CHAIN_BROKEN",
                "TRAVEL_TIME_GAP",
                "EVIDENCE_CONFLICT",
            }
        )
    if code in {"P4_DUPLICATE_APPLY", "P4_CONCURRENT_APPLY"}:
        return concurrency_status == "PASS"
    if code in {"P4_SOLVER_UNSAT", "P4_SOLVER_TIMEOUT", "P4_SOLVER_FALLBACK"}:
        return strategy_match
    return False


def score_case_v3(
    case: P5CaseV3,
    output: P5TerminalOutputV3,
    *,
    materialization: Mapping[str, Any] | None = None,
) -> P5CaseScoreV3:
    oracle = case.oracle
    if oracle is None:
        raise P5V3ScoringError("ORACLE_REQUIRED_FOR_SCORING")
    projection = output.evaluation_projection
    required = list(dict.fromkeys(oracle.required_reason_codes))
    wrong = projection.get("wrong_city_or_poi_count")
    wrong_count = (
        wrong if isinstance(wrong, int) and not isinstance(wrong, bool) and wrong >= 0 else None
    )
    expected_terminal = (
        TerminalStatusV3.NEEDS_USER_RESOLUTION
        if oracle.requires_user_resolution
        else TerminalStatusV3.SUCCEEDED
    )
    terminal_ok = output.terminal_status is expected_terminal
    resolution_match = (
        projection.get("requires_user_resolution") is oracle.requires_user_resolution
    )
    authoritative_unknown_present = _contains_unknown_or_unavailable_status(output.findings)
    unknown_status = (
        "PASS"
        if oracle.unknown_must_be_preserved
        and projection.get("unknown_preserved") is True
        and authoritative_unknown_present
        else "FAIL"
        if oracle.unknown_must_be_preserved
        else "NOT_REQUIRED"
    )
    if _unknown_promoted_to_pass(
        {"projection": projection, "findings": output.findings, "postcheck": output.postcheck}
    ):
        unknown_status = "FAIL"
    advice_status = (
        "PASS"
        if oracle.advice_required and bool(output.advice)
        else "FAIL"
        if oracle.advice_required
        else "NOT_REQUIRED"
    )
    candidate_status = _candidate_status(case, output, materialization)
    nonpass_count, covered_nonpass_count = _nonpass_finding_advice_coverage(output)
    unsupported_claim_count = _deterministic_unsupported_claim_count(
        output, candidate_status=candidate_status
    )
    usage_measurement = _usage_measurement(output)
    concurrency_status = _concurrency_result(
        output, oracle.concurrency_expectation, materialization
    )
    strategy_match = _strategy_matches(output, oracle.expected_strategy_outcome)
    missing = [
        code
        for code in required
        if not _required_obligation_satisfied(
            code,
            output=output,
            resolution_match=resolution_match,
            candidate_status=candidate_status,
            concurrency_status=concurrency_status,
            strategy_match=strategy_match,
        )
    ]
    repair_attempted = projection.get("repair_adoption_attempted")
    if oracle.requires_user_resolution or repair_attempted is False:
        postcheck_status = "NOT_REQUIRED"
    elif repair_attempted is True:
        postcheck = output.postcheck
        new_serious = postcheck.get("new_blocker_high_unknown_count") if postcheck else None
        postcheck_status = (
            "PASS"
            if postcheck
            and postcheck.get("schema_version") == "trip-check-p5-postcheck-projection-v3"
            and isinstance(postcheck.get("report_id"), str)
            and bool(postcheck.get("report_id"))
            and isinstance(new_serious, int)
            and not isinstance(new_serious, bool)
            and new_serious <= oracle.max_new_blocker_high_unknown
            and postcheck.get("overall_status")
            not in {None, "UNKNOWN", "UNAVAILABLE", "ERROR"}
            else "FAIL"
        )
    else:
        postcheck_status = "FAIL"
    replay_match = (
        output.semantic_output_hash == semantic_output_hash_v3(output)
        and output.replay_hash == output.semantic_output_hash
        and projection.get("replay_side_effect_counts_equal") is True
    )
    ocr_status = (
        "PASS"
        if oracle.ocr_required and output.ocr_receipt_hash is not None
        else "FAIL"
        if oracle.ocr_required
        else "NOT_REQUIRED"
    )
    checks = (
        (not terminal_ok, f"TERMINAL_{output.terminal_status.value}"),
        (not resolution_match, "RESOLUTION_MISMATCH"),
        (wrong_count is None, "FACT_PROJECTION_MISSING"),
        (
            wrong_count is not None and wrong_count > oracle.wrong_city_or_poi_max,
            "WRONG_CITY_OR_POI",
        ),
        (bool(missing), "HARD_FINDING_MISS"),
        (unknown_status == "FAIL", "UNKNOWN_OR_UNAVAILABLE_NOT_PRESERVED"),
        (advice_status == "FAIL", "ADVICE_MISSING"),
        (
            covered_nonpass_count != nonpass_count,
            "NONPASS_FINDING_ADVICE_COVERAGE_INCOMPLETE",
        ),
        (unsupported_claim_count != 0, "UNSUPPORTED_CLAIM"),
        (candidate_status == "FAIL", "CANDIDATE_RECEIPT_VIOLATION"),
        (concurrency_status == "FAIL", "CONCURRENCY_EXPECTATION_NOT_PROVEN"),
        (postcheck_status == "FAIL", "POSTCHECK_NOT_PROVEN"),
        (not strategy_match, "STRATEGY_OUTCOME_MISMATCH"),
        (ocr_status == "FAIL", "OCR_RECEIPT_MISSING"),
        (not replay_match, "REPLAY_HASH_MISMATCH"),
        (usage_measurement == "FAIL", "USAGE_MEASUREMENT_INVALID"),
    )
    failures = [code for failed, code in checks if failed]
    components = (
        (10, terminal_ok),
        (10, resolution_match),
        (15, wrong_count is not None and wrong_count <= oracle.wrong_city_or_poi_max),
        (20, not missing),
        (10, unknown_status != "FAIL"),
        (5, advice_status != "FAIL"),
        (10, candidate_status != "FAIL"),
        (5, concurrency_status != "FAIL"),
        (10, postcheck_status != "FAIL"),
        (5, replay_match and strategy_match and ocr_status != "FAIL"),
    )
    return P5CaseScoreV3(
        case_id=case.case_id,
        split=case.split,
        city=case.city,
        input_kind=case.input_kind,
        difficulty=case.difficulty,
        fault_profile_id=str(case.runner_control.get("fault_profile_id", "NONE")),
        variant_id=output.variant_id,
        terminal_status=output.terminal_status,
        task_success=oracle.task_success_required and not failures,
        deterministic_pass=not failures,
        score=float(sum(points for points, passed in components if passed)),
        terminal_ok=terminal_ok,
        resolution_match=resolution_match,
        required_reason_codes=required,
        missing_reason_codes=missing,
        wrong_city_or_poi_count=wrong_count,
        unknown_preservation=unknown_status,
        advice_coverage=advice_status,
        nonpass_finding_count=nonpass_count,
        covered_nonpass_finding_count=covered_nonpass_count,
        unsupported_claim_count=unsupported_claim_count,
        candidate_receipt_coverage=candidate_status,
        concurrency_result=concurrency_status,
        repair_postcheck=postcheck_status,
        replay_hash_match=replay_match,
        strategy_outcome_match=strategy_match,
        ocr_receipt_result=ocr_status,
        token_count=output.token_count,
        cost_usd=output.cost_usd,
        usage_measurement=usage_measurement,
        deterministic_failure_codes=failures,
    )


def _validate_dataset_v3(
    *, require_formal: bool
) -> tuple[dict[str, Any], list[P5CaseV3], list[dict[str, Any]]]:
    dataset = _load_json(MANIFEST_PATH_V3, "DATASET_MANIFEST_INVALID")
    if (
        dataset.get("schema_version") != "trip-check-p5-dataset-manifest-v3"
        or dataset.get("dataset_id") != "trip-check-p5-360-v3"
        or dataset.get("manifest_hash")
        != digest({key: value for key, value in dataset.items() if key != "manifest_hash"})
    ):
        raise P5V3ScoringError("DATASET_MANIFEST_HASH_MISMATCH")
    if require_formal and (
        dataset.get("frozen") is not True
        or dataset.get("formal_validation_eligible") is not True
        or dataset.get("seal_status") != "SEALED"
    ):
        raise P5V3ScoringError("DATASET_FORMAL_CONTRACT_INVALID")
    files = dataset.get("files")
    lane = dataset.get("lanes", {}).get("nonblind")
    if not isinstance(files, Mapping) or not isinstance(lane, Mapping):
        raise P5V3ScoringError("DATASET_NONBLIND_BINDING_INVALID")
    expected_files = {
        "nonblind_cases": (
            NONBLIND_PATH_V3,
            "evals/trip_check_v1/p5/cases_nonblind_v3.jsonl",
        ),
        "nonblind_materializations": (
            NONBLIND_MATERIALIZATIONS_PATH_V3,
            "evals/trip_check_v1/p5/materializations_nonblind_v3.jsonl",
        ),
    }
    raw_by_name: dict[str, list[dict[str, Any]]] = {}
    for name, (path, expected_path) in expected_files.items():
        binding = files.get(name)
        if not isinstance(binding, Mapping) or binding.get("path") != expected_path:
            raise P5V3ScoringError("DATASET_NONBLIND_FILE_BINDING_INVALID")
        rows = _load_jsonl(path, "DATASET_NONBLIND_JSONL_INVALID")
        if (
            binding.get("row_count") != len(rows)
            or binding.get("file_sha256") != _sha256_file(path)
            or binding.get("content_sha256") != digest(rows)
        ):
            raise P5V3ScoringError("DATASET_NONBLIND_FILE_BINDING_INVALID")
        raw_by_name[name] = rows
    case_rows = raw_by_name["nonblind_cases"]
    materialization_rows = raw_by_name["nonblind_materializations"]
    try:
        cases = [P5CaseV3.model_validate(row) for row in case_rows]
    except ValidationError as exc:
        raise P5V3ScoringError("CASE_SCHEMA_INVALID") from exc
    if (
        not cases
        or len({case.case_id for case in cases}) != len(cases)
        or any(case.split == "frozen_blind" or case.oracle is None for case in cases)
    ):
        raise P5V3ScoringError("NONBLIND_CASE_SET_INVALID")
    materialization_by_case: dict[str, dict[str, Any]] = {}
    case_by_id = {case.case_id: case for case in cases}
    for row in materialization_rows:
        case_id = row.get("case_id")
        if not isinstance(case_id, str) or case_id in materialization_by_case:
            raise P5V3ScoringError("MATERIALIZATION_CASE_SET_INVALID")
        case = case_by_id.get(case_id)
        if case is None:
            raise P5V3ScoringError("MATERIALIZATION_CASE_SET_INVALID")
        try:
            materialization_by_case[case_id] = validate_materialization_v3(case, row)
        except (ValueError, ValidationError) as exc:
            raise P5V3ScoringError("MATERIALIZATION_SCHEMA_OR_BINDING_INVALID") from exc
    if set(materialization_by_case) != set(case_by_id):
        raise P5V3ScoringError("MATERIALIZATION_CASE_SET_INVALID")
    if (
        lane.get("case_count") != len(cases)
        or lane.get("materialization_count") != len(materialization_rows)
        or lane.get("case_set_hash") != case_set_hash_v3(case_rows)
        or lane.get("materialization_set_hash")
        != materialization_set_hash_v3(materialization_rows)
    ):
        raise P5V3ScoringError("DATASET_NONBLIND_LANE_BINDING_INVALID")
    if require_formal:
        split_counts = Counter(case.split for case in cases)
        if len(cases) != 270 or split_counts != {
            "pilot": 18,
            "dev": 180,
            "regression": 72,
        }:
            raise P5V3ScoringError("FORMAL_NONBLIND_CASE_SHAPE_INVALID")
        contract_hashes = dataset.get("contract_hashes")
        if not isinstance(contract_hashes, Mapping) or {
            "run_spec_template_sha256": contract_hashes.get(
                "run_spec_template_sha256"
            ),
            "judge_rubric_sha256": contract_hashes.get("judge_rubric_sha256"),
        } != {
            "run_spec_template_sha256": file_sha256(RUN_SPEC_TEMPLATE_PATH_V3),
            "judge_rubric_sha256": file_sha256(JUDGE_RUBRIC_PATH_V2),
        } or (
            contract_hashes.get("contracts_v3_path")
            != "evals/trip_check_v1/p5/contracts_v3.py"
            or not isinstance(contract_hashes.get("contracts_v3_sha256"), str)
            or len(contract_hashes["contracts_v3_sha256"]) != 64
        ):
            raise P5V3ScoringError("DATASET_CONTRACT_HASH_BINDING_INVALID")
    return dataset, cases, materialization_rows


def _validate_active_and_git(
    *, repo_root: Path, manifest: Mapping[str, Any], dataset: Mapping[str, Any]
) -> None:
    active_path = Path(__file__).with_name("active_contract.json")
    try:
        active = require_v3_formal_ready(active_path)
    except (OSError, ValueError, RuntimeError) as exc:
        raise P5V3ScoringError("ACTIVE_V3_FORMAL_CONTRACT_INVALID") from exc
    subject = manifest.get("subject_commit")
    candidate = manifest.get("candidate_freeze_commit")
    if (
        not isinstance(subject, str)
        or len(subject) != 40
        or subject != _git(repo_root, "rev-parse", "HEAD")
        or _git(repo_root, "status", "--short")
        or manifest.get("dirty_tree") is not False
    ):
        raise P5V3ScoringError("FORMAL_SUBJECT_OR_TREE_MISMATCH")
    if (
        active.get("dataset_manifest_hash") != dataset.get("manifest_hash")
        or active.get("blind_seal_v3_sha256") != _sha256_file(BLIND_SEAL_PATH_V3)
        or active.get("candidate_freeze_commit") != candidate
        or manifest.get("active_contract_file_sha256") != _sha256_file(active_path)
        or manifest.get("blind_seal_file_sha256") != _sha256_file(BLIND_SEAL_PATH_V3)
        or not isinstance(candidate, str)
        or len(candidate) != 40
        or not _git_is_ancestor(repo_root, candidate, subject)
    ):
        raise P5V3ScoringError("FORMAL_ACTIVE_SEAL_OR_CANDIDATE_MISMATCH")
    commitment = dataset.get("sealing_commitment")
    if not isinstance(commitment, Mapping) or commitment.get(
        "candidate_freeze_commit"
    ) != candidate:
        raise P5V3ScoringError("FORMAL_SEALING_COMMITMENT_MISMATCH")


def _validate_artifact_index(
    *, run_dir: Path, manifest: Mapping[str, Any]
) -> tuple[Path, Path]:
    index_path = _safe_artifact_path(
        run_dir, manifest.get("artifact_index_path"), expected="artifact_index.json"
    )
    try:
        index = P5ArtifactIndexV3.model_validate(_load_json(index_path, "RUN_ARTIFACT_INDEX_INVALID"))
    except ValidationError as exc:
        raise P5V3ScoringError("RUN_ARTIFACT_INDEX_INVALID") from exc
    if (
        index.artifact_index_hash != manifest.get("artifact_index_hash")
        or index.subject_commit != manifest.get("subject_commit")
        or index.dirty_tree != manifest.get("dirty_tree")
    ):
        raise P5V3ScoringError("RUN_ARTIFACT_INDEX_BINDING_INVALID")
    entries = {entry.path: entry for entry in index.entries}
    if set(entries) != {"case_results.jsonl", "failure_records.jsonl"}:
        raise P5V3ScoringError("RUN_ARTIFACT_INDEX_SET_INVALID")
    result: dict[str, Path] = {}
    for name, entry in entries.items():
        path = _safe_artifact_path(run_dir, name, expected=name)
        if (
            entry.byte_size != path.stat().st_size
            or entry.sha256 != _sha256_file(path)
            or entry.generated_by != "scripts.run_trip_check_p5_v3_eval"
        ):
            raise P5V3ScoringError("RUN_ARTIFACT_INDEX_ENTRY_INVALID")
        result[name] = path
    if (
        manifest.get("case_results_path") != "case_results.jsonl"
        or manifest.get("failure_records_path") != "failure_records.jsonl"
    ):
        raise P5V3ScoringError("RUN_ARTIFACT_PATH_INVALID")
    return result["case_results.jsonl"], result["failure_records.jsonl"]


def _validate_terminal_binding(
    case: P5CaseV3, output: P5TerminalOutputV3, spec: P5VariantRunSpecV3
) -> None:
    materialization = case.materialization
    expected = {
        "input_hash": case.normalized_input_sha256,
        "materialization_hash": materialization.materialization_sha256,
        "render_receipt_hash": (
            materialization.render_receipt.content_sha256
            if materialization.render_receipt
            else None
        ),
        "ocr_receipt_hash": (
            materialization.ocr_baseline_receipt.content_sha256
            if materialization.ocr_baseline_receipt
            else None
        ),
        "provider_snapshot_hash": materialization.provider_snapshot.content_sha256,
        "evidence_snapshot_hash": materialization.evidence_snapshot.content_sha256,
        "fault_script_hash": materialization.fault_script.content_sha256,
        "run_spec_hash": spec.run_spec_hash,
        "adapter_version": spec.adapter_version,
        "repair_strategy": spec.repair_strategy,
    }
    if {key: getattr(output, key) for key in expected} != expected:
        raise P5V3ScoringError("TERMINAL_ARTIFACT_BINDING_MISMATCH")
    if sorted(output.candidate_set_hashes) != sorted(
        item.content_sha256 for item in materialization.candidate_sets
    ):
        raise P5V3ScoringError("TERMINAL_CANDIDATE_SET_BINDING_MISMATCH")
    if (
        output.split != case.split
        or output.city != case.city
        or output.input_kind != case.input_kind
        or output.semantic_output_hash != semantic_output_hash_v3(output)
        or output.replay_hash != output.semantic_output_hash
    ):
        raise P5V3ScoringError("TERMINAL_CASE_OR_HASH_BINDING_MISMATCH")


def _validate_ocr_provenance(
    *,
    manifest: Mapping[str, Any],
    cases: Sequence[P5CaseV3],
    materialization_by_case: Mapping[str, Mapping[str, Any]],
    outputs: Sequence[P5TerminalOutputV3],
    require_formal: bool,
) -> None:
    provenance = manifest.get("ocr_replay_provenance")
    if not isinstance(provenance, Mapping) or {
        key: provenance.get(key)
        for key in (
            "mode",
            "evidence_class",
            "actual_ocr_materialization",
            "fresh_actual_ocr_execution",
            "fresh_model_inference",
            "baseline_engine",
            "baseline_engine_version",
            "cache_implementation_version",
            "cache_key_policy",
            "v3_receipt_rebinding",
        )
    } != {
        "mode": "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY",
        "evidence_class": "snapshot_replay",
        "actual_ocr_materialization": "PASS_HISTORICAL_V2_RECEIPT",
        "fresh_actual_ocr_execution": "NOT_RUN",
        "fresh_model_inference": False,
        "baseline_engine": "paddleocr",
        "baseline_engine_version": "3.7.0",
        "cache_implementation_version": "p5-evaluation-ocr-cache-v3",
        "cache_key_policy": "image_bytes_sha256",
        "v3_receipt_rebinding": "PASS",
    }:
        raise P5V3ScoringError("RUN_OCR_REPLAY_PROVENANCE_INVALID")
    screenshot_cases = {
        case.case_id: case for case in cases if case.input_kind == "SYNTHETIC_SCREENSHOT"
    }
    variant_ids = set(manifest["variant_ids"])
    product_variants = variant_ids & {"core_b", "solver_c"}
    replay_factor = 2 if manifest.get("replay_executed") else 1
    expected_lookups = len(screenshot_cases) * len(product_variants) * replay_factor
    unique_hashes = {
        str(materialization_by_case[case_id]["ocr_baseline_receipt"]["asset_hash"])
        for case_id in screenshot_cases
    }
    expected_counts = {
        "preload_receipt_count": len(screenshot_cases) if product_variants else 0,
        "unique_hash_count": len(unique_hashes) if product_variants else 0,
        "lookup_count": expected_lookups,
        "hit_count": expected_lookups,
        "miss_count": 0,
        "fallback_count": 0,
        "fresh_prediction_count": 0,
        "receipt_match_count": expected_lookups,
        "cleanup_deleted_count": expected_lookups,
        "legacy_cache_access_count": 0,
        "terminal_provenance_count": expected_lookups,
    }
    if any(provenance.get(key) != value for key, value in expected_counts.items()):
        raise P5V3ScoringError("RUN_OCR_REPLAY_COUNTS_INVALID")
    if require_formal and (
        provenance.get("nonblind_unique_image_hashes") != 126
        or provenance.get("expected_formal_lookup_count") != 504
        or len(unique_hashes) != 126
        or expected_lookups != 504
    ):
        raise P5V3ScoringError("FORMAL_OCR_REPLAY_SHAPE_INVALID")
    for output in outputs:
        receipts = [
            item
            for item in output.receipts
            if isinstance(item, Mapping) and item.get("type") == "ocr_replay_provenance"
        ]
        required = output.case_id in screenshot_cases and output.variant_id in product_variants
        if not required:
            if receipts:
                raise P5V3ScoringError("TERMINAL_OCR_REPLAY_PROVENANCE_UNEXPECTED")
            continue
        if len(receipts) != 1:
            raise P5V3ScoringError("TERMINAL_OCR_REPLAY_PROVENANCE_MISSING")
        materialization = materialization_by_case[output.case_id]
        receipt = receipts[0]
        if {
            "mode": receipt.get("mode"),
            "evidence_class": receipt.get("evidence_class"),
            "fresh_model_inference": receipt.get("fresh_model_inference"),
            "cache_implementation_version": receipt.get("cache_implementation_version"),
            "image_sha256": receipt.get("image_sha256"),
            "materialization_hash": receipt.get("materialization_hash"),
            "receipt_match": receipt.get("receipt_match"),
            "cleanup_status": receipt.get("cleanup_status"),
            "cleanup_error_category": receipt.get("cleanup_error_category"),
            "temporary_original_absent": receipt.get("temporary_original_absent"),
        } != {
            "mode": "FROZEN_ACTUAL_OCR_RECEIPT_REPLAY",
            "evidence_class": "snapshot_replay",
            "fresh_model_inference": False,
            "cache_implementation_version": "p5-evaluation-ocr-cache-v3",
            "image_sha256": materialization["ocr_baseline_receipt"]["asset_hash"],
            "materialization_hash": materialization["materialization_hash"],
            "receipt_match": True,
            "cleanup_status": "DELETED",
            "cleanup_error_category": None,
            "temporary_original_absent": True,
        }:
            raise P5V3ScoringError("TERMINAL_OCR_REPLAY_PROVENANCE_INVALID")


def validate_run_group_v3(
    *,
    run_dir: Path,
    repo_root: Path | None = None,
    require_formal: bool = True,
) -> tuple[
    dict[str, Any],
    list[P5CaseV3],
    list[P5TerminalOutputV3],
    dict[str, dict[str, Any]],
]:
    """Validate a v3 non-blind run before exposing any scoring result."""

    root = repo_root.resolve() if repo_root is not None else Path(__file__).parents[4]
    run_dir = run_dir.absolute()
    manifest_path = _safe_artifact_path(
        run_dir, "run_group_manifest.json", expected="run_group_manifest.json"
    )
    manifest = _load_json(
        manifest_path, "RUN_GROUP_MANIFEST_INVALID"
    )
    if set(manifest) != RUN_GROUP_FIELDS_V3:
        raise P5V3ScoringError("RUN_GROUP_MANIFEST_FIELDS_INVALID")
    _validate_run_manifest_types(manifest)
    if manifest.get("schema_version") != "trip-check-p5-run-group-v3":
        raise P5V3ScoringError("RUN_GROUP_MANIFEST_VERSION_INVALID")
    if manifest.get("manifest_hash") != digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        raise P5V3ScoringError("RUN_GROUP_MANIFEST_HASH_MISMATCH")
    variants = manifest.get("variant_ids")
    variants_valid = (
        isinstance(variants, list)
        and bool(variants)
        and all(isinstance(item, str) for item in variants)
        and len(variants) == len(set(variants))
        and set(variants).issubset(VARIANT_IDS_V3)
        and manifest.get("variant_count") == len(variants)
    )
    if (
        manifest.get("lane") != "nonblind"
        or manifest.get("status") != "PASS"
        or not variants_valid
        or manifest.get("blind_labels_read") is not False
        or manifest.get("external_api_calls") != 0
        or manifest.get("hidden_retry_count") != 0
        or manifest.get("fresh_ocr_model_inferences") != 0
        or manifest.get("human_evidence") != "NOT_RUN"
        or manifest.get("replay_hash_policy") != "p5-semantic-projection-v3"
    ):
        raise P5V3ScoringError("RUN_GROUP_CONTRACT_INVALID")
    if require_formal and (
        variants != list(VARIANT_IDS_V3)
        or manifest.get("formal_evidence") is not True
        or manifest.get("dirty_tree") is not False
    ):
        raise P5V3ScoringError("FORMAL_RUN_GROUP_CONTRACT_INVALID")

    dataset, all_cases, all_materialization_rows = _validate_dataset_v3(
        require_formal=require_formal
    )
    if manifest.get("dataset_manifest_hash") != dataset.get("manifest_hash"):
        raise P5V3ScoringError("RUN_GROUP_DATASET_BINDING_MISMATCH")
    if (
        manifest.get("cases_file_sha256") != _sha256_file(NONBLIND_PATH_V3)
        or manifest.get("materializations_file_sha256")
        != _sha256_file(NONBLIND_MATERIALIZATIONS_PATH_V3)
    ):
        raise P5V3ScoringError("RUN_GROUP_DATASET_FILE_HASH_MISMATCH")
    if require_formal:
        _validate_active_and_git(repo_root=root, manifest=manifest, dataset=dataset)

    case_results_path, failure_records_path = _validate_artifact_index(
        run_dir=run_dir, manifest=manifest
    )
    if manifest.get("case_results_file_sha256") != _sha256_file(case_results_path):
        raise P5V3ScoringError("CASE_RESULTS_FILE_HASH_MISMATCH")
    case_result_rows = _load_jsonl(case_results_path, "CASE_RESULTS_JSONL_INVALID")
    try:
        case_results = [P5CaseResultV3.model_validate(row) for row in case_result_rows]
    except ValidationError as exc:
        raise P5V3ScoringError("CASE_RESULT_SCHEMA_OR_HASH_INVALID") from exc
    if digest([item.model_dump(mode="json") for item in case_results]) != manifest.get(
        "case_results_content_sha256"
    ):
        raise P5V3ScoringError("CASE_RESULTS_CONTENT_HASH_MISMATCH")
    outputs = [item.terminal_output for item in case_results]
    selected_case_ids = {output.case_id for output in outputs}
    all_case_by_id = {case.case_id: case for case in all_cases}
    if not selected_case_ids or not selected_case_ids.issubset(all_case_by_id):
        raise P5V3ScoringError("CASE_RESULT_EXACT_KEY_SET_MISMATCH")
    cases = [case for case in all_cases if case.case_id in selected_case_ids]
    materialization_by_case = {
        str(row["case_id"]): row
        for row in all_materialization_rows
        if row.get("case_id") in selected_case_ids
    }
    expected_keys = {
        (case.case_id, variant_id) for case in cases for variant_id in variants
    }
    actual_keys = [(output.case_id, output.variant_id) for output in outputs]
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        raise P5V3ScoringError("CASE_RESULT_EXACT_KEY_SET_MISMATCH")
    expected_count = len(cases) * len(variants)
    if (
        manifest.get("case_count") != len(cases)
        or manifest.get("case_set_hash")
        != case_set_hash_v3(
            [case.model_dump(mode="json", exclude_none=True) for case in cases]
        )
        or manifest.get("materialization_set_hash")
        != materialization_set_hash_v3(
            [materialization_by_case[case.case_id] for case in cases]
        )
        or manifest.get("terminal_count") != expected_count
        or manifest.get("expected_terminal_count") != expected_count
        or len(outputs) != expected_count
    ):
        raise P5V3ScoringError("RUN_GROUP_CASE_RESULT_BINDING_MISMATCH")
    if (
        manifest.get("replay_executed") is not True
        or manifest.get("replay_match_count") != expected_count
        or manifest.get("replay_mismatches") != []
    ):
        raise P5V3ScoringError("RUN_GROUP_REPLAY_INVALID")
    if require_formal and (len(cases) != 270 or expected_count != 810):
        raise P5V3ScoringError("FORMAL_CASE_RESULT_SHAPE_INVALID")

    raw_specs = manifest.get("run_specs")
    if not isinstance(raw_specs, Mapping) or set(raw_specs) != set(variants):
        raise P5V3ScoringError("RUN_SPEC_SET_INVALID")
    try:
        specs = {
            variant_id: P5VariantRunSpecV3.model_validate(raw_specs[variant_id])
            for variant_id in variants
        }
        validate_run_spec_whitelist_v3(list(specs.values()))
    except (ValidationError, ValueError) as exc:
        raise P5V3ScoringError("RUN_SPEC_SCHEMA_OR_WHITELIST_INVALID") from exc
    common: list[dict[str, Any]] = []
    for variant_id, spec in specs.items():
        if (
            spec.variant_id != variant_id
            or spec.subject_commit != manifest.get("subject_commit")
            or spec.dirty_tree != manifest.get("dirty_tree")
            or spec.lane != "nonblind"
            or spec.dataset_manifest_hash != dataset.get("manifest_hash")
            or spec.case_set_hash != manifest.get("case_set_hash")
            or spec.materialization_set_hash != manifest.get("materialization_set_hash")
            or spec.run_spec_template_hash != file_sha256(RUN_SPEC_TEMPLATE_PATH_V3)
            or spec.rubric_hash != file_sha256(JUDGE_RUBRIC_PATH_V2)
            or spec.replay_hash_policy != "p5-semantic-projection-v3"
        ):
            raise P5V3ScoringError("RUN_SPEC_BINDING_MISMATCH")
        common.append(
            {
                key: value
                for key, value in spec.model_dump(mode="json").items()
                if key not in RUN_SPEC_VARIANT_WHITELIST_V3
            }
        )
    if any(item != common[0] for item in common[1:]):
        raise P5V3ScoringError("RUN_SPEC_VARIANT_WHITELIST_VIOLATION")

    for result in case_results:
        output = result.terminal_output
        case = all_case_by_id[output.case_id]
        if (
            result.run_id != manifest.get("run_id")
            or result.case_result_id
            != f"{manifest['run_id']}:{output.case_id}:{output.variant_id}"
            or result.revision_lineage != revision_lineage_v3(case=case, terminal=output)
        ):
            raise P5V3ScoringError("CASE_RESULT_RUN_OR_LINEAGE_BINDING_MISMATCH")
        _validate_terminal_binding(case, output, specs[output.variant_id])

    if manifest.get("failure_records_file_sha256") != _sha256_file(
        failure_records_path
    ):
        raise P5V3ScoringError("FAILURE_RECORDS_FILE_HASH_MISMATCH")
    failure_rows = _load_jsonl(failure_records_path, "FAILURE_RECORDS_JSONL_INVALID")
    try:
        failures = [P5FailureRecordV3.model_validate(row) for row in failure_rows]
    except ValidationError as exc:
        raise P5V3ScoringError("FAILURE_RECORD_SCHEMA_OR_HASH_INVALID") from exc
    if digest([item.model_dump(mode="json") for item in failures]) != manifest.get(
        "failure_records_content_sha256"
    ):
        raise P5V3ScoringError("FAILURE_RECORDS_CONTENT_HASH_MISMATCH")
    expected_failures = {
        (output.case_id, output.variant_id): build_failure_record_v3(
            run_id=str(manifest["run_id"]), lane="nonblind", terminal=output
        )
        for output in outputs
        if output.terminal_status
        in {
            TerminalStatusV3.ERROR,
            TerminalStatusV3.TIMEOUT,
            TerminalStatusV3.UNSUPPORTED_CAPABILITY,
        }
    }
    actual_failure_keys = [(item.case_id, item.variant_id) for item in failures]
    if (
        len(actual_failure_keys) != len(set(actual_failure_keys))
        or set(actual_failure_keys) != set(expected_failures)
        or manifest.get("failure_record_count") != len(failures)
        or any(
            failure
            != expected_failures[(failure.case_id, failure.variant_id)]
            for failure in failures
        )
    ):
        raise P5V3ScoringError("FAILURE_RECORD_EXACT_BINDING_INVALID")

    _validate_ocr_provenance(
        manifest=manifest,
        cases=cases,
        materialization_by_case=materialization_by_case,
        outputs=outputs,
        require_formal=require_formal,
    )
    return manifest, cases, outputs, materialization_by_case


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(
        0,
        min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1),
    )
    return ordered[index]


def _buckets(
    scores: Sequence[P5CaseScoreV3], key: Callable[[P5CaseScoreV3], str]
) -> dict[str, Any]:
    grouped: dict[str, list[P5CaseScoreV3]] = defaultdict(list)
    for score in scores:
        grouped[key(score)].append(score)
    return {
        name: aggregate_scores_v2(items) for name, items in sorted(grouped.items())
    }


def _paired(
    core: Mapping[str, P5CaseScoreV3], challenger: Mapping[str, P5CaseScoreV3]
) -> dict[str, Any]:
    if set(core) != set(challenger):
        raise P5V3ScoringError("PAIRED_CASE_SET_MISMATCH")
    differences = [
        int(challenger[case_id].task_success) - int(core[case_id].task_success)
        for case_id in sorted(core)
    ]
    improvement = mean(differences) if differences else 0.0
    if len(differences) > 1:
        variance = sum((item - improvement) ** 2 for item in differences) / (
            len(differences) - 1
        )
        standard_error = (variance / len(differences)) ** 0.5
    else:
        standard_error = 0.0
    lower = max(-1.0, improvement - 1.96 * standard_error)
    upper = min(1.0, improvement + 1.96 * standard_error)
    return {
        "paired_case_count": len(differences),
        "challenger_wins": sum(item == 1 for item in differences),
        "core_wins": sum(item == -1 for item in differences),
        "ties": sum(item == 0 for item in differences),
        "task_success_improvement_percentage_points": improvement * 100,
        "confidence_interval_95_percentage_points": [lower * 100, upper * 100],
        "confidence_interval_crosses_zero": lower <= 0 <= upper,
    }


def _coverage_metrics(
    items: Sequence[P5CaseScoreV3], case_by_id: Mapping[str, P5CaseV3]
) -> dict[str, Any]:
    advice_required = [item for item in items if item.advice_coverage != "NOT_REQUIRED"]
    candidate_required = [
        item
        for item in items
        if case_by_id[item.case_id].oracle is not None
        and case_by_id[item.case_id].oracle.candidate_receipt_mode == "REQUIRED"
    ]
    return {
        "advice_required_case_count": len(advice_required),
        "advice_covered_case_count": sum(
            item.advice_coverage == "PASS" for item in advice_required
        ),
        "candidate_receipt_required_case_count": len(candidate_required),
        "candidate_receipt_covered_case_count": sum(
            item.candidate_receipt_coverage == "PASS" for item in candidate_required
        ),
    }


def build_score_report_v3(
    *,
    manifest: Mapping[str, Any],
    cases: Sequence[P5CaseV3],
    outputs: Sequence[P5TerminalOutputV3],
    materializations: Mapping[str, Mapping[str, Any]],
    formal_validation_performed: bool,
    include_case_scores: bool = True,
) -> dict[str, Any]:
    case_by_id = {case.case_id: case for case in cases}
    scores = [
        score_case_v3(
            case_by_id[output.case_id],
            output,
            materialization=materializations[output.case_id],
        )
        for output in outputs
    ]
    outputs_by_key = {(item.case_id, item.variant_id): item for item in outputs}
    variant_metrics: dict[str, Any] = {}
    score_maps: dict[str, dict[str, P5CaseScoreV3]] = {}
    for variant_id in VARIANT_IDS_V3:
        items = [item for item in scores if item.variant_id == variant_id]
        score_maps[variant_id] = {item.case_id: item for item in items}
        latencies = [outputs_by_key[(item.case_id, variant_id)].latency_ms for item in items]
        overall = aggregate_scores_v2(items)
        overall.update(_coverage_metrics(items, case_by_id))
        overall.update(
            {
                "execution_status": "PASS" if items else "NOT_RUN",
                "latency_p50_ms": median(latencies) if latencies else None,
                "latency_p95_ms": _percentile(latencies, 0.95) if latencies else None,
                "terminal_status_counts": dict(
                    sorted(Counter(item.terminal_status.value for item in items).items())
                ),
            }
        )
        variant_metrics[variant_id] = {
            "overall": overall,
            "by_split": _buckets(items, lambda item: item.split),
            "by_city": _buckets(items, lambda item: item.city),
            "by_input_kind": _buckets(items, lambda item: item.input_kind),
            "by_difficulty": _buckets(items, lambda item: item.difficulty),
            "by_fault_profile": _buckets(items, lambda item: item.fault_profile_id),
            "by_finding": _buckets(
                items, lambda item: "+".join(item.required_reason_codes) or "NONE"
            ),
            "by_repair_outcome": _buckets(
                items,
                lambda item: str(
                    case_by_id[item.case_id].oracle.expected_strategy_outcome
                ),
            ),
        }
    core = variant_metrics["core_b"]["overall"]
    pilot = variant_metrics["core_b"]["by_split"].get("pilot", {})
    regression = variant_metrics["core_b"]["by_split"].get("regression", {})
    dimensions = core["quality_dimensions"]
    location = dimensions["location_city_facts"]
    continuity = dimensions["time_route_hotel_continuity"]
    other = dimensions["other_advice"]
    zero_tolerance_checks = {
        "deterministic_failure_zero": core["deterministic_failure_count"] == 0,
        "wrong_city_or_poi_zero": core["wrong_city_or_poi_count"] == 0,
        "hard_finding_miss_zero": core["hard_finding_miss_count"] == 0,
        "unknown_failure_zero": core["unknown_failure_count"] == 0,
        "candidate_receipt_failure_zero": core["candidate_receipt_failure_count"] == 0,
        "concurrency_failure_zero": core["concurrency_failure_count"] == 0,
        "postcheck_failure_zero": core["postcheck_failure_count"] == 0,
        "replay_failure_zero": core["replay_failure_count"] == 0,
        "unsupported_claim_zero": core["unsupported_claim_count"] == 0,
        "usage_failure_zero": core["usage_measurement_failure_count"] == 0
        and core["token_count_total"] == 0
        and core["cost_usd_total"] == 0,
    }
    stage_checks = {
        "pilot_18_of_18": pilot.get("case_count") == 18
        and pilot.get("task_success_count") == 18,
        "regression_72_of_72": regression.get("case_count") == 72
        and regression.get("task_success_count") == 72,
        "mean_score_gte_88": core["mean_score"] >= 88,
        "location_city_gte_90_nonempty": location.get("case_count", 0) > 0
        and location.get("mean_score") is not None
        and location["mean_score"] >= 90,
        "time_route_hotel_gte_90_nonempty": continuity.get("case_count", 0) > 0
        and continuity.get("mean_score") is not None
        and continuity["mean_score"] >= 90,
        "other_advice_each_gte_80_nonempty": bool(other.get("buckets"))
        and other.get("minimum_bucket_score") is not None
        and other["minimum_bucket_score"] >= 80,
        "advice_required_coverage_100_nonempty": core[
            "advice_required_case_count"
        ]
        > 0
        and core["advice_covered_case_count"] == core["advice_required_case_count"],
        "nonpass_finding_advice_coverage_100_nonempty": core[
            "nonpass_finding_count"
        ]
        > 0
        and core["covered_nonpass_finding_count"] == core["nonpass_finding_count"],
        "specific_candidate_receipt_coverage_100_nonempty": core[
            "candidate_receipt_required_case_count"
        ]
        > 0
        and core["candidate_receipt_covered_case_count"]
        == core["candidate_receipt_required_case_count"],
    }
    core_executed = len(score_maps["core_b"]) == len(cases) and bool(cases)
    passed = (
        formal_validation_performed
        and core_executed
        and all(zero_tolerance_checks.values())
        and all(stage_checks.values())
    )
    paired = {
        variant_id: (
            _paired(score_maps["core_b"], score_maps[variant_id])
            if score_maps["core_b"] and score_maps[variant_id]
            else {"status": "NOT_RUN"}
        )
        for variant_id in ("legacy_a", "solver_c")
    }
    report: dict[str, Any] = {
        "schema_version": "trip-check-p5-nonblind-score-report-v3",
        "status": "PASS" if passed else "REJECT",
        "formal_validation_performed": formal_validation_performed,
        "subject_commit": manifest["subject_commit"],
        "dataset_manifest_hash": manifest["dataset_manifest_hash"],
        "run_group_manifest_hash": manifest["manifest_hash"],
        "artifact_index_hash": manifest["artifact_index_hash"],
        "case_count": len(cases),
        "terminal_count": len(outputs),
        "variant_metrics": variant_metrics,
        "paired_comparisons": paired,
        "zero_tolerance_checks": zero_tolerance_checks,
        "stage_gate_checks": stage_checks,
        "promotion_decision": "KEEP_CORE_B" if passed else "REJECT_ALL_CANDIDATES",
        "solver_admission_inherited": "REJECT",
        "solver_may_promote_from_p5_score": False,
        "evidence_boundary": {
            "controlled_snapshot": "PASS" if formal_validation_performed else "DIAGNOSTIC_ONLY",
            "historical_ocr_receipt_replay": "PASS_HISTORICAL_V2_RECEIPT",
            "fresh_actual_ocr_execution": "NOT_RUN",
            "blind_labels_read": False,
            "blind_score": "NOT_RUN",
            "automated_proxy_judge": "NOT_RUN",
            "human_calibration_performed": False,
            "human_evidence": "NOT_RUN",
            "live_provider": "NOT_RUN",
            "public_e2e": "NOT_RUN",
            "production_release": "NOT_RUN",
            "main_merge": "NOT_RUN",
            "replay_readback": "RUNNER_ATTESTED_HASH_ONLY",
        },
    }
    if include_case_scores:
        report["case_scores"] = [item.model_dump(mode="json") for item in scores]
    report["report_hash"] = digest(report)
    return report


def score_run_group_v3(
    *,
    run_dir: Path,
    repo_root: Path | None = None,
    require_formal: bool = True,
) -> dict[str, Any]:
    manifest, cases, outputs, materializations = validate_run_group_v3(
        run_dir=run_dir,
        repo_root=repo_root,
        require_formal=require_formal,
    )
    return build_score_report_v3(
        manifest=manifest,
        cases=cases,
        outputs=outputs,
        materializations=materializations,
        formal_validation_performed=require_formal,
        include_case_scores=True,
    )


__all__ = [
    "P5CaseScoreV3",
    "P5V3ScoringError",
    "build_score_report_v3",
    "score_case_v3",
    "score_run_group_v3",
    "semantic_output_hash_v3",
    "validate_run_group_v3",
]
