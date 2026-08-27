from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from app.trip_intake.models import TripIntakeExtraction, validate_extraction_evidence
from evals.trip_nlu_v2.scorer import score_predictions
from evals.trip_nlu_v2.validator import DatasetValidationError, _read_jsonl, validate_dataset


def run_gate(
    data_root: Path,
    predictions_path: Path,
    external_blind_labels: Path,
    run_spec_path: Path,
    manifest_path: Path | None = None,
) -> dict:
    selected_manifest_path = manifest_path or data_root / "manifest.json"
    validation_receipt = validate_dataset(
        data_root,
        external_blind_labels=external_blind_labels,
        manifest_path=selected_manifest_path,
    )
    inputs = _read_jsonl(data_root / "frozen_blind.inputs.jsonl")
    predictions = _read_jsonl(predictions_path)
    prediction_by_id = {item["case_id"]: item for item in predictions}
    if len(prediction_by_id) != 24:
        raise DatasetValidationError("prediction coverage must be exactly 24/24")
    for item in inputs:
        prediction = prediction_by_id.get(item["case_id"])
        if prediction is None:
            raise DatasetValidationError("prediction coverage must be exactly 24/24")
        extraction = TripIntakeExtraction.model_validate(prediction["prediction"])
        validate_extraction_evidence(extraction, {item["source_id"]: item["input_text"]})
    score = score_predictions(predictions_path, external_blind_labels)
    run_spec = json.loads(run_spec_path.read_text(encoding="utf-8"))
    manifest = json.loads(selected_manifest_path.read_text(encoding="utf-8"))
    expected_binding = {
        "schema_version": "trip-nlu-v2-run-spec-v1",
        "dataset_manifest_sha256": hashlib.sha256(
            selected_manifest_path.read_bytes()
        ).hexdigest(),
        "blind_label_sha256": validation_receipt["blind_label_commitment"],
        "product_outputs_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        "validator_sha256": manifest["code_bindings"]["validator_sha256"],
        "scorer_sha256": manifest["code_bindings"]["scorer_sha256"],
        "schema_sha256": manifest["code_bindings"]["schema_sha256"],
    }
    if not isinstance(run_spec, dict) or any(
        run_spec.get(key) != value for key, value in expected_binding.items()
    ):
        raise DatasetValidationError("RunSpec is not bound to dataset, labels, and product outputs")
    if not isinstance(run_spec.get("run_id"), str) or not run_spec["run_id"].strip():
        raise DatasetValidationError("RunSpec requires a non-empty run_id")
    model_binding = run_spec.get("model_binding")
    if not isinstance(model_binding, dict):
        raise DatasetValidationError("RunSpec requires model_binding")
    for field in ("model_name", "model_version"):
        if not isinstance(model_binding.get(field), str) or not model_binding[field].strip():
            raise DatasetValidationError(f"RunSpec model_binding requires {field}")
    for field in ("prompt_sha256", "schema_sha256", "config_sha256"):
        if not isinstance(model_binding.get(field), str) or not re.fullmatch(
            r"[0-9a-f]{64}", model_binding[field]
        ):
            raise DatasetValidationError(f"RunSpec model_binding requires {field}")
    if model_binding["schema_sha256"] != expected_binding["schema_sha256"]:
        raise DatasetValidationError("RunSpec model schema binding mismatch")
    run_spec_hash = hashlib.sha256(run_spec_path.read_bytes()).hexdigest()
    return {
        **score,
        "dataset_valid": validation_receipt["valid"],
        "evidence_span_validity": 1.0,
        "blind_label_commitment": validation_receipt["blind_label_commitment"],
        "dataset_manifest_sha256": expected_binding["dataset_manifest_sha256"],
        "product_outputs_sha256": expected_binding["product_outputs_sha256"],
        "run_id": run_spec["run_id"],
        "run_spec_sha256": run_spec_hash,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("data_root", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("external_blind_labels", type=Path)
    parser.add_argument("run_spec", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(
            run_gate(
                args.data_root,
                args.predictions,
                args.external_blind_labels,
                args.run_spec,
                manifest_path=args.manifest,
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
