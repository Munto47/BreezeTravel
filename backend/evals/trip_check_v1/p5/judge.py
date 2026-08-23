"""Anonymous P5 Judge export and strict three-round aggregation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Sequence

from evals.trip_check_v1.p5.contracts import P5TerminalOutput, VARIANT_IDS
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest


class P5JudgeError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5JudgeError(reason) from exc
    if not isinstance(value, dict):
        raise P5JudgeError(reason)
    return value


def _load_jsonl(path: Path, reason: str) -> list[dict[str, Any]]:
    try:
        values = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5JudgeError(reason) from exc
    if any(not isinstance(item, dict) for item in values):
        raise P5JudgeError(reason)
    return values


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _public_input(case: dict[str, Any]) -> dict[str, Any]:
    product_input = case["product_input"]
    if case["input_kind"] == "TEXT":
        return {
            "input_kind": "TEXT",
            "source_type": product_input["source_type"],
            "text": product_input["raw_text"],
        }
    return {
        "input_kind": "SYNTHETIC_SCREENSHOT",
        "source_type": product_input["source_type"],
        "text": product_input["ocr_text"],
        "ocr_boundary": "POST_OCR_CONTROLLED_TEXT",
    }


def _candidate_expression(output: P5TerminalOutput) -> dict[str, Any]:
    native = output.native_output
    response_text = native.get("recommendation_text")
    if output.variant_id == "solver_c":
        native = native.get("core_shell", {})
    return {
        "terminal_status": output.terminal_status.value,
        "response_text": response_text if isinstance(response_text, str) else None,
        "advice": output.advice,
        "capability_boundary": {
            key: value
            for key, value in sorted(output.capability_outcomes.items())
            if key
            in {
                "input_stage",
                "native_audit_advice_repair_postcheck",
                "candidate_set_replacement",
                "bounded_repair",
                "cp_sat_native_scope",
                "candidate_place_replacement",
                "unknown_fact_resolution",
            }
        },
    }


def _evidence_summary(output: P5TerminalOutput) -> dict[str, Any]:
    return {
        "finding_summaries": [
            {
                "reason_code": item.get("reason_code"),
                "severity": item.get("severity"),
                "status": item.get("status"),
            }
            for item in output.findings
        ],
        "postcheck_boundary": {
            key: value
            for key, value in (output.postcheck or {}).items()
            if key
            in {
                "new_high_count",
                "new_unknown_count",
                "replay_side_effect_counts_equal",
                "solver_projection_only",
                "solver_primary_status",
            }
        },
        "fact_authority": "DETERMINISTIC_ORACLE_ONLY",
    }


def _validate_export_source(
    *,
    run_dir: Path,
    cases_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[P5TerminalOutput]]:
    manifest = _load_json(run_dir / "run_group_manifest.json", "JUDGE_RUN_MANIFEST_INVALID")
    if manifest.get("manifest_hash") != digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        raise P5JudgeError("JUDGE_RUN_MANIFEST_HASH_MISMATCH")
    if manifest.get("dirty_tree") or manifest.get("blind_labels_read") is not False:
        raise P5JudgeError("JUDGE_RUN_NOT_SEALED_CLEAN")
    cases = _load_jsonl(cases_path, "JUDGE_CASE_INPUTS_INVALID")
    case_by_id = {case.get("case_id"): case for case in cases}
    if len(case_by_id) != len(cases):
        raise P5JudgeError("JUDGE_CASE_SET_INVALID")
    terminal_path = run_dir / str(manifest.get("terminal_outputs_path"))
    if _sha256(terminal_path) != manifest.get("terminal_outputs_file_sha256"):
        raise P5JudgeError("JUDGE_TERMINAL_FILE_HASH_MISMATCH")
    try:
        outputs = [
            P5TerminalOutput.model_validate(row)
            for row in _load_jsonl(terminal_path, "JUDGE_TERMINALS_INVALID")
        ]
    except Exception as exc:
        raise P5JudgeError("JUDGE_TERMINAL_SCHEMA_INVALID") from exc
    if digest([output.model_dump(mode="json") for output in outputs]) != manifest.get(
        "terminal_outputs_content_sha256"
    ):
        raise P5JudgeError("JUDGE_TERMINAL_CONTENT_HASH_MISMATCH")
    variant_ids = tuple(manifest.get("variant_ids", []))
    if set(variant_ids) != set(VARIANT_IDS):
        raise P5JudgeError("JUDGE_VARIANT_SET_INVALID")
    selected_ids = {output.case_id for output in outputs}
    expected_keys = {
        (case_id, variant_id) for case_id in selected_ids for variant_id in VARIANT_IDS
    }
    actual_keys = [(output.case_id, output.variant_id) for output in outputs]
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        raise P5JudgeError("JUDGE_TERMINAL_KEY_SET_INVALID")
    if selected_ids != set(case_by_id):
        raise P5JudgeError("JUDGE_CASE_OUTPUT_BINDING_MISMATCH")
    if len(outputs) != manifest.get("terminal_count"):
        raise P5JudgeError("JUDGE_TERMINAL_COUNT_MISMATCH")
    return manifest, cases, outputs


def export_judge_bundles(
    *,
    repo_root: Path,
    run_dir: Path,
    cases_path: Path,
    output_dir: Path,
    rubric_path: Path,
) -> dict[str, Any]:
    root = repo_root.resolve()
    manifest, cases, outputs = _validate_export_source(
        run_dir=run_dir.resolve(), cases_path=cases_path.resolve()
    )
    destination = output_dir.resolve()
    if manifest["lane"] == "frozen_blind" and _inside(destination, root):
        raise P5JudgeError("BLIND_JUDGE_EXPORT_INSIDE_REPOSITORY")
    rubric = _load_json(rubric_path.resolve(), "JUDGE_RUBRIC_INVALID")
    if (
        rubric.get("schema_version") != "trip-check-p5-judge-rubric-v1"
        or rubric.get("fact_authority") != "DETERMINISTIC_ORACLE_ONLY"
        or rubric.get("human_evidence") is not False
    ):
        raise P5JudgeError("JUDGE_RUBRIC_CONTRACT_INVALID")
    destination.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    ordered_cases = sorted(cases, key=lambda item: item["case_id"])
    output_by_key = {(output.case_id, output.variant_id): output for output in outputs}
    blind_ids = {
        case["case_id"]: hmac.new(
            secret,
            f"{manifest['manifest_hash']}:{case['case_id']}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        for case in ordered_cases
    }
    mapping_rows = []
    round_payloads = []
    for round_index in range(1, 4):
        items = []
        for case_index, case in enumerate(ordered_cases):
            # Each round and case rotates all three variants. Across 90 cases,
            # every variant appears exactly 30 times in every slot per round.
            shift = (case_index + round_index - 1) % 3
            variant_order = list(VARIANT_IDS[shift:] + VARIANT_IDS[:shift])
            for slot_index, variant_id in enumerate(variant_order, 1):
                slot_id = f"slot_{slot_index}"
                output = output_by_key[(case["case_id"], variant_id)]
                items.append(
                    {
                        "blind_item_id": blind_ids[case["case_id"]],
                        "slot_id": slot_id,
                        "public_input": _public_input(case),
                        "candidate_expression": _candidate_expression(output),
                        "evidence_summary": _evidence_summary(output),
                    }
                )
                mapping_rows.append(
                    {
                        "round_index": round_index,
                        "blind_item_id": blind_ids[case["case_id"]],
                        "slot_id": slot_id,
                        "case_id": case["case_id"],
                        "variant_id": variant_id,
                        "runtime_generator_model": "none-controlled-runtime",
                    }
                )
        round_payloads.append((round_index, items))
    mapping_commitment = digest(mapping_rows)
    bundle_receipts = []
    for round_index, items in round_payloads:
        bundle = {
            "schema_version": "trip-check-p5-judge-bundle-v1",
            "round_index": round_index,
            "evidence_class": "automated_proxy_judge_input",
            "human_evidence": False,
            "run_binding": {
                "subject_commit": manifest["subject_commit"],
                "run_group_manifest_hash": manifest["manifest_hash"],
                "terminal_outputs_file_sha256": manifest["terminal_outputs_file_sha256"],
                "rubric_sha256": _sha256(rubric_path.resolve()),
                "mapping_commitment": mapping_commitment,
            },
            "rubric": rubric,
            "items": items,
        }
        path = destination / f"judge_bundle_round_{round_index}.json"
        path.write_bytes(canonical_bytes(bundle) + b"\n")
        bundle_receipts.append(
            {
                "round_index": round_index,
                "path": path.name,
                "sha256": _sha256(path),
                "rubric_sha256": bundle["run_binding"]["rubric_sha256"],
                "item_count": len(items),
            }
        )
    mapping = {
        "schema_version": "trip-check-p5-judge-mapping-v1",
        "run_group_manifest_hash": manifest["manifest_hash"],
        "mapping_commitment": mapping_commitment,
        "bundle_receipts": bundle_receipts,
        "rows": mapping_rows,
    }
    mapping_path = destination / "judge_variant_mapping.json"
    mapping_path.write_bytes(canonical_bytes(mapping) + b"\n")
    return {
        "schema_version": "trip-check-p5-judge-export-receipt-v1",
        "lane": manifest["lane"],
        "case_count": len(cases),
        "items_per_round": len(outputs),
        "round_count": 3,
        "balanced_permutation": True,
        "mapping_commitment": mapping_commitment,
        "mapping_file_sha256": _sha256(mapping_path),
        "bundle_receipts": bundle_receipts,
        "oracle_payload_exported": False,
        "variant_identity_exported_to_judges": False,
        "human_evidence": False,
    }


def _validate_round_report(
    *, report: dict[str, Any], round_index: int, bundle_receipt: dict[str, Any]
) -> list[dict[str, Any]]:
    allowed_top = {
        "schema_version",
        "round_index",
        "evaluator_id",
        "agent_task_id",
        "model_id",
        "started_at",
        "ended_at",
        "bundle_sha256",
        "rubric_sha256",
        "api_usage_count",
        "tool_usage_count",
        "scores",
    }
    if (
        set(report) != allowed_top
        or report.get("schema_version") != "trip-check-p5-judge-round-v1"
        or report.get("round_index") != round_index
        or report.get("bundle_sha256") != bundle_receipt["sha256"]
        or report.get("rubric_sha256") != bundle_receipt["rubric_sha256"]
        or report.get("api_usage_count") != 0
        or report.get("tool_usage_count") != 0
    ):
        raise P5JudgeError("JUDGE_ROUND_CONTRACT_INVALID")
    for key in ("evaluator_id", "agent_task_id", "model_id", "started_at", "ended_at"):
        if not isinstance(report.get(key), str) or not report[key]:
            raise P5JudgeError("JUDGE_ROUND_PROVENANCE_INVALID")
    scores = report.get("scores")
    if not isinstance(scores, list) or len(scores) != bundle_receipt["item_count"]:
        raise P5JudgeError("JUDGE_ROUND_SCORE_COUNT_INVALID")
    allowed_score = {
        "blind_item_id",
        "slot_id",
        "clarity",
        "actionability",
        "evidence_boundary_expression",
        "unsupported_claim_candidate_ids",
        "derived_verdict",
    }
    keys = []
    for score in scores:
        if not isinstance(score, dict) or set(score) != allowed_score:
            raise P5JudgeError("JUDGE_SCORE_SCHEMA_INVALID")
        dimensions = [
            score.get("clarity"),
            score.get("actionability"),
            score.get("evidence_boundary_expression"),
        ]
        if any(not isinstance(value, int) or not 0 <= value <= 4 for value in dimensions):
            raise P5JudgeError("JUDGE_SCORE_RANGE_INVALID")
        candidates = score.get("unsupported_claim_candidate_ids")
        if not isinstance(candidates, list) or any(not isinstance(value, str) for value in candidates):
            raise P5JudgeError("JUDGE_CLAIM_CANDIDATES_INVALID")
        expected_verdict = (
            "PASS" if min(dimensions) >= 2 and not candidates else "NEEDS_REVISION"
        )
        if score.get("derived_verdict") != expected_verdict:
            raise P5JudgeError("JUDGE_DERIVED_VERDICT_INVALID")
        keys.append((score.get("blind_item_id"), score.get("slot_id")))
    if len(keys) != len(set(keys)):
        raise P5JudgeError("JUDGE_SCORE_KEY_DUPLICATE")
    return scores


def aggregate_judge_rounds(
    *,
    repo_root: Path,
    mapping_path: Path,
    mapping_sha256: str,
    round_paths: Sequence[Path],
    minimum_agreement: float = 0.85,
) -> dict[str, Any]:
    root = repo_root.resolve()
    resolved_mapping = mapping_path.resolve()
    mapping = _load_json(resolved_mapping, "JUDGE_MAPPING_INVALID")
    if _sha256(resolved_mapping) != mapping_sha256:
        raise P5JudgeError("JUDGE_MAPPING_HASH_MISMATCH")
    if len(round_paths) != 3:
        raise P5JudgeError("JUDGE_ROUND_COUNT_INVALID")
    bundle_receipts = mapping.get("bundle_receipts")
    rows = mapping.get("rows")
    if (
        mapping.get("schema_version") != "trip-check-p5-judge-mapping-v1"
        or not isinstance(bundle_receipts, list)
        or len(bundle_receipts) != 3
        or not isinstance(rows, list)
        or digest(rows) != mapping.get("mapping_commitment")
    ):
        raise P5JudgeError("JUDGE_MAPPING_CONTRACT_INVALID")
    lane_is_blind = len(rows) == 810 and all(str(row.get("case_id", "")).startswith("p5.blind.") for row in rows)
    if lane_is_blind and _inside(resolved_mapping, root):
        raise P5JudgeError("BLIND_JUDGE_MAPPING_INSIDE_REPOSITORY")
    mapping_by_round_key = {
        (row["round_index"], row["blind_item_id"], row["slot_id"]): row for row in rows
    }
    reports = []
    evaluator_ids = set()
    task_ids = set()
    for round_index, path in enumerate(round_paths, 1):
        resolved = path.resolve()
        if lane_is_blind and _inside(resolved, root):
            raise P5JudgeError("BLIND_JUDGE_ROUND_INSIDE_REPOSITORY")
        report = _load_json(resolved, "JUDGE_ROUND_INVALID")
        scores = _validate_round_report(
            report=report,
            round_index=round_index,
            bundle_receipt=bundle_receipts[round_index - 1],
        )
        evaluator_ids.add(report["evaluator_id"])
        task_ids.add(report["agent_task_id"])
        report["scores"] = scores
        reports.append(report)
    if len(evaluator_ids) != 3 or len(task_ids) != 3:
        raise P5JudgeError("JUDGE_ROUND_IDENTITY_NOT_UNIQUE")
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        round_index = report["round_index"]
        for score in report["scores"]:
            mapping_row = mapping_by_round_key.get(
                (round_index, score["blind_item_id"], score["slot_id"])
            )
            if mapping_row is None:
                raise P5JudgeError("JUDGE_SCORE_MAPPING_MISSING")
            if report["model_id"] == mapping_row["runtime_generator_model"]:
                raise P5JudgeError("JUDGE_RUNTIME_SELF_REVIEW")
            grouped[(score["blind_item_id"], mapping_row["variant_id"])].append(score)
    if len(grouped) * 3 != len(rows) or any(len(values) != 3 for values in grouped.values()):
        raise P5JudgeError("JUDGE_VARIANT_COVERAGE_INVALID")
    agreement_count = 0
    dimension_agreement_count = 0
    variant_values: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for (_, variant_id), values in grouped.items():
        verdicts = [value["derived_verdict"] for value in values]
        unanimous = len(set(verdicts)) == 1
        agreement_count += int(unanimous)
        dimensions_agree = all(
            max(value[dimension] for value in values)
            - min(value[dimension] for value in values)
            <= 1
            for dimension in (
                "clarity",
                "actionability",
                "evidence_boundary_expression",
            )
        )
        dimension_agreement_count += int(dimensions_agree)
        majority_pass = verdicts.count("PASS") >= 2
        variant_values[variant_id].append(
            {
                "majority_pass": majority_pass,
                "unanimous": unanimous,
                "dimensions_agree": dimensions_agree,
                "clarity": median(value["clarity"] for value in values),
                "actionability": median(value["actionability"] for value in values),
                "evidence_boundary_expression": median(
                    value["evidence_boundary_expression"] for value in values
                ),
                "claim_candidate_count": len(
                    {
                        candidate
                        for value in values
                        for candidate in value["unsupported_claim_candidate_ids"]
                    }
                ),
            }
        )
    total = len(grouped)
    agreement_rate = agreement_count / total
    dimension_agreement_rate = dimension_agreement_count / total
    variant_metrics = {}
    for variant_id in VARIANT_IDS:
        values = variant_values[variant_id]
        variant_metrics[variant_id] = {
            "candidate_count": len(values),
            "majority_pass_count": sum(value["majority_pass"] for value in values),
            "majority_pass_rate": sum(value["majority_pass"] for value in values) / len(values),
            "unanimous_rate": sum(value["unanimous"] for value in values) / len(values),
            "dimension_agreement_rate": sum(value["dimensions_agree"] for value in values)
            / len(values),
            "median_clarity": median(value["clarity"] for value in values),
            "median_actionability": median(value["actionability"] for value in values),
            "median_evidence_boundary_expression": median(
                value["evidence_boundary_expression"] for value in values
            ),
            "unsupported_claim_candidate_count": sum(
                value["claim_candidate_count"] for value in values
            ),
        }
    passed = agreement_rate >= minimum_agreement and dimension_agreement_rate >= minimum_agreement
    return {
        "schema_version": "trip-check-p5-judge-panel-v1",
        "status": "PASS" if passed else "BLOCKED",
        "evidence_class": "automated_proxy_judge",
        "human_calibration_performed": False,
        "round_count": 3,
        "candidate_count": total,
        "agreement_threshold": minimum_agreement,
        "verdict_agreement_rate": agreement_rate,
        "dimension_agreement_rate": dimension_agreement_rate,
        "variant_metrics": variant_metrics,
        "provenance": [
            {
                "round_index": report["round_index"],
                "evaluator_id": report["evaluator_id"],
                "agent_task_id": report["agent_task_id"],
                "model_id": report["model_id"],
                "bundle_sha256": report["bundle_sha256"],
                "api_usage_count": 0,
                "tool_usage_count": 0,
            }
            for report in reports
        ],
        "mapping_sha256": mapping_sha256,
        "run_group_manifest_hash": mapping["run_group_manifest_hash"],
        "deterministic_oracle_priority": True,
        "judge_may_override_deterministic_failure": False,
    }
