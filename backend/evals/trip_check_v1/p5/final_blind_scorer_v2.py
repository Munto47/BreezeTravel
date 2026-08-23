"""Isolated, aggregate-only scorer for the sealed P5 v2 blind lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready
from evals.trip_check_v1.p5.contracts_v2 import P5OracleV2, VARIANT_IDS_V2
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.scorer_v2 import (
    P5CaseScoreV2,
    aggregate_scores_v2,
    score_case_v2,
    validate_run_group_v2,
)


MINIMUM_BLIND_BUCKET_SIZE_V2 = 5
SCHEMA_CONTRACT_PATHS_V2 = (
    "backend/evals/trip_check_v1/p5/blind_bundle_v2.schema.json",
    "backend/evals/trip_check_v1/p5/blind_seal_v2.schema.json",
    "backend/evals/trip_check_v1/p5/case_v2.schema.json",
    "backend/evals/trip_check_v1/p5/materialization_v2.schema.json",
    "backend/evals/trip_check_v1/p5/oracle_v2.schema.json",
)
BLIND_SEAL_FIELDS_V2 = frozenset(
    {
        "schema_version",
        "split",
        "case_count",
        "case_ids_sha256",
        "inputs_file_sha256",
        "inputs_content_sha256",
        "materializations_file_sha256",
        "materializations_content_sha256",
        "schema_contract_sha256",
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "rubric_sha256",
        "run_spec_template_sha256",
        "variant_ids_sha256",
        "review_receipt_sha256",
        "label_storage",
        "label_access",
        "scoring_payload_present",
        "human_evidence",
    }
)
BLIND_BUNDLE_FIELDS_V2 = frozenset(
    {"schema_version", "evidence_class", "human_evidence", "dataset_binding", "labels"}
)
BLIND_DATASET_BINDING_FIELDS_V2 = frozenset(
    {
        "case_count",
        "case_ids_sha256",
        "inputs_file_sha256",
        "inputs_content_sha256",
        "materializations_file_sha256",
        "materializations_content_sha256",
        "schema_contract_sha256",
        "run_spec_template_sha256",
        "rubric_sha256",
        "variant_ids_sha256",
    }
)


class P5BlindScoringErrorV2(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _fail_closed(reason_code: str, exc: Exception | None = None) -> None:
    if exc is None:
        raise P5BlindScoringErrorV2(reason_code)
    raise P5BlindScoringErrorV2(reason_code) from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path, reason: str) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError as exc:
        _fail_closed(reason, exc)
    raise AssertionError("unreachable")


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail_closed(reason, exc)
    if not isinstance(value, dict):
        _fail_closed(reason)
    return value


def _load_jsonl(path: Path, reason: str) -> list[dict[str, Any]]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _fail_closed(reason, exc)
    if any(not isinstance(row, dict) for row in rows):
        _fail_closed(reason)
    return rows


def _require_sha256(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail_closed(reason)
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _contains_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def canonical_labels_hash_v2(labels: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(labels, key=lambda item: str(item.get("case_id", "")))
    return _sha256_bytes(b"".join(canonical_bytes(item) + b"\n" for item in ordered))


def schema_contract_sha256_v2(repo_root: Path) -> str:
    """Bind the exact bytes of every frozen P5 v2 scoring schema."""

    root = repo_root.resolve()
    bindings = [
        {
            "path": relative_path,
            "file_sha256": _sha256_file(
                root / Path(relative_path), "BLIND_SCHEMA_CONTRACT_UNREADABLE"
            ),
        }
        for relative_path in sorted(SCHEMA_CONTRACT_PATHS_V2)
    ]
    return digest(bindings)


def _read_external_bundle(
    *,
    repo_root: Path,
    bundle_path: Path | None,
    bundle_bytes: bytes | None,
    expected_sha256: str,
) -> dict[str, Any]:
    if (bundle_path is None) == (bundle_bytes is None):
        _fail_closed("BLIND_BUNDLE_SOURCE_REQUIRED")
    payload = bundle_bytes
    if bundle_path is not None:
        if not bundle_path.is_absolute() or ".." in bundle_path.parts:
            _fail_closed("BLIND_BUNDLE_PATH_ESCAPE")
        absolute = bundle_path.absolute()
        if _contains_symlink(absolute):
            _fail_closed("BLIND_BUNDLE_SYMLINK_FORBIDDEN")
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            _fail_closed("BLIND_BUNDLE_UNREADABLE", exc)
        if _inside(resolved, repo_root.resolve()):
            _fail_closed("BLIND_BUNDLE_INSIDE_REPOSITORY")
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            _fail_closed("BLIND_BUNDLE_UNREADABLE", exc)
    assert payload is not None
    if _sha256_bytes(payload) != expected_sha256:
        _fail_closed("BLIND_BUNDLE_SHA256_MISMATCH")
    try:
        bundle = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        _fail_closed("BLIND_BUNDLE_INVALID_JSON", exc)
    if not isinstance(bundle, dict):
        _fail_closed("BLIND_BUNDLE_SCHEMA_INVALID")
    return bundle


def _repo_state(repo_root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--short"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        _fail_closed("REPOSITORY_STATE_UNAVAILABLE", exc)
    return head, dirty


def _validate_seal_and_artifacts(
    *,
    repo_root: Path,
    seal_path: Path,
    inputs_path: Path,
    materializations_path: Path,
    run_spec_template_path: Path,
    rubric_path: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    seal = _load_json(seal_path, "BLIND_SEAL_INVALID")
    if set(seal) != BLIND_SEAL_FIELDS_V2:
        _fail_closed("BLIND_SEAL_EXTRA_OR_MISSING_FIELDS")
    if (
        seal.get("schema_version") != "trip-check-p5-blind-seal-v2"
        or seal.get("split") != "frozen_blind"
        or seal.get("case_count") != 90
        or seal.get("label_storage") != "external_bundle_only"
        or seal.get("label_access") != "isolated_scorer_only"
        or seal.get("scoring_payload_present") is not False
        or seal.get("human_evidence") is not False
    ):
        _fail_closed("BLIND_SEAL_CONTRACT_INVALID")
    for field in (
        "case_ids_sha256",
        "inputs_file_sha256",
        "inputs_content_sha256",
        "materializations_file_sha256",
        "materializations_content_sha256",
        "schema_contract_sha256",
        "labels_canonical_sha256",
        "external_bundle_sha256",
        "rubric_sha256",
        "run_spec_template_sha256",
        "variant_ids_sha256",
        "review_receipt_sha256",
    ):
        _require_sha256(seal.get(field), "BLIND_SEAL_HASH_INVALID")
    if seal["schema_contract_sha256"] != schema_contract_sha256_v2(repo_root):
        _fail_closed("BLIND_SCHEMA_CONTRACT_MISMATCH")
    inputs = _load_jsonl(inputs_path, "BLIND_INPUTS_INVALID")
    materializations = _load_jsonl(
        materializations_path, "BLIND_MATERIALIZATIONS_INVALID"
    )
    case_ids = [item.get("case_id") for item in inputs]
    if (
        len(inputs) != 90
        or len(set(case_ids)) != 90
        or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
    ):
        _fail_closed("BLIND_CASE_SET_INVALID")
    if _sha256_file(inputs_path, "BLIND_INPUTS_UNREADABLE") != seal["inputs_file_sha256"]:
        _fail_closed("BLIND_INPUT_FILE_HASH_MISMATCH")
    if digest(inputs) != seal["inputs_content_sha256"]:
        _fail_closed("BLIND_INPUT_CONTENT_HASH_MISMATCH")
    if _sha256_file(
        materializations_path, "BLIND_MATERIALIZATIONS_UNREADABLE"
    ) != seal["materializations_file_sha256"]:
        _fail_closed("BLIND_MATERIALIZATION_FILE_HASH_MISMATCH")
    if digest(materializations) != seal["materializations_content_sha256"]:
        _fail_closed("BLIND_MATERIALIZATION_CONTENT_HASH_MISMATCH")
    if digest(sorted(case_ids)) != seal["case_ids_sha256"]:
        _fail_closed("BLIND_CASE_SET_HASH_MISMATCH")
    if _sha256_file(
        run_spec_template_path, "BLIND_RUN_SPEC_TEMPLATE_UNREADABLE"
    ) != seal["run_spec_template_sha256"]:
        _fail_closed("BLIND_RUN_SPEC_TEMPLATE_HASH_MISMATCH")
    if _sha256_file(rubric_path, "BLIND_RUBRIC_UNREADABLE") != seal["rubric_sha256"]:
        _fail_closed("BLIND_RUBRIC_HASH_MISMATCH")
    if digest(list(VARIANT_IDS_V2)) != seal["variant_ids_sha256"]:
        _fail_closed("BLIND_VARIANT_SET_HASH_MISMATCH")
    return seal, inputs


def _validate_bundle(
    *, bundle: dict[str, Any], inputs: Sequence[Mapping[str, Any]], seal: Mapping[str, Any]
) -> dict[str, P5OracleV2]:
    if set(bundle) != BLIND_BUNDLE_FIELDS_V2:
        _fail_closed("BLIND_BUNDLE_EXTRA_OR_MISSING_FIELDS")
    if (
        bundle.get("schema_version") != "trip-check-p5-blind-label-bundle-v2"
        or bundle.get("evidence_class") != "controlled_blind_oracle"
        or bundle.get("human_evidence") is not False
    ):
        _fail_closed("BLIND_BUNDLE_CONTRACT_INVALID")
    binding = bundle.get("dataset_binding")
    if not isinstance(binding, dict) or set(binding) != BLIND_DATASET_BINDING_FIELDS_V2:
        _fail_closed("BLIND_BUNDLE_DATASET_BINDING_SCHEMA_INVALID")
    expected_binding = {
        "case_count": 90,
        "case_ids_sha256": seal["case_ids_sha256"],
        "inputs_file_sha256": seal["inputs_file_sha256"],
        "inputs_content_sha256": seal["inputs_content_sha256"],
        "materializations_file_sha256": seal["materializations_file_sha256"],
        "materializations_content_sha256": seal["materializations_content_sha256"],
        "schema_contract_sha256": seal["schema_contract_sha256"],
        "run_spec_template_sha256": seal["run_spec_template_sha256"],
        "rubric_sha256": seal["rubric_sha256"],
        "variant_ids_sha256": seal["variant_ids_sha256"],
    }
    if binding != expected_binding:
        _fail_closed("BLIND_BUNDLE_STALE_DATASET_BINDING")
    labels = bundle.get("labels")
    if not isinstance(labels, list) or len(labels) != 90:
        _fail_closed("BLIND_BUNDLE_LABEL_SET_INVALID")
    labels_by_id: dict[str, P5OracleV2] = {}
    for label in labels:
        if (
            not isinstance(label, dict)
            or set(label) != {"schema_version", "case_id", "oracle"}
            or label.get("schema_version") != "trip-check-p5-blind-label-v2"
            or not isinstance(label.get("case_id"), str)
            or not isinstance(label.get("oracle"), dict)
        ):
            _fail_closed("BLIND_BUNDLE_LABEL_SCHEMA_INVALID")
        try:
            oracle = P5OracleV2.model_validate(label["oracle"])
        except ValidationError as exc:
            _fail_closed("BLIND_BUNDLE_ORACLE_SCHEMA_INVALID", exc)
        case_id = label["case_id"]
        if case_id in labels_by_id:
            _fail_closed("BLIND_BUNDLE_LABEL_DUPLICATE")
        labels_by_id[case_id] = oracle
    input_ids = {str(item["case_id"]) for item in inputs}
    if set(labels_by_id) != input_ids:
        _fail_closed("BLIND_BUNDLE_LABEL_CASE_SET_MISMATCH")
    if canonical_labels_hash_v2(labels) != seal["labels_canonical_sha256"]:
        _fail_closed("BLIND_LABEL_COMMITMENT_MISMATCH")
    return labels_by_id


def _safe_buckets(
    scores: Sequence[P5CaseScoreV2],
    key: Callable[[P5CaseScoreV2], str],
) -> dict[str, Any]:
    groups: dict[str, list[P5CaseScoreV2]] = defaultdict(list)
    for score in scores:
        groups[key(score)].append(score)
    return {
        name: aggregate_scores_v2(items)
        for name, items in sorted(groups.items())
        if len(items) >= MINIMUM_BLIND_BUCKET_SIZE_V2
    }


def score_external_blind_run_group_v2(
    *,
    repo_root: Path,
    run_dir: Path,
    dataset_manifest_path: Path,
    inputs_path: Path,
    materializations_path: Path,
    seal_path: Path,
    run_spec_template_path: Path,
    rubric_path: Path,
    expected_bundle_sha256: str,
    bundle_path: Path | None = None,
    bundle_bytes: bytes | None = None,
    require_current_subject: bool = True,
) -> dict[str, Any]:
    if require_current_subject:
        try:
            require_v2_formal_ready()
        except RuntimeError as exc:
            _fail_closed("P5_V2_FORMAL_CONTRACT_NOT_READY", exc)
    root = repo_root.resolve()
    seal, raw_inputs = _validate_seal_and_artifacts(
        repo_root=root,
        seal_path=seal_path,
        inputs_path=inputs_path,
        materializations_path=materializations_path,
        run_spec_template_path=run_spec_template_path,
        rubric_path=rubric_path,
    )
    expected_hash = _require_sha256(
        expected_bundle_sha256, "BLIND_BUNDLE_SHA256_REQUIRED"
    )
    if expected_hash != seal["external_bundle_sha256"]:
        _fail_closed("BLIND_BUNDLE_SEAL_HASH_MISMATCH")
    try:
        manifest, cases, outputs = validate_run_group_v2(
            run_dir=run_dir.resolve(),
            cases_path=inputs_path.resolve(),
            materializations_path=materializations_path.resolve(),
            dataset_manifest_path=dataset_manifest_path.resolve(),
            expected_lane="frozen_blind",
            require_formal=True,
        )
    except Exception as exc:
        reason = getattr(exc, "reason_code", "BLIND_RUN_GROUP_INVALID")
        _fail_closed(str(reason), exc)
    if require_current_subject:
        head, dirty = _repo_state(root)
        if dirty or head != manifest["subject_commit"]:
            _fail_closed("BLIND_RUN_SUBJECT_STATE_MISMATCH")
    bundle = _read_external_bundle(
        repo_root=root,
        bundle_path=bundle_path,
        bundle_bytes=bundle_bytes,
        expected_sha256=expected_hash,
    )
    labels_by_id = _validate_bundle(bundle=bundle, inputs=raw_inputs, seal=seal)
    case_by_id = {case.case_id: case for case in cases}
    scores = [
        score_case_v2(
            case_by_id[output.case_id],
            output,
            oracle_override=labels_by_id[output.case_id],
        )
        for output in outputs
    ]
    variant_metrics: dict[str, Any] = {}
    for variant_id in VARIANT_IDS_V2:
        items = [item for item in scores if item.variant_id == variant_id]
        variant_metrics[variant_id] = {
            "overall": aggregate_scores_v2(items),
            "by_city": _safe_buckets(items, lambda item: item.city),
            "by_input_kind": _safe_buckets(items, lambda item: item.input_kind),
            "by_difficulty": _safe_buckets(items, lambda item: item.difficulty),
            "by_fault_profile": _safe_buckets(items, lambda item: item.fault_profile_id),
            "by_finding": _safe_buckets(
                items, lambda item: "+".join(item.required_reason_codes) or "NONE"
            ),
            "by_repair_outcome": _safe_buckets(
                items,
                lambda item: labels_by_id[item.case_id].expected_strategy_outcome,
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
    }
    passed = all(core_gate_checks.values())
    receipt = {
        "schema_version": "trip-check-p5-isolated-blind-score-v2",
        "status": "PASS" if passed else "REJECT",
        "decision": "ACCEPT_BLIND_SCORE" if passed else "REJECT",
        "evidence_class": "CONTROLLED_BLIND_ORACLE",
        "truth_provenance": "external_controlled_blind_oracle",
        "human_evidence": False,
        "minimum_bucket_size": MINIMUM_BLIND_BUCKET_SIZE_V2,
        "bindings": {
            "subject_commit": manifest["subject_commit"],
            "dataset_manifest_hash": manifest["dataset_manifest_hash"],
            "run_group_manifest_hash": manifest["manifest_hash"],
            "terminal_outputs_file_sha256": manifest["terminal_outputs_file_sha256"],
            "external_bundle_sha256": expected_hash,
            "labels_canonical_sha256": seal["labels_canonical_sha256"],
            "case_ids_sha256": seal["case_ids_sha256"],
            "materializations_content_sha256": seal[
                "materializations_content_sha256"
            ],
            "schema_contract_sha256": seal["schema_contract_sha256"],
        },
        "case_count": 90,
        "terminal_count": 270,
        "variant_metrics": variant_metrics,
        "core_gate_checks": core_gate_checks,
        "automated_proxy_judge": "NOT_RUN",
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
    }
    serialized = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    if any(case_id in serialized for case_id in labels_by_id):
        _fail_closed("BLIND_OUTPUT_CASE_ID_LEAK")
    return receipt


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _invalid_receipt(reason_code: str) -> dict[str, Any]:
    return {
        "schema_version": "trip-check-p5-isolated-blind-score-v2",
        "status": "INVALID",
        "decision": "REJECT",
        "reason_code": reason_code,
        "human_evidence": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.trip_check_v1.p5.final_blind_scorer_v2"
    )
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dataset-manifest", type=Path, required=True)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--materializations", type=Path, required=True)
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument("--run-spec-template", type=Path, required=True)
    parser.add_argument("--rubric", type=Path, required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    bundle_path = None if args.bundle == "-" else Path(args.bundle)
    bundle_bytes = sys.stdin.buffer.read() if args.bundle == "-" else None
    try:
        receipt = score_external_blind_run_group_v2(
            repo_root=args.repo_root,
            run_dir=args.run_dir,
            dataset_manifest_path=args.dataset_manifest,
            inputs_path=args.inputs,
            materializations_path=args.materializations,
            seal_path=args.seal,
            run_spec_template_path=args.run_spec_template,
            rubric_path=args.rubric,
            expected_bundle_sha256=args.bundle_sha256,
            bundle_path=bundle_path,
            bundle_bytes=bundle_bytes,
        )
    except P5BlindScoringErrorV2 as exc:
        receipt = _invalid_receipt(exc.reason_code)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2
    if args.output:
        _atomic_write(args.output.resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["decision"] == "ACCEPT_BLIND_SCORE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
