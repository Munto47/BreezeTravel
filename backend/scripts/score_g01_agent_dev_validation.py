from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from evals.agent_gate_v1.path_security import (
    ArtifactSnapshot,
    discover_repository_root,
    read_external_snapshot,
    require_canonical_data_root,
    write_external_bytes_exclusive,
)
from evals.agent_gate_v1.authority import load_anchored_authority_policy
from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature
from evals.trip_text_cards_agent_v2.annotations import verify_agent_adjudication
from evals.trip_text_cards_agent_v2.contracts import (
    AgentPredictionRunEnvelope,
    AgentInferenceCaseOutputV2,
    InferenceRuntimeReceiptBundle,
)
from evals.trip_text_cards_agent_v2.split_loader import load_agent_split
from evals.trip_text_cards_v1.contracts import (
    TextCardPrediction,
    canonical_sha256,
    normalized_text,
)
from evals.trip_text_cards_v1.scorer import ScoringError, score_predictions


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_prediction_envelope(path: Path) -> AgentPredictionRunEnvelope:
    try:
        return AgentPredictionRunEnvelope.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScoringError(f"invalid prediction run envelope: {exc}") from exc


def _read_jsonl(content: bytes, model_type, label: str):
    values = []
    try:
        for line in content.decode("utf-8").splitlines():
            if line.strip():
                values.append(model_type.model_validate_json(line))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ScoringError(f"invalid {label}: {exc}") from exc
    if not values:
        raise ScoringError(f"{label} is empty")
    return values


