"""Isolated, fail-closed scorer for the frozen-blind promotion lane.

This module is intentionally not imported by ``evals.continuous``.  Product
runners freeze inputs and outputs without loading blind labels.  A separate
process may call this scorer only after it receives an external bundle and an
independently supplied SHA-256 commitment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from evals.dual_entry_scorer import (
    METRIC_NAMES,
    aggregate_metric_scores,
    builder_metric_actuals,
    evaluate_metric_thresholds,
    score_metric_oracles,
)


_SHA256_LENGTH = 64
_BUNDLE_SCHEMA_VERSION = "dual-entry-blind-label-bundle-v1"
_SEAL_SCHEMA_VERSION = "dual-entry-sealed-label-manifest-v1"


class BlindScoringError(RuntimeError):
    """A stable fail-closed error that is safe to expose in gate receipts."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class _DatasetBinding:
    dataset_content_sha256: str
    manifest_sha256: str
    case_ids_sha256: str
    case_ids: tuple[str, ...]
    labels_canonical_sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_bytes(value))


def _canonical_labels_sha256(labels: Sequence[Mapping[str, Any]]) -> str:
    ordered = sorted(labels, key=lambda row: str(row.get("case_id", "")))
    payload = b"".join(_canonical_bytes(row) + b"\n" for row in ordered)
    return _sha256_bytes(payload)


