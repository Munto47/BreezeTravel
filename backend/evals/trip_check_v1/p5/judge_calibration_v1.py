"""Non-blind synthetic preflight for the P5 automated Judge protocol."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.judge_v5 import (
    DIMENSIONS_V5,
    JUDGE_AGREEMENT_THRESHOLD_V5,
    ROUND_COUNT_V5,
    _judge_protocol_projection,
    _judge_rubric_projection,
)


CALIBRATION_ITEM_COUNT_V1 = 10


class P5JudgeCalibrationErrorV1(RuntimeError):
    """Stable fail-closed calibration error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5JudgeCalibrationErrorV1(reason) from exc
    if not isinstance(value, dict):
        raise P5JudgeCalibrationErrorV1(reason)
    return value


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_ARTIFACT_UNREADABLE") from exc


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


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


def _external_distinct_dirs(repo_root: Path, paths: Sequence[Path]) -> list[Path]:
    if len(paths) != ROUND_COUNT_V5 + 1 or any(not path.is_absolute() for path in paths):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_CUSTODY_PATH_INVALID")
    resolved = [path.absolute() for path in paths]
    if any(_inside(path, repo_root) or _contains_link(path) for path in resolved):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_CUSTODY_PATH_INVALID")
    if len({str(path.resolve()) for path in resolved}) != len(resolved):
        raise P5JudgeCalibrationErrorV1(
            "JUDGE_CALIBRATION_CUSTODY_PATH_NOT_DISTINCT"
        )
    for left in resolved:
        for right in resolved:
            if left == right:
                continue
            try:
                right.resolve().relative_to(left.resolve())
            except ValueError:
                continue
            raise P5JudgeCalibrationErrorV1(
                "JUDGE_CALIBRATION_CUSTODY_PATH_NOT_DISTINCT"
            )
    return resolved