def validate_prediction_run(
    *,
    prediction_path: Path,
    inference_outputs_path: Path,
    envelope_path: Path,
    repository_root: Path,
    split: str,
    expected_bindings: dict[str, str],
    inference_receipt_bundle_path: Path,
    model_binding_artifact_path: Path,
    prompt_artifact_path: Path,
    schema_artifact_path: Path,
    config_artifact_path: Path,
    require_live_inference_evidence: bool = False,
    artifact_snapshots: dict[str, ArtifactSnapshot] | None = None,
):
    def snapshot(label: str, path: Path) -> ArtifactSnapshot:
        if artifact_snapshots is None:
            return read_external_snapshot(path, repository_root)
        value = artifact_snapshots.get(label)
        if value is None or value.path.resolve(strict=True) != path.resolve(strict=True):
            raise ScoringError(f"{label} was not frozen in the supplied snapshot set")
        return value

    prediction_snapshot = snapshot("predictions", prediction_path)
    output_snapshot = snapshot("inference_outputs", inference_outputs_path)
    envelope_snapshot = snapshot("prediction_envelope", envelope_path)
    try:
        envelope = AgentPredictionRunEnvelope.model_validate_json(envelope_snapshot.content)
    except ValueError as exc:
        raise ScoringError(f"invalid prediction run envelope: {exc}") from exc
    if envelope.split != split:
        raise ScoringError("prediction run split binding mismatch")
    for field, expected in expected_bindings.items():
        if getattr(envelope, field) != expected:
            raise ScoringError(f"prediction run {field} binding mismatch")
    if envelope.predictions_sha256 != prediction_snapshot.sha256:
        raise ScoringError("prediction file hash does not match its run envelope")
    if envelope.inference_outputs_sha256 != output_snapshot.sha256:
        raise ScoringError("combined inference output hash does not match its run envelope")
    predictions = _read_jsonl(
        prediction_snapshot.content,
        TextCardPrediction,
        "prediction bundle",
    )
    inference_outputs = _read_jsonl(
        output_snapshot.content,
        AgentInferenceCaseOutputV2,
        "combined inference output bundle",
    )
    if [item.case_id for item in inference_outputs] != [item.case_id for item in predictions]:
        raise ScoringError("combined inference outputs must match prediction order")
    for combined, prediction in zip(inference_outputs, predictions, strict=True):
        if combined.text_card_prediction != prediction:
            raise ScoringError("combined inference prediction projection mismatch")
    if any(
        canonical_sha256(prediction.provider_binding)
        != expected_bindings["provider_binding_sha256"]
        for prediction in predictions
    ):
        raise ScoringError("prediction provider binding mismatch")
    destinations = [item.destination_prediction for item in inference_outputs]
    if envelope.destination_predictions != destinations:
        raise ScoringError("destination envelope is not a strict combined-output projection")

    artifact_bindings = {
        "model_binding_sha256": model_binding_artifact_path.resolve(strict=True),
        "prompt_sha256": prompt_artifact_path.resolve(strict=True),
        "schema_sha256": schema_artifact_path.resolve(strict=True),
        "config_sha256": config_artifact_path.resolve(strict=True),
    }
    for field, artifact in artifact_bindings.items():
        if _sha256(artifact) != expected_bindings[field]:
            raise ScoringError(f"prediction run {field} artifact hash mismatch")

    inference_snapshot = snapshot("inference_receipts", inference_receipt_bundle_path)
    if inference_snapshot.sha256 != expected_bindings["inference_receipt_bundle_sha256"]:
        raise ScoringError("inference receipt bundle artifact hash mismatch")
    try:
        inference = InferenceRuntimeReceiptBundle.model_validate_json(inference_snapshot.content)
    except ValueError as exc:
        raise ScoringError(f"invalid inference receipt bundle: {exc}") from exc
    inference_bindings = (
        inference.split,
        inference.candidate_commit,
        inference.candidate_tree,
        inference.model_binding_sha256,
        inference.prompt_sha256,
        inference.schema_sha256,
        inference.config_sha256,
        inference.provider_binding_sha256,
        inference.predictions_sha256,
        inference.inference_outputs_sha256,
    )
    expected_inference_bindings = (
        split,
        expected_bindings["candidate_commit"],
        expected_bindings["candidate_tree"],
        expected_bindings["model_binding_sha256"],
        expected_bindings["prompt_sha256"],
        expected_bindings["schema_sha256"],
        expected_bindings["config_sha256"],
        expected_bindings["provider_binding_sha256"],
        prediction_snapshot.sha256,
        output_snapshot.sha256,
    )
    if inference_bindings != expected_inference_bindings:
        raise ScoringError("inference receipt candidate or runtime binding mismatch")
    if require_live_inference_evidence and inference.execution_mode != "LIVE":
        raise ScoringError("live Qwen inference evidence is required for this lane")
    if inference.execution_mode == "LIVE":
        anchored = load_anchored_authority_policy(
            repository_root,
            expected_bindings["candidate_commit"],
        )
        if inference.authority_policy_sha256 != anchored.sha256:
            raise ScoringError("Qwen inference authority policy mismatch")
        if inference.authority_signature is None:
            raise ScoringError("Qwen inference authority signature is missing")
        verify_payload_signature(
            payload=unsigned_payload(inference),
            signature=inference.authority_signature,
            manifest=anchored.manifest,
            expected_role="QWEN_LIVE_EXPORTER",
        )
        exporter_path = inference.exporter_path
        if exporter_path is None or inference.exporter_sha256 is None:
            raise ScoringError("Qwen live exporter binding is incomplete")
        exporter = (repository_root / exporter_path).resolve(strict=True)
        if repository_root.resolve() not in exporter.parents:
            raise ScoringError("Qwen exporter must be frozen in the repository")
        if _sha256(exporter) != inference.exporter_sha256:
            raise ScoringError("Qwen exporter byte binding mismatch")
    effects = {item.case_id: item for item in inference.effects}
    if set(effects) != {item.case_id for item in predictions}:
        raise ScoringError("inference receipts must cover every prediction exactly once")
    combined_by_id = {item.case_id: item for item in inference_outputs}
    for prediction in predictions:
        effect = effects[prediction.case_id]
        combined = combined_by_id[prediction.case_id]
        if effect.input_sha256 != prediction.source_sha256:
            raise ScoringError("inference input receipt does not match prediction source")
        if effect.output_sha256 != canonical_sha256(combined.model_dump(mode="json")):
            raise ScoringError("inference effect does not bind the combined output")
    return envelope, predictions, inference, inference_outputs