def _load_json(path: Path, reason_code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BlindScoringError(reason_code, "required JSON artifact is unreadable") from exc
    if not isinstance(value, dict):
        raise BlindScoringError(reason_code, "required JSON artifact must be an object")
    return value


def _load_jsonl(path: Path, reason_code: str) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BlindScoringError(reason_code, "required JSONL artifact is unreadable") from exc
    rows: list[dict[str, Any]] = []
    try:
        for line in lines:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("row is not an object")
            rows.append(row)
    except (json.JSONDecodeError, ValueError) as exc:
        raise BlindScoringError(reason_code, "required JSONL artifact is malformed") from exc
    return rows


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _repo_file(repo_root: Path, relative_path: Any, reason_code: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise BlindScoringError(reason_code, "repository-relative path is missing")
    path = (repo_root / relative_path).resolve()
    if not _inside(path, repo_root):
        raise BlindScoringError(reason_code, "repository-relative path escapes the repository")
    return path


def _require_hash(value: Any, reason_code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BlindScoringError(reason_code, "a concrete lowercase SHA-256 is required")
    return value


def _read_bundle(
    *,
    repo_root: Path,
    expected_bundle_sha256: str,
    bundle_path: str | Path | None,
    bundle_bytes: bytes | None,
) -> tuple[dict[str, Any], str]:
    expected = _require_hash(expected_bundle_sha256, "BLIND_BUNDLE_SHA256_REQUIRED")
    if (bundle_path is None) == (bundle_bytes is None):
        raise BlindScoringError(
            "BLIND_BUNDLE_SOURCE_REQUIRED",
            "provide exactly one explicit external bundle path or isolated-process input",
        )
    origin = "isolated_process_input"
    payload = bundle_bytes
    if bundle_path is not None:
        resolved = Path(bundle_path).resolve()
        if _inside(resolved, repo_root):
            raise BlindScoringError(
                "BLIND_BUNDLE_PATH_INSIDE_REPOSITORY",
                "blind scoring payloads must never be read from the repository",
            )
        if not resolved.is_file():
            raise BlindScoringError("BLIND_BUNDLE_MISSING", "external blind bundle is missing")
        try:
            payload = resolved.read_bytes()
        except OSError as exc:
            raise BlindScoringError("BLIND_BUNDLE_READ_FAILED", "external blind bundle is unreadable") from exc
        origin = "external_bundle_path"
    assert payload is not None
    actual = _sha256_bytes(payload)
    if actual != expected:
        raise BlindScoringError("BLIND_BUNDLE_SHA256_MISMATCH", "external blind bundle bytes were changed")
    try:
        bundle = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BlindScoringError("BLIND_BUNDLE_INVALID_JSON", "external blind bundle is invalid JSON") from exc
    if not isinstance(bundle, dict):
        raise BlindScoringError("BLIND_BUNDLE_NOT_OBJECT", "external blind bundle must be an object")
    return bundle, origin


def _selected_dataset_binding(repo_root: Path, run_spec: Mapping[str, Any]) -> _DatasetBinding:
    dataset = run_spec.get("dataset")
    execution = run_spec.get("execution")
    if not isinstance(dataset, dict) or not isinstance(execution, dict):
        raise BlindScoringError("RUN_SPEC_DATASET_BINDING_MISSING", "RunSpec dataset binding is missing")
    if dataset.get("splits") != ["frozen_blind"] or dataset.get("label_access") != "isolated_scorer_only":
        raise BlindScoringError("RUN_SPEC_NOT_FROZEN_BLIND", "RunSpec is not the isolated frozen-blind lane")
    manifest_path = _repo_file(
        repo_root,
        dataset.get("manifest"),
        "RUN_SPEC_DATASET_MANIFEST_PATH_INVALID",
    )
    if not manifest_path.is_file():
        raise BlindScoringError("RUN_SPEC_DATASET_MANIFEST_MISSING", "dataset manifest is missing")
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = _sha256_bytes(manifest_bytes)
    if manifest_sha256 != _require_hash(dataset.get("manifest_sha256"), "RUN_SPEC_MANIFEST_SHA256_INVALID"):
        raise BlindScoringError("RUN_SPEC_MANIFEST_SHA256_MISMATCH", "dataset manifest changed after the run")
    manifest = _load_json(manifest_path, "RUN_SPEC_DATASET_MANIFEST_INVALID")
    entries = {
        entry.get("split"): entry
        for entry in manifest.get("files", [])
        if isinstance(entry, dict) and isinstance(entry.get("split"), str)
    }
    entry = entries.get("frozen_blind")
    if not isinstance(entry, dict):
        raise BlindScoringError("BLIND_DATASET_ENTRY_MISSING", "frozen_blind dataset entry is missing")
    if "labels" in entry:
        raise BlindScoringError(
            "REPOSITORY_BLIND_LABEL_PAYLOAD_EXPOSED",
            "frozen_blind manifest must not expose a repository label path",
        )

    input_name = entry.get("inputs")
    input_path = _repo_file(repo_root, str(manifest_path.parent.relative_to(repo_root) / str(input_name)), "BLIND_INPUT_PATH_INVALID")
    input_rows = _load_jsonl(input_path, "BLIND_INPUTS_INVALID")
    declared_count = entry.get("case_count")
    if declared_count != len(input_rows):
        raise BlindScoringError("BLIND_INPUT_COUNT_MISMATCH", "blind input count does not match manifest")
    case_ids = [row.get("case_id") for row in input_rows]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids) or len(set(case_ids)) != len(case_ids):
        raise BlindScoringError("BLIND_INPUT_CASE_IDS_INVALID", "blind input case IDs are missing or duplicated")
    sorted_case_ids = tuple(sorted(str(case_id) for case_id in case_ids))
    case_ids_sha256 = _sha256_json(list(sorted_case_ids))
    if case_ids_sha256 != _require_hash(dataset.get("case_ids_sha256"), "RUN_SPEC_CASE_IDS_SHA256_INVALID"):
        raise BlindScoringError("RUN_SPEC_CASE_IDS_SHA256_MISMATCH", "selected blind case IDs changed after the run")

    seal_name = entry.get("labels_seal")
    seal_path = _repo_file(
        repo_root,
        str(manifest_path.parent.relative_to(repo_root) / str(seal_name)),
        "BLIND_LABEL_SEAL_PATH_INVALID",
    )
    if not seal_path.is_file():
        raise BlindScoringError("BLIND_LABEL_SEAL_MISSING", "checked-in blind label seal is missing")
    seal_bytes = seal_path.read_bytes()
    seal_sha256 = _sha256_bytes(seal_bytes)
    if seal_sha256 != _require_hash(entry.get("labels_seal_sha256"), "BLIND_LABEL_SEAL_SHA256_INVALID"):
        raise BlindScoringError("BLIND_LABEL_SEAL_SHA256_MISMATCH", "checked-in blind label seal was changed")
    seal = _load_json(seal_path, "BLIND_LABEL_SEAL_INVALID")
    if (
        seal.get("schema_version") != _SEAL_SCHEMA_VERSION
        or seal.get("split") != "frozen_blind"
        or seal.get("scoring_payload_present") is not False
        or seal.get("external_bundle_required") is not True
    ):
        raise BlindScoringError("BLIND_LABEL_SEAL_CONTRACT_INVALID", "checked-in seal is not metadata-only")
    if seal.get("case_count") != len(sorted_case_ids) or seal.get("case_ids_sha256") != case_ids_sha256:
        raise BlindScoringError("BLIND_LABEL_SEAL_CASE_BINDING_MISMATCH", "checked-in seal does not bind selected cases")
    labels_canonical_sha256 = _require_hash(
        seal.get("labels_canonical_sha256"),
        "BLIND_LABEL_COMMITMENT_INVALID",
    )

    input_hashes = {str(input_name): _sha256_bytes(input_path.read_bytes())}
    seal_hashes = {str(seal_name): seal_sha256}
    dataset_content_sha256 = _sha256_json(
        {
            "manifest_sha256": manifest_sha256,
            "input_files": input_hashes,
            "sealed_label_manifests": seal_hashes,
        }
    )
    bindings = execution.get("bindings")
    if not isinstance(bindings, dict):
        raise BlindScoringError("RUN_SPEC_EXECUTION_BINDINGS_MISSING", "resolved RunSpec bindings are missing")
    if bindings.get("manifest_sha256") != manifest_sha256:
        raise BlindScoringError("RUN_SPEC_MANIFEST_BINDING_MISMATCH", "RunSpec execution manifest binding differs")
    if bindings.get("case_ids_sha256") != case_ids_sha256:
        raise BlindScoringError("RUN_SPEC_CASE_BINDING_MISMATCH", "RunSpec execution case binding differs")
    if bindings.get("dataset_content_sha256") != dataset_content_sha256:
        raise BlindScoringError("RUN_SPEC_DATASET_HASH_MISMATCH", "RunSpec dataset hash differs from current bytes")
    return _DatasetBinding(
        dataset_content_sha256=dataset_content_sha256,
        manifest_sha256=manifest_sha256,
        case_ids_sha256=case_ids_sha256,
        case_ids=sorted_case_ids,
        labels_canonical_sha256=labels_canonical_sha256,
    )


def _product_actuals(output: Mapping[str, Any]) -> Mapping[str, Any]:
    if output.get("schema_version") == "continuous-builder-product-output-v1":
        return builder_metric_actuals(output)
    actuals = output.get("metric_actuals")
    if isinstance(actuals, dict):
        return actuals
    raise BlindScoringError("PRODUCT_METRIC_ACTUALS_MISSING", "product output has no scoreable actuals")


def _public_gate_receipt(value: Any) -> Any:
    """Remove row-level blind feedback while preserving aggregate gate evidence.

    Publishing failed case IDs or per-case scores would let ordinary
    development runs tune against the frozen set even though the label payload
    itself stayed outside the repository.  The isolated process may use those
    values transiently to decide the gate, but its exported receipt is
    aggregate-only.
    """

    if isinstance(value, dict):
        return {
            key: _public_gate_receipt(item)
            for key, item in value.items()
            if not key.endswith("_case_ids") and key != "selected_case_ids"
        }
    if isinstance(value, list):
        return [_public_gate_receipt(item) for item in value]
    return value


def score_external_blind_bundle(
    run_dir: str | Path,
    *,
    repo_root: str | Path,
    expected_bundle_sha256: str,
    bundle_path: str | Path | None = None,
    bundle_bytes: bytes | None = None,
) -> dict[str, Any]:
    """Verify every binding, then score without exposing blind payloads to runners."""

    root = Path(repo_root).resolve()
    run_path = Path(run_dir).resolve()
    run_spec_path = run_path / "run_spec.json"
    outputs_path = run_path / "product_outputs.jsonl"
    run_spec = _load_json(run_spec_path, "RUN_SPEC_ARTIFACT_INVALID")
    run_id = run_spec.get("run_id")
    if not isinstance(run_id, str) or not run_id or run_path.name != run_id:
        raise BlindScoringError("RUN_ID_ARTIFACT_BINDING_MISMATCH", "run directory and RunSpec run_id differ")
    if run_spec.get("lane") != "release_blind" or run_spec.get("purpose") != "promotion":
        raise BlindScoringError("RUN_SPEC_NOT_RELEASE_BLIND", "only a release_blind promotion run can be scored")
    dataset_binding = _selected_dataset_binding(root, run_spec)
    run_spec_sha256 = _sha256_bytes(run_spec_path.read_bytes())
    if not outputs_path.is_file():
        raise BlindScoringError("PRODUCT_OUTPUTS_MISSING", "frozen product outputs are missing")
    product_outputs_bytes = outputs_path.read_bytes()
    product_outputs_sha256 = _sha256_bytes(product_outputs_bytes)
    outputs = _load_jsonl(outputs_path, "PRODUCT_OUTPUTS_INVALID")

    bundle, bundle_origin = _read_bundle(
        repo_root=root,
        expected_bundle_sha256=expected_bundle_sha256,
        bundle_path=bundle_path,
        bundle_bytes=bundle_bytes,
    )
    if bundle.get("schema_version") != _BUNDLE_SCHEMA_VERSION:
        raise BlindScoringError("BLIND_BUNDLE_SCHEMA_UNSUPPORTED", "blind bundle schema is unsupported")
    if set(bundle) != {"schema_version", "evidence_class", "human_evidence", "run_binding", "labels"}:
        raise BlindScoringError("BLIND_BUNDLE_FIELDS_INVALID", "blind bundle contains missing or unregistered fields")
    if bundle.get("evidence_class") != "controlled_blind_oracle" or bundle.get("human_evidence") is not False:
        raise BlindScoringError("BLIND_BUNDLE_TRUTH_BOUNDARY_INVALID", "blind bundle must not claim human evidence")
    binding = bundle.get("run_binding")
    if not isinstance(binding, dict):
        raise BlindScoringError("BLIND_BUNDLE_RUN_BINDING_MISSING", "blind bundle run binding is missing")
    expected_bindings = {
        "run_id": run_id,
        "run_spec_sha256": run_spec_sha256,
        "dataset_content_sha256": dataset_binding.dataset_content_sha256,
        "manifest_sha256": dataset_binding.manifest_sha256,
        "case_ids_sha256": dataset_binding.case_ids_sha256,
        "product_outputs_sha256": product_outputs_sha256,
    }
    if set(binding) != set(expected_bindings):
        raise BlindScoringError(
            "BLIND_BUNDLE_RUN_BINDING_FIELDS_INVALID",
            "blind bundle run binding contains missing or unregistered fields",
        )
    for key, expected in expected_bindings.items():
        if binding.get(key) != expected:
            raise BlindScoringError(
                f"BLIND_BUNDLE_{key.upper()}_MISMATCH",
                f"blind bundle is not bound to the frozen run artifact: {key}",
            )

    labels = bundle.get("labels")
    if not isinstance(labels, list) or any(not isinstance(row, dict) for row in labels):
        raise BlindScoringError("BLIND_BUNDLE_LABELS_INVALID", "blind bundle labels must be an object array")
    labels_by_id = {row.get("case_id"): row for row in labels}
    label_ids = [row.get("case_id") for row in labels]
    if any(not isinstance(case_id, str) or not case_id for case_id in label_ids) or len(labels_by_id) != len(labels):
        raise BlindScoringError("BLIND_BUNDLE_LABEL_CASE_IDS_INVALID", "blind label case IDs are missing or duplicated")
    if tuple(sorted(str(case_id) for case_id in label_ids)) != dataset_binding.case_ids:
        raise BlindScoringError("BLIND_BUNDLE_LABEL_CASE_SET_MISMATCH", "blind labels do not match the selected case set")
    allowed_label_fields = {
        "schema_version",
        "case_id",
        "deterministic_truth",
        "judge_rubric",
        "metric_oracles",
        "gate_assertions",
    }
    for label in labels:
        if (
            label.get("schema_version") != "dual-entry-label-v1"
            or not set(label) <= allowed_label_fields
            or not isinstance(label.get("deterministic_truth"), dict)
            or not isinstance(label.get("gate_assertions"), list)
            or not isinstance(label.get("metric_oracles"), dict)
            or set(label["metric_oracles"]) != set(METRIC_NAMES)
        ):
            raise BlindScoringError("BLIND_BUNDLE_LABEL_SCHEMA_INVALID", "blind label contract is malformed")
    if _canonical_labels_sha256(labels) != dataset_binding.labels_canonical_sha256:
        raise BlindScoringError("BLIND_LABEL_COMMITMENT_MISMATCH", "blind label payload differs from the checked-in seal")

    output_ids = [row.get("case_id") for row in outputs]
    outputs_by_id = {row.get("case_id"): row for row in outputs}
    if (
        any(not isinstance(case_id, str) or not case_id for case_id in output_ids)
        or len(outputs_by_id) != len(outputs)
        or tuple(sorted(str(case_id) for case_id in output_ids)) != dataset_binding.case_ids
    ):
        raise BlindScoringError("PRODUCT_OUTPUT_CASE_SET_MISMATCH", "product outputs do not match the selected blind cases")

    case_scores = [
        score_metric_oracles(labels_by_id[case_id], _product_actuals(outputs_by_id[case_id]))
        for case_id in dataset_binding.case_ids
    ]
    aggregate = aggregate_metric_scores(case_scores)
    threshold_gates = evaluate_metric_thresholds(aggregate, run_spec.get("thresholds", {}))
    invalid_case_ids = [score["case_id"] for score in case_scores if score.get("status") != "SCORED"]
    passed = not invalid_case_ids and all(gate.get("status") == "PASS" for gate in threshold_gates)
    return {
        "schema_version": "dual-entry-isolated-blind-score-v1",
        "status": "PASS" if passed else "INVALID",
        "decision": "ACCEPT_BLIND_SCORE" if passed else "REJECT",
        "run_id": run_id,
        "bundle_origin": bundle_origin,
        "truth_provenance": "controlled_blind_oracle",
        "human_evidence": False,
        "bindings": {
            **expected_bindings,
            "bundle_sha256": expected_bundle_sha256,
            "labels_canonical_sha256": dataset_binding.labels_canonical_sha256,
        },
        "scored_case_count": len(case_scores),
        "invalid_case_count": len(invalid_case_ids),
        "aggregate": _public_gate_receipt(aggregate),
        "threshold_gates": _public_gate_receipt(threshold_gates),
    }


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(_canonical_bytes(value) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.final_blind_scorer")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument(
        "--bundle",
        default=os.environ.get("BREEZE_BLIND_BUNDLE_PATH"),
        help="external path, or '-' to read the bundle from this isolated process' stdin",
    )
    parser.add_argument(
        "--bundle-sha256",
        default=os.environ.get("BREEZE_BLIND_BUNDLE_SHA256"),
        help="independently supplied bundle byte hash",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bundle_path: str | Path | None = args.bundle
    bundle_bytes: bytes | None = None
    if bundle_path == "-":
        bundle_path = None
        bundle_bytes = sys.stdin.buffer.read()
    try:
        receipt = score_external_blind_bundle(
            args.run_dir,
            repo_root=args.repo_root,
            expected_bundle_sha256=args.bundle_sha256,
            bundle_path=bundle_path,
            bundle_bytes=bundle_bytes,
        )
    except BlindScoringError as exc:
        receipt = {
            "schema_version": "dual-entry-isolated-blind-score-v1",
            "status": "INVALID",
            "decision": "REJECT",
            "reason_code": exc.reason_code,
        }
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 2
    if args.output is not None:
        _atomic_write_json(args.output.resolve(), receipt)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0 if receipt["decision"] == "ACCEPT_BLIND_SCORE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
