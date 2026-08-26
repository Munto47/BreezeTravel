"""Sealed non-blind holdout calibration for P5 Judge protocols v2 and v3."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.formal_receipts_v5 import (
    RepoBindingV5,
    read_repo_binding_v5,
)
from evals.trip_check_v1.p5.judge_v5 import (
    DIMENSIONS_V5,
    ROUND_COUNT_V5,
    _judge_protocol_projection,
    _judge_rubric_projection,
)


HOLDOUT_ITEM_COUNT_V2 = 30
PANEL_AGREEMENT_THRESHOLD_V2 = 0.9
SUPPORTED_HOLDOUT_VERSIONS = ("v2", "v3")
HASH_BINDING_FIELDS_V2 = (
    "source_rubric_sha256",
    "judge_input_rubric_sha256",
    "source_protocol_sha256",
    "judge_input_protocol_sha256",
    "holdout_commitment_sha256",
    "holdout_package_sha256",
    "holdout_public_content_sha256",
    "holdout_expected_content_sha256",
)
REPO_BINDING_FIELDS_V2 = (
    "subject_commit",
    "upstream_ref",
    "upstream_commit",
    "dirty_tree",
)


class P5JudgeHoldoutErrorV2(RuntimeError):
    """Stable fail-closed Judge holdout error."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _schema_version(value: object, stem: str) -> str | None:
    for version in SUPPORTED_HOLDOUT_VERSIONS:
        if value == f"trip-check-p5-judge-holdout-{stem}-{version}":
            return version
    return None


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5JudgeHoldoutErrorV2(reason) from exc
    if not isinstance(value, dict):
        raise P5JudgeHoldoutErrorV2(reason)
    return value


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ARTIFACT_UNREADABLE") from exc


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


def _external_distinct_paths(repo_root: Path, paths: Sequence[Path]) -> list[Path]:
    if any(not path.is_absolute() for path in paths):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_CUSTODY_PATH_INVALID")
    resolved = [path.absolute() for path in paths]
    if any(_inside(path, repo_root) or _contains_link(path) for path in resolved):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_CUSTODY_PATH_INVALID")
    if len({str(path.resolve()) for path in resolved}) != len(resolved):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_CUSTODY_PATH_NOT_DISTINCT")
    return resolved


