"""Fail-closed deterministic scorer for P5 v2 run groups."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from statistics import mean, median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from evals.trip_check_v1.p5.contracts_v2 import (
    P5CaseV2,
    P5MaterializationBindingV2,
    P5OracleV2,
    P5TerminalOutputV2,
    P5VariantRunSpecV2,
    TerminalStatusV2,
    VARIANT_IDS_V2,
)
from evals.trip_check_v1.p5.data_contract import digest


RUN_GROUP_FIELDS_V2 = frozenset(
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
        "terminal_outputs_path",
        "terminal_outputs_file_sha256",
        "terminal_outputs_content_sha256",
        "variant_output_sha256",
        "replay_executed",
        "replay_match_count",
        "replay_mismatches",
        "blind_labels_read",
        "external_api_calls",
        "human_evidence",
        "manifest_hash",
    }
)
RUN_SPEC_VARIANT_WHITELIST_V2 = frozenset({"variant_id", "adapter_version", "repair_strategy"})


class P5V2ScoringError(ValueError):
    """A stable fail-closed run/scoring validation error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class P5CaseScoreV2(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "trip-check-p5-score-v2"
    case_id: str
    split: str
    city: str
    input_kind: str
    difficulty: str
    fault_profile_id: str
    variant_id: str
    terminal_status: TerminalStatusV2
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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V2ScoringError("JSONL_INVALID") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise P5V2ScoringError("JSONL_INVALID")
    return rows


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5V2ScoringError(reason) from exc
    if not isinstance(value, dict):
        raise P5V2ScoringError(reason)
    return value


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5V2ScoringError("ARTIFACT_UNREADABLE") from exc


def _contains_symlink_or_junction(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                return True
        except OSError:
            return True
    return False


def case_set_hash_v2(cases: Sequence[P5CaseV2]) -> str:
    return digest(
        [
            {"case_id": case.case_id, "case_hash": case.case_hash}
            for case in sorted(cases, key=lambda item: item.case_id)
        ]
    )


def materialization_set_hash_v2(cases: Sequence[P5CaseV2]) -> str:
    return digest(
        [
            {
                "case_id": case.case_id,
                "materialization_id": case.materialization.materialization_id,
                "materialization_hash": case.materialization.materialization_sha256,
            }
            for case in sorted(cases, key=lambda item: item.case_id)
        ]
    )


def semantic_output_hash_v2(output: P5TerminalOutputV2) -> str:
    value = output.model_dump(mode="json")
    return digest(
        {
            key: value[key]
            for key in (
                "case_id",
                "input_hash",
                "materialization_hash",
                "run_spec_hash",
                "variant_id",
                "adapter_version",
                "repair_strategy",
                "terminal_status",
                "capability_outcomes",
                "native_output",
                "evaluation_projection",
                "findings",
                "advice",
                "postcheck",
                "receipts",
                "token_count",
                "cost_usd",
                "error_category",
            )
        }
    )


def variant_output_hashes_v2(
    outputs: Sequence[P5TerminalOutputV2],
) -> dict[str, str]:
    return {
        variant_id: digest(
            [
                output.model_dump(mode="json")
                for output in sorted(
                    (item for item in outputs if item.variant_id == variant_id),
                    key=lambda item: item.case_id,
                )
            ]
        )
        for variant_id in VARIANT_IDS_V2
    }


def _materialization_rows_by_id(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, P5MaterializationBindingV2]:
    allowed_fields = {
        "schema_version",
        "materialization_id",
        "case_id",
        "source_payload",
        "render_receipt",
        "ocr_baseline_receipt",
        "provider_snapshot",
        "evidence_snapshot",
        "candidate_sets",
        "fault_script",
        "receipts",
        "materialization_hash",
    }
    result: dict[str, P5MaterializationBindingV2] = {}

    def artifact_binding(value: Any, *, case_id: str, synthetic_prefix: str | None = None) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise P5V2ScoringError("MATERIALIZATION_ROW_SCHEMA_INVALID")
        try:
            schema_version = value["schema_version"]
            artifact_id = value.get("artifact_id")
            content_sha256 = value.get("content_sha256")
            if synthetic_prefix is not None and artifact_id is None and content_sha256 is None:
                artifact_id = f"{synthetic_prefix}-{case_id}"
                content_sha256 = digest(value)
            if not isinstance(artifact_id, str) or not isinstance(content_sha256, str):
                raise KeyError("artifact binding")
            return {
                "artifact_id": artifact_id,
                "schema_version": schema_version,
                "content_sha256": content_sha256,
            }
        except KeyError as exc:
            raise P5V2ScoringError("MATERIALIZATION_ROW_SCHEMA_INVALID") from exc

    for row in rows:
        if set(row) != allowed_fields:
            raise P5V2ScoringError("MATERIALIZATION_ROW_SCHEMA_INVALID")
        case_id = row.get("case_id")
        materialization_hash = row.get("materialization_hash")
        if (
            not isinstance(case_id, str)
            or not isinstance(materialization_hash, str)
            or digest({key: value for key, value in row.items() if key != "materialization_hash"})
            != materialization_hash
        ):
            raise P5V2ScoringError("MATERIALIZATION_ROW_SCHEMA_INVALID")
        if case_id in result:
            raise P5V2ScoringError("MATERIALIZATION_CASE_DUPLICATE")
        raw_binding = {
            "schema_version": "trip-check-p5-materialization-binding-v2",
            "materialization_id": row["materialization_id"],
            "materialization_sha256": materialization_hash,
            "source_payload": artifact_binding(row["source_payload"], case_id=case_id),
            "render_receipt": artifact_binding(row["render_receipt"], case_id=case_id, synthetic_prefix="render"),
            "ocr_baseline_receipt": artifact_binding(
                row["ocr_baseline_receipt"], case_id=case_id, synthetic_prefix="ocr"
            ),
            "provider_snapshot": artifact_binding(row["provider_snapshot"], case_id=case_id),
            "evidence_snapshot": artifact_binding(row["evidence_snapshot"], case_id=case_id),
            "candidate_sets": [artifact_binding(item, case_id=case_id) for item in row["candidate_sets"]],
            "fault_script": artifact_binding(row["fault_script"], case_id=case_id),
        }
        try:
            result[case_id] = P5MaterializationBindingV2.model_validate(raw_binding)
        except ValidationError as exc:
            raise P5V2ScoringError("MATERIALIZATION_ROW_SCHEMA_INVALID") from exc
    return result


def _finding_reason_codes(output: P5TerminalOutputV2) -> set[str]:
    return {
        str(item["reason_code"])
        for item in output.findings
        if isinstance(item.get("reason_code"), str) and item["reason_code"]
    }


def _contains_unknown_or_unavailable_status(value: Any) -> bool:
    if isinstance(value, dict):
        for key in ("status", "source_status", "evidence_status", "fact_status"):
            if value.get(key) in {"UNKNOWN", "UNAVAILABLE"}:
                return True
        return any(_contains_unknown_or_unavailable_status(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_unknown_or_unavailable_status(item) for item in value)
    return False


def _unknown_promoted_to_pass(value: Any) -> bool:
    if isinstance(value, dict):
        original = value.get("source_status", value.get("evidence_status"))
        promoted = value.get(
            "resolved_status",
            value.get("result_status", value.get("fact_status")),
        )
        if original in {"UNKNOWN", "UNAVAILABLE"} and promoted == "PASS":
            return True
        for key in (
            "unknown_or_unavailable_pass_count",
            "unknown_promoted_to_pass_count",
            "unavailable_promoted_to_pass_count",
        ):
            count = value.get(key)
            if isinstance(count, int) and not isinstance(count, bool) and count > 0:
                return True
        return any(_unknown_promoted_to_pass(item) for item in value.values())
    if isinstance(value, list):
        return any(_unknown_promoted_to_pass(item) for item in value)
    return False


def _strategy_matches(output: P5TerminalOutputV2, expected: str) -> bool:
    strategy = output.native_output.get("solver_strategy", {})
    if not isinstance(strategy, dict):
        strategy = {}
    primary = strategy.get("primary_status", strategy.get("status"))
    effective = strategy.get("effective_status", primary)
    fallback_used = strategy.get("fallback_used") is True
    if expected == "FEASIBLE":
        if output.variant_id == "solver_c" and primary is not None:
            return primary == "SUCCESS" or (fallback_used and effective == "SUCCESS")
        return output.terminal_status in {
            TerminalStatusV2.SUCCEEDED,
            TerminalStatusV2.NEEDS_USER_RESOLUTION,
        }
    if expected == "UNSAT":
        return primary == "UNSAT"
    if expected == "TIMEOUT":
        return primary == "TIMEOUT"
    if expected == "FALLBACK":
        return primary in {"ERROR", "TIMEOUT", "UNSAT"} and fallback_used and effective == "SUCCESS"
    return False


def _concurrency_receipt(output: P5TerminalOutputV2) -> dict[str, Any] | None:
    def find(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            if value.get("schema_version") == "trip-check-p5-apply-fault-receipt-v2":
                return value
            for key in ("receipt", "payload", "data"):
                nested = value.get(key)
                found = find(nested)
                if found is not None:
                    return found
        return None

    for receipt in output.receipts:
        found = find(receipt)
        if found is not None:
            return found
    return None


def _concurrency_result(
    output: P5TerminalOutputV2,
    expectation: str,
    materialization: Mapping[str, Any] | None,
) -> str:
    if expectation == "NONE":
        return "NOT_REQUIRED"
    if materialization is None:
        return "FAIL"
    fault_artifact = materialization.get("fault_script")
    if not isinstance(fault_artifact, dict):
        return "FAIL"
    script = fault_artifact.get("script")
    if not isinstance(script, dict):
        return "FAIL"
    script_hash = script.get("script_sha256")
    if (
        not isinstance(script_hash, str)
        or digest({key: value for key, value in script.items() if key != "script_sha256"}) != script_hash
        or fault_artifact.get("content_sha256") != output.fault_script_hash
    ):
        return "FAIL"
    receipt = _concurrency_receipt(output)
    if not receipt or receipt.get("status") != "PASS":
        return "FAIL"
    expected_receipt_fields = {
        "type",
        "schema_version",
        "status",
        "case_id",
        "workspace_id",
        "fault_profile_id",
        "script_sha256",
        "barrier",
        "attempts",
        "side_effects",
        "error_categories",
        "semantic_projection",
        "semantic_hash",
        "receipt_sha256",
    }
    if set(receipt) != expected_receipt_fields:
        return "FAIL"
    if (
        receipt.get("type") != "concurrency"
        or receipt.get("case_id") != output.case_id
        or receipt.get("workspace_id") != script.get("workspace_id")
        or receipt.get("fault_profile_id") != script.get("fault_profile_id")
        or receipt.get("script_sha256") != script_hash
        or receipt.get("receipt_sha256")
        != digest({key: value for key, value in receipt.items() if key != "receipt_sha256"})
    ):
        return "FAIL"
    projection = receipt.get("semantic_projection")
    if (
        not isinstance(projection, dict)
        or projection.get("all_invariants_passed") is not True
        or projection.get("case_id") != output.case_id
        or projection.get("fault_profile_id") != script.get("fault_profile_id")
        or projection.get("script_sha256") != script_hash
        or receipt.get("semantic_hash") != digest(projection)
    ):
        return "FAIL"
    expected_attempts = script.get("attempts")
    actual_attempts = receipt.get("attempts")
    if not isinstance(expected_attempts, list) or not isinstance(actual_attempts, list):
        return "FAIL"
    expected_attempt_bindings = [
        {key: attempt.get(key) for key in ("attempt_id", "ordinal", "repair_id", "idempotency_key")}
        for attempt in sorted(expected_attempts, key=lambda item: item.get("ordinal", -1))
        if isinstance(attempt, dict)
    ]
    actual_attempt_bindings = [
        {key: attempt.get(key) for key in ("attempt_id", "ordinal", "repair_id", "idempotency_key")}
        for attempt in sorted(actual_attempts, key=lambda item: item.get("ordinal", -1))
        if isinstance(attempt, dict)
    ]
    if len(expected_attempt_bindings) != 2 or actual_attempt_bindings != expected_attempt_bindings:
        return "FAIL"
    counts = projection.get("outcome_counts")
    if not isinstance(counts, dict):
        return "FAIL"
    if expectation == "IDEMPOTENT_REPLAY":
        return (
            "PASS"
            if receipt.get("fault_profile_id") == "duplicate_apply"
            and counts.get("APPLIED") == 1
            and counts.get("IDEMPOTENT_REPLAY") == 1
            and sum(value for value in counts.values() if isinstance(value, int)) == 2
            else "FAIL"
        )
    return (
        "PASS"
        if receipt.get("fault_profile_id") == "concurrent_apply"
        and counts.get("APPLIED") == 1
        and counts.get("STALE", 0) + counts.get("CONFLICT", 0) == 1
        and sum(value for value in counts.values() if isinstance(value, int)) == 2
        else "FAIL"
    )


def _terminal_is_ok(output: P5TerminalOutputV2, oracle: P5OracleV2) -> bool:
    expected = TerminalStatusV2.NEEDS_USER_RESOLUTION if oracle.requires_user_resolution else TerminalStatusV2.SUCCEEDED
    return output.terminal_status == expected


def _required_obligation_satisfied(
    code: str,
    *,
    output: P5TerminalOutputV2,
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
        return detected in {"DETECTED", "PASS"} or (
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
                "TRAVEL_TIME_GAP",
            }
        )
    if code in {"P4_DUPLICATE_APPLY", "P4_CONCURRENT_APPLY"}:
        return concurrency_status == "PASS"
    if code in {"P4_SOLVER_UNSAT", "P4_SOLVER_TIMEOUT", "P4_SOLVER_FALLBACK"}:
        return strategy_match
    return False


def _candidate_status(
    case: P5CaseV2,
    oracle: P5OracleV2,
    output: P5TerminalOutputV2,
    materialization: Mapping[str, Any] | None,
) -> str:
    projection = output.evaluation_projection
    selected = projection.get("selected_place_ids")
    selected_ids = selected if isinstance(selected, list) else None
    coverage = projection.get("candidate_receipt_coverage")
    materialized_hashes = [item.content_sha256 for item in case.materialization.candidate_sets]
    if sorted(output.candidate_set_hashes) != sorted(materialized_hashes):
        return "FAIL"
    if not oracle.specific_place_allowed and selected_ids != []:
        return "FAIL"
    if oracle.candidate_receipt_mode == "REQUIRED":
        if (
            not selected_ids
            or not materialized_hashes
            or materialization is None
            or not isinstance(coverage, (int, float))
            or isinstance(coverage, bool)
            or float(coverage) != 1.0
        ):
            return "FAIL"
        raw_sets = materialization.get("candidate_sets")
        if not isinstance(raw_sets, list):
            return "FAIL"
        candidates: dict[str, Mapping[str, Any]] = {}
        for artifact in raw_sets:
            if not isinstance(artifact, Mapping):
                return "FAIL"
            candidate_set = artifact.get("candidate_set")
            raw_candidates = candidate_set.get("candidates") if isinstance(candidate_set, Mapping) else None
            if not isinstance(raw_candidates, list):
                return "FAIL"
            for candidate in raw_candidates:
                if not isinstance(candidate, Mapping):
                    return "FAIL"
                candidate_id = candidate.get("canonical_place_id")
                if not isinstance(candidate_id, str) or candidate_id in candidates:
                    return "FAIL"
                candidates[candidate_id] = candidate
        successful_receipt_ids = {
            receipt["receipt_id"]
            for receipt in output.receipts
            if isinstance(receipt, dict)
            and isinstance(receipt.get("receipt_id"), str)
            and receipt.get("status") in {"PASS", "SUCCEEDED"}
        }
        for selected_id in selected_ids:
            candidate = candidates.get(selected_id) if isinstance(selected_id, str) else None
            if candidate is None:
                return "FAIL"
            place_receipt_id = candidate.get("place_receipt_id")
            route_receipt_ids = candidate.get("route_receipt_ids")
            if (
                not isinstance(place_receipt_id, str)
                or place_receipt_id not in successful_receipt_ids
                or not isinstance(route_receipt_ids, list)
                or not route_receipt_ids
                or any(
                    not isinstance(receipt_id, str) or receipt_id not in successful_receipt_ids
                    for receipt_id in route_receipt_ids
                )
            ):
                return "FAIL"
        return "PASS"
    if oracle.candidate_receipt_mode == "FORBIDDEN":
        return "PASS" if selected_ids == [] and not materialized_hashes else "FAIL"
    return "NOT_REQUIRED" if selected_ids is not None else "FAIL"


def _nonpass_finding_advice_coverage(
    output: P5TerminalOutputV2,
) -> tuple[int, int]:
    nonpass = [
        item
        for item in output.findings
        if isinstance(item, Mapping)
        and str(item.get("status", "")).upper()
        not in {"PASS", "PASSED", "SATISFIED", "RESOLVED"}
    ]
    # The frozen v2 adapter emits one stable Advice projection for each
    # non-PASS Finding, in Finding order.  Do not infer coverage from a case-
    # level boolean: missing rows must reduce the measured coverage.
    covered = min(
        len(nonpass),
        sum(isinstance(item, Mapping) and bool(item) for item in output.advice),
    )
    return len(nonpass), covered


def _deterministic_unsupported_claim_count(
    output: P5TerminalOutputV2,
    *,
    candidate_status: str,
) -> int:
    count = 0
    for advice in output.advice:
        if not isinstance(advice, Mapping):
            count += 1
            continue
        if advice.get("unsupported_claim") is True or str(
            advice.get("claim_support", "")
        ).upper() in {"UNSUPPORTED", "UNVERIFIED"}:
            count += 1
        if advice.get("candidate_set_bound") is True and candidate_status != "PASS":
            count += 1
    return count


def _usage_measurement(output: P5TerminalOutputV2) -> str:
    token_valid = output.token_count == "NOT_MEASURED" or (
        isinstance(output.token_count, int)
        and not isinstance(output.token_count, bool)
        and output.token_count == 0
    )
    cost_valid = output.cost_usd == "NOT_MEASURED" or (
        isinstance(output.cost_usd, (int, float))
        and not isinstance(output.cost_usd, bool)
        and float(output.cost_usd) == 0.0
    )
    if not token_valid or not cost_valid:
        return "FAIL"
    if output.token_count == "NOT_MEASURED" or output.cost_usd == "NOT_MEASURED":
        return "NOT_MEASURED"
    return "MEASURED_ZERO"


def score_case_v2(
    case: P5CaseV2,
    output: P5TerminalOutputV2,
    *,
    oracle_override: P5OracleV2 | None = None,
    materialization: Mapping[str, Any] | None = None,
) -> P5CaseScoreV2:
    oracle = oracle_override or case.oracle
    if oracle is None:
        raise P5V2ScoringError("ORACLE_REQUIRED_FOR_SCORING")
    projection = output.evaluation_projection
    required = list(dict.fromkeys(oracle.required_reason_codes))
    wrong = projection.get("wrong_city_or_poi_count")
    wrong_count = wrong if isinstance(wrong, int) and not isinstance(wrong, bool) and wrong >= 0 else None
    terminal_ok = _terminal_is_ok(output, oracle)
    resolution_match = projection.get("requires_user_resolution") is oracle.requires_user_resolution
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
    candidate_status = _candidate_status(case, oracle, output, materialization)
    nonpass_finding_count, covered_nonpass_finding_count = (
        _nonpass_finding_advice_coverage(output)
    )
    unsupported_claim_count = _deterministic_unsupported_claim_count(
        output,
        candidate_status=candidate_status,
    )
    usage_measurement = _usage_measurement(output)
    concurrency_status = _concurrency_result(output, oracle.concurrency_expectation, materialization)
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
    if oracle.requires_user_resolution:
        postcheck_status = "NOT_REQUIRED"
    else:
        postcheck = output.postcheck
        new_serious = postcheck.get("new_blocker_high_unknown_count") if postcheck else None
        postcheck_status = (
            "PASS"
            if postcheck
            and postcheck.get("schema_version") == "trip-check-p5-postcheck-projection-v2"
            and isinstance(postcheck.get("report_id"), str)
            and bool(postcheck.get("report_id"))
            and isinstance(new_serious, int)
            and not isinstance(new_serious, bool)
            and new_serious <= oracle.max_new_blocker_high_unknown
            and postcheck.get("overall_status") not in {None, "UNKNOWN", "UNAVAILABLE", "ERROR"}
            else "FAIL"
        )
    replay_match = (
        output.semantic_output_hash == semantic_output_hash_v2(output)
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
    failures: list[str] = []
    checks = (
        (not terminal_ok, f"TERMINAL_{output.terminal_status.value}"),
        (not resolution_match, "RESOLUTION_MISMATCH"),
        (wrong_count is None, "FACT_PROJECTION_MISSING"),
        (wrong_count is not None and wrong_count > oracle.wrong_city_or_poi_max, "WRONG_CITY_OR_POI"),
        (bool(missing), "HARD_FINDING_MISS"),
        (unknown_status == "FAIL", "UNKNOWN_OR_UNAVAILABLE_NOT_PRESERVED"),
        (advice_status == "FAIL", "ADVICE_MISSING"),
        (
            covered_nonpass_finding_count != nonpass_finding_count,
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
    failures.extend(code for failed, code in checks if failed)
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
    deterministic_pass = not failures
    return P5CaseScoreV2(
        case_id=case.case_id,
        split=case.split,
        city=case.city,
        input_kind=case.input_kind,
        difficulty=case.difficulty,
        fault_profile_id=str(case.runner_control.get("fault_profile_id", "NONE")),
        variant_id=output.variant_id,
        terminal_status=output.terminal_status,
        task_success=oracle.task_success_required and deterministic_pass,
        deterministic_pass=deterministic_pass,
        score=float(sum(points for points, passed in components if passed)),
        terminal_ok=terminal_ok,
        resolution_match=resolution_match,
        required_reason_codes=required,
        missing_reason_codes=missing,
        wrong_city_or_poi_count=wrong_count,
        unknown_preservation=unknown_status,
        advice_coverage=advice_status,
        nonpass_finding_count=nonpass_finding_count,
        covered_nonpass_finding_count=covered_nonpass_finding_count,
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


def _validate_case_output_binding(
    case: P5CaseV2,
    output: P5TerminalOutputV2,
    spec: P5VariantRunSpecV2,
) -> None:
    materialization = case.materialization
    expected = {
        "input_hash": case.normalized_input_sha256,
        "materialization_hash": materialization.materialization_sha256,
        "render_receipt_hash": (
            materialization.render_receipt.content_sha256 if materialization.render_receipt else None
        ),
        "ocr_receipt_hash": (
            materialization.ocr_baseline_receipt.content_sha256 if materialization.ocr_baseline_receipt else None
        ),
        "provider_snapshot_hash": materialization.provider_snapshot.content_sha256,
        "evidence_snapshot_hash": materialization.evidence_snapshot.content_sha256,
        "fault_script_hash": materialization.fault_script.content_sha256,
        "run_spec_hash": spec.run_spec_hash,
        "adapter_version": spec.adapter_version,
        "repair_strategy": spec.repair_strategy,
    }
    actual = {key: getattr(output, key) for key in expected}
    if actual != expected:
        raise P5V2ScoringError("TERMINAL_ARTIFACT_BINDING_MISMATCH")
    if sorted(output.candidate_set_hashes) != sorted(item.content_sha256 for item in materialization.candidate_sets):
        raise P5V2ScoringError("TERMINAL_CANDIDATE_SET_BINDING_MISMATCH")
    if (
        output.split != case.split
        or output.city != case.city
        or output.input_kind != case.input_kind
        or output.semantic_output_hash != semantic_output_hash_v2(output)
    ):
        raise P5V2ScoringError("TERMINAL_CASE_OR_HASH_BINDING_MISMATCH")


def validate_run_group_v2(
    *,
    run_dir: Path,
    cases_path: Path,
    materializations_path: Path,
    dataset_manifest_path: Path,
    expected_lane: str,
    require_formal: bool = True,
) -> tuple[dict[str, Any], list[P5CaseV2], list[P5TerminalOutputV2]]:
    manifest = _load_json(run_dir / "run_group_manifest.json", "RUN_GROUP_MANIFEST_INVALID")
    if set(manifest) != RUN_GROUP_FIELDS_V2:
        raise P5V2ScoringError("RUN_GROUP_MANIFEST_FIELDS_INVALID")
    if manifest.get("schema_version") != "trip-check-p5-run-group-v2":
        raise P5V2ScoringError("RUN_GROUP_MANIFEST_VERSION_INVALID")
    claimed_hash = manifest["manifest_hash"]
    if claimed_hash != digest({key: value for key, value in manifest.items() if key != "manifest_hash"}):
        raise P5V2ScoringError("RUN_GROUP_MANIFEST_HASH_MISMATCH")
    if (
        manifest["lane"] != expected_lane
        or manifest["status"] != "PASS"
        or manifest["dirty_tree"] is not False
        or manifest["variant_ids"] != list(VARIANT_IDS_V2)
        or manifest["variant_count"] != 3
        or manifest["blind_labels_read"] is not False
        or manifest["external_api_calls"] != 0
        or manifest["human_evidence"] is not False
        or (require_formal and manifest["formal_evidence"] is not True)
    ):
        raise P5V2ScoringError("RUN_GROUP_CONTRACT_INVALID")

    dataset_manifest = _load_json(dataset_manifest_path, "DATASET_MANIFEST_INVALID")
    dataset_hash = dataset_manifest.get("manifest_hash")
    if not isinstance(dataset_hash, str) or dataset_hash != digest(
        {key: value for key, value in dataset_manifest.items() if key != "manifest_hash"}
    ):
        raise P5V2ScoringError("DATASET_MANIFEST_HASH_MISMATCH")
    if manifest["dataset_manifest_hash"] != dataset_hash:
        raise P5V2ScoringError("RUN_GROUP_DATASET_BINDING_MISMATCH")
    if manifest["cases_file_sha256"] != _sha256_file(cases_path):
        raise P5V2ScoringError("RUN_GROUP_CASES_FILE_HASH_MISMATCH")
    if manifest["materializations_file_sha256"] != _sha256_file(materializations_path):
        raise P5V2ScoringError("RUN_GROUP_MATERIALIZATIONS_FILE_HASH_MISMATCH")

    terminal_path = run_dir / str(manifest["terminal_outputs_path"])
    if _contains_symlink_or_junction(terminal_path.absolute()):
        raise P5V2ScoringError("TERMINAL_OUTPUT_SYMLINK_FORBIDDEN")
    try:
        if terminal_path.resolve().relative_to(run_dir.resolve()) is None:
            raise AssertionError
    except (ValueError, OSError, AssertionError) as exc:
        raise P5V2ScoringError("TERMINAL_OUTPUT_PATH_ESCAPE") from exc
    if manifest["terminal_outputs_file_sha256"] != _sha256_file(terminal_path):
        raise P5V2ScoringError("TERMINAL_OUTPUT_FILE_HASH_MISMATCH")
    try:
        outputs = [P5TerminalOutputV2.model_validate(row) for row in load_jsonl(terminal_path)]
    except ValidationError as exc:
        raise P5V2ScoringError("TERMINAL_OUTPUT_SCHEMA_INVALID") from exc
    if digest([output.model_dump(mode="json") for output in outputs]) != manifest["terminal_outputs_content_sha256"]:
        raise P5V2ScoringError("TERMINAL_OUTPUT_CONTENT_HASH_MISMATCH")

    try:
        case_rows = load_jsonl(cases_path)
        if any(
            not isinstance(row.get("case_hash"), str)
            or digest({key: value for key, value in row.items() if key != "case_hash"}) != row["case_hash"]
            for row in case_rows
        ):
            raise P5V2ScoringError("CASE_HASH_MISMATCH")
        cases = [P5CaseV2.model_validate(row) for row in case_rows]
    except ValidationError as exc:
        raise P5V2ScoringError("CASE_SCHEMA_INVALID") from exc
    if not cases or len({case.case_id for case in cases}) != len(cases):
        raise P5V2ScoringError("CASE_SET_INVALID")
    if expected_lane == "nonblind" and any(case.split == "frozen_blind" for case in cases):
        raise P5V2ScoringError("NONBLIND_CASE_SET_CONTAINS_BLIND")
    if expected_lane == "frozen_blind" and any(case.split != "frozen_blind" for case in cases):
        raise P5V2ScoringError("BLIND_CASE_SET_CONTAINS_NONBLIND")
    materialization_rows = load_jsonl(materializations_path)
    materializations = _materialization_rows_by_id(materialization_rows)
    if set(materializations) != {case.case_id for case in cases}:
        raise P5V2ScoringError("MATERIALIZATION_CASE_SET_MISMATCH")
    if any(materializations[case.case_id] != case.materialization for case in cases):
        raise P5V2ScoringError("CASE_MATERIALIZATION_BINDING_MISMATCH")
    file_prefix = "nonblind" if expected_lane == "nonblind" else "blind"
    dataset_files = dataset_manifest.get("files")
    dataset_lane = dataset_manifest.get("lanes", {}).get(expected_lane)
    case_file_binding = dataset_files.get(f"{file_prefix}_cases") if isinstance(dataset_files, dict) else None
    materialization_file_binding = (
        dataset_files.get(f"{file_prefix}_materializations") if isinstance(dataset_files, dict) else None
    )
    if (
        not isinstance(case_file_binding, dict)
        or not isinstance(materialization_file_binding, dict)
        or not isinstance(dataset_lane, dict)
        or case_file_binding.get("row_count") != len(cases)
        or case_file_binding.get("file_sha256") != manifest["cases_file_sha256"]
        or case_file_binding.get("content_sha256") != digest(case_rows)
        or materialization_file_binding.get("row_count") != len(materialization_rows)
        or materialization_file_binding.get("file_sha256") != manifest["materializations_file_sha256"]
        or materialization_file_binding.get("content_sha256") != digest(materialization_rows)
        or dataset_lane.get("case_count") != len(cases)
        or dataset_lane.get("materialization_count") != len(materialization_rows)
        or dataset_lane.get("case_set_hash") != case_set_hash_v2(cases)
        or dataset_lane.get("materialization_set_hash") != materialization_set_hash_v2(cases)
    ):
        raise P5V2ScoringError("DATASET_LANE_BINDING_MISMATCH")

    if not require_formal:
        selected_case_ids = {output.case_id for output in outputs}
        all_case_ids = {case.case_id for case in cases}
        if not selected_case_ids or not selected_case_ids.issubset(all_case_ids):
            raise P5V2ScoringError("TERMINAL_OUTPUT_EXACT_KEY_SET_MISMATCH")
        cases = [case for case in cases if case.case_id in selected_case_ids]
    if (
        manifest["case_count"] != len(cases)
        or manifest["case_set_hash"] != case_set_hash_v2(cases)
        or manifest["materialization_set_hash"] != materialization_set_hash_v2(cases)
    ):
        raise P5V2ScoringError("RUN_GROUP_CASE_SET_BINDING_MISMATCH")
    formal_count = 270 if expected_lane == "nonblind" else 90
    if require_formal and len(cases) != formal_count:
        raise P5V2ScoringError("FORMAL_CASE_COUNT_INVALID")

    raw_specs = manifest["run_specs"]
    if not isinstance(raw_specs, dict) or set(raw_specs) != set(VARIANT_IDS_V2):
        raise P5V2ScoringError("RUN_SPEC_SET_INVALID")
    try:
        specs = {variant_id: P5VariantRunSpecV2.model_validate(raw_specs[variant_id]) for variant_id in VARIANT_IDS_V2}
    except ValidationError as exc:
        raise P5V2ScoringError("RUN_SPEC_SCHEMA_INVALID") from exc
    common = []
    for variant_id, spec in specs.items():
        if (
            spec.variant_id != variant_id
            or spec.subject_commit != manifest["subject_commit"]
            or spec.dirty_tree is not False
            or spec.lane != expected_lane
            or spec.dataset_manifest_hash != dataset_hash
            or spec.case_set_hash != manifest["case_set_hash"]
            or spec.materialization_set_hash != manifest["materialization_set_hash"]
        ):
            raise P5V2ScoringError("RUN_SPEC_BINDING_MISMATCH")
        common.append(
            {
                key: value
                for key, value in spec.model_dump(mode="json").items()
                if key not in RUN_SPEC_VARIANT_WHITELIST_V2
            }
        )
    if any(item != common[0] for item in common[1:]):
        raise P5V2ScoringError("RUN_SPEC_VARIANT_WHITELIST_VIOLATION")

    expected_keys = {(case.case_id, variant_id) for case in cases for variant_id in VARIANT_IDS_V2}
    actual_keys = [(output.case_id, output.variant_id) for output in outputs]
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        raise P5V2ScoringError("TERMINAL_OUTPUT_EXACT_KEY_SET_MISMATCH")
    expected_terminal_count = len(cases) * 3
    if (
        manifest["terminal_count"] != expected_terminal_count
        or manifest["expected_terminal_count"] != expected_terminal_count
        or len(outputs) != expected_terminal_count
        or manifest["variant_output_sha256"] != variant_output_hashes_v2(outputs)
    ):
        raise P5V2ScoringError("TERMINAL_OUTPUT_GROUP_BINDING_MISMATCH")
    if (
        manifest["replay_executed"] is not True
        or manifest["replay_match_count"] != expected_terminal_count
        or manifest["replay_mismatches"] != []
    ):
        raise P5V2ScoringError("RUN_GROUP_REPLAY_INVALID")
    case_by_id = {case.case_id: case for case in cases}
    for output in outputs:
        _validate_case_output_binding(case_by_id[output.case_id], output, specs[output.variant_id])
    return manifest, cases, outputs


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


_LOCATION_CITY_REASON_CODES = frozenset(
    {
        "PLACE_CITY_MISMATCH",
        "WRONG_CITY_OR_POI",
        "P4_EMPTY_CANDIDATE_SET",
        "P4_CANDIDATE_RECEIPT_MISSING",
    }
)
_TIME_ROUTE_HOTEL_MARKERS = ("TIME", "ROUTE", "HOTEL", "LODGING", "ACCOMMODATION")


def _quality_dimensions(items: Sequence[P5CaseScoreV2]) -> dict[str, Any]:
    location = [
        item
        for item in items
        if set(item.required_reason_codes) & _LOCATION_CITY_REASON_CODES
    ]
    continuity = [
        item
        for item in items
        if any(
            marker in code
            for code in item.required_reason_codes
            for marker in _TIME_ROUTE_HOTEL_MARKERS
        )
    ]
    excluded = _LOCATION_CITY_REASON_CODES
    advice_buckets: dict[str, list[P5CaseScoreV2]] = defaultdict(list)
    for item in items:
        if item.advice_coverage == "NOT_REQUIRED":
            continue
        for code in item.required_reason_codes or ["ADVICE_WITHOUT_REASON_CODE"]:
            if code in excluded or any(
                marker in code for marker in _TIME_ROUTE_HOTEL_MARKERS
            ):
                continue
            advice_buckets[code].append(item)

    def score_bucket(values: Sequence[P5CaseScoreV2]) -> dict[str, Any]:
        return {
            "case_count": len(values),
            "mean_score": mean(item.score for item in values) if values else None,
        }

    other = {
        name: score_bucket(values)
        for name, values in sorted(advice_buckets.items())
    }
    numeric_other = [
        float(bucket["mean_score"])
        for bucket in other.values()
        if isinstance(bucket.get("mean_score"), (int, float))
    ]
    return {
        "location_city_facts": score_bucket(location),
        "time_route_hotel_continuity": score_bucket(continuity),
        "other_advice": {
            "case_count": sum(bucket["case_count"] for bucket in other.values()),
            "minimum_bucket_score": min(numeric_other) if numeric_other else None,
            "buckets": other,
        },
    }


def aggregate_scores_v2(scores: Iterable[P5CaseScoreV2]) -> dict[str, Any]:
    items = list(scores)
    count = len(items)
    nonpass_findings = sum(item.nonpass_finding_count for item in items)
    covered_nonpass_findings = sum(
        item.covered_nonpass_finding_count for item in items
    )
    measured_tokens = [
        item.token_count for item in items if isinstance(item.token_count, int)
    ]
    measured_costs = [
        float(item.cost_usd)
        for item in items
        if isinstance(item.cost_usd, (int, float))
    ]
    return {
        "case_count": count,
        "task_success_count": sum(item.task_success for item in items),
        "task_success_rate": sum(item.task_success for item in items) / count if count else 0.0,
        "mean_score": mean(item.score for item in items) if items else 0.0,
        "deterministic_failure_count": sum(not item.deterministic_pass for item in items),
        "wrong_city_or_poi_count": sum(item.wrong_city_or_poi_count or 0 for item in items),
        "hard_finding_miss_count": sum(len(item.missing_reason_codes) for item in items),
        "unknown_failure_count": sum(item.unknown_preservation == "FAIL" for item in items),
        "candidate_receipt_failure_count": sum(item.candidate_receipt_coverage == "FAIL" for item in items),
        "concurrency_failure_count": sum(item.concurrency_result == "FAIL" for item in items),
        "postcheck_failure_count": sum(item.repair_postcheck == "FAIL" for item in items),
        "replay_failure_count": sum(not item.replay_hash_match for item in items),
        "nonpass_finding_count": nonpass_findings,
        "covered_nonpass_finding_count": covered_nonpass_findings,
        "nonpass_finding_advice_coverage_rate": (
            covered_nonpass_findings / nonpass_findings
            if nonpass_findings
            else 1.0
        ),
        "unsupported_claim_count": sum(item.unsupported_claim_count for item in items),
        "unsupported_claim_rate": (
            sum(item.unsupported_claim_count for item in items) / count
            if count
            else 0.0
        ),
        "usage_measurement_failure_count": sum(
            item.usage_measurement == "FAIL" for item in items
        ),
        "token_count_total": sum(measured_tokens),
        "token_count_not_measured_count": sum(
            item.token_count == "NOT_MEASURED" for item in items
        ),
        "cost_usd_total": sum(measured_costs),
        "cost_not_measured_count": sum(
            item.cost_usd == "NOT_MEASURED" for item in items
        ),
        "quality_dimensions": _quality_dimensions(items),
    }


def _buckets(scores: Sequence[P5CaseScoreV2], key: Callable[[P5CaseScoreV2], str]) -> dict[str, Any]:
    grouped: dict[str, list[P5CaseScoreV2]] = defaultdict(list)
    for score in scores:
        grouped[key(score)].append(score)
    return {name: aggregate_scores_v2(items) for name, items in sorted(grouped.items())}


def _paired(core: Mapping[str, P5CaseScoreV2], challenger: Mapping[str, P5CaseScoreV2]) -> dict[str, Any]:
    if set(core) != set(challenger):
        raise P5V2ScoringError("PAIRED_CASE_SET_MISMATCH")
    differences = [int(challenger[case_id].task_success) - int(core[case_id].task_success) for case_id in sorted(core)]
    improvement = mean(differences) if differences else 0.0
    if len(differences) > 1:
        variance = sum((item - improvement) ** 2 for item in differences) / (len(differences) - 1)
        standard_error = math.sqrt(variance / len(differences))
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


def build_score_report_v2(
    *,
    manifest: Mapping[str, Any],
    cases: Sequence[P5CaseV2],
    outputs: Sequence[P5TerminalOutputV2],
    include_case_scores: bool,
    materializations: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    case_by_id = {case.case_id: case for case in cases}
    scores = [
        score_case_v2(
            case_by_id[output.case_id],
            output,
            materialization=(materializations or {}).get(output.case_id),
        )
        for output in outputs
    ]
    outputs_by_key = {(output.case_id, output.variant_id): output for output in outputs}
    variant_metrics: dict[str, Any] = {}
    score_maps: dict[str, dict[str, P5CaseScoreV2]] = {}
    for variant_id in VARIANT_IDS_V2:
        items = [item for item in scores if item.variant_id == variant_id]
        score_maps[variant_id] = {item.case_id: item for item in items}
        latencies = [outputs_by_key[(item.case_id, variant_id)].latency_ms for item in items]
        overall = aggregate_scores_v2(items)
        overall.update(
            {
                "latency_p50_ms": median(latencies),
                "latency_p95_ms": _percentile(latencies, 0.95),
                "terminal_status_counts": dict(sorted(Counter(item.terminal_status.value for item in items).items())),
            }
        )
        variant_metrics[variant_id] = {
            "overall": overall,
            "by_split": _buckets(items, lambda item: item.split),
            "by_city": _buckets(items, lambda item: item.city),
            "by_input_kind": _buckets(items, lambda item: item.input_kind),
            "by_difficulty": _buckets(items, lambda item: item.difficulty),
            "by_fault_profile": _buckets(items, lambda item: item.fault_profile_id),
            "by_finding": _buckets(items, lambda item: "+".join(item.required_reason_codes) or "NONE"),
            "by_repair_outcome": _buckets(
                items,
                lambda item: case_by_id[item.case_id].oracle.expected_strategy_outcome
                if case_by_id[item.case_id].oracle
                else "MISSING",
            ),
        }
    core = variant_metrics["core_b"]["overall"]
    core_gate_checks = {
        "mean_score_gte_88": core["mean_score"] >= 88,
        "deterministic_failure_zero": core["deterministic_failure_count"] == 0,
        "wrong_city_or_poi_zero": core["wrong_city_or_poi_count"] == 0,
        "hard_finding_miss_zero": core["hard_finding_miss_count"] == 0,
        "unknown_failure_zero": core["unknown_failure_count"] == 0,
        "candidate_receipt_failure_zero": core["candidate_receipt_failure_count"] == 0,
        "concurrency_failure_zero": core["concurrency_failure_count"] == 0,
        "postcheck_failure_zero": core["postcheck_failure_count"] == 0,
        "replay_failure_zero": core["replay_failure_count"] == 0,
        "nonpass_finding_advice_coverage_100": core[
            "nonpass_finding_advice_coverage_rate"
        ]
        == 1.0,
        "unsupported_claim_rate_zero": core["unsupported_claim_count"] == 0
        and core["unsupported_claim_rate"] == 0.0,
        "usage_is_zero_or_not_measured": core["usage_measurement_failure_count"]
        == 0
        and core["token_count_total"] == 0
        and core["cost_usd_total"] == 0,
    }
    passed = all(core_gate_checks.values())
    report: dict[str, Any] = {
        "schema_version": "trip-check-p5-nonblind-score-report-v2",
        "status": "PASS" if passed else "REJECT",
        "evidence_class": "CONTROLLED_FIXTURE",
        "subject_commit": manifest["subject_commit"],
        "dataset_manifest_hash": manifest["dataset_manifest_hash"],
        "run_group_manifest_hash": manifest["manifest_hash"],
        "case_count": len(cases),
        "terminal_count": len(outputs),
        "variant_metrics": variant_metrics,
        "paired_comparisons": {
            variant_id: _paired(score_maps["core_b"], score_maps[variant_id]) for variant_id in ("legacy_a", "solver_c")
        },
        "core_gate_checks": core_gate_checks,
        "promotion_decision": "KEEP_CORE_B" if passed else "REJECT_ALL_CANDIDATES",
        "solver_admission_inherited": "REJECT",
        "solver_may_promote_from_p5_score": False,
        "automated_proxy_judge": "NOT_RUN",
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
        "human_evidence": False,
    }
    if include_case_scores:
        report["case_scores"] = [item.model_dump(mode="json") for item in scores]
    report["report_hash"] = digest(report)
    return report


def score_run_group_v2(
    *,
    run_dir: Path,
    cases_path: Path,
    materializations_path: Path,
    dataset_manifest_path: Path,
    require_formal: bool = True,
) -> dict[str, Any]:
    manifest, cases, outputs = validate_run_group_v2(
        run_dir=run_dir,
        cases_path=cases_path,
        materializations_path=materializations_path,
        dataset_manifest_path=dataset_manifest_path,
        expected_lane="nonblind",
        require_formal=require_formal,
    )
    return build_score_report_v2(
        manifest=manifest,
        cases=cases,
        outputs=outputs,
        include_case_scores=True,
        materializations={row["case_id"]: row for row in load_jsonl(materializations_path)},
    )
