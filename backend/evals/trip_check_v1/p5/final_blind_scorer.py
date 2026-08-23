"""Isolated P5 blind scorer with exact 90x3 run-group binding.

Normal runners and Judge exporters must not import this module.  It accepts the
label bundle only through an explicit repository-external path or stdin and
returns a strict aggregate allowlist with no case-level feedback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

from evals.trip_check_v1.p5.contracts import P5TerminalOutput, VARIANT_IDS
from evals.trip_check_v1.p5.active_contract import require_v2_formal_ready
from evals.trip_check_v1.p5.data_contract import canonical_bytes, digest
from evals.trip_check_v1.p5.scorer import P5CaseScore, score_case


class P5BlindScoringError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _require_active_contract() -> None:
    try:
        require_v2_formal_ready()
    except RuntimeError as exc:
        raise P5BlindScoringError("P5_V2_FORMAL_CONTRACT_NOT_READY") from exc


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path, reason: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5BlindScoringError(reason) from exc
    if not isinstance(value, dict):
        raise P5BlindScoringError(reason)
    return value


def _load_jsonl(path: Path, reason: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P5BlindScoringError(reason) from exc
    if any(not isinstance(row, dict) for row in rows):
        raise P5BlindScoringError(reason)
    return rows


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_sha256(value: Any, reason: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise P5BlindScoringError(reason)
    return value


def _canonical_labels_hash(labels: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(labels, key=lambda item: str(item.get("case_id", "")))
    return _sha256_bytes(b"".join(canonical_bytes(item) + b"\n" for item in ordered))


def _read_external_bundle(
    *,
    repo_root: Path,
    bundle_path: Path | None,
    bundle_bytes: bytes | None,
    expected_sha256: str,
) -> dict[str, Any]:
    if (bundle_path is None) == (bundle_bytes is None):
        raise P5BlindScoringError("BLIND_BUNDLE_SOURCE_REQUIRED")
    payload = bundle_bytes
    if bundle_path is not None:
        resolved = bundle_path.resolve()
        if _inside(resolved, repo_root):
            raise P5BlindScoringError("BLIND_BUNDLE_INSIDE_REPOSITORY")
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            raise P5BlindScoringError("BLIND_BUNDLE_UNREADABLE") from exc
    assert payload is not None
    if _sha256_bytes(payload) != expected_sha256:
        raise P5BlindScoringError("BLIND_BUNDLE_SHA256_MISMATCH")
    try:
        bundle = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise P5BlindScoringError("BLIND_BUNDLE_INVALID_JSON") from exc
    if not isinstance(bundle, dict):
        raise P5BlindScoringError("BLIND_BUNDLE_INVALID_SCHEMA")
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
        raise P5BlindScoringError("REPOSITORY_STATE_UNAVAILABLE") from exc
    return head, dirty


def _validate_inputs_and_seal(repo_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    p5_root = repo_root / "backend" / "evals" / "trip_check_v1" / "p5"
    inputs_path = p5_root / "frozen_blind.inputs.jsonl"
    seal_path = p5_root / "sealed" / "frozen_blind.seal.json"
    template_path = p5_root / "run_spec_template_v1.json"
    rubric_path = p5_root / "judge_rubric_v1.json"
    seal = _load_json(seal_path, "BLIND_SEAL_INVALID")
    allowed_seal_fields = {
        "schema_version",
        "split",
        "case_count",
        "case_ids_sha256",
        "inputs_file_sha256",
        "inputs_content_sha256",
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
    if (
        set(seal) != allowed_seal_fields
        or seal.get("schema_version") != "trip-check-p5-blind-seal-v1"
        or seal.get("split") != "frozen_blind"
        or seal.get("case_count") != 90
        or seal.get("scoring_payload_present") is not False
        or seal.get("human_evidence") is not False
        or seal.get("label_access") != "isolated_scorer_only"
    ):
        raise P5BlindScoringError("BLIND_SEAL_CONTRACT_INVALID")
    inputs = _load_jsonl(inputs_path, "BLIND_INPUTS_INVALID")
    case_ids = [item.get("case_id") for item in inputs]
    if (
        len(inputs) != 90
        or any(not isinstance(case_id, str) or not case_id for case_id in case_ids)
        or len(set(case_ids)) != 90
    ):
        raise P5BlindScoringError("BLIND_INPUT_CASE_SET_INVALID")
    if _sha256_bytes(inputs_path.read_bytes()) != seal["inputs_file_sha256"]:
        raise P5BlindScoringError("BLIND_INPUT_FILE_HASH_MISMATCH")
    if digest(inputs) != seal["inputs_content_sha256"]:
        raise P5BlindScoringError("BLIND_INPUT_CONTENT_HASH_MISMATCH")
    if digest(sorted(case_ids)) != seal["case_ids_sha256"]:
        raise P5BlindScoringError("BLIND_CASE_SET_HASH_MISMATCH")
    if _sha256_bytes(template_path.read_bytes()) != seal["run_spec_template_sha256"]:
        raise P5BlindScoringError("BLIND_RUN_SPEC_TEMPLATE_HASH_MISMATCH")
    if _sha256_bytes(rubric_path.read_bytes()) != seal["rubric_sha256"]:
        raise P5BlindScoringError("BLIND_RUBRIC_HASH_MISMATCH")
    if digest(list(VARIANT_IDS)) != seal["variant_ids_sha256"]:
        raise P5BlindScoringError("BLIND_VARIANT_SET_HASH_MISMATCH")
    return inputs, seal


def _validate_run_group(
    *,
    repo_root: Path,
    run_dir: Path,
    inputs: list[dict[str, Any]],
    require_current_subject: bool,
) -> tuple[dict[str, Any], list[P5TerminalOutput]]:
    manifest = _load_json(run_dir / "run_group_manifest.json", "BLIND_RUN_MANIFEST_INVALID")
    claimed_manifest_hash = manifest.get("manifest_hash")
    if claimed_manifest_hash != digest(
        {key: value for key, value in manifest.items() if key != "manifest_hash"}
    ):
        raise P5BlindScoringError("BLIND_RUN_MANIFEST_HASH_MISMATCH")
    if (
        manifest.get("lane") != "frozen_blind"
        or manifest.get("formal_evidence") is not True
        or manifest.get("dirty_tree") is not False
        or manifest.get("case_count") != 90
        or manifest.get("variant_count") != 3
        or manifest.get("terminal_count") != 270
        or manifest.get("expected_terminal_count") != 270
        or manifest.get("variant_ids") != list(VARIANT_IDS)
        or manifest.get("replay_executed") is not True
        or manifest.get("replay_match_count") != 270
        or manifest.get("replay_mismatches") != []
        or manifest.get("blind_labels_read") is not False
        or manifest.get("external_api_calls") != 0
    ):
        raise P5BlindScoringError("BLIND_RUN_GROUP_CONTRACT_INVALID")
    if require_current_subject:
        head, dirty = _repo_state(repo_root)
        if dirty or head != manifest.get("subject_commit"):
            raise P5BlindScoringError("BLIND_RUN_SUBJECT_STATE_MISMATCH")
    terminal_path = run_dir / str(manifest.get("terminal_outputs_path"))
    try:
        terminal_bytes = terminal_path.read_bytes()
    except OSError as exc:
        raise P5BlindScoringError("BLIND_TERMINALS_UNREADABLE") from exc
    if _sha256_bytes(terminal_bytes) != manifest.get("terminal_outputs_file_sha256"):
        raise P5BlindScoringError("BLIND_TERMINAL_FILE_HASH_MISMATCH")
    rows = _load_jsonl(terminal_path, "BLIND_TERMINALS_INVALID")
    try:
        outputs = [P5TerminalOutput.model_validate(row) for row in rows]
    except Exception as exc:
        raise P5BlindScoringError("BLIND_TERMINAL_SCHEMA_INVALID") from exc
    if digest([output.model_dump(mode="json") for output in outputs]) != manifest.get(
        "terminal_outputs_content_sha256"
    ):
        raise P5BlindScoringError("BLIND_TERMINAL_CONTENT_HASH_MISMATCH")
    input_by_id = {item["case_id"]: item for item in inputs}
    expected_keys = {
        (case_id, variant_id) for case_id in input_by_id for variant_id in VARIANT_IDS
    }
    actual_keys = [(output.case_id, output.variant_id) for output in outputs]
    if len(actual_keys) != len(set(actual_keys)) or set(actual_keys) != expected_keys:
        raise P5BlindScoringError("BLIND_TERMINAL_KEY_SET_MISMATCH")
    if manifest.get("case_set_hash") != digest(sorted(input_by_id)):
        raise P5BlindScoringError("BLIND_RUN_CASE_SET_HASH_MISMATCH")
    common_specs = []
    for variant_id in VARIANT_IDS:
        spec = manifest.get("run_specs", {}).get(variant_id)
        if not isinstance(spec, dict):
            raise P5BlindScoringError("BLIND_RUN_SPEC_MISSING")
        if (
            spec.get("variant_id") != variant_id
            or spec.get("lane") != "frozen_blind"
            or spec.get("subject_commit") != manifest.get("subject_commit")
            or spec.get("dirty_tree") is not False
            or spec.get("case_set_hash") != manifest.get("case_set_hash")
        ):
            raise P5BlindScoringError("BLIND_RUN_SPEC_BINDING_MISMATCH")
        common_specs.append(
            {
                key: value
                for key, value in spec.items()
                if key not in {"variant_id", "adapter_version", "repair_strategy"}
            }
        )
    if any(value != common_specs[0] for value in common_specs[1:]):
        raise P5BlindScoringError("BLIND_RUN_SPEC_VARIANT_DIFF_INVALID")
    for output in outputs:
        case = input_by_id[output.case_id]
        spec = manifest["run_specs"][output.variant_id]
        if (
            output.input_hash != case["normalized_input_sha256"]
            or output.provider_snapshot_id != case["runner_control"]["provider_snapshot_id"]
            or output.fault_profile_id != case["runner_control"]["fault_profile_id"]
            or output.case_seed != case["runner_control"]["seed"]
            or output.split != "frozen_blind"
            or output.city != case["city"]
            or output.input_kind != case["input_kind"]
            or output.run_spec_hash != digest(spec)
            or output.adapter_version != spec["adapter_version"]
            or output.repair_strategy != spec["repair_strategy"]
        ):
            raise P5BlindScoringError("BLIND_TERMINAL_BINDING_MISMATCH")
    return manifest, outputs


def _validate_bundle(
    *,
    bundle: dict[str, Any],
    inputs: list[dict[str, Any]],
    seal: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    if (
        set(bundle) != {
            "schema_version",
            "evidence_class",
            "human_evidence",
            "dataset_binding",
            "labels",
        }
        or bundle.get("schema_version") != "trip-check-p5-blind-label-bundle-v1"
        or bundle.get("evidence_class") != "controlled_blind_oracle"
        or bundle.get("human_evidence") is not False
    ):
        raise P5BlindScoringError("BLIND_BUNDLE_CONTRACT_INVALID")
    binding = bundle.get("dataset_binding")
    expected_binding = {
        "case_count": 90,
        "case_ids_sha256": seal["case_ids_sha256"],
        "inputs_content_sha256": seal["inputs_content_sha256"],
        "inputs_file_sha256": seal["inputs_file_sha256"],
        "rubric_sha256": seal["rubric_sha256"],
        "run_spec_template_sha256": seal["run_spec_template_sha256"],
        "variant_ids_sha256": seal["variant_ids_sha256"],
    }
    if binding != expected_binding:
        raise P5BlindScoringError("BLIND_BUNDLE_DATASET_BINDING_MISMATCH")
    labels = bundle.get("labels")
    if not isinstance(labels, list) or len(labels) != 90:
        raise P5BlindScoringError("BLIND_BUNDLE_LABEL_SET_INVALID")
    allowed_oracle = {
        "task_success_required",
        "requires_user_resolution",
        "required_reason_codes",
        "wrong_city_or_poi_max",
        "max_new_blocker_high_unknown",
        "unknown_must_be_preserved",
        "advice_required",
        "specific_place_allowed",
        "expected_strategy_outcome",
    }
    labels_by_id = {}
    for label in labels:
        if (
            not isinstance(label, dict)
            or set(label) != {"schema_version", "case_id", "oracle"}
            or label.get("schema_version") != "trip-check-p5-blind-label-v1"
            or not isinstance(label.get("case_id"), str)
            or not isinstance(label.get("oracle"), dict)
            or set(label["oracle"]) != allowed_oracle
        ):
            raise P5BlindScoringError("BLIND_BUNDLE_LABEL_SCHEMA_INVALID")
        labels_by_id[label["case_id"]] = label
    input_ids = {item["case_id"] for item in inputs}
    if len(labels_by_id) != 90 or set(labels_by_id) != input_ids:
        raise P5BlindScoringError("BLIND_BUNDLE_LABEL_CASE_SET_MISMATCH")
    if _canonical_labels_hash(labels) != seal["labels_canonical_sha256"]:
        raise P5BlindScoringError("BLIND_LABEL_COMMITMENT_MISMATCH")
    return labels_by_id


def _aggregate(scores: Sequence[P5CaseScore]) -> dict[str, Any]:
    count = len(scores)
    return {
        "case_count": count,
        "task_success_count": sum(score.task_success for score in scores),
        "task_success_rate": sum(score.task_success for score in scores) / count,
        "mean_score": mean(score.score for score in scores),
        "wrong_city_or_poi_count": sum(score.wrong_city_or_poi_count for score in scores),
        "hard_finding_miss_count": sum(score.hard_finding_miss_count for score in scores),
        "unknown_failure_count": sum(score.unknown_preservation == "FAIL" for score in scores),
        "candidate_receipt_failure_count": sum(
            score.candidate_receipt_coverage == "FAIL" for score in scores
        ),
        "postcheck_failure_count": sum(score.repair_postcheck == "FAIL" for score in scores),
        "replay_failure_count": sum(not score.replay_hash_match for score in scores),
        "terminal_failure_count": sum(not score.terminal_ok for score in scores),
    }


def _safe_buckets(
    scores: Sequence[P5CaseScore],
    key,
    *,
    minimum_bucket_size: int,
) -> dict[str, Any]:
    groups: dict[str, list[P5CaseScore]] = defaultdict(list)
    for score in scores:
        groups[str(key(score))].append(score)
    return {
        name: _aggregate(items)
        for name, items in sorted(groups.items())
        if len(items) >= minimum_bucket_size
    }


def score_external_blind_run_group(
    *,
    repo_root: Path,
    run_dir: Path,
    expected_bundle_sha256: str,
    bundle_path: Path | None = None,
    bundle_bytes: bytes | None = None,
    require_current_subject: bool = True,
    minimum_bucket_size: int = 3,
) -> dict[str, Any]:
    if require_current_subject:
        _require_active_contract()
    root = repo_root.resolve()
    inputs, seal = _validate_inputs_and_seal(root)
    expected_hash = _require_sha256(
        expected_bundle_sha256, "BLIND_BUNDLE_SHA256_REQUIRED"
    )
    if expected_hash != seal["external_bundle_sha256"]:
        raise P5BlindScoringError("BLIND_BUNDLE_SEAL_HASH_MISMATCH")
    manifest, outputs = _validate_run_group(
        repo_root=root,
        run_dir=run_dir.resolve(),
        inputs=inputs,
        require_current_subject=require_current_subject,
    )
    bundle = _read_external_bundle(
        repo_root=root,
        bundle_path=bundle_path,
        bundle_bytes=bundle_bytes,
        expected_sha256=expected_hash,
    )
    labels_by_id = _validate_bundle(bundle=bundle, inputs=inputs, seal=seal)
    input_by_id = {item["case_id"]: item for item in inputs}
    scores = [
        score_case(
            {**input_by_id[output.case_id], "oracle": labels_by_id[output.case_id]["oracle"]},
            output,
        )
        for output in outputs
    ]
    variants = {}
    for variant_id in VARIANT_IDS:
        items = [score for score in scores if score.variant_id == variant_id]
        variants[variant_id] = {
            "overall": _aggregate(items),
            "by_city": _safe_buckets(
                items, lambda score: score.city, minimum_bucket_size=minimum_bucket_size
            ),
            "by_input_kind": _safe_buckets(
                items, lambda score: score.input_kind, minimum_bucket_size=minimum_bucket_size
            ),
            "by_difficulty": _safe_buckets(
                items, lambda score: score.difficulty, minimum_bucket_size=minimum_bucket_size
            ),
            "by_fault_profile": _safe_buckets(
                items, lambda score: score.fault_profile_id, minimum_bucket_size=minimum_bucket_size
            ),
            "by_finding": _safe_buckets(
                items,
                lambda score: "+".join(score.required_obligations) or "NONE",
                minimum_bucket_size=minimum_bucket_size,
            ),
            "by_repair_outcome": _safe_buckets(
                items,
                lambda score: labels_by_id[score.case_id]["oracle"][
                    "expected_strategy_outcome"
                ],
                minimum_bucket_size=minimum_bucket_size,
            ),
        }
    core = variants["core_b"]["overall"]
    core_gate_checks = {
        "mean_score_gte_88": core["mean_score"] >= 88,
        "wrong_city_or_poi_zero": core["wrong_city_or_poi_count"] == 0,
        "hard_finding_miss_zero": core["hard_finding_miss_count"] == 0,
        "unknown_failure_zero": core["unknown_failure_count"] == 0,
        "candidate_receipt_failure_zero": core["candidate_receipt_failure_count"] == 0,
        "postcheck_failure_zero": core["postcheck_failure_count"] == 0,
        "replay_failure_zero": core["replay_failure_count"] == 0,
    }
    passed = all(core_gate_checks.values())
    return {
        "schema_version": "trip-check-p5-isolated-blind-score-v1",
        "status": "PASS" if passed else "REJECT",
        "decision": "ACCEPT_BLIND_SCORE" if passed else "REJECT",
        "evidence_class": "CONTROLLED_BLIND_ORACLE",
        "truth_provenance": "external_controlled_blind_oracle",
        "human_evidence": False,
        "minimum_bucket_size": minimum_bucket_size,
        "bindings": {
            "subject_commit": manifest["subject_commit"],
            "run_group_manifest_hash": manifest["manifest_hash"],
            "terminal_outputs_file_sha256": manifest["terminal_outputs_file_sha256"],
            "external_bundle_sha256": expected_hash,
            "labels_canonical_sha256": seal["labels_canonical_sha256"],
            "case_ids_sha256": seal["case_ids_sha256"],
        },
        "case_count": 90,
        "terminal_count": 270,
        "variant_metrics": variants,
        "core_gate_checks": core_gate_checks,
        "automated_proxy_judge": "NOT_RUN",
        "live_provider_evidence": False,
        "public_e2e_evidence": False,
    }


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
        "schema_version": "trip-check-p5-isolated-blind-score-v1",
        "status": "INVALID",
        "decision": "REJECT",
        "reason_code": reason_code,
        "human_evidence": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.trip_check_v1.p5.final_blind_scorer")
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    bundle_path = None if args.bundle == "-" else Path(args.bundle)
    bundle_bytes = sys.stdin.buffer.read() if args.bundle == "-" else None
    try:
        receipt = score_external_blind_run_group(
            repo_root=args.repo_root,
            run_dir=args.run_dir,
            expected_bundle_sha256=args.bundle_sha256,
            bundle_path=bundle_path,
            bundle_bytes=bundle_bytes,
        )
    except P5BlindScoringError as exc:
        receipt = _invalid_receipt(exc.reason_code)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2
    if args.output:
        _atomic_write(args.output.resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["decision"] == "ACCEPT_BLIND_SCORE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
