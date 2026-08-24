"""Anonymous, three-round automated Judge boundary for P5 v5.

The module deliberately uses a delayed blind-run validator instead of importing
``contracts_v5``.  This keeps the Judge boundary mergeable while the v5 runner
and scorer are developed on isolated branches.  A formal call still fails
closed unless the validator returns the exact 90-case/270-terminal run shape.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import secrets
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest


VARIANT_IDS_V5 = ("legacy_a", "core_b", "solver_c")
DIMENSIONS_V5 = ("clarity", "actionability", "evidence_boundary_expression")
ROUND_COUNT_V5 = 3
BLIND_CASE_COUNT_V5 = 90
BLIND_TERMINAL_COUNT_V5 = 270
JUDGE_AGREEMENT_THRESHOLD_V5 = 0.85
STRUCTURED_EXPRESSION_FIELDS_V5 = (
    "finding_reason",
    "action",
    "uncertainty",
    "has_repair",
    "candidate_set_bound",
)


class P5JudgeErrorV5(RuntimeError):
    """Stable fail-closed Judge error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


BlindRunValidatorV5 = Callable[..., object]


def _value(item: object, name: str, default: object = None) -> object:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _plain(value: object) -> object:
    return getattr(value, "value", value)


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5JudgeErrorV5("JUDGE_ARTIFACT_UNREADABLE") from exc


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5JudgeErrorV5(reason) from exc
    if not isinstance(value, dict):
        raise P5JudgeErrorV5(reason)
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _contains_link(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.exists() and (
                current.is_symlink()
                or (hasattr(current, "is_junction") and current.is_junction())
            ):
                return True
        except OSError:
            return True
    return False


def _require_external_distinct_dirs(repo_root: Path, paths: Sequence[Path]) -> list[Path]:
    if len(paths) != ROUND_COUNT_V5 + 1 or any(not path.is_absolute() for path in paths):
        raise P5JudgeErrorV5("JUDGE_CUSTODY_PATH_INVALID")
    resolved = [path.absolute() for path in paths]
    if any(_inside(path, repo_root) or _contains_link(path) for path in resolved):
        raise P5JudgeErrorV5("JUDGE_CUSTODY_PATH_INVALID")
    if len({str(path.resolve()) for path in resolved}) != len(resolved):
        raise P5JudgeErrorV5("JUDGE_CUSTODY_PATH_NOT_DISTINCT")
    for left in resolved:
        for right in resolved:
            if left == right:
                continue
            try:
                right.resolve().relative_to(left.resolve())
            except ValueError:
                continue
            raise P5JudgeErrorV5("JUDGE_CUSTODY_PATH_NOT_DISTINCT")
    return resolved


def _default_blind_run_validator_v5(
    *, run_dir: Path, repo_root: Path, require_formal: bool
) -> object:
    """Load the v5 blind validator only after the integrated slice exists."""

    candidates = (
        ("evals.trip_check_v1.p5.runner_v5", "validate_blind_run_group_v5"),
        (
            "evals.trip_check_v1.p5.blind_scorer_v5",
            "validate_blind_run_group_v5",
        ),
        (
            "evals.trip_check_v1.p5.final_blind_scorer_v5",
            "validate_blind_run_group_v5",
        ),
        ("evals.trip_check_v1.p5.scorer_v5", "validate_blind_run_group_v5"),
    )
    for module_name, function_name in candidates:
        try:
            module = importlib.import_module(module_name)
        except ModuleNotFoundError:
            continue
        validator = getattr(module, function_name, None)
        if callable(validator):
            return validator(
                run_dir=run_dir,
                repo_root=repo_root,
                require_formal=require_formal,
            )
    raise P5JudgeErrorV5("V5_BLIND_RUN_VALIDATOR_UNAVAILABLE")


def _normalize_validated_run_v5(result: object) -> tuple[
    dict[str, Any], list[object], list[object], dict[str, Mapping[str, Any]]
]:
    if not isinstance(result, tuple) or len(result) != 4:
        raise P5JudgeErrorV5("V5_BLIND_RUN_VALIDATOR_PROTOCOL_INVALID")
    manifest, raw_cases, raw_outputs, raw_materializations = result
    if (
        not isinstance(manifest, dict)
        or not isinstance(raw_cases, list)
        or not isinstance(raw_outputs, list)
    ):
        raise P5JudgeErrorV5("V5_BLIND_RUN_VALIDATOR_PROTOCOL_INVALID")
    if isinstance(raw_materializations, Mapping):
        materializations = {
            str(key): value
            for key, value in raw_materializations.items()
            if isinstance(value, Mapping)
        }
    elif isinstance(raw_materializations, list):
        materializations = {
            str(row.get("case_id")): row
            for row in raw_materializations
            if isinstance(row, Mapping)
        }
    else:
        raise P5JudgeErrorV5("V5_BLIND_RUN_VALIDATOR_PROTOCOL_INVALID")
    if len(materializations) != len(raw_cases):
        raise P5JudgeErrorV5("JUDGE_MATERIALIZATION_SET_INVALID")
    return manifest, raw_cases, raw_outputs, materializations


def _validate_blind_run_shape_v5(
    manifest: Mapping[str, Any], cases: Sequence[object], outputs: Sequence[object]
) -> None:
    case_ids = [_value(case, "case_id") for case in cases]
    output_keys = [
        (_value(output, "case_id"), _value(output, "variant_id")) for output in outputs
    ]
    expected_keys = {
        (case_id, variant_id) for case_id in case_ids for variant_id in VARIANT_IDS_V5
    }
    required_hashes = (
        "manifest_hash",
        "dataset_manifest_hash",
        "artifact_index_hash",
        "terminal_outputs_file_sha256",
        "terminal_outputs_content_sha256",
        "run_spec_template_sha256",
    )
    if (
        len(cases) != BLIND_CASE_COUNT_V5
        or len(outputs) != BLIND_TERMINAL_COUNT_V5
        or len(case_ids) != len(set(case_ids))
        or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
        or len(output_keys) != len(set(output_keys))
        or set(output_keys) != expected_keys
        or manifest.get("lane") != "frozen_blind"
        or manifest.get("status") != "PASS"
        or manifest.get("formal_evidence") is not True
        or manifest.get("dirty_tree") is not False
        or manifest.get("case_count") != BLIND_CASE_COUNT_V5
        or manifest.get("terminal_count") != BLIND_TERMINAL_COUNT_V5
        or manifest.get("replay_executed") is not True
        or manifest.get("replay_readback_count") != BLIND_TERMINAL_COUNT_V5
        or manifest.get("replay_mismatches") != []
        or manifest.get("blind_labels_read") is not False
        or any(not _is_sha256(manifest.get(field)) for field in required_hashes)
    ):
        raise P5JudgeErrorV5("JUDGE_FORMAL_BLIND_RUN_INVALID")


def _public_input(case: object, materialization: Mapping[str, Any]) -> dict[str, Any]:
    input_kind = str(_plain(_value(case, "input_kind")))
    product_input = _value(case, "product_input")
    if not isinstance(product_input, Mapping):
        raise P5JudgeErrorV5("JUDGE_PRODUCT_INPUT_INVALID")
    if input_kind == "TEXT":
        text = product_input.get("raw_text")
    else:
        receipt = materialization.get("ocr_baseline_receipt")
        lines = receipt.get("lines") if isinstance(receipt, Mapping) else None
        if not isinstance(lines, list) or not lines:
            raise P5JudgeErrorV5("JUDGE_SCREENSHOT_OCR_TEXT_MISSING")
        if any(
            not isinstance(line, Mapping) or not isinstance(line.get("text"), str)
            for line in lines
        ):
            raise P5JudgeErrorV5("JUDGE_SCREENSHOT_OCR_TEXT_INVALID")
        text = "\n".join(str(line["text"]) for line in lines)
    if not isinstance(text, str) or not text.strip():
        raise P5JudgeErrorV5("JUDGE_PUBLIC_INPUT_TEXT_INVALID")
    return {
        "input_kind": input_kind,
        "source_type": product_input.get("source_type"),
        "text": text,
        "input_evidence_boundary": (
            "SOURCE_TEXT" if input_kind == "TEXT" else "FROZEN_OCR_TEXT_RECEIPT"
        ),
    }


def _candidate_expression(output: object) -> dict[str, Any]:
    raw_advice = _value(output, "advice")
    if not isinstance(raw_advice, list):
        raise P5JudgeErrorV5("JUDGE_ADVICE_INVALID")
    advice: list[dict[str, Any]] = []
    allowed = {
        "finding_reason",
        "action",
        "uncertainty",
        "has_repair",
        "candidate_set_bound",
    }
    for index, item in enumerate(raw_advice, 1):
        if not isinstance(item, Mapping):
            raise P5JudgeErrorV5("JUDGE_ADVICE_INVALID")
        advice.append(
            {
                "claim_id": f"claim_{index:03d}",
                **{key: item.get(key) for key in sorted(allowed)},
            }
        )
    projection = _value(output, "evaluation_projection")
    if not isinstance(projection, Mapping):
        raise P5JudgeErrorV5("JUDGE_PROJECTION_INVALID")
    return {
        "terminal_status": str(_plain(_value(output, "terminal_status"))),
        "advice": advice,
        "requires_user_resolution": projection.get("requires_user_resolution"),
    }


def _evidence_summary(
    output: object, materialization: Mapping[str, Any]
) -> dict[str, Any]:
    snapshot = materialization.get("evidence_snapshot")
    snapshot_body = snapshot.get("snapshot") if isinstance(snapshot, Mapping) else None
    facts = snapshot_body.get("facts") if isinstance(snapshot_body, Mapping) else None
    freshness = Counter(
        str(item.get("freshness_status"))
        for item in facts or []
        if isinstance(item, Mapping)
    )
    findings = _value(output, "findings")
    postcheck = _value(output, "postcheck")
    if not isinstance(findings, list) or (
        postcheck is not None and not isinstance(postcheck, Mapping)
    ):
        raise P5JudgeErrorV5("JUDGE_EVIDENCE_SUMMARY_INVALID")
    postcheck_boundary = (
        {
            key: postcheck.get(key)
            for key in (
                "overall_status",
                "new_high_count",
                "new_unknown_count",
                "replay_side_effect_counts_equal",
            )
            if key in postcheck
        }
        if isinstance(postcheck, Mapping)
        else {"availability": "NOT_PRESENT"}
    )
    return {
        "finding_summaries": [
            {
                "reason_code": item.get("reason_code"),
                "severity": item.get("severity"),
                "status": item.get("status"),
            }
            for item in findings
            if isinstance(item, Mapping)
        ],
        "evidence_availability": {
            key: freshness.get(key, 0)
            for key in ("FRESH", "STALE", "UNAVAILABLE", "CONFLICTING")
        },
        "postcheck_boundary": postcheck_boundary,
        "fact_authority": "DETERMINISTIC_SCORER_ONLY",
    }


def _judge_rubric_projection(source: Mapping[str, Any]) -> dict[str, Any]:
    if (
        source.get("schema_version") != "trip-check-p5-judge-rubric-v2"
        or source.get("judge_class") != "automated_proxy_judge"
        or source.get("human_calibration_performed") is not False
        or source.get("human_evidence") is not False
        or source.get("judge_may_decide") != list(DIMENSIONS_V5)
        or not isinstance(source.get("dimensions"), Mapping)
        or set(source["dimensions"]) != set(DIMENSIONS_V5)
    ):
        raise P5JudgeErrorV5("JUDGE_RUBRIC_CONTRACT_INVALID")
    return {
        "schema_version": "trip-check-p5-judge-rubric-projection-v5",
        "judge_class": "automated_proxy_judge",
        "fact_authority": "DETERMINISTIC_SCORER_ONLY",
        "human_evidence": False,
        "human_calibration_performed": False,
        "judge_may_decide": list(DIMENSIONS_V5),
        "judge_must_not_decide": source.get("judge_must_not_decide"),
        "dimensions": source["dimensions"],
    }


def judge_rubric_projection_hash_v5(source: Mapping[str, Any]) -> str:
    """Return the canonical hash of the rubric payload exported to Judges."""

    return digest(_judge_rubric_projection(source))


def _judge_protocol_projection(
    source_rubric: Mapping[str, Any], source_protocol: Mapping[str, Any]
) -> dict[str, Any]:
    anchors = source_protocol.get("dimension_anchors")
    agreement = source_protocol.get("agreement_rule")
    verdict = source_protocol.get("verdict_rule")
    calibration = source_protocol.get("preblind_calibration")
    instructions = source_protocol.get("evaluator_instruction")
    if (
        source_protocol.get("schema_version")
        != "trip-check-p5-judge-protocol-v1"
        or source_protocol.get("rubric_schema_version")
        != source_rubric.get("schema_version")
        or source_protocol.get("judge_class") != "automated_proxy_judge"
        or source_protocol.get("fact_authority") != "DETERMINISTIC_SCORER_ONLY"
        or source_protocol.get("human_calibration_performed") is not False
        or source_protocol.get("human_evidence") is not False
        or source_protocol.get("structured_expression_fields")
        != list(STRUCTURED_EXPRESSION_FIELDS_V5)
        or not isinstance(anchors, Mapping)
        or set(anchors) != set(DIMENSIONS_V5)
        or any(
            not isinstance(anchors[dimension], Mapping)
            or set(anchors[dimension]) != {str(score) for score in range(5)}
            or any(
                not isinstance(description, str) or not description.strip()
                for description in anchors[dimension].values()
            )
            for dimension in DIMENSIONS_V5
        )
        or not isinstance(instructions, list)
        or not instructions
        or any(not isinstance(item, str) or not item.strip() for item in instructions)
        or verdict
        != {
            "minimum_dimension_score": 2,
            "unsupported_claim_candidate_count": 0,
        }
        or agreement
        != {
            "dimension_max_spread": 1,
            "minimum_rate": JUDGE_AGREEMENT_THRESHOLD_V5,
            "required_dimensions": list(DIMENSIONS_V5),
            "verdict_unanimity_required_rate": JUDGE_AGREEMENT_THRESHOLD_V5,
        }
        or calibration
        != {
            "minimum_dimension_agreement_rate": JUDGE_AGREEMENT_THRESHOLD_V5,
            "minimum_verdict_agreement_rate": JUDGE_AGREEMENT_THRESHOLD_V5,
            "required": True,
            "source_lane": "NONBLIND_SYNTHETIC_ANCHORS",
        }
    ):
        raise P5JudgeErrorV5("JUDGE_PROTOCOL_CONTRACT_INVALID")
    return {
        "schema_version": "trip-check-p5-judge-protocol-projection-v1",
        "rubric_schema_version": source_protocol["rubric_schema_version"],
        "judge_class": "automated_proxy_judge",
        "fact_authority": "DETERMINISTIC_SCORER_ONLY",
        "human_evidence": False,
        "human_calibration_performed": False,
        "structured_expression_fields": list(STRUCTURED_EXPRESSION_FIELDS_V5),
        "dimension_anchors": anchors,
        "evaluator_instruction": instructions,
        "verdict_rule": verdict,
        "agreement_rule": agreement,
        "preblind_calibration": calibration,
    }


def judge_protocol_projection_hash_v5(
    source_rubric: Mapping[str, Any], source_protocol: Mapping[str, Any]
) -> str:
    """Return the canonical hash of the operational protocol sent to Judges."""

    return digest(_judge_protocol_projection(source_rubric, source_protocol))


def _assert_anonymous_bundle(bundle: Mapping[str, Any]) -> None:
    serialized = json.dumps(bundle, ensure_ascii=False, sort_keys=True).lower()
    forbidden_fragments = (
        '"variant_id"',
        '"case_id"',
        '"oracle"',
        '"blind_label"',
        '"label_payload"',
        '"other_judge',
        '"judge_result',
        *VARIANT_IDS_V5,
    )
    if any(fragment in serialized for fragment in forbidden_fragments):
        raise P5JudgeErrorV5("JUDGE_INPUT_ANONYMITY_VIOLATION")


def export_judge_bundles_v5(
    *,
    repo_root: Path,
    run_dir: Path,
    round_output_dirs: Sequence[Path],
    custody_output_dir: Path,
    rubric_path: Path,
    protocol_path: Path,
    blind_run_validator: BlindRunValidatorV5 | None = None,
) -> dict[str, Any]:
    """Export one anonymous input-only bundle per independent Judge round."""

    root = repo_root.resolve()
    paths = _require_external_distinct_dirs(
        root, [*round_output_dirs, custody_output_dir]
    )
    round_dirs, custody_dir = paths[:3], paths[3]
    validator = blind_run_validator or _default_blind_run_validator_v5
    result = validator(run_dir=run_dir, repo_root=root, require_formal=True)
    manifest, cases, outputs, materializations = _normalize_validated_run_v5(result)
    _validate_blind_run_shape_v5(manifest, cases, outputs)

    source_rubric = _load_json(rubric_path.resolve(), "JUDGE_RUBRIC_INVALID")
    rubric = _judge_rubric_projection(source_rubric)
    source_protocol = _load_json(protocol_path.resolve(), "JUDGE_PROTOCOL_INVALID")
    protocol = _judge_protocol_projection(source_rubric, source_protocol)
    source_rubric_sha256 = _sha256(rubric_path.resolve())
    judge_input_rubric_sha256 = judge_rubric_projection_hash_v5(source_rubric)
    source_protocol_sha256 = _sha256(protocol_path.resolve())
    judge_input_protocol_sha256 = judge_protocol_projection_hash_v5(
        source_rubric, source_protocol
    )
    case_by_id = {str(_value(case, "case_id")): case for case in cases}
    output_by_key = {
        (str(_value(output, "case_id")), str(_value(output, "variant_id"))): output
        for output in outputs
    }
    ordered_case_ids = sorted(case_by_id)
    secret = secrets.token_bytes(32)
    anonymous_ids = {
        case_id: hmac.new(
            secret,
            f"{manifest['manifest_hash']}:{case_id}".encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        for case_id in ordered_case_ids
    }

    mapping_rows: list[dict[str, Any]] = []
    items_by_round: dict[int, list[dict[str, Any]]] = {}
    for round_index in range(1, ROUND_COUNT_V5 + 1):
        round_items: list[dict[str, Any]] = []
        for case_index, case_id in enumerate(ordered_case_ids):
            shift = (case_index + round_index - 1) % len(VARIANT_IDS_V5)
            ordered_variants = VARIANT_IDS_V5[shift:] + VARIANT_IDS_V5[:shift]
            for slot_index, variant_id in enumerate(ordered_variants, 1):
                output = output_by_key[(case_id, variant_id)]
                expression = _candidate_expression(output)
                slot_id = f"slot_{slot_index}"
                round_items.append(
                    {
                        "anonymous_item_id": anonymous_ids[case_id],
                        "slot_id": slot_id,
                        "public_input": _public_input(
                            case_by_id[case_id], materializations[case_id]
                        ),
                        "candidate_expression": expression,
                        "evidence_summary": _evidence_summary(
                            output, materializations[case_id]
                        ),
                    }
                )
                mapping_rows.append(
                    {
                        "round_index": round_index,
                        "anonymous_item_id": anonymous_ids[case_id],
                        "slot_id": slot_id,
                        "case_id": case_id,
                        "variant_id": variant_id,
                        "runtime_generator_model": "none-controlled-runtime",
                        "claim_ids": [item["claim_id"] for item in expression["advice"]],
                    }
                )
        items_by_round[round_index] = round_items

    if len(mapping_rows) != BLIND_TERMINAL_COUNT_V5 * ROUND_COUNT_V5:
        raise P5JudgeErrorV5("JUDGE_MAPPING_COVERAGE_INVALID")
    for round_index in range(1, ROUND_COUNT_V5 + 1):
        counts = Counter(
            (row["slot_id"], row["variant_id"])
            for row in mapping_rows
            if row["round_index"] == round_index
        )
        if set(counts.values()) != {BLIND_CASE_COUNT_V5 // 3}:
            raise P5JudgeErrorV5("JUDGE_PERMUTATION_UNBALANCED")

    mapping_commitment = digest(mapping_rows)
    bundle_receipts: list[dict[str, Any]] = []
    for round_index, (round_dir, items) in enumerate(
        zip(round_dirs, items_by_round.values(), strict=True), 1
    ):
        round_dir.mkdir(parents=True, exist_ok=True)
        path = round_dir / f"judge_input_round_{round_index}.v5.json"
        if path.exists():
            raise P5JudgeErrorV5("JUDGE_BUNDLE_ALREADY_EXISTS")
        bundle = {
            "schema_version": "trip-check-p5-judge-bundle-v5",
            "round_index": round_index,
            "evidence_class": "automated_proxy_judge_input",
            "automated_proxy_judge": True,
            "human_calibration_performed": False,
            "run_binding": {
                "subject_commit": manifest["subject_commit"],
                "run_group_manifest_hash": manifest["manifest_hash"],
                "artifact_index_hash": manifest["artifact_index_hash"],
                "terminal_outputs_file_sha256": manifest[
                    "terminal_outputs_file_sha256"
                ],
                "terminal_outputs_content_sha256": manifest[
                    "terminal_outputs_content_sha256"
                ],
                "source_rubric_sha256": source_rubric_sha256,
                "judge_input_rubric_sha256": judge_input_rubric_sha256,
                "source_protocol_sha256": source_protocol_sha256,
                "judge_input_protocol_sha256": judge_input_protocol_sha256,
                "mapping_commitment": mapping_commitment,
            },
            "input_boundary": {
                "anonymous": True,
                "identity_payload_present": False,
                "expected_answer_payload_present": False,
                "custodian_metadata_present": False,
                "peer_round_output_present": False,
            },
            "rubric": rubric,
            "protocol": protocol,
            "items": items,
        }
        _assert_anonymous_bundle(bundle)
        path.write_bytes(canonical_bytes(bundle) + b"\n")
        bundle_receipts.append(
            {
                "round_index": round_index,
                "path": path.name,
                "sha256": _sha256(path),
                "source_rubric_sha256": source_rubric_sha256,
                "judge_input_rubric_sha256": judge_input_rubric_sha256,
                "source_protocol_sha256": source_protocol_sha256,
                "judge_input_protocol_sha256": judge_input_protocol_sha256,
                "terminal_outputs_content_sha256": manifest[
                    "terminal_outputs_content_sha256"
                ],
                "item_count": BLIND_TERMINAL_COUNT_V5,
            }
        )

    custody_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = custody_dir / "judge_variant_mapping.v5.json"
    if mapping_path.exists():
        raise P5JudgeErrorV5("JUDGE_MAPPING_ALREADY_EXISTS")
    mapping = {
        "schema_version": "trip-check-p5-judge-mapping-v5",
        "subject_commit": manifest["subject_commit"],
        "dataset_manifest_hash": manifest["dataset_manifest_hash"],
        "run_group_manifest_hash": manifest["manifest_hash"],
        "artifact_index_hash": manifest["artifact_index_hash"],
        "terminal_outputs_content_sha256": manifest[
            "terminal_outputs_content_sha256"
        ],
        "mapping_commitment": mapping_commitment,
        "bundle_receipts": bundle_receipts,
        "rows": mapping_rows,
    }
    mapping_path.write_bytes(canonical_bytes(mapping) + b"\n")
    return {
        "schema_version": "trip-check-p5-judge-export-receipt-v5",
        "lane": "frozen_blind",
        "case_count": BLIND_CASE_COUNT_V5,
        "items_per_round": BLIND_TERMINAL_COUNT_V5,
        "round_count": ROUND_COUNT_V5,
        "balanced_permutation": True,
        "mapping_commitment": mapping_commitment,
        "mapping_file_sha256": _sha256(mapping_path),
        "bundle_receipts": bundle_receipts,
        "identity_payload_exported": False,
        "expected_answer_payload_exported": False,
        "custodian_metadata_exported": False,
        "peer_round_output_exported": False,
        "automated_proxy_judge": True,
        "human_calibration_performed": False,
    }


def _validate_round_report_v5(
    report: Mapping[str, Any], round_index: int, receipt: Mapping[str, Any]
) -> list[dict[str, Any]]:
    allowed = {
        "schema_version",
        "round_index",
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "context_id",
        "model_id",
        "started_at",
        "ended_at",
        "bundle_sha256",
        "source_rubric_sha256",
        "judge_input_rubric_sha256",
        "source_protocol_sha256",
        "judge_input_protocol_sha256",
        "terminal_outputs_content_sha256",
        "api_usage_count",
        "tool_usage_count",
        "automated_proxy_judge",
        "human_calibration_performed",
        "identity_payload_observed",
        "expected_answer_payload_observed",
        "custodian_metadata_observed",
        "peer_round_output_observed",
        "scores",
    }
    if (
        set(report) != allowed
        or report.get("schema_version") != "trip-check-p5-judge-round-v5"
        or report.get("round_index") != round_index
        or report.get("bundle_sha256") != receipt["sha256"]
        or report.get("source_rubric_sha256") != receipt["source_rubric_sha256"]
        or report.get("judge_input_rubric_sha256")
        != receipt["judge_input_rubric_sha256"]
        or report.get("source_protocol_sha256")
        != receipt["source_protocol_sha256"]
        or report.get("judge_input_protocol_sha256")
        != receipt["judge_input_protocol_sha256"]
        or report.get("terminal_outputs_content_sha256")
        != receipt["terminal_outputs_content_sha256"]
        or report.get("api_usage_count") != 0
        or report.get("tool_usage_count") != 0
        or report.get("automated_proxy_judge") is not True
        or report.get("human_calibration_performed") is not False
        or any(
            report.get(field) is not False
            for field in (
                "identity_payload_observed",
                "expected_answer_payload_observed",
                "custodian_metadata_observed",
                "peer_round_output_observed",
            )
        )
    ):
        raise P5JudgeErrorV5("JUDGE_ROUND_CONTRACT_INVALID")
    for key in (
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "context_id",
        "model_id",
        "started_at",
        "ended_at",
    ):
        if not isinstance(report.get(key), str) or not report[key]:
            raise P5JudgeErrorV5("JUDGE_ROUND_PROVENANCE_INVALID")
    scores = report.get("scores")
    if not isinstance(scores, list) or len(scores) != BLIND_TERMINAL_COUNT_V5:
        raise P5JudgeErrorV5("JUDGE_ROUND_SCORE_COUNT_INVALID")
    allowed_score = {
        "anonymous_item_id",
        "slot_id",
        *DIMENSIONS_V5,
        "unsupported_claim_candidate_ids",
        "derived_verdict",
    }
    keys: list[tuple[object, object]] = []
    for score in scores:
        if not isinstance(score, dict) or set(score) != allowed_score:
            raise P5JudgeErrorV5("JUDGE_SCORE_SCHEMA_INVALID")
        dimensions = [score.get(dimension) for dimension in DIMENSIONS_V5]
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 4
            for value in dimensions
        ):
            raise P5JudgeErrorV5("JUDGE_SCORE_RANGE_INVALID")
        claims = score.get("unsupported_claim_candidate_ids")
        if (
            not isinstance(claims, list)
            or any(not isinstance(value, str) or not value for value in claims)
            or len(claims) != len(set(claims))
        ):
            raise P5JudgeErrorV5("JUDGE_CLAIM_CANDIDATES_INVALID")
        if (
            not isinstance(score.get("anonymous_item_id"), str)
            or not score["anonymous_item_id"]
            or score.get("slot_id") not in {"slot_1", "slot_2", "slot_3"}
        ):
            raise P5JudgeErrorV5("JUDGE_SCORE_ID_INVALID")
        verdict = "PASS" if min(dimensions) >= 2 and not claims else "NEEDS_REVISION"
        if score.get("derived_verdict") != verdict:
            raise P5JudgeErrorV5("JUDGE_DERIVED_VERDICT_INVALID")
        keys.append((score["anonymous_item_id"], score["slot_id"]))
    if len(keys) != len(set(keys)):
        raise P5JudgeErrorV5("JUDGE_SCORE_KEY_DUPLICATE")
    return scores


def aggregate_judge_rounds_v5(
    *,
    repo_root: Path,
    mapping_path: Path,
    mapping_sha256: str,
    round_paths: Sequence[Path],
    minimum_agreement: float = JUDGE_AGREEMENT_THRESHOLD_V5,
) -> dict[str, Any]:
    """Aggregate exactly three independently-provenanced Judge rounds."""

    if minimum_agreement != JUDGE_AGREEMENT_THRESHOLD_V5:
        raise P5JudgeErrorV5("JUDGE_AGREEMENT_THRESHOLD_IMMUTABLE")
    root = repo_root.resolve()
    if (
        not mapping_path.is_absolute()
        or _inside(mapping_path, root)
        or _contains_link(mapping_path.absolute())
        or _sha256(mapping_path) != mapping_sha256
        or len(round_paths) != ROUND_COUNT_V5
    ):
        raise P5JudgeErrorV5("JUDGE_MAPPING_OR_ROUND_COUNT_INVALID")
    mapping = _load_json(mapping_path, "JUDGE_MAPPING_INVALID")
    expected_mapping_fields = {
        "schema_version",
        "subject_commit",
        "dataset_manifest_hash",
        "run_group_manifest_hash",
        "artifact_index_hash",
        "terminal_outputs_content_sha256",
        "mapping_commitment",
        "bundle_receipts",
        "rows",
    }
    receipts = mapping.get("bundle_receipts")
    rows = mapping.get("rows")
    if (
        set(mapping) != expected_mapping_fields
        or mapping.get("schema_version") != "trip-check-p5-judge-mapping-v5"
        or any(
            not _is_sha256(mapping.get(field))
            for field in (
                "dataset_manifest_hash",
                "run_group_manifest_hash",
                "artifact_index_hash",
                "terminal_outputs_content_sha256",
                "mapping_commitment",
            )
        )
        or not isinstance(receipts, list)
        or len(receipts) != ROUND_COUNT_V5
        or not isinstance(rows, list)
        or len(rows) != BLIND_TERMINAL_COUNT_V5 * ROUND_COUNT_V5
        or digest(rows) != mapping.get("mapping_commitment")
    ):
        raise P5JudgeErrorV5("JUDGE_MAPPING_CONTRACT_INVALID")
    expected_receipt_fields = {
        "round_index",
        "path",
        "sha256",
        "source_rubric_sha256",
        "judge_input_rubric_sha256",
        "source_protocol_sha256",
        "judge_input_protocol_sha256",
        "terminal_outputs_content_sha256",
        "item_count",
    }
    for round_index, receipt in enumerate(receipts, 1):
        if (
            not isinstance(receipt, Mapping)
            or set(receipt) != expected_receipt_fields
            or receipt.get("round_index") != round_index
            or receipt.get("item_count") != BLIND_TERMINAL_COUNT_V5
            or receipt.get("terminal_outputs_content_sha256")
            != mapping["terminal_outputs_content_sha256"]
            or any(
                not _is_sha256(receipt.get(field))
                for field in (
                    "sha256",
                    "source_rubric_sha256",
                    "judge_input_rubric_sha256",
                    "source_protocol_sha256",
                    "judge_input_protocol_sha256",
                )
            )
        ):
            raise P5JudgeErrorV5("JUDGE_BUNDLE_RECEIPT_INVALID")

    expected_row_fields = {
        "round_index",
        "anonymous_item_id",
        "slot_id",
        "case_id",
        "variant_id",
        "runtime_generator_model",
        "claim_ids",
    }
    if any(
        not isinstance(row, Mapping)
        or set(row) != expected_row_fields
        or row.get("round_index") not in {1, 2, 3}
        or not isinstance(row.get("anonymous_item_id"), str)
        or row.get("slot_id") not in {"slot_1", "slot_2", "slot_3"}
        or not isinstance(row.get("case_id"), str)
        or row.get("variant_id") not in VARIANT_IDS_V5
        or not isinstance(row.get("runtime_generator_model"), str)
        or not isinstance(row.get("claim_ids"), list)
        for row in rows
    ):
        raise P5JudgeErrorV5("JUDGE_MAPPING_ROW_INVALID")
    mapping_by_key = {
        (row["round_index"], row["anonymous_item_id"], row["slot_id"]): row
        for row in rows
    }
    if len(mapping_by_key) != len(rows):
        raise P5JudgeErrorV5("JUDGE_MAPPING_KEY_SET_INVALID")
    anonymous_ids = {row["anonymous_item_id"] for row in rows}
    if len(anonymous_ids) != BLIND_CASE_COUNT_V5:
        raise P5JudgeErrorV5("JUDGE_ANONYMOUS_MAPPING_NOT_CLOSED")
    for round_index in range(1, ROUND_COUNT_V5 + 1):
        round_rows = [row for row in rows if row["round_index"] == round_index]
        expected_keys = {
            (anonymous_id, slot_id)
            for anonymous_id in anonymous_ids
            for slot_id in ("slot_1", "slot_2", "slot_3")
        }
        if (
            len(round_rows) != BLIND_TERMINAL_COUNT_V5
            or {(row["anonymous_item_id"], row["slot_id"]) for row in round_rows}
            != expected_keys
        ):
            raise P5JudgeErrorV5("JUDGE_MAPPING_ROUND_COVERAGE_INVALID")

    reports: list[dict[str, Any]] = []
    identity_fields = ("evaluator_id", "agent_task_id", "agent_id", "context_id")
    identities = {field: set() for field in identity_fields}
    for path in round_paths:
        if not path.is_absolute() or _inside(path, root) or _contains_link(path.absolute()):
            raise P5JudgeErrorV5("JUDGE_ROUND_CUSTODY_INVALID")
        report = _load_json(path, "JUDGE_ROUND_INVALID")
        round_index = report.get("round_index")
        if not isinstance(round_index, int) or round_index not in {1, 2, 3}:
            raise P5JudgeErrorV5("JUDGE_ROUND_INDEX_INVALID")
        report["scores"] = _validate_round_report_v5(
            report, round_index, receipts[round_index - 1]
        )
        for field in identity_fields:
            identities[field].add(report[field])
        reports.append(report)
    if len({report["round_index"] for report in reports}) != 3 or any(
        len(values) != 3 for values in identities.values()
    ):
        raise P5JudgeErrorV5("JUDGE_ROUND_INDEPENDENCE_INVALID")

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        for score in report["scores"]:
            row = mapping_by_key.get(
                (report["round_index"], score["anonymous_item_id"], score["slot_id"])
            )
            if row is None:
                raise P5JudgeErrorV5("JUDGE_SCORE_MAPPING_MISSING")
            if not set(score["unsupported_claim_candidate_ids"]).issubset(
                row["claim_ids"]
            ):
                raise P5JudgeErrorV5("JUDGE_CLAIM_ID_NOT_IN_CANDIDATE")
            if report["model_id"] == row["runtime_generator_model"]:
                raise P5JudgeErrorV5("JUDGE_RUNTIME_SELF_REVIEW")
            grouped[(score["anonymous_item_id"], row["variant_id"])].append(score)
    if len(grouped) != BLIND_TERMINAL_COUNT_V5 or any(
        len(values) != 3 for values in grouped.values()
    ):
        raise P5JudgeErrorV5("JUDGE_VARIANT_COVERAGE_INVALID")

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
            for dimension in DIMENSIONS_V5
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
                    for dimension in DIMENSIONS_V5
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
    verdict_agreement_rate = unanimous_count / total
    per_dimension_agreement_rate = {
        dimension: dimension_agreement_counts[dimension] / total
        for dimension in DIMENSIONS_V5
    }
    unsupported_count = sum(
        item["unsupported_claim_candidate_count"]
        for values in variant_values.values()
        for item in values
    )
    agreement_pass = verdict_agreement_rate >= minimum_agreement and all(
        value >= minimum_agreement for value in per_dimension_agreement_rate.values()
    )
    passed = agreement_pass and unsupported_count == 0
    panel = {
        "schema_version": "trip-check-p5-judge-panel-v5",
        "status": "PASS" if passed else "BLOCKED",
        "evidence_class": "automated_proxy_judge",
        "automated_proxy_judge": True,
        "human_calibration_performed": False,
        "round_count": ROUND_COUNT_V5,
        "candidate_count": BLIND_TERMINAL_COUNT_V5,
        "agreement_threshold": minimum_agreement,
        "verdict_agreement_rate": verdict_agreement_rate,
        "per_dimension_agreement_rate": per_dimension_agreement_rate,
        "variant_metrics": {
            variant_id: {
                "candidate_count": len(values),
                "majority_pass_count": sum(item["majority_pass"] for item in values),
                "majority_pass_rate": sum(item["majority_pass"] for item in values)
                / len(values),
                "unanimous_rate": sum(item["unanimous"] for item in values)
                / len(values),
                "per_dimension_agreement_rate": {
                    dimension: sum(
                        item["dimension_agreement"][dimension] for item in values
                    )
                    / len(values)
                    for dimension in DIMENSIONS_V5
                },
                **{
                    f"median_{dimension}": median(
                        item[dimension] for item in values
                    )
                    for dimension in DIMENSIONS_V5
                },
            }
            for variant_id, values in sorted(variant_values.items())
        },
        "provenance": [
            {
                key: report[key]
                for key in (
                    "round_index",
                    "evaluator_id",
                    "agent_task_id",
                    "agent_id",
                    "context_id",
                    "model_id",
                    "started_at",
                    "ended_at",
                    "bundle_sha256",
                    "source_rubric_sha256",
                    "judge_input_rubric_sha256",
                    "source_protocol_sha256",
                    "judge_input_protocol_sha256",
                    "terminal_outputs_content_sha256",
                )
            }
            for report in sorted(reports, key=lambda item: item["round_index"])
        ],
        "mapping_sha256": mapping_sha256,
        "subject_commit": mapping["subject_commit"],
        "dataset_manifest_hash": mapping["dataset_manifest_hash"],
        "run_group_manifest_hash": mapping["run_group_manifest_hash"],
        "artifact_index_hash": mapping["artifact_index_hash"],
        "terminal_outputs_content_sha256": mapping[
            "terminal_outputs_content_sha256"
        ],
        "deterministic_scorer_priority": True,
        "judge_may_override_deterministic_failure": False,
        "unsupported_claim_candidate_count": unsupported_count,
    }
    panel["report_hash"] = digest(panel)
    return panel


__all__ = [
    "BLIND_CASE_COUNT_V5",
    "BLIND_TERMINAL_COUNT_V5",
    "JUDGE_AGREEMENT_THRESHOLD_V5",
    "P5JudgeErrorV5",
    "aggregate_judge_rounds_v5",
    "export_judge_bundles_v5",
    "judge_rubric_projection_hash_v5",
    "judge_protocol_projection_hash_v5",
]
