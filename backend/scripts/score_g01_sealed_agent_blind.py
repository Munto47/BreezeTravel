from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from evals.agent_gate_v1.contracts import (
    BLIND_ERROR_CATEGORY_ORDER,
    CurrentGoalBinding,
    SealedAgentBlindReceipt,
    SealedAgentBlindThresholds,
)
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.trip_text_cards_agent_v2.annotations import (
    agent_input_bundle_sha256,
    validate_provider_receipt_assets,
)
from evals.trip_text_cards_agent_v2.contracts import (
    ProviderRuntimeReceiptBundle,
    SealedAgentReferenceBundle,
    validate_agent_case_annotation,
)
from evals.trip_text_cards_v1.contracts import (
    TextCardInputCase,
    canonical_sha256,
)
from evals.trip_text_cards_v1.scorer import ScoringError, score_predictions
from scripts.score_g01_agent_dev_validation import (
    _validate_destination_predictions,
    validate_prediction_run,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
G01_ROOT = REPOSITORY_ROOT / "backend/eval_data/trip_text_cards_agent_v2"
FROZEN_INPUTS = (
    REPOSITORY_ROOT
    / "backend/eval_data/trip_text_cards_v1/frozen_blind.inputs.jsonl"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise ScoringError("sealed scorer candidate Git binding failed")
    return result.stdout.strip()


def _read_jsonl(path: Path, model_type, label: str):
    values = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                values.append(model_type.model_validate_json(line))
    except (OSError, ValueError) as exc:
        raise ScoringError(f"invalid sealed {label}: {exc}") from exc
    if not values:
        raise ScoringError(f"sealed {label} is empty")
    return values


def _flatten_score(score: dict[str, object], predictions) -> dict[str, float | int | bool]:
    executable = score["executable_mentions"]
    day = score["day_assignment"]
    auto = score["auto_match"]
    deep = score["deep_city_auto_match"]
    confirmation = score["estimated_confirmation_required_count"]
    other_city_confirmation = score["other_city_confirmation_required_count"]
    destination = score["destination"]
    projection = score["public_projection"]
    latency = score["public_api_latency"]
    role_metrics = score["role_metrics"]
    assert isinstance(executable, dict)
    assert isinstance(day, dict)
    assert isinstance(auto, dict)
    assert isinstance(deep, dict)
    assert isinstance(confirmation, dict)
    assert isinstance(other_city_confirmation, dict)
    assert isinstance(destination, dict)
    assert isinstance(projection, dict)
    assert isinstance(latency, dict)
    assert isinstance(role_metrics, dict)
    role_errors = sum(
        int(value["fp"]) + int(value["fn"])
        for value in role_metrics.values()
        if isinstance(value, dict)
    )
    day_errors = int(day["fp"]) + int(day["fn"])
    latency_violations = sum(
        int(
            (item.first_progress_ms is not None and item.first_progress_ms > 500)
            or (item.cards_ready_ms is not None and item.cards_ready_ms > 8000)
        )
        for item in predictions
        if item.measurement_scope == "PUBLIC_API_BROWSER"
    )
    severe = int(score["severe_wrong_auto_match_count"])
    wrong_city = int(score["wrong_city_auto_match_count"])
    wrong_category = int(score["wrong_category_auto_match_count"])
    provider_resolution_errors = max(0, severe - wrong_city - wrong_category)
    cards_p95 = latency.get("cards_ready_p95_ms")
    confirmation_population_valid = confirmation.get("population") == "DEEP_CITY"
    other_city_population_valid = (
        other_city_confirmation.get("population") == "OTHER_CITY"
    )
    other_city_auto_matches = int(other_city_confirmation.get("auto_match_count", -1))
    other_city_total = int(other_city_confirmation.get("total", -1))
    other_city_gold = int(
        other_city_confirmation.get("gold_executable_count", -2)
    )
    other_city_case_count = int(other_city_confirmation.get("case_count", 0))
    other_city_scope_errors = sum(
        (
            not confirmation_population_valid,
            not other_city_population_valid,
            other_city_case_count <= 0,
            other_city_total != other_city_gold,
        )
    )
    return {
        "forbidden_content_as_place_count": int(
            score["forbidden_content_as_place_count"]
        ),
        "severe_wrong_auto_match_count": severe,
        "wrong_city_auto_match_count": wrong_city,
        "wrong_category_auto_match_count": wrong_category,
        "auto_match.precision": float(auto["precision"]),
        "auto_match.denominator": int(auto["denominator"]),
        "executable_mentions.precision": float(executable["precision"]),
        "executable_mentions.recall": float(executable["recall"]),
        "executable_mentions.fp": int(executable["fp"]),
        "executable_mentions.fn": int(executable["fn"]),
        "day_assignment.f1": float(day["f1"]),
        "day_assignment.error_count": day_errors,
        "role_macro_f1": float(score["role_macro_f1"]),
        "deep_city_auto_match.coverage": float(deep["coverage"]),
        "estimated_confirmation_required_count.median": float(
            confirmation["median"]
        ),
        "estimated_confirmation_required_count.p90": float(confirmation["p90"]),
        "estimated_confirmation_required_count.population_is_deep_city": (
            confirmation_population_valid
        ),
        "other_city.case_count": other_city_case_count,
        "other_city.gold_executable_count": other_city_gold,
        "other_city.auto_match_count": other_city_auto_matches,
        "other_city.confirmation_required_count.total": other_city_total,
        "other_city.confirmation_required_count.median": float(
            other_city_confirmation["median"]
        ),
        "other_city.confirmation_required_count.p90": float(
            other_city_confirmation["p90"]
        ),
        "other_city.confirmation_required_count.max": int(
            other_city_confirmation["max"]
        ),
        "other_city.population_is_other_city": other_city_population_valid,
        "role_classification.error_count": role_errors,
        "provider_resolution.error_count": provider_resolution_errors,
        "evidence_span_validity": float(score["evidence_span_validity"]),
        "destination.exact_name_accuracy": float(destination["exact_name_accuracy"]),
        "destination.basis_accuracy": float(destination["basis_accuracy"]),
        "destination.explicit_evidence_validity": float(
            destination["explicit_evidence_validity"]
        ),
        "public_projection.forbidden_key_hits": int(
            projection["forbidden_key_hits"]
        ),
        "public_projection.full_source_leak_hits": int(
            projection["full_source_leak_hits"]
        ),
        "public_api_latency.cards_ready_p95_ms": (
            float(cards_p95) if cards_p95 is not None else 1_000_000_000.0
        ),
        "latency.violation_count": latency_violations,
        "other_aggregated_error_count": (
            other_city_auto_matches + other_city_scope_errors
        ),
    }


def _thresholds_pass(
    metrics: dict[str, float | int | bool],
    thresholds: SealedAgentBlindThresholds,
) -> bool:
    if (
        metrics.get(
            "estimated_confirmation_required_count.population_is_deep_city"
        )
        is not True
        or metrics.get("other_city.population_is_other_city") is not True
        or int(metrics.get("other_city.case_count", 0)) <= 0
        or int(metrics.get("other_city.auto_match_count", -1)) != 0
        or int(metrics.get("other_city.confirmation_required_count.total", -1))
        != int(metrics.get("other_city.gold_executable_count", -2))
        or int(metrics.get("other_aggregated_error_count", -1)) != 0
    ):
        return False
    for condition in thresholds.conditions:
        value = metrics.get(condition.metric)
        if value is None:
            return False
        if condition.operator == "EQ" and value != condition.value:
            return False
        if condition.operator == "GE" and float(value) < float(condition.value):
            return False
        if condition.operator == "LE" and float(value) > float(condition.value):
            return False
    return set(thresholds.required_metric_names).issubset(metrics)


def _taxonomy_counts(metrics: dict[str, float | int | bool]) -> dict[str, int]:
    return {
        "WRONG_CITY": int(metrics["wrong_city_auto_match_count"]),
        "WRONG_CATEGORY": int(metrics["wrong_category_auto_match_count"]),
        "NON_ATOMIC_PLACE": int(metrics["forbidden_content_as_place_count"]),
        "MENTION_FALSE_POSITIVE": int(metrics["executable_mentions.fp"]),
        "MENTION_FALSE_NEGATIVE": int(metrics["executable_mentions.fn"]),
        "DAY_ASSIGNMENT": int(metrics["day_assignment.error_count"]),
        "ROLE_CLASSIFICATION": int(metrics["role_classification.error_count"]),
        "PROVIDER_RESOLUTION": int(metrics["provider_resolution.error_count"]),
        "PUBLIC_LEAK": int(metrics["public_projection.forbidden_key_hits"])
        + int(metrics["public_projection.full_source_leak_hits"]),
        "LATENCY": int(metrics["latency.violation_count"]),
        "OTHER_AGGREGATED": int(metrics["other_aggregated_error_count"]),
    }


def tranche_commitment_sha256(
    *,
    input_bundle_sha256: str,
    case_set_commitment_sha256: str,
    truth_bundle_commitment: dict[str, str],
) -> str:
    """Frozen public formula used before a one-shot tranche is minted."""

    return canonical_sha256(
        {
            "input_bundle_sha256": input_bundle_sha256,
            "case_set_commitment_sha256": case_set_commitment_sha256,
            "truth_bundle_commitment": truth_bundle_commitment,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--truth", required=True, type=Path)
    parser.add_argument("--predictions", required=True, type=Path)
    parser.add_argument("--inference-outputs", required=True, type=Path)
    parser.add_argument("--prediction-envelope", required=True, type=Path)
    parser.add_argument("--inference-receipts", required=True, type=Path)
    parser.add_argument("--provider-receipt-index", required=True, type=Path)
    parser.add_argument("--provider-runtime-receipts", required=True, type=Path)
    parser.add_argument("--score-input-output", required=True, type=Path)
    parser.add_argument("--score-receipt-output", required=True, type=Path)
    parser.add_argument("--receipt-output", type=Path)
    parser.add_argument("--candidate-commit")
    parser.add_argument("--candidate-tree")
    parser.add_argument("--custodian-task-id")
    args = parser.parse_args()
    binding = CurrentGoalBinding.model_validate_json(
        (REPOSITORY_ROOT / "docs/governance/current_goal_binding.json").read_bytes()
    )
    if binding.gate_profile == "CORE_AGENT_GATE":
        required = {
            "receipt-output": args.receipt_output,
            "candidate-commit": args.candidate_commit,
            "candidate-tree": args.candidate_tree,
            "custodian-task-id": args.custodian_task_id,
        }
        missing = [name for name, value in required.items() if value is None]
        if missing:
            parser.error(f"CORE sealed scoring requires: {', '.join(missing)}")
        if binding.goal_sequence > 6:
            parser.error("CORE sealed scoring is restricted to G01-G06")
        if _git("rev-parse", "HEAD") != args.candidate_commit or _git(
            "show", "-s", "--format=%T", "HEAD"
        ) != args.candidate_tree:
            parser.error("CORE sealed scoring candidate does not match the clean checkout")
        if args.inputs.resolve(strict=True) != FROZEN_INPUTS.resolve(strict=True):
            parser.error("CORE sealed scoring requires the canonical frozen inputs")
        source_cases = _read_jsonl(args.inputs, TextCardInputCase, "input bundle")
        if len(source_cases) != 18 or any(
            case.split != "frozen_blind" for case in source_cases
        ):
            parser.error("CORE sealed scoring requires exactly 18 frozen blind inputs")
        truth_snapshot = read_external_snapshot(args.truth, REPOSITORY_ROOT)
        try:
            truth = SealedAgentReferenceBundle.model_validate_json(
                truth_snapshot.content
            )
        except ValueError as exc:
            parser.error(f"invalid CORE sealed reference: {exc}")
        if (
            truth.human_evidence
            or truth.attestation.subject_commit != args.candidate_commit
            or truth.attestation.subject_tree != args.candidate_tree
        ):
            parser.error("CORE sealed reference candidate or evidence boundary mismatch")

        provider_snapshot = read_external_snapshot(
            args.provider_runtime_receipts,
            REPOSITORY_ROOT,
        )
        try:
            provider_runtime = ProviderRuntimeReceiptBundle.model_validate_json(
                provider_snapshot.content
            )
        except ValueError as exc:
            parser.error(f"invalid CORE sealed Provider runtime: {exc}")
        provider_binding_path = G01_ROOT / "provider_binding.json"
        provider_binding_sha = _sha256(provider_binding_path)
        try:
            provider_index, _runtime, provider_verified = (
                validate_provider_receipt_assets(
                    split="frozen_blind",
                    provider_receipt_index_path=args.provider_receipt_index,
                    provider_runtime_receipt_bundle_path=(
                        args.provider_runtime_receipts
                    ),
                    repository_root=REPOSITORY_ROOT,
                    expected_candidate_commit=args.candidate_commit,
                    expected_candidate_tree=args.candidate_tree,
                    expected_goal_id="TC-VNEXT-G01-TEXT-CARDS",
                    expected_provider_binding_sha256=provider_binding_sha,
                    expected_runtime_receipt_bundle_sha256=(
                        provider_snapshot.sha256
                    ),
                    expected_database_export_receipt_sha256=(
                        provider_runtime.database_export_receipt_sha256
                    ),
                    expected_provider_http_receipt_bundle_sha256=(
                        provider_runtime.provider_http_receipt_bundle_sha256
                    ),
                    require_live_provider_evidence=True,
                )
            )
        except ValueError as exc:
            parser.error(f"invalid CORE sealed Provider evidence: {exc}")
        if truth.attestation.input_bundle_sha256 != agent_input_bundle_sha256(
            "frozen_blind",
            source_cases,
            provider_verified["provider_receipt_index_sha256"],
        ):
            parser.error("CORE sealed reference input bundle binding mismatch")
        if truth.attestation.output_schema_sha256 != _sha256(
            G01_ROOT / "sealed_agent_reference.schema.json"
        ):
            parser.error("CORE sealed reference schema binding mismatch")
        source_by_id = {case.case_id: case for case in source_cases}
        if [case.case_id for case in truth.agent_reference_cases] != [
            case.case_id for case in source_cases
        ]:
            parser.error("CORE sealed reference does not cover inputs in source order")
        provider_receipt_ids = {item.receipt_id for item in provider_index.receipts}
        for case in truth.agent_reference_cases:
            try:
                validate_agent_case_annotation(case, source_by_id[case.case_id])
            except ValueError as exc:
                parser.error(f"invalid CORE sealed reference case: {exc}")
            if any(
                mention.provider_resolution_receipt is not None
                and mention.provider_resolution_receipt.receipt_id
                not in provider_receipt_ids
                for mention in case.mentions
            ):
                parser.error("CORE sealed reference cites an unknown Provider receipt")

        model_path = G01_ROOT / "qwen_model_panel.json"
        prompt_path = G01_ROOT / "qwen_inference_prompt.md"
        schema_path = G01_ROOT / "qwen_semantic_draft.schema.json"
        config_path = G01_ROOT / "qwen_inference_config.json"
        inference_snapshot = read_external_snapshot(
            args.inference_receipts,
            REPOSITORY_ROOT,
        )
        expected_bindings = {
            "candidate_commit": args.candidate_commit,
            "candidate_tree": args.candidate_tree,
            "model_binding_sha256": _sha256(model_path),
            "prompt_sha256": _sha256(prompt_path),
            "schema_sha256": _sha256(schema_path),
            "config_sha256": _sha256(config_path),
            "provider_binding_sha256": provider_binding_sha,
            "inference_receipt_bundle_sha256": inference_snapshot.sha256,
        }
        try:
            envelope, predictions, inference, _inference_outputs = validate_prediction_run(
                prediction_path=args.predictions,
                inference_outputs_path=args.inference_outputs,
                envelope_path=args.prediction_envelope,
                repository_root=REPOSITORY_ROOT,
                split="frozen_blind",
                expected_bindings=expected_bindings,
                inference_receipt_bundle_path=args.inference_receipts,
                model_binding_artifact_path=model_path,
                prompt_artifact_path=prompt_path,
                schema_artifact_path=schema_path,
                config_artifact_path=config_path,
                require_live_inference_evidence=True,
            )
        except ValueError as exc:
            parser.error(f"invalid CORE sealed prediction evidence: {exc}")
        if len(inference.effects) != 18:
            parser.error("CORE sealed inference effects must cover all 18 cases")
        score = score_predictions(
            source_cases=source_cases,
            gold_cases=truth.agent_reference_cases,
            predictions=predictions,
        )
        score["estimated_confirmation_required_count"] = score.pop(
            "human_confirmation_count"
        )
        score["destination"] = _validate_destination_predictions(
            envelope=envelope,
            source_cases=source_cases,
            reference_cases=truth.agent_reference_cases,
        )
        metrics = _flatten_score(score, predictions)
        thresholds_path = G01_ROOT / "sealed_blind_thresholds.json"
        thresholds = SealedAgentBlindThresholds.model_validate_json(
            thresholds_path.read_bytes()
        )
        passed = _thresholds_pass(metrics, thresholds)
        taxonomy = _taxonomy_counts(metrics)
        errors = [name for name in BLIND_ERROR_CATEGORY_ORDER if taxonomy[name] > 0]
        completed_at = datetime.now(UTC)
        score_receipt = SealedAgentBlindReceipt(
            gate_profile="CORE_AGENT_GATE",
            goal_id="TC-VNEXT-G01-TEXT-CARDS",
            candidate_commit=args.candidate_commit,
            candidate_tree=args.candidate_tree,
            prompt_sha256=expected_bindings["prompt_sha256"],
            schema_sha256=expected_bindings["schema_sha256"],
            thresholds_sha256=_sha256(thresholds_path),
            config_sha256=expected_bindings["config_sha256"],
            provider_binding_sha256=provider_binding_sha,
            scorer_sha256=_sha256(Path(__file__).resolve(strict=True)),
            input_bundle_sha256=_sha256(args.inputs),
            prediction_bundle_sha256=_sha256(args.predictions),
            scored_case_count=18,
            custodian_task_id=args.custodian_task_id,
            aggregate_metrics=metrics,
            taxonomy_counts=taxonomy,
            error_taxonomy=errors,
            required_gate_metrics_passed=passed,
            verdict="PASS" if passed else "FAIL",
            completed_at=completed_at,
        )
        score_bytes = (
            json.dumps(
                score_receipt.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        write_external_bytes_exclusive(
            args.score_receipt_output,
            score_bytes,
            REPOSITORY_ROOT,
        )
        summary = score_receipt.model_copy(
            update={
                "deterministic_score_receipt_sha256": hashlib.sha256(
                    score_bytes
                ).hexdigest()
            }
        )
        summary_bytes = (
            json.dumps(
                summary.model_dump(mode="json", exclude_none=True),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        write_external_bytes_exclusive(
            args.receipt_output,
            summary_bytes,
            REPOSITORY_ROOT,
        )
        score_input = {
            "schema_version": "core-sealed-score-input-readback-v1",
            "candidate_commit": args.candidate_commit,
            "candidate_tree": args.candidate_tree,
            "input_bundle_sha256": _sha256(args.inputs),
            "prediction_bundle_sha256": _sha256(args.predictions),
            "truth_bundle_sha256": truth_snapshot.sha256,
            "provider_receipt_index_sha256": provider_verified[
                "provider_receipt_index_sha256"
            ],
            "inference_receipt_bundle_sha256": inference_snapshot.sha256,
            "raw_truth_in_receipt": False,
            "human_evidence": False,
        }
        write_external_bytes_exclusive(
            args.score_input_output,
            (json.dumps(score_input, sort_keys=True, indent=2) + "\n").encode(
                "utf-8"
            ),
            REPOSITORY_ROOT,
        )
        print(
            json.dumps(
                {
                    "verdict": summary.verdict,
                    "scored_case_count": summary.scored_case_count,
                    "human_evidence": False,
                    "blind_truth_returned_to_developer": False,
                    "receipt_sha256": hashlib.sha256(summary_bytes).hexdigest(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if binding.goal_sequence != 7:
        parser.error("HARDENED_CANDIDATE_GATE is restricted to G07")
    parser.error(
        "HARDENED sealed scoring is fail-closed until the repository-external "
        "custodian scorer and signer IPC are activated"
    )


if __name__ == "__main__":
    raise SystemExit(main())