def _validate_destination_predictions(
    *,
    envelope: AgentPredictionRunEnvelope,
    source_cases,
    reference_cases,
) -> dict[str, object]:
    expected_ids = [case.case_id for case in source_cases]
    if [item.case_id for item in envelope.destination_predictions] != expected_ids:
        raise ScoringError("destination predictions must cover the split in source order")
    reference_by_id = {case.case_id: case for case in reference_cases}
    exact = basis = evidence_total = evidence_valid = 0
    for source, prediction in zip(source_cases, envelope.destination_predictions, strict=True):
        reference = reference_by_id[source.case_id]
        exact += int(prediction.destination_name == reference.destination_name)
        basis += int(prediction.destination_basis == reference.destination_basis)
        if prediction.destination_basis != "EXPLICIT":
            continue
        evidence_total += 1
        start = prediction.evidence_span_start
        end = prediction.evidence_span_end
        raw = prediction.evidence_raw_text
        if start is None or end is None or raw is None:
            continue
        text = normalized_text(source.input_text)
        evidence_valid += int(text[start:end] == raw == prediction.destination_name)
    count = len(source_cases)
    return {
        "case_count": count,
        "exact_name_accuracy": exact / count if count else 0.0,
        "basis_accuracy": basis / count if count else 0.0,
        "explicit_evidence_validity": (
            evidence_valid / evidence_total if evidence_total else 1.0
        ),
        "explicit_evidence_count": evidence_total,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", required=True, choices=("dev", "validation"))
    parser.add_argument("--reference-a", required=True, type=Path)
    parser.add_argument("--reference-b", required=True, type=Path)
    parser.add_argument("--adjudication", required=True, type=Path)
    parser.add_argument("--provider-receipt-index", required=True, type=Path)
    parser.add_argument("--provider-runtime-receipts", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--inference-outputs", required=True, type=Path)
    parser.add_argument("--prediction-envelope", required=True, type=Path)
    parser.add_argument("--inference-receipt-bundle", required=True, type=Path)
    parser.add_argument("--model-binding-artifact", required=True, type=Path)
    parser.add_argument("--prompt-artifact", required=True, type=Path)
    parser.add_argument("--schema-artifact", required=True, type=Path)
    parser.add_argument("--config-artifact", required=True, type=Path)
    parser.add_argument("--expected-candidate-commit", required=True)
    parser.add_argument("--expected-candidate-tree", required=True)
    parser.add_argument("--expected-model-binding-sha256", required=True)
    parser.add_argument("--expected-prompt-sha256", required=True)
    parser.add_argument("--expected-schema-sha256", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--expected-provider-binding-sha256", required=True)
    parser.add_argument("--expected-inference-receipt-bundle-sha256", required=True)
    parser.add_argument("--expected-provider-runtime-bundle-sha256", required=True)
    parser.add_argument("--expected-database-export-receipt-sha256", required=True)
    parser.add_argument("--expected-provider-http-receipt-bundle-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--data-root", type=Path, default=Path("eval_data/trip_text_cards_v1"))
    args = parser.parse_args()

    repository_root = discover_repository_root(Path(__file__).parent)
    data_root = require_canonical_data_root(args.data_root, repository_root)
    source_cases, access_receipt = load_agent_split(data_root, args.split)
    adjudication, verification = verify_agent_adjudication(
        split=args.split,
        source_cases=source_cases,
        first_path=args.reference_a,
        second_path=args.reference_b,
        adjudication_path=args.adjudication,
        provider_receipt_index_path=args.provider_receipt_index,
        provider_runtime_receipt_bundle_path=args.provider_runtime_receipts,
        repository_root=repository_root,
        expected_candidate_commit=args.expected_candidate_commit,
        expected_candidate_tree=args.expected_candidate_tree,
        expected_provider_binding_sha256=args.expected_provider_binding_sha256,
        expected_runtime_receipt_bundle_sha256=(
            args.expected_provider_runtime_bundle_sha256
        ),
        expected_database_export_receipt_sha256=(
            args.expected_database_export_receipt_sha256
        ),
        expected_provider_http_receipt_bundle_sha256=(
            args.expected_provider_http_receipt_bundle_sha256
        ),
        require_live_provider_evidence=True,
    )

    prediction_path = args.predictions
    envelope_path = args.prediction_envelope
    expected_bindings = {
        "candidate_commit": args.expected_candidate_commit,
        "candidate_tree": args.expected_candidate_tree,
        "model_binding_sha256": args.expected_model_binding_sha256,
        "prompt_sha256": args.expected_prompt_sha256,
        "schema_sha256": args.expected_schema_sha256,
        "config_sha256": args.expected_config_sha256,
        "provider_binding_sha256": args.expected_provider_binding_sha256,
        "inference_receipt_bundle_sha256": (
            args.expected_inference_receipt_bundle_sha256
        ),
    }
    envelope, predictions, inference, _inference_outputs = validate_prediction_run(
        prediction_path=prediction_path,
        inference_outputs_path=args.inference_outputs,
        envelope_path=envelope_path,
        repository_root=repository_root,
        split=args.split,
        expected_bindings=expected_bindings,
        inference_receipt_bundle_path=args.inference_receipt_bundle,
        model_binding_artifact_path=args.model_binding_artifact,
        prompt_artifact_path=args.prompt_artifact,
        schema_artifact_path=args.schema_artifact,
        config_artifact_path=args.config_artifact,
        require_live_inference_evidence=True,
    )
    score = score_predictions(
        source_cases=source_cases,
        gold_cases=adjudication.agent_reference_cases,
        predictions=predictions,
    )
    score["estimated_confirmation_required_count"] = score.pop(
        "human_confirmation_count"
    )
    score["estimated_confirmation_required_count"]["evidence_basis"] = (
        "AUTOMATED_ESTIMATE"
    )
    score["destination"] = _validate_destination_predictions(
        envelope=envelope,
        source_cases=source_cases,
        reference_cases=adjudication.agent_reference_cases,
    )
    score["candidate_auto_selected_minimum_met"] = (
        args.split == "dev" or score["auto_match"]["denominator"] >= 50
    )

    receipt = {
        "schema_version": "g01-text-card-agent-scored-receipt-v2",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "split": args.split,
        "candidate_commit": args.expected_candidate_commit,
        "candidate_tree": args.expected_candidate_tree,
        "prediction_envelope_sha256": _sha256(envelope_path.resolve(strict=True)),
        "prediction_bindings": expected_bindings,
        "inference_effect_count": len(inference.effects),
        "agent_adjudication": verification,
        "score": score,
        "input_access": access_receipt.__dict__,
        "blind_inputs_read": access_receipt.blind_inputs_read,
        "blind_truth_read": access_receipt.blind_truth_read,
        "human_usability_status": "NOT_RUN",
        "production_status": "NOT_RUN",
        "evidence_levels": [
            "MULTI_AGENT_SIMULATED_REVIEW",
            "LIVE_PROVIDER_EVIDENCE",
        ],
        "limitations": ["PROCESS_ISOLATION_NOT_ORGANIZATIONAL_INDEPENDENCE"],
        "gate_claim": "NOT_RUN" if args.split == "dev" else "VALIDATION_ONLY",
    }
    output_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    write_external_bytes_exclusive(args.output, output_bytes, repository_root)
    print(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
