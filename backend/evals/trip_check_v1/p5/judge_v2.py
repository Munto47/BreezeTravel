"""Anonymous P5 v2 Judge export and strict three-round aggregation."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

from evals.trip_check_v1.p5.contracts_v2 import (
    P5CaseV2,
    P5TerminalOutputV2,
    VARIANT_IDS_V2,
)
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.scorer_v2 import load_jsonl, validate_run_group_v2


class P5JudgeErrorV2(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5JudgeErrorV2("JUDGE_ARTIFACT_UNREADABLE") from exc


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5JudgeErrorV2(reason) from exc
    if not isinstance(value, dict):
        raise P5JudgeErrorV2(reason)
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _public_input(case: P5CaseV2, materialization: Mapping[str, Any]) -> dict[str, Any]:
    if case.input_kind == "TEXT":
        return {
            "input_kind": "TEXT",
            "source_type": case.product_input.get("source_type"),
            "text": case.product_input.get("raw_text"),
        }
    receipt = materialization.get("ocr_baseline_receipt")
    lines = receipt.get("lines") if isinstance(receipt, Mapping) else None
    if not isinstance(lines, list) or not lines:
        raise P5JudgeErrorV2("JUDGE_SCREENSHOT_OCR_TEXT_MISSING")
    text_lines: list[str] = []
    for line in lines:
        if not isinstance(line, Mapping) or not isinstance(line.get("text"), str):
            raise P5JudgeErrorV2("JUDGE_SCREENSHOT_OCR_TEXT_INVALID")
        text_lines.append(line["text"])
    return {
        "input_kind": "SYNTHETIC_SCREENSHOT",
        "source_type": case.product_input.get("source_type"),
        "text": "\n".join(text_lines),
        "ocr_boundary": "FROZEN_ACTUAL_OCR_RECEIPT",
    }


def _candidate_expression(output: P5TerminalOutputV2) -> dict[str, Any]:
    advice = []
    allowed_advice = {
        "finding_reason",
        "action",
        "uncertainty",
        "has_repair",
        "candidate_set_bound",
    }
    for index, item in enumerate(output.advice, 1):
        if not isinstance(item, dict):
            raise P5JudgeErrorV2("JUDGE_ADVICE_INVALID")
        advice.append(
            {
                "claim_id": f"claim_{index:03d}",
                **{key: item.get(key) for key in sorted(allowed_advice)},
            }
        )
    return {
        "terminal_status": output.terminal_status.value,
        "advice": advice,
        "requires_user_resolution": output.evaluation_projection.get(
            "requires_user_resolution"
        ),
    }


def _evidence_summary(
    output: P5TerminalOutputV2, materialization: Mapping[str, Any]
) -> dict[str, Any]:
    snapshot = materialization.get("evidence_snapshot")
    snapshot_body = snapshot.get("snapshot") if isinstance(snapshot, Mapping) else None
    facts = snapshot_body.get("facts") if isinstance(snapshot_body, Mapping) else None
    freshness = Counter(
        str(item.get("freshness_status"))
        for item in facts or []
        if isinstance(item, Mapping)
    )
    postcheck = output.postcheck or {}
    return {
        "finding_summaries": [
            {
                "reason_code": item.get("reason_code"),
                "severity": item.get("severity"),
                "status": item.get("status"),
            }
            for item in output.findings
        ],
        "evidence_availability": {
            key: freshness.get(key, 0)
            for key in ("FRESH", "STALE", "UNAVAILABLE", "CONFLICTING")
        },
        "postcheck_boundary": {
            key: postcheck.get(key)
            for key in (
                "overall_status",
                "new_high_count",
                "new_unknown_count",
                "replay_side_effect_counts_equal",
            )
            if key in postcheck
        },
        "fact_authority": "DETERMINISTIC_SCORER_ONLY",
    }


def _validate_rubric(rubric: dict[str, Any]) -> None:
    allowed = {
        "schema_version",
        "judge_class",
        "fact_authority",
        "human_evidence",
        "human_calibration_performed",
        "input_policy",
        "judge_may_decide",
        "judge_must_not_decide",
        "dimensions",
    }
    dimensions = rubric.get("dimensions")
    if (
        set(rubric) != allowed
        or rubric.get("schema_version") != "trip-check-p5-judge-rubric-v2"
        or rubric.get("judge_class") != "automated_proxy_judge"
        or rubric.get("fact_authority") != "DETERMINISTIC_ORACLE_ONLY"
        or rubric.get("human_evidence") is not False
        or rubric.get("human_calibration_performed") is not False
        or rubric.get("judge_may_decide")
        != ["clarity", "actionability", "evidence_boundary_expression"]
        or not isinstance(dimensions, dict)
        or set(dimensions)
        != {"clarity", "actionability", "evidence_boundary_expression"}
    ):
        raise P5JudgeErrorV2("JUDGE_RUBRIC_CONTRACT_INVALID")


def export_judge_bundles_v2(
    *,
    repo_root: Path,
    run_dir: Path,
    cases_path: Path,
    materializations_path: Path,
    dataset_manifest_path: Path,
    output_dir: Path,
    rubric_path: Path,
) -> dict[str, Any]:
    root = repo_root.resolve()
    destination = output_dir.resolve()
    if _inside(destination, root):
        raise P5JudgeErrorV2("BLIND_JUDGE_EXPORT_INSIDE_REPOSITORY")
    manifest, cases, outputs = validate_run_group_v2(
        run_dir=run_dir.resolve(),
        cases_path=cases_path.resolve(),
        materializations_path=materializations_path.resolve(),
        dataset_manifest_path=dataset_manifest_path.resolve(),
        expected_lane="frozen_blind",
        require_formal=True,
    )
    if len(cases) != 90 or len(outputs) != 270:
        raise P5JudgeErrorV2("JUDGE_FORMAL_COVERAGE_INVALID")
    materialization_rows = load_jsonl(materializations_path.resolve())
    materializations = {
        str(row.get("case_id")): row for row in materialization_rows
    }
    if len(materializations) != 90 or set(materializations) != {case.case_id for case in cases}:
        raise P5JudgeErrorV2("JUDGE_MATERIALIZATION_SET_INVALID")
    rubric = _load_json(rubric_path.resolve(), "JUDGE_RUBRIC_INVALID")
    _validate_rubric(rubric)

    destination.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    ordered_cases = sorted(cases, key=lambda item: item.case_id)
    output_by_key = {(item.case_id, item.variant_id): item for item in outputs}
    blind_ids = {
        case.case_id: hmac.new(
            secret,
            f"{manifest['manifest_hash']}:{case.case_id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        for case in ordered_cases
    }
    mapping_rows: list[dict[str, Any]] = []
    round_items: dict[int, list[dict[str, Any]]] = {}
    for round_index in range(1, 4):
        items: list[dict[str, Any]] = []
        for case_index, case in enumerate(ordered_cases):
            shift = (case_index + round_index - 1) % 3
            order = VARIANT_IDS_V2[shift:] + VARIANT_IDS_V2[:shift]
            for slot_index, variant_id in enumerate(order, 1):
                output = output_by_key.get((case.case_id, variant_id))
                if output is None:
                    raise P5JudgeErrorV2("JUDGE_TERMINAL_KEY_MISSING")
                slot_id = f"slot_{slot_index}"
                candidate_expression = _candidate_expression(output)
                items.append(
                    {
                        "blind_item_id": blind_ids[case.case_id],
                        "slot_id": slot_id,
                        "public_input": _public_input(
                            case, materializations[case.case_id]
                        ),
                        "candidate_expression": candidate_expression,
                        "evidence_summary": _evidence_summary(
                            output, materializations[case.case_id]
                        ),
                    }
                )
                mapping_rows.append(
                    {
                        "round_index": round_index,
                        "blind_item_id": blind_ids[case.case_id],
                        "slot_id": slot_id,
                        "case_id": case.case_id,
                        "variant_id": variant_id,
                        "runtime_generator_model": "none-controlled-runtime",
                        "claim_ids": [
                            item["claim_id"]
                            for item in candidate_expression["advice"]
                        ],
                    }
                )
        round_items[round_index] = items
    if len(mapping_rows) != 810:
        raise P5JudgeErrorV2("JUDGE_MAPPING_COVERAGE_INVALID")
    for round_index in range(1, 4):
        counts = Counter(
            (row["slot_id"], row["variant_id"])
            for row in mapping_rows
            if row["round_index"] == round_index
        )
        if set(counts.values()) != {30}:
            raise P5JudgeErrorV2("JUDGE_PERMUTATION_UNBALANCED")

    mapping_commitment = digest(mapping_rows)
    rubric_sha256 = _sha256(rubric_path.resolve())
    bundle_receipts = []
    for round_index, items in round_items.items():
        bundle = {
            "schema_version": "trip-check-p5-judge-bundle-v2",
            "round_index": round_index,
            "evidence_class": "automated_proxy_judge_input",
            "human_evidence": False,
            "run_binding": {
                "subject_commit": manifest["subject_commit"],
                "run_group_manifest_hash": manifest["manifest_hash"],
                "terminal_outputs_file_sha256": manifest[
                    "terminal_outputs_file_sha256"
                ],
                "terminal_outputs_content_sha256": manifest[
                    "terminal_outputs_content_sha256"
                ],
                "variant_output_sha256": manifest["variant_output_sha256"],
                "rubric_sha256": rubric_sha256,
                "mapping_commitment": mapping_commitment,
            },
            "rubric": rubric,
            "items": items,
        }
        path = destination / f"judge_bundle_round_{round_index}.v2.json"
        path.write_bytes(canonical_bytes(bundle) + b"\n")
        bundle_receipts.append(
            {
                "round_index": round_index,
                "path": path.name,
                "sha256": _sha256(path),
                "rubric_sha256": rubric_sha256,
                "terminal_outputs_content_sha256": manifest[
                    "terminal_outputs_content_sha256"
                ],
                "item_count": 270,
            }
        )
    mapping = {
        "schema_version": "trip-check-p5-judge-mapping-v2",
        "run_group_manifest_hash": manifest["manifest_hash"],
        "terminal_outputs_content_sha256": manifest[
            "terminal_outputs_content_sha256"
        ],
        "mapping_commitment": mapping_commitment,
        "bundle_receipts": bundle_receipts,
        "rows": mapping_rows,
    }
    mapping_path = destination / "judge_variant_mapping.v2.json"
    mapping_path.write_bytes(canonical_bytes(mapping) + b"\n")
    return {
        "schema_version": "trip-check-p5-judge-export-receipt-v2",
        "lane": "frozen_blind",
        "case_count": 90,
        "items_per_round": 270,
        "round_count": 3,
        "balanced_permutation": True,
        "mapping_commitment": mapping_commitment,
        "mapping_file_sha256": _sha256(mapping_path),
        "bundle_receipts": bundle_receipts,
        "oracle_payload_exported": False,
        "variant_identity_exported_to_judges": False,
        "label_payload_exported": False,
        "other_round_exported": False,
        "human_evidence": False,
    }


def _validate_round_report(
    report: dict[str, Any], round_index: int, receipt: dict[str, Any]
) -> list[dict[str, Any]]:
    allowed = {
        "schema_version",
        "round_index",
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "model_id",
        "started_at",
        "ended_at",
        "bundle_sha256",
        "rubric_sha256",
        "terminal_outputs_content_sha256",
        "api_usage_count",
        "tool_usage_count",
        "scores",
    }
    if (
        set(report) != allowed
        or report.get("schema_version") != "trip-check-p5-judge-round-v2"
        or report.get("round_index") != round_index
        or report.get("bundle_sha256") != receipt["sha256"]
        or report.get("rubric_sha256") != receipt["rubric_sha256"]
        or report.get("terminal_outputs_content_sha256")
        != receipt["terminal_outputs_content_sha256"]
        or report.get("api_usage_count") != 0
        or report.get("tool_usage_count") != 0
    ):
        raise P5JudgeErrorV2("JUDGE_ROUND_CONTRACT_INVALID")
    for key in (
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "model_id",
        "started_at",
        "ended_at",
    ):
        if not isinstance(report.get(key), str) or not report[key]:
            raise P5JudgeErrorV2("JUDGE_ROUND_PROVENANCE_INVALID")
    scores = report.get("scores")
    if not isinstance(scores, list) or len(scores) != 270:
        raise P5JudgeErrorV2("JUDGE_ROUND_SCORE_COUNT_INVALID")
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
            raise P5JudgeErrorV2("JUDGE_SCORE_SCHEMA_INVALID")
        dimensions = [
            score.get("clarity"),
            score.get("actionability"),
            score.get("evidence_boundary_expression"),
        ]
        if any(not isinstance(value, int) or not 0 <= value <= 4 for value in dimensions):
            raise P5JudgeErrorV2("JUDGE_SCORE_RANGE_INVALID")
        candidates = score.get("unsupported_claim_candidate_ids")
        if not isinstance(candidates, list) or any(
            not isinstance(value, str) or not value.strip() for value in candidates
        ):
            raise P5JudgeErrorV2("JUDGE_CLAIM_CANDIDATES_INVALID")
        if len(candidates) != len(set(candidates)):
            raise P5JudgeErrorV2("JUDGE_CLAIM_CANDIDATES_INVALID")
        if (
            not isinstance(score.get("blind_item_id"), str)
            or not score["blind_item_id"]
            or score.get("slot_id") not in {"slot_1", "slot_2", "slot_3"}
        ):
            raise P5JudgeErrorV2("JUDGE_SCORE_ID_INVALID")
        verdict = "PASS" if min(dimensions) >= 2 and not candidates else "NEEDS_REVISION"
        if score.get("derived_verdict") != verdict:
            raise P5JudgeErrorV2("JUDGE_DERIVED_VERDICT_INVALID")
        keys.append((score.get("blind_item_id"), score.get("slot_id")))
    if len(keys) != len(set(keys)):
        raise P5JudgeErrorV2("JUDGE_SCORE_KEY_DUPLICATE")
    return scores


def aggregate_judge_rounds_v2(
    *,
    repo_root: Path,
    mapping_path: Path,
    mapping_sha256: str,
    round_paths: Sequence[Path],
    minimum_agreement: float = 0.85,
) -> dict[str, Any]:
    root = repo_root.resolve()
    resolved_mapping = mapping_path.resolve()
    if _inside(resolved_mapping, root):
        raise P5JudgeErrorV2("BLIND_JUDGE_MAPPING_INSIDE_REPOSITORY")
    if _sha256(resolved_mapping) != mapping_sha256 or len(round_paths) != 3:
        raise P5JudgeErrorV2("JUDGE_MAPPING_OR_ROUND_COUNT_INVALID")
    mapping = _load_json(resolved_mapping, "JUDGE_MAPPING_INVALID")
    receipts = mapping.get("bundle_receipts")
    rows = mapping.get("rows")
    if (
        set(mapping)
        != {
            "schema_version",
            "run_group_manifest_hash",
            "terminal_outputs_content_sha256",
            "mapping_commitment",
            "bundle_receipts",
            "rows",
        }
        or mapping.get("schema_version") != "trip-check-p5-judge-mapping-v2"
        or not isinstance(receipts, list)
        or len(receipts) != 3
        or not isinstance(rows, list)
        or len(rows) != 810
        or digest(rows) != mapping.get("mapping_commitment")
    ):
        raise P5JudgeErrorV2("JUDGE_MAPPING_CONTRACT_INVALID")
    expected_receipt_fields = {
        "round_index",
        "path",
        "sha256",
        "rubric_sha256",
        "terminal_outputs_content_sha256",
        "item_count",
    }
    if (
        not _is_sha256(mapping.get("run_group_manifest_hash"))
        or not _is_sha256(mapping.get("terminal_outputs_content_sha256"))
        or any(
            not isinstance(receipt, dict)
            or set(receipt) != expected_receipt_fields
            or receipt.get("round_index") != round_index
            or receipt.get("path") != f"judge_bundle_round_{round_index}.v2.json"
            or not _is_sha256(receipt.get("sha256"))
            or not _is_sha256(receipt.get("rubric_sha256"))
            or receipt.get("terminal_outputs_content_sha256")
            != mapping["terminal_outputs_content_sha256"]
            or receipt.get("item_count") != 270
            for round_index, receipt in enumerate(receipts, 1)
        )
        or len({receipt["rubric_sha256"] for receipt in receipts}) != 1
    ):
        raise P5JudgeErrorV2("JUDGE_BUNDLE_RECEIPT_INVALID")
    expected_mapping_fields = {
        "round_index",
        "blind_item_id",
        "slot_id",
        "case_id",
        "variant_id",
        "runtime_generator_model",
        "claim_ids",
    }
    if any(
        not isinstance(row, dict)
        or set(row) != expected_mapping_fields
        or type(row.get("round_index")) is not int
        or row["round_index"] not in {1, 2, 3}
        or not isinstance(row.get("blind_item_id"), str)
        or not row["blind_item_id"]
        or row.get("slot_id") not in {"slot_1", "slot_2", "slot_3"}
        or not isinstance(row.get("case_id"), str)
        or not row["case_id"]
        or row.get("variant_id") not in VARIANT_IDS_V2
        or not isinstance(row.get("runtime_generator_model"), str)
        or not row["runtime_generator_model"]
        or not isinstance(row.get("claim_ids"), list)
        or any(
            not isinstance(claim_id, str) or not claim_id
            for claim_id in row["claim_ids"]
        )
        or len(row["claim_ids"]) != len(set(row["claim_ids"]))
        for row in rows
    ):
        raise P5JudgeErrorV2("JUDGE_MAPPING_ROW_INVALID")
    mapping_by_key = {
        (row["round_index"], row["blind_item_id"], row["slot_id"]): row
        for row in rows
    }
    if len(mapping_by_key) != 810:
        raise P5JudgeErrorV2("JUDGE_MAPPING_KEY_SET_INVALID")
    blind_ids = {row["blind_item_id"] for row in rows}
    case_ids = {row["case_id"] for row in rows}
    blind_to_cases: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        blind_to_cases[row["blind_item_id"]].add(row["case_id"])
    if (
        len(blind_ids) != 90
        or len(case_ids) != 90
        or any(len(values) != 1 for values in blind_to_cases.values())
    ):
        raise P5JudgeErrorV2("JUDGE_ANONYMOUS_MAPPING_NOT_CLOSED")
    expected_round_keys = {
        (blind_item_id, slot_id)
        for blind_item_id in blind_ids
        for slot_id in ("slot_1", "slot_2", "slot_3")
    }
    for round_index in (1, 2, 3):
        round_rows = [row for row in rows if row["round_index"] == round_index]
        round_keys = {(row["blind_item_id"], row["slot_id"]) for row in round_rows}
        round_variants = Counter(
            (row["blind_item_id"], row["variant_id"]) for row in round_rows
        )
        if (
            len(round_rows) != 270
            or round_keys != expected_round_keys
            or set(round_variants) != {
                (blind_item_id, variant_id)
                for blind_item_id in blind_ids
                for variant_id in VARIANT_IDS_V2
            }
            or set(round_variants.values()) != {1}
        ):
            raise P5JudgeErrorV2("JUDGE_MAPPING_ROUND_COVERAGE_INVALID")

    reports = []
    identities: dict[str, set[str]] = {
        "evaluator_id": set(),
        "agent_task_id": set(),
        "agent_id": set(),
    }
    for round_index, path in enumerate(round_paths, 1):
        if _inside(path.resolve(), root):
            raise P5JudgeErrorV2("BLIND_JUDGE_ROUND_INSIDE_REPOSITORY")
        report = _load_json(path.resolve(), "JUDGE_ROUND_INVALID")
        report["scores"] = _validate_round_report(
            report, round_index, receipts[round_index - 1]
        )
        for key in identities:
            identities[key].add(report[key])
        reports.append(report)
    if any(len(values) != 3 for values in identities.values()):
        raise P5JudgeErrorV2("JUDGE_ROUND_IDENTITY_NOT_UNIQUE")

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        for score in report["scores"]:
            row = mapping_by_key.get(
                (report["round_index"], score["blind_item_id"], score["slot_id"])
            )
            if row is None:
                raise P5JudgeErrorV2("JUDGE_SCORE_MAPPING_MISSING")
            if not set(score["unsupported_claim_candidate_ids"]).issubset(
                row["claim_ids"]
            ):
                raise P5JudgeErrorV2("JUDGE_CLAIM_ID_NOT_IN_CANDIDATE")
            if report["model_id"] == row.get("runtime_generator_model"):
                raise P5JudgeErrorV2("JUDGE_RUNTIME_SELF_REVIEW")
            grouped[(score["blind_item_id"], row["variant_id"])].append(score)
    if len(grouped) != 270 or any(len(values) != 3 for values in grouped.values()):
        raise P5JudgeErrorV2("JUDGE_VARIANT_COVERAGE_INVALID")

    dimensions = ("clarity", "actionability", "evidence_boundary_expression")
    variant_values: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unanimous_count = 0
    dimension_agreement_counts = Counter()
    for (_, variant_id), values in grouped.items():
        verdicts = [value["derived_verdict"] for value in values]
        unanimous = len(set(verdicts)) == 1
        unanimous_count += int(unanimous)
        per_dimension = {
            dimension: max(value[dimension] for value in values)
            - min(value[dimension] for value in values)
            <= 1
            for dimension in dimensions
        }
        dimension_agreement_counts.update(
            dimension for dimension, agreed in per_dimension.items() if agreed
        )
        variant_values[variant_id].append(
            {
                "majority_pass": verdicts.count("PASS") >= 2,
                "unanimous": unanimous,
                "dimension_agreement": per_dimension,
                **{
                    dimension: median(value[dimension] for value in values)
                    for dimension in dimensions
                },
                "unsupported_claim_candidate_count": len(
                    {
                        claim_id
                        for value in values
                        for claim_id in value["unsupported_claim_candidate_ids"]
                    }
                ),
            }
        )
    total = len(grouped)
    verdict_agreement = unanimous_count / total
    per_dimension_agreement = {
        dimension: dimension_agreement_counts[dimension] / total
        for dimension in dimensions
    }
    passed = verdict_agreement >= minimum_agreement and all(
        value >= minimum_agreement for value in per_dimension_agreement.values()
    )
    variant_metrics = {}
    for variant_id in VARIANT_IDS_V2:
        values = variant_values[variant_id]
        variant_metrics[variant_id] = {
            "candidate_count": len(values),
            "majority_pass_count": sum(item["majority_pass"] for item in values),
            "majority_pass_rate": sum(item["majority_pass"] for item in values)
            / len(values),
            "unanimous_rate": sum(item["unanimous"] for item in values) / len(values),
            "per_dimension_agreement_rate": {
                dimension: sum(
                    item["dimension_agreement"][dimension] for item in values
                )
                / len(values)
                for dimension in dimensions
            },
            **{
                f"median_{dimension}": median(item[dimension] for item in values)
                for dimension in dimensions
            },
        }
    unsupported_claim_candidate_count = sum(
        item["unsupported_claim_candidate_count"]
        for values in variant_values.values()
        for item in values
    )
    panel = {
        "schema_version": "trip-check-p5-judge-panel-v2",
        "status": "PASS" if passed else "BLOCKED",
        "evidence_class": "automated_proxy_judge",
        "human_calibration_performed": False,
        "round_count": 3,
        "candidate_count": 270,
        "agreement_threshold": minimum_agreement,
        "verdict_agreement_rate": verdict_agreement,
        "per_dimension_agreement_rate": per_dimension_agreement,
        "variant_metrics": variant_metrics,
        "provenance": [
            {
                "round_index": report["round_index"],
                "evaluator_id": report["evaluator_id"],
                "agent_task_id": report["agent_task_id"],
                "agent_id": report["agent_id"],
                "model_id": report["model_id"],
                "bundle_sha256": report["bundle_sha256"],
                "rubric_sha256": report["rubric_sha256"],
                "terminal_outputs_content_sha256": report[
                    "terminal_outputs_content_sha256"
                ],
                "api_usage_count": 0,
                "tool_usage_count": 0,
            }
            for report in reports
        ],
        "mapping_sha256": mapping_sha256,
        "run_group_manifest_hash": mapping["run_group_manifest_hash"],
        "terminal_outputs_content_sha256": mapping[
            "terminal_outputs_content_sha256"
        ],
        "deterministic_scorer_priority": True,
        "judge_may_override_deterministic_failure": False,
        "unsupported_claim_candidate_count": unsupported_claim_candidate_count,
    }
    panel["report_hash"] = digest(panel)
    return panel


__all__ = [
    "P5JudgeErrorV2",
    "aggregate_judge_rounds_v2",
    "export_judge_bundles_v2",
]
