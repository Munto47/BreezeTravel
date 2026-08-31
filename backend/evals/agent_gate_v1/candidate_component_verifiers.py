from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from evals.agent_gate_v1.contracts import (
    AutomatedProductExecutionManifest,
    AutomatedProductGateContract,
    CandidateGateComponentReceipt,
    SealedAgentBlindThresholds,
)
from evals.agent_gate_v1.path_security import ArtifactSnapshot, read_external_snapshot
from evals.agent_gate_v1.validator import AgentGateValidationError, verify_review_panel
from evals.g07_candidate.browser_performance import _p95, validate_browser_report
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest
from evals.trip_text_cards_agent_v2.contracts import (
    AgentPredictionRunEnvelope,
    SealedAgentReferenceBundle,
    validate_agent_case_annotation,
)
from evals.trip_text_cards_v1.contracts import TextCardInputCase, TextCardPrediction
from evals.trip_text_cards_v1.scorer import ScoringError, score_predictions
from scripts.score_g01_agent_dev_validation import _validate_destination_predictions
from scripts.score_g01_sealed_agent_blind import _flatten_score, _thresholds_pass


VERIFIER_PATH = "backend/evals/agent_gate_v1/candidate_component_verifiers.py"
SEALED_THRESHOLDS_PATH = (
    "backend/eval_data/trip_text_cards_agent_v2/sealed_blind_thresholds.json"
)
_LIVE_CITIES = ("beijing", "shanghai", "hangzhou")
_LIVE_OPERATIONS = {
    "risk.weather_alert": 3,
    "route.bicycling": 3,
    "route.driving": 3,
    "route.transit": 3,
    "route.walking": 3,
    "weather.daily": 3,
}


class CandidateComponentVerificationError(ValueError):
    pass