def _validate_calibration_set(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    if (
        set(value)
        != {
            "schema_version",
            "evidence_class",
            "human_evidence",
            "human_calibration_performed",
            "items",
        }
        or value.get("schema_version")
        != "trip-check-p5-judge-calibration-set-v1"
        or value.get("evidence_class")
        != "nonblind_synthetic_anchor_calibration"
        or value.get("human_evidence") is not False
        or value.get("human_calibration_performed") is not False
        or not isinstance(value.get("items"), list)
        or len(value["items"]) != CALIBRATION_ITEM_COUNT_V1
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_SET_INVALID")
    items: list[dict[str, Any]] = []
    ids: set[str] = set()
    for raw in value["items"]:
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "calibration_item_id",
                "public_input",
                "candidate_expression",
                "evidence_summary",
                "expected",
            }
            or not isinstance(raw.get("calibration_item_id"), str)
            or not raw["calibration_item_id"]
            or raw["calibration_item_id"] in ids
            or not isinstance(raw.get("public_input"), dict)
            or not isinstance(raw.get("candidate_expression"), dict)
            or not isinstance(raw.get("evidence_summary"), dict)
            or not isinstance(raw.get("expected"), dict)
            or set(raw["expected"]) != {*DIMENSIONS_V5, "derived_verdict"}
        ):
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_ITEM_INVALID")
        dimensions = [raw["expected"].get(dimension) for dimension in DIMENSIONS_V5]
        expected_verdict = (
            "PASS" if min(dimensions) >= 2 else "NEEDS_REVISION"
        ) if all(
            isinstance(score, int)
            and not isinstance(score, bool)
            and 0 <= score <= 4
            for score in dimensions
        ) else None
        if raw["expected"].get("derived_verdict") != expected_verdict:
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_EXPECTED_INVALID")
        ids.add(raw["calibration_item_id"])
        items.append(raw)
    serialized = json.dumps(items, ensure_ascii=False, sort_keys=True).lower()
    if any(
        fragment in serialized
        for fragment in (
            '"case_id"',
            '"variant_id"',
            '"blind_label"',
            '"label_payload"',
            "p5.blind.",
        )
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_BOUNDARY_INVALID")
    return items


def export_judge_calibration_bundles_v1(
    *,
    repo_root: Path,
    round_output_dirs: Sequence[Path],
    custody_output_dir: Path,
    rubric_path: Path,
    protocol_path: Path,
    calibration_set_path: Path,
) -> dict[str, Any]:
    """Export three identical input-only synthetic calibration bundles."""

    root = repo_root.resolve()
    paths = _external_distinct_dirs(
        root, [*round_output_dirs, custody_output_dir]
    )
    round_dirs, custody_dir = paths[:3], paths[3]
    source_rubric = _load_json(rubric_path, "JUDGE_CALIBRATION_RUBRIC_INVALID")
    source_protocol = _load_json(protocol_path, "JUDGE_CALIBRATION_PROTOCOL_INVALID")
    rubric = _judge_rubric_projection(source_rubric)
    protocol = _judge_protocol_projection(source_rubric, source_protocol)
    calibration_set = _load_json(
        calibration_set_path, "JUDGE_CALIBRATION_SET_INVALID"
    )
    items = _validate_calibration_set(calibration_set)
    public_items = [
        {
            key: item[key]
            for key in (
                "calibration_item_id",
                "public_input",
                "candidate_expression",
                "evidence_summary",
            )
        }
        for item in items
    ]
    bindings = {
        "source_rubric_sha256": _sha256(rubric_path),
        "judge_input_rubric_sha256": digest(rubric),
        "source_protocol_sha256": _sha256(protocol_path),
        "judge_input_protocol_sha256": digest(protocol),
        "calibration_set_sha256": _sha256(calibration_set_path),
        "calibration_input_content_sha256": digest(public_items),
    }
    receipts: list[dict[str, Any]] = []
    for round_index, round_dir in enumerate(round_dirs, 1):
        round_dir.mkdir(parents=True, exist_ok=True)
        path = round_dir / f"judge_calibration_round_{round_index}.v1.json"
        if path.exists():
            raise P5JudgeCalibrationErrorV1(
                "JUDGE_CALIBRATION_BUNDLE_ALREADY_EXISTS"
            )
        bundle = {
            "schema_version": "trip-check-p5-judge-calibration-bundle-v1",
            "round_index": round_index,
            "evidence_class": "automated_proxy_judge_calibration_input",
            "automated_proxy_judge": True,
            "human_evidence": False,
            "human_calibration_performed": False,
            "run_binding": bindings,
            "rubric": rubric,
            "protocol": protocol,
            "items": public_items,
        }
        path.write_bytes(canonical_bytes(bundle) + b"\n")
        receipts.append(
            {
                "round_index": round_index,
                "path": path.name,
                "sha256": _sha256(path),
                **bindings,
                "item_count": CALIBRATION_ITEM_COUNT_V1,
            }
        )
    custody_dir.mkdir(parents=True, exist_ok=True)
    key_path = custody_dir / "judge_calibration_key.v1.json"
    if key_path.exists():
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_KEY_ALREADY_EXISTS")
    key = {
        "schema_version": "trip-check-p5-judge-calibration-key-v1",
        **bindings,
        "bundle_receipts": receipts,
        "expected": [
            {
                "calibration_item_id": item["calibration_item_id"],
                **item["expected"],
            }
            for item in items
        ],
    }
    key_path.write_bytes(canonical_bytes(key) + b"\n")
    return {
        "schema_version": "trip-check-p5-judge-calibration-export-receipt-v1",
        "status": "EXPORTED",
        "round_count": ROUND_COUNT_V5,
        "item_count": CALIBRATION_ITEM_COUNT_V1,
        "key_file_sha256": _sha256(key_path),
        "bundle_receipts": receipts,
        **bindings,
        "expected_scores_exported_to_judges": False,
        "human_evidence": False,
        "human_calibration_performed": False,
    }


def _validate_round(
    value: Mapping[str, Any], receipt: Mapping[str, Any]
) -> list[dict[str, Any]]:
    binding_fields = (
        "source_rubric_sha256",
        "judge_input_rubric_sha256",
        "source_protocol_sha256",
        "judge_input_protocol_sha256",
        "calibration_set_sha256",
        "calibration_input_content_sha256",
    )
    required = {
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
        *binding_fields,
        "api_usage_count",
        "tool_usage_count",
        "automated_proxy_judge",
        "human_calibration_performed",
        "expected_scores_observed",
        "peer_round_output_observed",
        "scores",
    }
    if (
        set(value) != required
        or value.get("schema_version")
        != "trip-check-p5-judge-calibration-round-v1"
        or value.get("round_index") != receipt.get("round_index")
        or value.get("bundle_sha256") != receipt.get("sha256")
        or any(value.get(field) != receipt.get(field) for field in binding_fields)
        or value.get("api_usage_count") != 0
        or value.get("tool_usage_count") != 0
        or value.get("automated_proxy_judge") is not True
        or value.get("human_calibration_performed") is not False
        or value.get("expected_scores_observed") is not False
        or value.get("peer_round_output_observed") is not False
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_ROUND_INVALID")
    for field in (
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "context_id",
        "model_id",
        "started_at",
        "ended_at",
    ):
        if not isinstance(value.get(field), str) or not value[field]:
            raise P5JudgeCalibrationErrorV1(
                "JUDGE_CALIBRATION_PROVENANCE_INVALID"
            )
    scores = value.get("scores")
    if not isinstance(scores, list) or len(scores) != CALIBRATION_ITEM_COUNT_V1:
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_SCORE_COUNT_INVALID")
    ids: set[str] = set()
    for score in scores:
        if (
            not isinstance(score, dict)
            or set(score) != {"calibration_item_id", *DIMENSIONS_V5, "derived_verdict"}
            or not isinstance(score.get("calibration_item_id"), str)
            or score["calibration_item_id"] in ids
        ):
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_SCORE_INVALID")
        dimensions = [score.get(dimension) for dimension in DIMENSIONS_V5]
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or not 0 <= value <= 4
            for value in dimensions
        ):
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_SCORE_INVALID")
        verdict = "PASS" if min(dimensions) >= 2 else "NEEDS_REVISION"
        if score.get("derived_verdict") != verdict:
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_VERDICT_INVALID")
        ids.add(score["calibration_item_id"])
    return scores


def aggregate_judge_calibration_rounds_v1(
    *,
    repo_root: Path,
    key_path: Path,
    key_sha256: str,
    round_paths: Sequence[Path],
) -> dict[str, Any]:
    """Validate calibration against anchors and three-round agreement."""

    root = repo_root.resolve()
    if (
        not key_path.is_absolute()
        or _inside(key_path, root)
        or _contains_link(key_path)
        or _sha256(key_path) != key_sha256
        or len(round_paths) != ROUND_COUNT_V5
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_INPUT_INVALID")
    key = _load_json(key_path, "JUDGE_CALIBRATION_KEY_INVALID")
    binding_fields = (
        "source_rubric_sha256",
        "judge_input_rubric_sha256",
        "source_protocol_sha256",
        "judge_input_protocol_sha256",
        "calibration_set_sha256",
        "calibration_input_content_sha256",
    )
    if (
        key.get("schema_version") != "trip-check-p5-judge-calibration-key-v1"
        or any(not _is_sha256(key.get(field)) for field in binding_fields)
        or not isinstance(key.get("bundle_receipts"), list)
        or len(key["bundle_receipts"]) != ROUND_COUNT_V5
        or not isinstance(key.get("expected"), list)
        or len(key["expected"]) != CALIBRATION_ITEM_COUNT_V1
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_KEY_INVALID")
    expected_receipt_fields = {
        "round_index",
        "path",
        "sha256",
        *binding_fields,
        "item_count",
    }
    if any(
        not isinstance(receipt, Mapping)
        or set(receipt) != expected_receipt_fields
        or receipt.get("round_index") != round_index
        or receipt.get("item_count") != CALIBRATION_ITEM_COUNT_V1
        or not isinstance(receipt.get("path"), str)
        or any(
            not _is_sha256(receipt.get(field))
            for field in ("sha256", *binding_fields)
        )
        or any(receipt.get(field) != key.get(field) for field in binding_fields)
        for round_index, receipt in enumerate(key["bundle_receipts"], 1)
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_KEY_INVALID")
    expected = {
        item["calibration_item_id"]: item
        for item in key["expected"]
        if isinstance(item, dict) and isinstance(item.get("calibration_item_id"), str)
    }
    if len(expected) != CALIBRATION_ITEM_COUNT_V1 or any(
        set(item) != {"calibration_item_id", *DIMENSIONS_V5, "derived_verdict"}
        or any(
            not isinstance(item.get(dimension), int)
            or isinstance(item.get(dimension), bool)
            or not 0 <= item[dimension] <= 4
            for dimension in DIMENSIONS_V5
        )
        or item.get("derived_verdict")
        != (
            "PASS"
            if min(item[dimension] for dimension in DIMENSIONS_V5) >= 2
            else "NEEDS_REVISION"
        )
        for item in expected.values()
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_KEY_INVALID")
    reports: list[dict[str, Any]] = []
    round_result_paths: dict[int, Path] = {}
    identity_fields = ("evaluator_id", "agent_task_id", "agent_id", "context_id")
    identities = {field: set() for field in identity_fields}
    for path in round_paths:
        if not path.is_absolute() or _inside(path, root) or _contains_link(path):
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_ROUND_PATH_INVALID")
        report = _load_json(path, "JUDGE_CALIBRATION_ROUND_INVALID")
        round_index = report.get("round_index")
        if not isinstance(round_index, int) or round_index not in {1, 2, 3}:
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_ROUND_INVALID")
        receipt = key["bundle_receipts"][round_index - 1]
        report["scores"] = _validate_round(report, receipt)
        round_result_paths[round_index] = path
        if set(score["calibration_item_id"] for score in report["scores"]) != set(
            expected
        ):
            raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_COVERAGE_INVALID")
        for field in identity_fields:
            identities[field].add(report[field])
        reports.append(report)
    if len({report["round_index"] for report in reports}) != 3 or any(
        len(values) != 3 for values in identities.values()
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_INDEPENDENCE_INVALID")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        for score in report["scores"]:
            grouped[score["calibration_item_id"]].append(score)
    agreement_counts = Counter()
    unanimous_count = 0
    expected_verdict_match = 0
    expected_within_one = Counter()
    exact_match = Counter()
    for item_id, values in grouped.items():
        unanimous_count += int(
            len({value["derived_verdict"] for value in values}) == 1
        )
        expected_row = expected[item_id]
        for value in values:
            expected_verdict_match += int(
                value["derived_verdict"] == expected_row["derived_verdict"]
            )
            for dimension in DIMENSIONS_V5:
                expected_within_one[dimension] += int(
                    abs(value[dimension] - expected_row[dimension]) <= 1
                )
                exact_match[dimension] += int(
                    value[dimension] == expected_row[dimension]
                )
        for dimension in DIMENSIONS_V5:
            agreement_counts[dimension] += int(
                max(value[dimension] for value in values)
                - min(value[dimension] for value in values)
                <= 1
            )
    item_total = len(grouped)
    score_total = item_total * ROUND_COUNT_V5
    verdict_agreement_rate = unanimous_count / item_total
    per_dimension_agreement_rate = {
        dimension: agreement_counts[dimension] / item_total
        for dimension in DIMENSIONS_V5
    }
    expected_verdict_match_rate = expected_verdict_match / score_total
    expected_dimension_within_one_rate = {
        dimension: expected_within_one[dimension] / score_total
        for dimension in DIMENSIONS_V5
    }
    exact_score_match_rate = {
        dimension: exact_match[dimension] / score_total
        for dimension in DIMENSIONS_V5
    }
    passed = (
        verdict_agreement_rate >= JUDGE_AGREEMENT_THRESHOLD_V5
        and all(
            value >= JUDGE_AGREEMENT_THRESHOLD_V5
            for value in per_dimension_agreement_rate.values()
        )
        and expected_verdict_match_rate >= JUDGE_AGREEMENT_THRESHOLD_V5
        and all(
            value >= JUDGE_AGREEMENT_THRESHOLD_V5
            for value in expected_dimension_within_one_rate.values()
        )
    )
    panel = {
        "schema_version": "trip-check-p5-judge-calibration-panel-v1",
        "status": "PASS" if passed else "BLOCKED",
        "evidence_class": "automated_proxy_judge_calibration",
        "automated_proxy_judge": True,
        "human_evidence": False,
        "human_calibration_performed": False,
        "round_count": ROUND_COUNT_V5,
        "item_count": CALIBRATION_ITEM_COUNT_V1,
        "agreement_threshold": JUDGE_AGREEMENT_THRESHOLD_V5,
        "verdict_agreement_rate": verdict_agreement_rate,
        "per_dimension_agreement_rate": per_dimension_agreement_rate,
        "expected_verdict_match_rate": expected_verdict_match_rate,
        "expected_dimension_within_one_rate": expected_dimension_within_one_rate,
        "exact_score_match_rate": exact_score_match_rate,
        "key_sha256": key_sha256,
        "key_path": str(key_path.resolve()),
        **{field: key[field] for field in binding_fields},
        "provenance": [
            {
                field: report[field]
                for field in (
                    "round_index",
                    "evaluator_id",
                    "agent_task_id",
                    "agent_id",
                    "context_id",
                    "model_id",
                    "started_at",
                    "ended_at",
                    "bundle_sha256",
                    *binding_fields,
                )
            } | {
                "round_result_path": str(
                    round_result_paths[report["round_index"]].resolve()
                ),
                "round_result_sha256": _sha256(
                    round_result_paths[report["round_index"]]
                ),
            }
            for report in sorted(reports, key=lambda item: item["round_index"])
        ],
    }
    panel["report_hash"] = digest(panel)
    return panel


def validate_judge_calibration_panel_v1(
    *,
    repo_root: Path,
    panel_path: Path,
    rubric_path: Path,
    protocol_path: Path,
) -> dict[str, Any]:
    """Reaggregate and bind a PASS calibration panel to tracked contracts."""

    root = repo_root.resolve()
    if (
        not panel_path.is_absolute()
        or _inside(panel_path, root)
        or _contains_link(panel_path)
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_PANEL_PATH_INVALID")
    panel = _load_json(panel_path, "JUDGE_CALIBRATION_PANEL_INVALID")
    report_hash = panel.get("report_hash")
    unsigned = {key: value for key, value in panel.items() if key != "report_hash"}
    provenance = panel.get("provenance")
    key_path = Path(str(panel.get("key_path", "")))
    if (
        not _is_sha256(report_hash)
        or digest(unsigned) != report_hash
        or panel.get("schema_version")
        != "trip-check-p5-judge-calibration-panel-v1"
        or panel.get("status") != "PASS"
        or panel.get("agreement_threshold") != JUDGE_AGREEMENT_THRESHOLD_V5
        or panel.get("round_count") != ROUND_COUNT_V5
        or panel.get("item_count") != CALIBRATION_ITEM_COUNT_V1
        or panel.get("automated_proxy_judge") is not True
        or panel.get("human_evidence") is not False
        or panel.get("human_calibration_performed") is not False
        or not key_path.is_absolute()
        or not isinstance(provenance, list)
        or len(provenance) != ROUND_COUNT_V5
        or any(not isinstance(item, Mapping) for item in provenance)
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_PANEL_INVALID")
    round_paths = [
        Path(str(item.get("round_result_path", "")))
        for item in sorted(provenance, key=lambda item: item.get("round_index", 0))
    ]
    if len(round_paths) != ROUND_COUNT_V5 or any(
        not path.is_absolute() for path in round_paths
    ):
        raise P5JudgeCalibrationErrorV1("JUDGE_CALIBRATION_PANEL_INVALID")
    recalculated = aggregate_judge_calibration_rounds_v1(
        repo_root=root,
        key_path=key_path,
        key_sha256=str(panel.get("key_sha256")),
        round_paths=round_paths,
    )
    source_rubric = _load_json(rubric_path, "JUDGE_CALIBRATION_RUBRIC_INVALID")
    source_protocol = _load_json(
        protocol_path, "JUDGE_CALIBRATION_PROTOCOL_INVALID"
    )
    if (
        recalculated != panel
        or panel.get("source_rubric_sha256") != _sha256(rubric_path)
        or panel.get("judge_input_rubric_sha256")
        != digest(_judge_rubric_projection(source_rubric))
        or panel.get("source_protocol_sha256") != _sha256(protocol_path)
        or panel.get("judge_input_protocol_sha256")
        != digest(_judge_protocol_projection(source_rubric, source_protocol))
    ):
        raise P5JudgeCalibrationErrorV1(
            "JUDGE_CALIBRATION_PANEL_BINDING_INVALID"
        )
    return panel


__all__ = [
    "CALIBRATION_ITEM_COUNT_V1",
    "P5JudgeCalibrationErrorV1",
    "aggregate_judge_calibration_rounds_v1",
    "export_judge_calibration_bundles_v1",
    "validate_judge_calibration_panel_v1",
]
