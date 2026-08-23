"""Deterministic P5 scorer for non-blind run groups.

This module never calls a model.  It joins terminal outputs to the versioned
oracle only after validating the complete run-group binding.  Missing evidence
is a failure, not an inferred PASS.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable

from pydantic import BaseModel, ConfigDict, Field

from evals.trip_check_v1.p5.contracts import P5TerminalOutput, TerminalStatus
from evals.trip_check_v1.p5.data_contract import digest


class P5CaseScore(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = "trip-check-p5-score-v1"
    case_id: str
    split: str
    city: str
    input_kind: str
    difficulty: str
    fault_profile_id: str
    variant_id: str
    terminal_status: TerminalStatus
    task_success: bool
    deterministic_pass: bool
    score: float = Field(ge=0, le=100)
    terminal_ok: bool
    resolution_match: bool
    required_obligations: list[str]
    missing_obligations: list[str]
    wrong_city_or_poi_count: int = Field(ge=0)
    hard_finding_miss_count: int = Field(ge=0)
    unknown_preservation: str
    advice_coverage: str
    candidate_receipt_coverage: str
    repair_postcheck: str
    replay_hash_match: bool
    strategy_outcome_match: bool
    safety_failure_codes: list[str]


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _terminal_reason_codes(output: P5TerminalOutput) -> set[str]:
    native = output.native_output
    if output.variant_id == "solver_c":
        native = native.get("core_shell", {})
    values = native.get("observed_reason_codes", [])
    return {str(value) for value in values} | {
        str(item.get("reason_code"))
        for item in output.findings
        if item.get("reason_code")
    }


def _native_core(output: P5TerminalOutput) -> dict[str, Any]:
    if output.variant_id == "solver_c":
        return dict(output.native_output.get("core_shell", {}))
    return output.native_output


def _resolution_observed(output: P5TerminalOutput) -> bool:
    native = _native_core(output)
    if "requires_user_resolution" in native:
        return bool(native["requires_user_resolution"])
    return output.terminal_status == TerminalStatus.NEEDS_USER_RESOLUTION


def _strategy_outcome_matches(output: P5TerminalOutput, expected: str) -> bool:
    if expected == "FEASIBLE":
        if output.variant_id == "solver_c" and output.native_output.get("solver_applicable"):
            return output.native_output.get("solver_primary", {}).get("status") == "SUCCESS"
        return output.terminal_status not in {
            TerminalStatus.ERROR,
            TerminalStatus.TIMEOUT,
            TerminalStatus.UNSUPPORTED_CAPABILITY,
        }
    if output.variant_id != "solver_c":
        # Core/Legacy must provide their own strategy receipt; absence fails closed.
        return False
    primary = output.native_output.get("solver_primary", {})
    effective = output.native_output.get("solver_effective", {})
    if expected == "UNSAT":
        return primary.get("status") == "UNSAT"
    if expected == "TIMEOUT":
        return primary.get("status") == "TIMEOUT"
    if expected == "FALLBACK":
        return (
            primary.get("status") in {"ERROR", "TIMEOUT", "UNSAT"}
            and effective.get("status") == "SUCCESS"
            and output.native_output.get("fallback_used") is True
        )
    return False


def _obligation_satisfied(code: str, output: P5TerminalOutput) -> bool:
    reason_codes = _terminal_reason_codes(output)
    native = _native_core(output)
    if not code.startswith("P4_"):
        return code in reason_codes
    if code == "P4_ADVICE_COMPLETENESS":
        return bool(output.advice) and int(output.evaluation_projection.get("advice_action_count", 0)) > 0
    if code == "P4_EMPTY_CANDIDATE_SET":
        return _resolution_observed(output) and int(native.get("wrong_poi_auto_accept_count", 0)) == 0
    if code == "P4_CANDIDATE_RECEIPT_MISSING":
        return output.capability_outcomes.get("candidate_receipt_missing_detection") == "DETECTED"
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
    if code == "P4_DUPLICATE_APPLY":
        return native.get("replay_side_effect_counts_equal") is True
    if code == "P4_CONCURRENT_APPLY":
        return output.capability_outcomes.get("concurrent_apply_conflict") == "DETECTED"
    if code == "P4_SOLVER_UNSAT":
        return _strategy_outcome_matches(output, "UNSAT")
    if code == "P4_SOLVER_TIMEOUT":
        return _strategy_outcome_matches(output, "TIMEOUT")
    if code == "P4_SOLVER_FALLBACK":
        return _strategy_outcome_matches(output, "FALLBACK")
    return False


def score_case(case: dict[str, Any], output: P5TerminalOutput) -> P5CaseScore:
    oracle = case["oracle"]
    required = [str(item) for item in oracle["required_reason_codes"]]
    missing = [item for item in required if not _obligation_satisfied(item, output)]
    native = _native_core(output)
    wrong_count = int(native.get("wrong_poi_auto_accept_count", 0))
    resolution_match = _resolution_observed(output) == bool(oracle["requires_user_resolution"])
    terminal_ok = output.terminal_status not in {
        TerminalStatus.ERROR,
        TerminalStatus.TIMEOUT,
        TerminalStatus.UNSUPPORTED_CAPABILITY,
    }
    unknown_required = bool(oracle["unknown_must_be_preserved"])
    unknown_observed = output.evaluation_projection.get("unknown_preserved") is True
    unknown_status = (
        "NOT_REQUIRED"
        if not unknown_required
        else "PASS"
        if unknown_observed
        else "FAIL"
    )
    advice_required = bool(oracle["advice_required"])
    advice_status = (
        "NOT_REQUIRED"
        if not advice_required
        else "PASS"
        if output.advice
        else "FAIL"
    )
    specific_allowed = bool(oracle["specific_place_allowed"])
    candidate_coverage = output.evaluation_projection.get("candidate_receipt_coverage")
    if not specific_allowed:
        candidate_status = (
            "PASS"
            if output.evaluation_projection.get("unverified_specific_place_claim_count", 0) == 0
            else "FAIL"
        )
    else:
        candidate_status = "PASS" if candidate_coverage == 1.0 else "FAIL"
    postcheck = output.postcheck or {}
    new_serious = int(postcheck.get("new_high_count", 0)) + int(
        postcheck.get("new_unknown_count", 0)
    )
    if bool(oracle["requires_user_resolution"]):
        postcheck_status = "NOT_REQUIRED"
    else:
        postcheck_status = (
            "PASS"
            if postcheck
            and new_serious <= int(oracle["max_new_blocker_high_unknown"])
            and postcheck.get("replay_side_effect_counts_equal") is True
            else "FAIL"
        )
    strategy_match = _strategy_outcome_matches(output, str(oracle["expected_strategy_outcome"]))
    safety_failures = []
    if not terminal_ok:
        safety_failures.append(f"TERMINAL_{output.terminal_status.value}")
    if not resolution_match:
        safety_failures.append("RESOLUTION_MISMATCH")
    if wrong_count > int(oracle["wrong_city_or_poi_max"]):
        safety_failures.append("WRONG_CITY_OR_POI")
    if missing:
        safety_failures.append("HARD_FINDING_MISS")
    if unknown_status == "FAIL":
        safety_failures.append("UNKNOWN_NOT_PRESERVED")
    if advice_status == "FAIL":
        safety_failures.append("ADVICE_MISSING")
    if candidate_status == "FAIL":
        safety_failures.append("CANDIDATE_RECEIPT_MISSING")
    if postcheck_status == "FAIL":
        safety_failures.append("POSTCHECK_NOT_PROVEN")
    if not strategy_match:
        safety_failures.append("STRATEGY_OUTCOME_MISMATCH")
    if output.replay_hash != output.semantic_output_hash:
        safety_failures.append("REPLAY_HASH_MISMATCH")
    components = {
        "terminal": 10.0 if terminal_ok else 0.0,
        "resolution": 10.0 if resolution_match else 0.0,
        "finding": 20.0 if not missing else 0.0,
        "wrong_place": 15.0 if wrong_count <= int(oracle["wrong_city_or_poi_max"]) else 0.0,
        "unknown": 10.0 if unknown_status != "FAIL" else 0.0,
        "advice": 10.0 if advice_status != "FAIL" else 0.0,
        "candidate": 10.0 if candidate_status == "PASS" else 0.0,
        "postcheck": 10.0 if postcheck_status != "FAIL" else 0.0,
        "strategy": 5.0 if strategy_match else 0.0,
    }
    deterministic_pass = not safety_failures
    return P5CaseScore(
        case_id=case["case_id"],
        split=case["split"],
        city=case["city"],
        input_kind=case["input_kind"],
        difficulty=case["difficulty"],
        fault_profile_id=case["runner_control"]["fault_profile_id"],
        variant_id=output.variant_id,
        terminal_status=output.terminal_status,
        task_success=bool(oracle["task_success_required"]) and deterministic_pass,
        deterministic_pass=deterministic_pass,
        score=sum(components.values()),
        terminal_ok=terminal_ok,
        resolution_match=resolution_match,
        required_obligations=required,
        missing_obligations=missing,
        wrong_city_or_poi_count=wrong_count,
        hard_finding_miss_count=len(missing),
        unknown_preservation=unknown_status,
        advice_coverage=advice_status,
        candidate_receipt_coverage=candidate_status,
        repair_postcheck=postcheck_status,
        replay_hash_match=output.replay_hash == output.semantic_output_hash,
        strategy_outcome_match=strategy_match,
        safety_failure_codes=sorted(set(safety_failures)),
    )


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def _bucket(scores: Iterable[P5CaseScore]) -> dict[str, Any]:
    items = list(scores)
    count = len(items)
    return {
        "case_count": count,
        "task_success_count": sum(item.task_success for item in items),
        "task_success_rate": sum(item.task_success for item in items) / count if count else 0.0,
        "mean_score": mean(item.score for item in items) if items else 0.0,
        "wrong_city_or_poi_count": sum(item.wrong_city_or_poi_count for item in items),
        "hard_finding_miss_count": sum(item.hard_finding_miss_count for item in items),
        "unknown_failure_count": sum(item.unknown_preservation == "FAIL" for item in items),
        "candidate_receipt_failure_count": sum(
            item.candidate_receipt_coverage == "FAIL" for item in items
        ),
        "postcheck_failure_count": sum(item.repair_postcheck == "FAIL" for item in items),
        "replay_failure_count": sum(not item.replay_hash_match for item in items),
    }


def _group_buckets(scores: list[P5CaseScore], field: str) -> dict[str, Any]:
    groups: dict[str, list[P5CaseScore]] = defaultdict(list)
    for item in scores:
        groups[str(getattr(item, field))].append(item)
    return {key: _bucket(values) for key, values in sorted(groups.items())}


def validate_run_group(
    *,
    run_dir: Path,
    cases: list[dict[str, Any]],
    require_full_nonblind: bool = True,
) -> tuple[dict[str, Any], list[P5TerminalOutput]]:
    manifest_path = run_dir / "run_group_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    claimed_hash = manifest.get("manifest_hash")
    unhashed = {key: value for key, value in manifest.items() if key != "manifest_hash"}
    if claimed_hash != digest(unhashed):
        raise ValueError("run-group manifest hash mismatch")
    if manifest.get("lane") != "nonblind":
        raise ValueError("non-blind scorer rejects other lanes")
    if manifest.get("dirty_tree"):
        raise ValueError("dirty run group is not scoreable evidence")
    output_path = run_dir / str(manifest["terminal_outputs_path"])
    if _sha256_file(output_path) != manifest["terminal_outputs_file_sha256"]:
        raise ValueError("terminal output file hash mismatch")
    rows = load_jsonl(output_path)
    outputs = [P5TerminalOutput.model_validate(row) for row in rows]
    if digest([output.model_dump(mode="json") for output in outputs]) != manifest[
        "terminal_outputs_content_sha256"
    ]:
        raise ValueError("terminal output content hash mismatch")
    case_by_id = {case["case_id"]: case for case in cases}
    selected_ids = {output.case_id for output in outputs}
    if require_full_nonblind and selected_ids != set(case_by_id):
        raise ValueError("formal non-blind scoring requires the exact 270-case set")
    variants = set(manifest["variant_ids"])
    expected = {(case_id, variant) for case_id in selected_ids for variant in variants}
    actual = [(output.case_id, output.variant_id) for output in outputs]
    if len(actual) != len(set(actual)):
        raise ValueError("duplicate case/variant output")
    if set(actual) != expected:
        raise ValueError("incomplete or extra terminal output set")
    if len(outputs) != manifest["terminal_count"] or len(outputs) != manifest["expected_terminal_count"]:
        raise ValueError("terminal count binding mismatch")
    allowed_differences = {"variant_id", "adapter_version", "repair_strategy"}
    common_specs = []
    for variant_id in sorted(variants):
        spec = manifest["run_specs"][variant_id]
        if spec["subject_commit"] != manifest["subject_commit"]:
            raise ValueError("subject commit binding mismatch")
        common_specs.append({key: value for key, value in spec.items() if key not in allowed_differences})
    if any(spec != common_specs[0] for spec in common_specs[1:]):
        raise ValueError("RunSpecs differ outside the variant whitelist")
    if require_full_nonblind and (
        not manifest.get("replay_executed") or manifest.get("replay_mismatches")
    ):
        raise ValueError("formal scoring requires complete successful replay")
    for output in outputs:
        case = case_by_id.get(output.case_id)
        if case is None:
            raise ValueError(f"unknown case output: {output.case_id}")
        if output.input_hash != case["normalized_input_sha256"]:
            raise ValueError(f"input binding mismatch: {output.case_id}")
        if output.provider_snapshot_id != case["runner_control"]["provider_snapshot_id"]:
            raise ValueError(f"snapshot binding mismatch: {output.case_id}")
        if output.fault_profile_id != case["runner_control"]["fault_profile_id"]:
            raise ValueError(f"fault binding mismatch: {output.case_id}")
        if output.case_seed != case["runner_control"]["seed"]:
            raise ValueError(f"seed binding mismatch: {output.case_id}")
        spec = manifest["run_specs"][output.variant_id]
        if output.run_spec_hash != digest(spec):
            raise ValueError(f"RunSpec binding mismatch: {output.case_id}/{output.variant_id}")
        if (
            output.adapter_version != spec["adapter_version"]
            or output.repair_strategy != spec["repair_strategy"]
        ):
            raise ValueError(f"variant implementation binding mismatch: {output.case_id}")
        if (
            output.split != case["split"]
            or output.city != case["city"]
            or output.input_kind != case["input_kind"]
        ):
            raise ValueError(f"case metadata binding mismatch: {output.case_id}")
    return manifest, outputs


def score_run_group(
    *,
    run_dir: Path,
    cases_path: Path,
    require_full_nonblind: bool = True,
) -> dict[str, Any]:
    cases = load_jsonl(cases_path)
    manifest, outputs = validate_run_group(
        run_dir=run_dir,
        cases=cases,
        require_full_nonblind=require_full_nonblind,
    )
    case_by_id = {case["case_id"]: case for case in cases}
    scores = [score_case(case_by_id[output.case_id], output) for output in outputs]
    output_by_key = {(output.case_id, output.variant_id): output for output in outputs}
    variants = {}
    for variant_id in sorted({score.variant_id for score in scores}):
        variant_scores = [score for score in scores if score.variant_id == variant_id]
        latencies = [
            output_by_key[(score.case_id, variant_id)].latency_ms for score in variant_scores
        ]
        overall = _bucket(variant_scores)
        overall.update(
            {
                "latency_p50_ms": median(latencies),
                "latency_p95_ms": _percentile(latencies, 0.95),
                "terminal_status_counts": dict(
                    sorted(Counter(score.terminal_status.value for score in variant_scores).items())
                ),
            }
        )
        variants[variant_id] = {
            "overall": overall,
            "by_city": _group_buckets(variant_scores, "city"),
            "by_input_kind": _group_buckets(variant_scores, "input_kind"),
            "by_difficulty": _group_buckets(variant_scores, "difficulty"),
            "by_fault_profile": _group_buckets(variant_scores, "fault_profile_id"),
        }
    core = variants.get("core_b", {}).get("overall", {})
    core_gate_checks = {
        "mean_score_gte_88": core.get("mean_score", 0) >= 88,
        "wrong_city_or_poi_zero": core.get("wrong_city_or_poi_count", 1) == 0,
        "hard_finding_miss_zero": core.get("hard_finding_miss_count", 1) == 0,
        "unknown_failure_zero": core.get("unknown_failure_count", 1) == 0,
        "candidate_receipt_failure_zero": core.get("candidate_receipt_failure_count", 1) == 0,
        "postcheck_failure_zero": core.get("postcheck_failure_count", 1) == 0,
        "replay_failure_zero": core.get("replay_failure_count", 1) == 0,
    }
    report = {
        "schema_version": "trip-check-p5-nonblind-score-report-v1",
        "status": "PASS" if all(core_gate_checks.values()) else "REJECT",
        "evidence_class": "CONTROLLED_FIXTURE",
        "subject_commit": manifest["subject_commit"],
        "run_group_manifest_hash": manifest["manifest_hash"],
        "case_count": len({score.case_id for score in scores}),
        "terminal_count": len(scores),
        "variant_metrics": variants,
        "core_gate_checks": core_gate_checks,
        "case_scores": [score.model_dump(mode="json") for score in scores],
        "automated_proxy_judge": "NOT_RUN",
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
        "human_evidence": False,
    }
    report["report_hash"] = digest(report)
    return report