def _git_blob(root: Path, commit: str, path: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise CandidateComponentVerificationError(
            f"candidate verifier Git binding is missing: {path}"
        )
    return result.stdout


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _json(snapshot: ArtifactSnapshot, label: str) -> dict[str, Any]:
    try:
        value = json.loads(snapshot.content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateComponentVerificationError(
            f"invalid {label} JSON artifact"
        ) from exc
    if not isinstance(value, dict):
        raise CandidateComponentVerificationError(f"invalid {label} JSON object")
    return value


def _jsonl(snapshot: ArtifactSnapshot, model: type, label: str) -> list[Any]:
    values: list[Any] = []
    try:
        for line in snapshot.content.decode("utf-8").splitlines():
            if line.strip():
                values.append(model.model_validate_json(line))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CandidateComponentVerificationError(
            f"invalid {label} JSONL artifact"
        ) from exc
    if not values:
        raise CandidateComponentVerificationError(f"empty {label} JSONL artifact")
    return values


def _require_keys(
    snapshots: Mapping[str, ArtifactSnapshot], required: set[str], component: str
) -> None:
    missing = sorted(required - set(snapshots))
    if missing:
        raise CandidateComponentVerificationError(
            f"{component} raw artifacts are incomplete: {','.join(missing)}"
        )


def _receipt_digest_is_valid(value: dict[str, Any]) -> bool:
    claimed = value.get("receipt_hash")
    unsigned = dict(value)
    unsigned.pop("receipt_hash", None)
    return isinstance(claimed, str) and claimed == digest(unsigned)


def _verify_automated(
    *,
    root: Path,
    receipt: CandidateGateComponentReceipt,
    snapshots: Mapping[str, ArtifactSnapshot],
) -> dict[str, Any]:
    _require_keys(snapshots, {"automated.execution_manifest"}, receipt.component)
    try:
        manifest = AutomatedProductExecutionManifest.model_validate_json(
            snapshots["automated.execution_manifest"].content
        )
        contract = AutomatedProductGateContract.model_validate_json(
            _git_blob(
                root,
                receipt.candidate_commit,
                "backend/eval_data/agent_gate_v1/g07_automated_product_gate.json",
            )
        )
    except ValueError as exc:
        raise CandidateComponentVerificationError(
            "invalid automated execution evidence"
        ) from exc
    expected_check_ids = [item.check_id for item in contract.checks]
    actual_check_ids = [item.check_id for item in manifest.checks]
    output_keys = {
        f"automated.{stream}_{check_id}"
        for check_id in expected_check_ids
        for stream in ("stdout", "stderr")
    }
    _require_keys(snapshots, output_keys, receipt.component)
    if (
        manifest.goal_id != receipt.goal_id
        or manifest.gate_profile != "HARDENED_CANDIDATE_GATE"
        or manifest.candidate_commit != receipt.candidate_commit
        or manifest.candidate_tree != receipt.candidate_tree
        or manifest.candidate_config_sha256 != receipt.candidate_config_sha256
        or manifest.candidate_data_sha256 != receipt.candidate_data_sha256
        or manifest.gate_contract_sha256
        != receipt.automated_gate_contract_sha256
        or manifest.isolation_mode != receipt.isolation_mode
        or manifest.verdict != "PASS"
        or manifest.checks_not_run
        or actual_check_ids != expected_check_ids
    ):
        raise CandidateComponentVerificationError(
            "automated execution evidence does not recompute to PASS"
        )
    for execution, check in zip(manifest.checks, contract.checks, strict=True):
        if execution.argv_sha256 != _canonical_sha256(check.argv):
            raise CandidateComponentVerificationError(
                "automated execution command binding mismatch"
            )
        if execution.workdir != check.workdir:
            raise CandidateComponentVerificationError(
                "automated execution workdir binding mismatch"
            )
        if (
            execution.stdout_sha256
            != snapshots[f"automated.stdout_{check.check_id}"].sha256
            or execution.stderr_sha256
            != snapshots[f"automated.stderr_{check.check_id}"].sha256
        ):
            raise CandidateComponentVerificationError(
                "automated execution output binding mismatch"
            )
    return {
        "executed_check_count": len(actual_check_ids),
        "isolation_mode": manifest.isolation_mode,
    }


def _verify_qwen_live(
    receipt: CandidateGateComponentReceipt,
    snapshots: Mapping[str, ArtifactSnapshot],
) -> dict[str, int]:
    summary = _json(snapshots["live.qwen_summary"], "Qwen summary")
    predictions = snapshots["live.qwen_predictions"]
    if (
        summary.get("schema_version") != "g01-qwen-model-prediction-summary-v1"
        or summary.get("candidate_commit") != receipt.candidate_commit
        or summary.get("candidate_tree") != receipt.candidate_tree
        or summary.get("case_count") != 72
        or summary.get("external_call_count") != 72
        or summary.get("schema_valid_model_output_count") != 72
        or summary.get("schema_valid_rate") != 1.0
        or summary.get("runner_error_count") != 0
        or summary.get("repair_call_count") != 0
        or summary.get("blind_inputs_read") != 0
        or summary.get("blind_truth_read") != 0
        or summary.get("raw_request_or_response_retained") is not False
        or summary.get("raw_predictions_sha256") != predictions.sha256
        or summary.get("raw_predictions_size") != len(predictions.content)
        or summary.get("failure_categories") != {}
        or summary.get("validation_failures") != {}
    ):
        raise CandidateComponentVerificationError(
            "Qwen live evidence does not recompute to PASS"
        )
    values: list[dict[str, Any]] = []
    try:
        values = [
            json.loads(line)
            for line in predictions.content.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateComponentVerificationError(
            "invalid Qwen prediction JSONL"
        ) from exc
    if len(values) != 72 or len({item.get("case_id") for item in values}) != 72:
        raise CandidateComponentVerificationError(
            "Qwen prediction coverage is incomplete"
        )
    fallback_count = sum(
        item.get("status") == "EDITABLE_PARTIAL_RESULT" for item in values
    )
    if summary.get("editable_partial_result_count") != fallback_count:
        raise CandidateComponentVerificationError(
            "Qwen fallback count contradicts raw predictions"
        )
    for item in values:
        binding = item.get("provider_binding")
        if not (
            item.get("status") == "VALID_MODEL_OUTPUT"
            and item.get("schema_valid_model_output") is True
            and isinstance(binding, dict)
            and binding.get("execution_mode") == "LIVE"
            and binding.get("external_calls") == 1
            and binding.get("repair_call_count") == 0
            and binding.get("raw_request_or_response_retained") is False
        ):
            raise CandidateComponentVerificationError(
                "Qwen raw prediction contradicts its PASS summary"
            )
    return {"qwen_case_count": 72, "qwen_external_call_count": 72}


def _verify_map_weather_live(
    receipt: CandidateGateComponentReceipt,
    snapshots: Mapping[str, ArtifactSnapshot],
) -> dict[str, int]:
    gate = _json(snapshots["live.g4_gate_receipt"], "G4 Gate receipt")
    binding = _json(snapshots["live.g4_binding_readback"], "G4 binding")
    manifest = _json(snapshots["live.g4_manifest"], "G4 manifest")
    spec = _json(snapshots["live.g4_spec"], "G4 execution spec")
    if not _receipt_digest_is_valid(gate) or not _receipt_digest_is_valid(binding):
        raise CandidateComponentVerificationError("G4 receipt digest mismatch")
    if (
        gate.get("status") != "PASS"
        or gate.get("subject_commit") != receipt.candidate_commit
        or gate.get("candidate_tree") != receipt.candidate_tree
        or manifest.get("status") != "PASS"
        or manifest.get("subject_commit") != receipt.candidate_commit
        or manifest.get("actual_network_call_count") != 18
        or manifest.get("actual_receipt_count") != 18
        or manifest.get("hidden_retry_count") != 0
        or spec.get("subject_commit") != receipt.candidate_commit
        or spec.get("candidate_tree") != receipt.candidate_tree
        or spec.get("goal_id") != receipt.goal_id
        or binding.get("subject_commit") != receipt.candidate_commit
        or binding.get("candidate_tree") != receipt.candidate_tree
        or binding.get("live_manifest_file_sha256")
        != snapshots["live.g4_manifest"].sha256
        or binding.get("operation_counts") != _LIVE_OPERATIONS
        or binding.get("provider_receipt_count") != 18
        or binding.get("fixture_fallback_count") != 0
        or binding.get("secret_leak_count") != 0
    ):
        raise CandidateComponentVerificationError(
            "G4 live evidence does not recompute to PASS"
        )
    operations: Counter[str] = Counter()
    count = 0
    for city in _LIVE_CITIES:
        key = f"live.g4_{city}_provider_receipts"
        run_spec = _json(
            snapshots[f"live.g4_{city}_run_spec"], "G4 Provider run spec"
        )
        try:
            observations = json.loads(
                snapshots[f"live.g4_{city}_observations"].content
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateComponentVerificationError(
                "invalid G4 evidence observations"
            ) from exc
        if (
            run_spec.get("commit_sha") != receipt.candidate_commit
            or run_spec.get("execution_mode") != "live"
            or not isinstance(observations, list)
            or len(observations) != 8
            or any(
                not isinstance(item, dict)
                or item.get("provider")
                not in {"amap", "qweather", "qweather_alert"}
                or not isinstance(item.get("observed_at"), str)
                or not isinstance(item.get("fact_type"), str)
                for item in observations
            )
        ):
            raise CandidateComponentVerificationError(
                "G4 Provider observation binding is invalid"
            )
        try:
            values = json.loads(snapshots[key].content)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CandidateComponentVerificationError(
                "invalid G4 Provider receipts"
            ) from exc
        if not isinstance(values, list) or len(values) != 6:
            raise CandidateComponentVerificationError(
                "G4 Provider receipt coverage is incomplete"
            )
        for item in values:
            if not (
                isinstance(item, dict)
                and item.get("status") == "SUCCEEDED"
                and item.get("execution_mode") == "live"
                and item.get("failure_category") is None
                and isinstance(item.get("request_hash"), str)
                and isinstance(item.get("response_hash"), str)
                and str(item.get("source_url", "")).startswith("https://")
            ):
                raise CandidateComponentVerificationError(
                    "G4 Provider raw receipt is not successful live evidence"
                )
            operations[str(item.get("operation"))] += 1
            count += 1
    if dict(sorted(operations.items())) != _LIVE_OPERATIONS or count != 18:
        raise CandidateComponentVerificationError("G4 operation set is invalid")
    return {"map_weather_external_call_count": 18}


def _verify_browser_and_performance(
    receipt: CandidateGateComponentReceipt,
    snapshots: Mapping[str, ArtifactSnapshot],
) -> dict[str, int | float]:
    browser_receipt = _json(
        snapshots["live.g5_browser_receipt"], "browser receipt"
    )
    report = _json(snapshots["live.g5_browser_report"], "browser report")
    try:
        browser_proof = validate_browser_report(report, receipt.candidate_commit)
    except P6ContractError as exc:
        raise CandidateComponentVerificationError(
            "browser report does not recompute to PASS"
        ) from exc
    if (
        not _receipt_digest_is_valid(browser_receipt)
        or browser_receipt.get("status") != "PASS"
        or browser_receipt.get("subject_commit") != receipt.candidate_commit
        or browser_receipt.get("candidate_tree") != receipt.candidate_tree
        or browser_receipt.get("browser_report_sha256")
        != snapshots["live.g5_browser_report"].sha256
        or browser_receipt.get("stdout_sha256")
        != snapshots["live.g5_browser_stdout"].sha256
        or browser_receipt.get("stderr_sha256")
        != snapshots["live.g5_browser_stderr"].sha256
        or browser_receipt.get("test_count") != browser_proof["test_count"]
        or browser_receipt.get("file_counts") != browser_proof["file_counts"]
        or browser_receipt.get("title_set_sha256")
        != browser_proof["title_set_sha256"]
    ):
        raise CandidateComponentVerificationError(
            "browser receipt contradicts the raw report"
        )
    performance = _json(
        snapshots["live.g5_performance_receipt"], "performance receipt"
    )
    samples: list[dict[str, Any]] = []
    try:
        samples = [
            json.loads(line)
            for line in snapshots["live.g5_performance_samples"]
            .content.decode("utf-8")
            .splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CandidateComponentVerificationError(
            "invalid performance samples"
        ) from exc
    progress_p95 = _p95([float(item["create_to_progress_ms"]) for item in samples])
    cards_p95 = _p95(
        [float(item["create_to_editable_cards_ms"]) for item in samples]
    )
    if (
        len(samples) != 50
        or len({item.get("chain_id") for item in samples}) != 50
        or any(
            item.get("candidate_commit") != receipt.candidate_commit
            or item.get("candidate_tree") != receipt.candidate_tree
            or item.get("qwen_external_calls") != 1
            or item.get("route_external_call_count") != 6
            or item.get("automatic_route_calls_after_edit") != 0
            or item.get("public_forbidden_key_count") != 0
            or item.get("raw_request_or_response_retained") is not False
            for item in samples
        )
        or not _receipt_digest_is_valid(performance)
        or performance.get("status") != "PASS"
        or performance.get("subject_commit") != receipt.candidate_commit
        or performance.get("candidate_tree") != receipt.candidate_tree
        or performance.get("sample_count") != 50
        or performance.get("sample_file_sha256")
        != snapshots["live.g5_performance_samples"].sha256
        or performance.get("threshold_failures") != []
        or performance.get("qwen_external_call_count") != 50
        or performance.get("route_external_call_count") != 300
        or performance.get("usable_map_count") != 50
        or performance.get("public_forbidden_key_count") != 0
        or performance.get("edit_triggered_route_call_count") != 0
        or performance.get("metrics", {}).get("create_to_progress_p95_ms")
        != progress_p95
        or performance.get("metrics", {}).get("create_to_editable_cards_p95_ms")
        != cards_p95
        or progress_p95 > 500
        or cards_p95 > 8000
    ):
        raise CandidateComponentVerificationError(
            "live performance evidence does not recompute to PASS"
        )
    return {
        "browser_test_count": int(browser_proof["test_count"]),
        "performance_sample_count": 50,
        "create_to_progress_p95_ms": progress_p95,
        "create_to_editable_cards_p95_ms": cards_p95,
    }


def _verify_live(
    *, receipt: CandidateGateComponentReceipt, snapshots: Mapping[str, ArtifactSnapshot]
) -> dict[str, Any]:
    required = {
        "live.g4_gate_receipt",
        "live.g4_binding_readback",
        "live.g4_manifest",
        "live.g4_spec",
        "live.qwen_summary",
        "live.qwen_predictions",
        "live.g5_browser_receipt",
        "live.g5_browser_report",
        "live.g5_browser_stdout",
        "live.g5_browser_stderr",
        "live.g5_performance_receipt",
        "live.g5_performance_samples",
        *(f"live.g4_{city}_provider_receipts" for city in _LIVE_CITIES),
        *(f"live.g4_{city}_run_spec" for city in _LIVE_CITIES),
        *(f"live.g4_{city}_observations" for city in _LIVE_CITIES),
    }
    _require_keys(snapshots, required, receipt.component)
    return {
        **_verify_map_weather_live(receipt, snapshots),
        **_verify_qwen_live(receipt, snapshots),
        **_verify_browser_and_performance(receipt, snapshots),
    }


def _verify_panel(
    *,
    root: Path,
    receipt: CandidateGateComponentReceipt,
    snapshots: Mapping[str, ArtifactSnapshot],
) -> dict[str, Any]:
    roles = ("product_ux", "semantic_domain", "reliability_security")
    required = {
        "panel.adjudication",
        *(f"panel.bundle_{role}" for role in roles),
        *(f"panel.review_{role}" for role in roles),
    }
    _require_keys(snapshots, required, receipt.component)
    role_names = {
        "product_ux": "PRODUCT_UX",
        "semantic_domain": "SEMANTIC_DOMAIN",
        "reliability_security": "RELIABILITY_SECURITY",
    }
    expected_input_hashes = {
        role_names[role]: snapshots[f"panel.bundle_{role}"].sha256
        for role in roles
    }
    try:
        result = verify_review_panel(
            review_paths=[
                Path(receipt.upstream_artifact_path[f"panel.review_{role}"])
                for role in roles
            ],
            adjudication_path=Path(
                receipt.upstream_artifact_path["panel.adjudication"]
            ),
            repository_root=root,
            expected_goal_id=receipt.goal_id,
            expected_candidate_commit=receipt.candidate_commit,
            expected_candidate_tree=receipt.candidate_tree,
            expected_candidate_config_sha256=receipt.candidate_config_sha256,
            expected_candidate_data_sha256=receipt.candidate_data_sha256,
            expected_input_bundle_sha256=expected_input_hashes,
        )
    except AgentGateValidationError as exc:
        raise CandidateComponentVerificationError(
            "multi-agent panel does not recompute to PASS"
        ) from exc
    if (
        result["verdict"] != "PASS"
        or result["accepted_p0_count"] != 0
        or result["accepted_p1_count"] != 0
        or result["accepted_in_scope_p2_count"] != 0
        or not result["roles_complete"]
    ):
        raise CandidateComponentVerificationError(
            "multi-agent panel has unresolved accepted findings"
        )
    return {
        "review_count": 3,
        "accepted_p0_count": 0,
        "accepted_p1_count": 0,
        "accepted_in_scope_p2_count": 0,
    }


def _verify_sealed(
    *,
    root: Path,
    receipt: CandidateGateComponentReceipt,
    snapshots: Mapping[str, ArtifactSnapshot],
) -> dict[str, Any]:
    required = {
        "sealed.inputs",
        "sealed.truth",
        "sealed.predictions",
        "sealed.prediction_envelope",
    }
    _require_keys(snapshots, required, receipt.component)
    source_cases = _jsonl(
        snapshots["sealed.inputs"], TextCardInputCase, "sealed inputs"
    )
    predictions = _jsonl(
        snapshots["sealed.predictions"], TextCardPrediction, "sealed predictions"
    )
    try:
        truth = SealedAgentReferenceBundle.model_validate_json(
            snapshots["sealed.truth"].content
        )
        envelope = AgentPredictionRunEnvelope.model_validate_json(
            snapshots["sealed.prediction_envelope"].content
        )
        thresholds = SealedAgentBlindThresholds.model_validate_json(
            _git_blob(root, receipt.candidate_commit, SEALED_THRESHOLDS_PATH)
        )
    except ValueError as exc:
        raise CandidateComponentVerificationError(
            "invalid sealed blind artifact"
        ) from exc
    if (
        len(source_cases) != 18
        or any(case.split != "frozen_blind" for case in source_cases)
        or truth.human_evidence
        or truth.attestation.subject_commit != receipt.candidate_commit
        or truth.attestation.subject_tree != receipt.candidate_tree
        or envelope.candidate_commit != receipt.candidate_commit
        or envelope.candidate_tree != receipt.candidate_tree
        or envelope.split != "frozen_blind"
        or envelope.predictions_sha256
        != snapshots["sealed.predictions"].sha256
    ):
        raise CandidateComponentVerificationError(
            "sealed blind candidate or coverage binding mismatch"
        )
    source_by_id = {case.case_id: case for case in source_cases}
    if [case.case_id for case in truth.agent_reference_cases] != [
        case.case_id for case in source_cases
    ]:
        raise CandidateComponentVerificationError(
            "sealed truth does not cover frozen inputs in source order"
        )
    try:
        for case in truth.agent_reference_cases:
            validate_agent_case_annotation(case, source_by_id[case.case_id])
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
        passed = _thresholds_pass(metrics, thresholds)
    except (ValueError, ScoringError) as exc:
        raise CandidateComponentVerificationError(
            "sealed blind deterministic scoring failed"
        ) from exc
    if not passed:
        raise CandidateComponentVerificationError(
            "sealed blind thresholds did not recompute to PASS"
        )
    return {
        "scored_case_count": 18,
        "required_metric_count": len(thresholds.required_metric_names),
        "metrics_sha256": _canonical_sha256(metrics),
    }


def compute_candidate_component_summary(
    *,
    receipt: CandidateGateComponentReceipt,
    repository_root: Path,
) -> dict[str, Any]:
    root = repository_root.resolve(strict=True)
    if receipt.verifier_path != VERIFIER_PATH:
        raise CandidateComponentVerificationError("candidate verifier path drifted")
    verifier_sha256 = _sha256(
        _git_blob(root, receipt.candidate_commit, receipt.verifier_path)
    )
    if receipt.verifier_sha256 != verifier_sha256:
        raise CandidateComponentVerificationError("candidate verifier hash drifted")
    snapshots: dict[str, ArtifactSnapshot] = {}
    for key, raw_path in receipt.upstream_artifact_path.items():
        try:
            snapshot = read_external_snapshot(Path(raw_path), root)
        except (OSError, ValueError) as exc:
            raise CandidateComponentVerificationError(
                f"candidate raw artifact is unavailable: {key}"
            ) from exc
        if snapshot.sha256 != receipt.upstream_artifact_sha256[key]:
            raise CandidateComponentVerificationError(
                f"candidate raw artifact hash drifted: {key}"
            )
        snapshots[key] = snapshot
    dispatch = {
        "AUTOMATED_PRODUCT_GATE": lambda: _verify_automated(
            root=root, receipt=receipt, snapshots=snapshots
        ),
        "LIVE_PROVIDER_GATE": lambda: _verify_live(
            receipt=receipt, snapshots=snapshots
        ),
        "MULTI_AGENT_PANEL": lambda: _verify_panel(
            root=root, receipt=receipt, snapshots=snapshots
        ),
        "SEALED_AGENT_BLIND": lambda: _verify_sealed(
            root=root, receipt=receipt, snapshots=snapshots
        ),
    }
    details = dispatch[receipt.component]()
    summary = {
        "schema_version": "candidate-component-verification-summary-v1",
        "goal_id": receipt.goal_id,
        "candidate_commit": receipt.candidate_commit,
        "candidate_tree": receipt.candidate_tree,
        "component": receipt.component,
        "verifier_path": receipt.verifier_path,
        "verifier_sha256": verifier_sha256,
        "upstream_artifact_sha256": dict(
            sorted(receipt.upstream_artifact_sha256.items())
        ),
        "details": details,
        "human_evidence": False,
        "production_evidence": False,
        "verdict": "PASS",
    }
    return summary


def verify_candidate_component_receipt(
    *,
    receipt: CandidateGateComponentReceipt,
    repository_root: Path,
) -> dict[str, Any]:
    summary = compute_candidate_component_summary(
        receipt=receipt,
        repository_root=repository_root,
    )
    if _canonical_sha256(summary) != receipt.verification_summary_sha256:
        raise CandidateComponentVerificationError(
            "candidate verification summary hash mismatch"
        )
    return summary


def verification_summary_sha256(summary: Mapping[str, Any]) -> str:
    return _canonical_sha256(summary)