def require_external_judge_holdout_artifact_path_v2(
    *, repo_root: Path, path: Path
) -> Path:
    """Return an absolute repository-external holdout artifact path."""

    absolute = path.absolute()
    if (
        not path.is_absolute()
        or _inside(absolute, repo_root)
        or _contains_link(absolute)
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ARTIFACT_PATH_INVALID")
    return absolute


def _require_repo_binding(binding: RepoBindingV5) -> None:
    if (
        len(binding.subject_commit) != 40
        or any(
            character not in "0123456789abcdef"
            for character in binding.subject_commit
        )
        or binding.upstream_commit != binding.subject_commit
        or not binding.upstream_ref
        or binding.dirty_tree
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_SUBJECT_NOT_CLEAN_PUSHED")


def _validate_package(
    package: Mapping[str, Any], commitment: Mapping[str, Any], version: str | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    contract_version = version or _schema_version(
        commitment.get("schema_version"), "commitment"
    )
    if (
        contract_version not in SUPPORTED_HOLDOUT_VERSIONS
        or
        set(package)
        != {
            "schema_version",
            "evidence_class",
            "source_lane",
            "item_count",
            "blind_source_used",
            "human_evidence",
            "human_calibration_performed",
            "items",
        }
        or package.get("schema_version")
        != f"trip-check-p5-judge-holdout-package-{contract_version}"
        or package.get("evidence_class") != "sealed_nonblind_synthetic_holdout"
        or package.get("source_lane") != "NONBLIND_SYNTHETIC_HOLDOUT"
        or package.get("item_count") != HOLDOUT_ITEM_COUNT_V2
        or package.get("blind_source_used") is not False
        or package.get("human_evidence") is not False
        or package.get("human_calibration_performed") is not False
        or not isinstance(package.get("items"), list)
        or len(package["items"]) != HOLDOUT_ITEM_COUNT_V2
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_PACKAGE_INVALID")
    public: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    ids: set[str] = set()
    actionability_counts: Counter[int] = Counter()
    for raw in package["items"]:
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "holdout_item_id",
                "public_input",
                "candidate_expression",
                "evidence_summary",
                "expected",
            }
            or not isinstance(raw.get("holdout_item_id"), str)
            or not raw["holdout_item_id"]
            or raw["holdout_item_id"] in ids
            or not (
                isinstance(raw.get("public_input"), dict)
                or (
                    isinstance(raw.get("public_input"), str)
                    and bool(raw["public_input"].strip())
                )
            )
            or not isinstance(raw.get("candidate_expression"), dict)
            or not isinstance(raw.get("evidence_summary"), dict)
            or not isinstance(raw.get("expected"), dict)
            or set(raw["expected"]) != {*DIMENSIONS_V5, "derived_verdict"}
        ):
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ITEM_INVALID")
        scores = [raw["expected"].get(dimension) for dimension in DIMENSIONS_V5]
        if any(
            not isinstance(score, int)
            or isinstance(score, bool)
            or not 0 <= score <= 4
            for score in scores
        ):
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_EXPECTED_INVALID")
        verdict = "PASS" if min(scores) >= 2 else "NEEDS_REVISION"
        if raw["expected"].get("derived_verdict") != verdict:
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_EXPECTED_INVALID")
        item_id = raw["holdout_item_id"]
        ids.add(item_id)
        actionability_counts[raw["expected"]["actionability"]] += 1
        public.append(
            {
                "holdout_item_id": item_id,
                "public_input": raw["public_input"],
                "candidate_expression": raw["candidate_expression"],
                "evidence_summary": raw["evidence_summary"],
            }
        )
        expected.append({"holdout_item_id": item_id, **raw["expected"]})
    serialized = json.dumps(public, ensure_ascii=False, sort_keys=True).lower()
    if any(
        fragment in serialized
        for fragment in (
            '"case_id"',
            '"variant_id"',
            '"blind_label"',
            '"label_payload"',
            "p5.blind.",
        )
    ) or any(actionability_counts[score] < 5 for score in range(5)):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_BOUNDARY_INVALID")
    if (
        commitment.get("public_items_content_sha256") != digest(public)
        or commitment.get("expected_items_content_sha256") != digest(expected)
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_COMMITMENT_INVALID")
    return public, expected


def _validate_commitment(
    commitment: Mapping[str, Any], package_path: Path
) -> str:
    version = _schema_version(commitment.get("schema_version"), "commitment")
    if (
        version not in SUPPORTED_HOLDOUT_VERSIONS
        or
        set(commitment)
        != {
            "schema_version",
            "status",
            "item_count",
            "source_lane",
            "blind_source_used",
            "package_sha256",
            "public_items_content_sha256",
            "expected_items_content_sha256",
            "custodian_receipt_sha256",
            "review_receipt_sha256",
        }
        or commitment.get("status") != "SEALED"
        or commitment.get("item_count") != HOLDOUT_ITEM_COUNT_V2
        or commitment.get("source_lane") != "NONBLIND_SYNTHETIC_HOLDOUT"
        or commitment.get("blind_source_used") is not False
        or any(
            not _is_sha256(commitment.get(field))
            for field in (
                "package_sha256",
                "public_items_content_sha256",
                "expected_items_content_sha256",
                "custodian_receipt_sha256",
                "review_receipt_sha256",
            )
        )
        or commitment.get("package_sha256") != _sha256(package_path)
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_COMMITMENT_INVALID")
    return version


def export_judge_holdout_bundles_v2(
    *,
    repo_root: Path,
    round_output_dirs: Sequence[Path],
    custody_output_dir: Path,
    rubric_path: Path,
    protocol_path: Path,
    commitment_path: Path,
    package_path: Path,
    repo_binding: RepoBindingV5 | None = None,
) -> dict[str, Any]:
    """Export three slot-bound, expected-free holdout bundles."""

    root = repo_root.resolve()
    if len(round_output_dirs) != ROUND_COUNT_V5:
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ROUND_COUNT_INVALID")
    paths = _external_distinct_paths(
        root, [*round_output_dirs, custody_output_dir, package_path]
    )
    round_dirs = paths[:ROUND_COUNT_V5]
    custody_dir = paths[ROUND_COUNT_V5]
    package_file = paths[-1]
    commitment = _load_json(commitment_path, "JUDGE_HOLDOUT_COMMITMENT_INVALID")
    version = _validate_commitment(commitment, package_file)
    package = _load_json(package_file, "JUDGE_HOLDOUT_PACKAGE_INVALID")
    public_items, expected = _validate_package(package, commitment, version)
    rubric_source = _load_json(rubric_path, "JUDGE_HOLDOUT_RUBRIC_INVALID")
    protocol_source = _load_json(protocol_path, "JUDGE_HOLDOUT_PROTOCOL_INVALID")
    rubric = _judge_rubric_projection(rubric_source)
    protocol = _judge_protocol_projection(rubric_source, protocol_source)
    if protocol.get("schema_version") != (
        f"trip-check-p5-judge-protocol-projection-{version}"
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_PROTOCOL_INVALID")
    binding = repo_binding or read_repo_binding_v5(root)
    _require_repo_binding(binding)
    bindings = {
        **binding.as_dict(),
        "source_rubric_sha256": _sha256(rubric_path),
        "judge_input_rubric_sha256": digest(rubric),
        "source_protocol_sha256": _sha256(protocol_path),
        "judge_input_protocol_sha256": digest(protocol),
        "holdout_commitment_sha256": _sha256(commitment_path),
        "holdout_package_sha256": _sha256(package_file),
        "holdout_public_content_sha256": digest(public_items),
        "holdout_expected_content_sha256": digest(expected),
    }
    receipts: list[dict[str, Any]] = []
    for round_index, (round_dir, slot) in enumerate(
        zip(round_dirs, protocol["evaluator_slots"], strict=True), 1
    ):
        round_dir.mkdir(parents=True, exist_ok=True)
        output = round_dir / f"judge_holdout_round_{round_index}.{version}.json"
        if output.exists():
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_BUNDLE_ALREADY_EXISTS")
        bundle = {
            "schema_version": f"trip-check-p5-judge-holdout-bundle-{version}",
            "round_index": round_index,
            "evaluator_slot": slot,
            "evidence_class": "automated_proxy_judge_holdout_input",
            "automated_proxy_judge": True,
            "human_evidence": False,
            "human_calibration_performed": False,
            "run_binding": bindings,
            "rubric": rubric,
            "protocol": protocol,
            "items": public_items,
        }
        output.write_bytes(canonical_bytes(bundle) + b"\n")
        receipts.append(
            {
                "round_index": round_index,
                "path": output.name,
                "sha256": _sha256(output),
                **slot,
                **bindings,
                "item_count": HOLDOUT_ITEM_COUNT_V2,
            }
        )
    custody_dir.mkdir(parents=True, exist_ok=True)
    key_path = custody_dir / f"judge_holdout_key.{version}.json"
    if key_path.exists():
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_KEY_ALREADY_EXISTS")
    key = {
        "schema_version": f"trip-check-p5-judge-holdout-key-{version}",
        **bindings,
        "bundle_receipts": receipts,
        "expected": expected,
    }
    key_path.write_bytes(canonical_bytes(key) + b"\n")
    return {
        "schema_version": f"trip-check-p5-judge-holdout-export-receipt-{version}",
        "status": "EXPORTED",
        "round_count": ROUND_COUNT_V5,
        "item_count": HOLDOUT_ITEM_COUNT_V2,
        "key_file_sha256": _sha256(key_path),
        "bundle_receipts": receipts,
        **bindings,
        "expected_scores_exported_to_judges": False,
        "human_evidence": False,
        "human_calibration_performed": False,
    }


def _validate_round(
    value: Mapping[str, Any], receipt: Mapping[str, Any], version: str
) -> list[dict[str, Any]]:
    binding_fields = (*REPO_BINDING_FIELDS_V2, *HASH_BINDING_FIELDS_V2)
    required = {
        "schema_version",
        "round_index",
        "evaluator_profile_id",
        "reasoning_effort",
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
        != f"trip-check-p5-judge-holdout-round-{version}"
        or value.get("round_index") != receipt.get("round_index")
        or value.get("bundle_sha256") != receipt.get("sha256")
        or value.get("evaluator_profile_id")
        != receipt.get("evaluator_profile_id")
        or value.get("model_id") != receipt.get("model_id")
        or value.get("reasoning_effort") != receipt.get("reasoning_effort")
        or any(value.get(field) != receipt.get(field) for field in binding_fields)
        or value.get("api_usage_count") != 0
        or value.get("tool_usage_count") != 0
        or value.get("automated_proxy_judge") is not True
        or value.get("human_calibration_performed") is not False
        or value.get("expected_scores_observed") is not False
        or value.get("peer_round_output_observed") is not False
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ROUND_INVALID")
    identity_fields = (
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "context_id",
        "started_at",
        "ended_at",
    )
    if any(not isinstance(value.get(field), str) or not value[field] for field in identity_fields):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_PROVENANCE_INVALID")
    scores = value.get("scores")
    if not isinstance(scores, list) or len(scores) != HOLDOUT_ITEM_COUNT_V2:
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_SCORE_COUNT_INVALID")
    ids: set[str] = set()
    for score in scores:
        if (
            not isinstance(score, dict)
            or set(score) != {"holdout_item_id", *DIMENSIONS_V5, "derived_verdict"}
            or not isinstance(score.get("holdout_item_id"), str)
            or score["holdout_item_id"] in ids
        ):
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_SCORE_INVALID")
        dimensions = [score.get(dimension) for dimension in DIMENSIONS_V5]
        if any(
            not isinstance(raw, int)
            or isinstance(raw, bool)
            or not 0 <= raw <= 4
            for raw in dimensions
        ):
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_SCORE_INVALID")
        verdict = "PASS" if min(dimensions) >= 2 else "NEEDS_REVISION"
        if score.get("derived_verdict") != verdict:
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_VERDICT_INVALID")
        ids.add(score["holdout_item_id"])
    return scores


def build_judge_holdout_round_report_v2(
    *,
    repo_root: Path,
    bundle_path: Path,
    score_payload_path: Path,
) -> dict[str, Any]:
    """Bind Judge-authored holdout scores to deterministic bundle metadata."""

    root = repo_root.resolve()
    bundle_file, payload_file = (
        require_external_judge_holdout_artifact_path_v2(
            repo_root=root, path=path
        )
        for path in (bundle_path, score_payload_path)
    )
    bundle = _load_json(bundle_file, "JUDGE_HOLDOUT_BUNDLE_INVALID")
    payload = _load_json(payload_file, "JUDGE_HOLDOUT_SCORE_PAYLOAD_INVALID")
    version = _schema_version(bundle.get("schema_version"), "bundle")
    expected_bundle_fields = {
        "schema_version",
        "round_index",
        "evidence_class",
        "automated_proxy_judge",
        "human_evidence",
        "human_calibration_performed",
        "evaluator_slot",
        "run_binding",
        "rubric",
        "protocol",
        "items",
    }
    evaluator_slot = bundle.get("evaluator_slot")
    run_binding = bundle.get("run_binding")
    items = bundle.get("items")
    if (
        version not in SUPPORTED_HOLDOUT_VERSIONS
        or set(bundle) != expected_bundle_fields
        or bundle.get("evidence_class")
        != "automated_proxy_judge_holdout_input"
        or bundle.get("automated_proxy_judge") is not True
        or bundle.get("human_evidence") is not False
        or bundle.get("human_calibration_performed") is not False
        or not isinstance(evaluator_slot, Mapping)
        or not isinstance(run_binding, Mapping)
        or not isinstance(items, list)
        or len(items) != HOLDOUT_ITEM_COUNT_V2
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_BUNDLE_INVALID")

    expected_payload_fields = {
        "schema_version",
        "round_index",
        "evaluator_id",
        "agent_task_id",
        "agent_id",
        "context_id",
        "model_id",
        "started_at",
        "ended_at",
        "api_usage_count",
        "tool_usage_count",
        "automated_proxy_judge",
        "human_calibration_performed",
        "expected_scores_observed",
        "peer_round_output_observed",
        "scores",
    }
    round_index = bundle.get("round_index")
    if (
        set(payload) != expected_payload_fields
        or payload.get("schema_version")
        != f"trip-check-p5-judge-holdout-score-payload-{version}"
        or payload.get("round_index") != round_index
        or payload.get("model_id") != evaluator_slot.get("model_id")
    ):
        raise P5JudgeHoldoutErrorV2(
            "JUDGE_HOLDOUT_SCORE_PAYLOAD_CONTRACT_INVALID"
        )

    receipt = {
        "round_index": round_index,
        "sha256": _sha256(bundle_file),
        **dict(evaluator_slot),
        **dict(run_binding),
    }
    report = {
        **{
            key: value
            for key, value in payload.items()
            if key != "schema_version"
        },
        "schema_version": f"trip-check-p5-judge-holdout-round-{version}",
        "evaluator_profile_id": evaluator_slot.get("evaluator_profile_id"),
        "reasoning_effort": evaluator_slot.get("reasoning_effort"),
        "bundle_sha256": receipt["sha256"],
        **dict(run_binding),
    }
    scores = _validate_round(report, receipt, version)
    expected_ids = {item.get("holdout_item_id") for item in items}
    actual_ids = {score["holdout_item_id"] for score in scores}
    if actual_ids != expected_ids:
        raise P5JudgeHoldoutErrorV2(
            "JUDGE_HOLDOUT_SCORE_BUNDLE_COVERAGE_INVALID"
        )
    return report


def aggregate_judge_holdout_rounds_v2(
    *,
    repo_root: Path,
    key_path: Path,
    key_sha256: str,
    round_paths: Sequence[Path],
) -> dict[str, Any]:
    """Aggregate slot-bound holdout results without exposing expected scores."""

    root = repo_root.resolve()
    if (
        not key_path.is_absolute()
        or _inside(key_path, root)
        or _contains_link(key_path)
        or _sha256(key_path) != key_sha256
        or len(round_paths) != ROUND_COUNT_V5
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_INPUT_INVALID")
    key = _load_json(key_path, "JUDGE_HOLDOUT_KEY_INVALID")
    version = _schema_version(key.get("schema_version"), "key")
    binding_fields = (*REPO_BINDING_FIELDS_V2, *HASH_BINDING_FIELDS_V2)
    receipts = key.get("bundle_receipts")
    expected_rows = key.get("expected")
    receipt_fields = {
        "round_index",
        "path",
        "sha256",
        "evaluator_profile_id",
        "model_id",
        "reasoning_effort",
        *binding_fields,
        "item_count",
    }
    if (
        version not in SUPPORTED_HOLDOUT_VERSIONS
        or any(not _is_sha256(key.get(field)) for field in HASH_BINDING_FIELDS_V2)
        or key.get("upstream_commit") != key.get("subject_commit")
        or key.get("dirty_tree") is not False
        or not isinstance(receipts, list)
        or len(receipts) != ROUND_COUNT_V5
        or not isinstance(expected_rows, list)
        or len(expected_rows) != HOLDOUT_ITEM_COUNT_V2
        or digest(expected_rows) != key.get("holdout_expected_content_sha256")
        or any(
            not isinstance(receipt, Mapping)
            or set(receipt) != receipt_fields
            or receipt.get("round_index") != round_index
            or receipt.get("item_count") != HOLDOUT_ITEM_COUNT_V2
            or not isinstance(receipt.get("path"), str)
            or not receipt["path"]
            or not _is_sha256(receipt.get("sha256"))
            or not isinstance(receipt.get("evaluator_profile_id"), str)
            or not receipt["evaluator_profile_id"]
            or not isinstance(receipt.get("model_id"), str)
            or not receipt["model_id"]
            or receipt.get("reasoning_effort") != "high"
            or any(receipt.get(field) != key.get(field) for field in binding_fields)
            for round_index, receipt in enumerate(receipts or [], 1)
        )
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_KEY_INVALID")
    if (
        len({receipt["evaluator_profile_id"] for receipt in receipts})
        != ROUND_COUNT_V5
        or len({receipt["model_id"] for receipt in receipts}) != ROUND_COUNT_V5
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_KEY_INVALID")
    expected = {
        row["holdout_item_id"]: row
        for row in expected_rows
        if isinstance(row, dict) and isinstance(row.get("holdout_item_id"), str)
    }
    if len(expected) != HOLDOUT_ITEM_COUNT_V2:
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_KEY_INVALID")
    if any(
        set(row) != {"holdout_item_id", *DIMENSIONS_V5, "derived_verdict"}
        or any(
            not isinstance(row.get(dimension), int)
            or isinstance(row.get(dimension), bool)
            or not 0 <= row[dimension] <= 4
            for dimension in DIMENSIONS_V5
        )
        or row.get("derived_verdict")
        != (
            "PASS"
            if min(row[dimension] for dimension in DIMENSIONS_V5) >= 2
            else "NEEDS_REVISION"
        )
        for row in expected.values()
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_KEY_INVALID")
    reports: list[dict[str, Any]] = []
    result_paths: dict[int, Path] = {}
    identities = {field: set() for field in ("evaluator_id", "agent_task_id", "agent_id", "context_id")}
    for path in round_paths:
        if not path.is_absolute() or _inside(path, root) or _contains_link(path):
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ROUND_PATH_INVALID")
        report = _load_json(path, "JUDGE_HOLDOUT_ROUND_INVALID")
        round_index = report.get("round_index")
        if not isinstance(round_index, int) or round_index not in {1, 2, 3}:
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_ROUND_INVALID")
        report["scores"] = _validate_round(
            report, receipts[round_index - 1], version
        )
        if set(score["holdout_item_id"] for score in report["scores"]) != set(expected):
            raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_COVERAGE_INVALID")
        result_paths[round_index] = path
        for field in identities:
            identities[field].add(report[field])
        reports.append(report)
    if len({report["round_index"] for report in reports}) != ROUND_COUNT_V5 or any(
        len(values) != ROUND_COUNT_V5 for values in identities.values()
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_INDEPENDENCE_INVALID")
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for report in reports:
        for score in report["scores"]:
            grouped[score["holdout_item_id"]].append(score)
    dimension_agreement = Counter()
    verdict_agreement = 0
    slot_metrics: list[dict[str, Any]] = []
    for report in sorted(reports, key=lambda item: item["round_index"]):
        exact = Counter()
        within_one = Counter()
        verdict_matches = 0
        for score in report["scores"]:
            target = expected[score["holdout_item_id"]]
            verdict_matches += int(score["derived_verdict"] == target["derived_verdict"])
            for dimension in DIMENSIONS_V5:
                exact[dimension] += int(score[dimension] == target[dimension])
                within_one[dimension] += int(abs(score[dimension] - target[dimension]) <= 1)
        slot_metrics.append(
            {
                "round_index": report["round_index"],
                "evaluator_profile_id": report["evaluator_profile_id"],
                "model_id": report["model_id"],
                "reasoning_effort": report["reasoning_effort"],
                "expected_verdict_match_rate": verdict_matches / HOLDOUT_ITEM_COUNT_V2,
                "exact_score_match_rate": {
                    dimension: exact[dimension] / HOLDOUT_ITEM_COUNT_V2
                    for dimension in DIMENSIONS_V5
                },
                "expected_dimension_within_one_rate": {
                    dimension: within_one[dimension] / HOLDOUT_ITEM_COUNT_V2
                    for dimension in DIMENSIONS_V5
                },
            }
        )
    for scores in grouped.values():
        verdict_agreement += int(len({score["derived_verdict"] for score in scores}) == 1)
        for dimension in DIMENSIONS_V5:
            dimension_agreement[dimension] += int(
                max(score[dimension] for score in scores)
                - min(score[dimension] for score in scores)
                <= 1
            )
    verdict_agreement_rate = verdict_agreement / HOLDOUT_ITEM_COUNT_V2
    per_dimension_agreement_rate = {
        dimension: dimension_agreement[dimension] / HOLDOUT_ITEM_COUNT_V2
        for dimension in DIMENSIONS_V5
    }
    passed = (
        verdict_agreement_rate >= PANEL_AGREEMENT_THRESHOLD_V2
        and all(value >= PANEL_AGREEMENT_THRESHOLD_V2 for value in per_dimension_agreement_rate.values())
        and all(metric["expected_verdict_match_rate"] == 1.0 for metric in slot_metrics)
        and all(
            metric["exact_score_match_rate"]["actionability"] >= 0.9
            and all(value == 1.0 for value in metric["expected_dimension_within_one_rate"].values())
            for metric in slot_metrics
        )
    )
    panel = {
        "schema_version": f"trip-check-p5-judge-holdout-panel-{version}",
        "status": "PASS" if passed else "BLOCKED",
        "evidence_class": "automated_proxy_judge_sealed_holdout",
        "automated_proxy_judge": True,
        "human_evidence": False,
        "human_calibration_performed": False,
        "round_count": ROUND_COUNT_V5,
        "item_count": HOLDOUT_ITEM_COUNT_V2,
        "agreement_threshold": PANEL_AGREEMENT_THRESHOLD_V2,
        "verdict_agreement_rate": verdict_agreement_rate,
        "per_dimension_agreement_rate": per_dimension_agreement_rate,
        "slot_metrics": slot_metrics,
        "key_sha256": key_sha256,
        "key_path": str(key_path.resolve()),
        **{field: key[field] for field in binding_fields},
        "provenance": [
            {
                field: report[field]
                for field in (
                    "round_index",
                    "evaluator_profile_id",
                    "reasoning_effort",
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
            }
            | {
                "round_result_path": str(result_paths[report["round_index"]].resolve()),
                "round_result_sha256": _sha256(result_paths[report["round_index"]]),
            }
            for report in sorted(reports, key=lambda item: item["round_index"])
        ],
    }
    panel["report_hash"] = digest(panel)
    return panel


def validate_judge_holdout_panel_v2(
    *,
    repo_root: Path,
    panel_path: Path,
    rubric_path: Path,
    protocol_path: Path,
    commitment_path: Path,
) -> dict[str, Any]:
    """Reaggregate and bind one PASS holdout panel to immutable contracts."""

    root = repo_root.resolve()
    if not panel_path.is_absolute() or _inside(panel_path, root) or _contains_link(panel_path):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_PANEL_PATH_INVALID")
    panel = _load_json(panel_path, "JUDGE_HOLDOUT_PANEL_INVALID")
    commitment = _load_json(
        commitment_path, "JUDGE_HOLDOUT_COMMITMENT_INVALID"
    )
    report_hash = panel.get("report_hash")
    unsigned = {key: value for key, value in panel.items() if key != "report_hash"}
    provenance = panel.get("provenance")
    key_path = Path(str(panel.get("key_path", "")))
    version = _schema_version(panel.get("schema_version"), "panel")
    if (
        not _is_sha256(report_hash)
        or digest(unsigned) != report_hash
        or version not in SUPPORTED_HOLDOUT_VERSIONS
        or panel.get("status") != "PASS"
        or panel.get("agreement_threshold") != PANEL_AGREEMENT_THRESHOLD_V2
        or not isinstance(provenance, list)
        or len(provenance) != ROUND_COUNT_V5
        or not key_path.is_absolute()
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_PANEL_INVALID")
    round_paths = [
        Path(str(item.get("round_result_path", "")))
        for item in sorted(provenance, key=lambda item: item.get("round_index", 0))
        if isinstance(item, Mapping)
    ]
    recalculated = aggregate_judge_holdout_rounds_v2(
        repo_root=root,
        key_path=key_path,
        key_sha256=str(panel.get("key_sha256")),
        round_paths=round_paths,
    )
    rubric_source = _load_json(rubric_path, "JUDGE_HOLDOUT_RUBRIC_INVALID")
    protocol_source = _load_json(protocol_path, "JUDGE_HOLDOUT_PROTOCOL_INVALID")
    if (
        recalculated != panel
        or protocol_source.get("schema_version")
        != f"trip-check-p5-judge-protocol-{version}"
        or rubric_source.get("schema_version")
        != "trip-check-p5-judge-rubric-v2"
        or panel.get("source_rubric_sha256") != _sha256(rubric_path)
        or panel.get("judge_input_rubric_sha256")
        != digest(_judge_rubric_projection(rubric_source))
        or panel.get("source_protocol_sha256") != _sha256(protocol_path)
        or panel.get("judge_input_protocol_sha256")
        != digest(_judge_protocol_projection(rubric_source, protocol_source))
        or panel.get("holdout_commitment_sha256") != _sha256(commitment_path)
        or set(commitment)
        != {
            "schema_version",
            "status",
            "item_count",
            "source_lane",
            "blind_source_used",
            "package_sha256",
            "public_items_content_sha256",
            "expected_items_content_sha256",
            "custodian_receipt_sha256",
            "review_receipt_sha256",
        }
        or commitment.get("schema_version")
        != f"trip-check-p5-judge-holdout-commitment-{version}"
        or commitment.get("status") != "SEALED"
        or commitment.get("item_count") != HOLDOUT_ITEM_COUNT_V2
        or commitment.get("source_lane") != "NONBLIND_SYNTHETIC_HOLDOUT"
        or commitment.get("blind_source_used") is not False
        or panel.get("holdout_package_sha256")
        != commitment.get("package_sha256")
        or panel.get("holdout_public_content_sha256")
        != commitment.get("public_items_content_sha256")
        or panel.get("holdout_expected_content_sha256")
        != commitment.get("expected_items_content_sha256")
    ):
        raise P5JudgeHoldoutErrorV2("JUDGE_HOLDOUT_PANEL_BINDING_INVALID")
    return panel


__all__ = [
    "HOLDOUT_ITEM_COUNT_V2",
    "P5JudgeHoldoutErrorV2",
    "aggregate_judge_holdout_rounds_v2",
    "export_judge_holdout_bundles_v2",
    "validate_judge_holdout_panel_v2",
]
