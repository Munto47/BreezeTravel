from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import datetime
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
from evals.g07_candidate.browser_performance import (
    PERFORMANCE_CHAIN_COUNT,
    PERFORMANCE_THRESHOLDS_MS,
    _p95,
    _validate_chain,
    validate_browser_report,
)
from evals.trip_check_v1.p6.contracts_v1 import P6ContractError, digest
from evals.trip_text_cards_agent_v2.contracts import (
    AgentCanonicalPlaceLabel,
    AgentInferenceCaseOutputV2,
    AgentPredictionRunEnvelope,
    ProviderReceiptRef,
    ProviderRuntimeEffectReceipt,
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


def _is_sha256(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Provider timestamp is missing")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _forbidden_raw_keys(value: object) -> set[str]:
    forbidden = {
        "api_key",
        "authorization",
        "credential",
        "raw_request",
        "raw_response",
        "secret",
        "token",
    }
    if isinstance(value, dict):
        own = {str(key).casefold() for key in value} & forbidden
        return own | set().union(
            *(_forbidden_raw_keys(child) for child in value.values()), set()
        )
    if isinstance(value, list):
        return set().union(*(_forbidden_raw_keys(child) for child in value), set())
    return set()


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
    try:
        for sample in samples:
            _validate_chain(
                sample,
                {
                    "subject_commit": receipt.candidate_commit,
                    "candidate_tree": receipt.candidate_tree,
                },
            )
    except (KeyError, TypeError, ValueError, P6ContractError) as exc:
        raise CandidateComponentVerificationError(
            "live performance sample does not recompute to PASS"
        ) from exc
    qwen_external_call_count = sum(
        int(item["qwen_external_calls"]) for item in samples
    )
    qwen_repair_call_count = sum(int(item["qwen_repair_calls"]) for item in samples)
    route_external_call_count = sum(
        int(item["route_external_call_count"]) for item in samples
    )
    usable_map_count = sum(
        item["initial_map_terminal_status"] in {"AVAILABLE", "LIMITED"}
        for item in samples
    )
    editable_partial_result_count = sum(
        item["public_result_status"] == "PARTIAL_RESULT" for item in samples
    )
    edit_triggered_route_call_count = sum(
        int(item["automatic_route_calls_after_edit"]) for item in samples
    )
    public_forbidden_key_count = sum(
        int(item["public_forbidden_key_count"]) for item in samples
    )
    if (
        len(samples) != PERFORMANCE_CHAIN_COUNT
        or len({item.get("chain_id") for item in samples})
        != PERFORMANCE_CHAIN_COUNT
        or not _receipt_digest_is_valid(performance)
        or performance.get("status") != "PASS"
        or performance.get("subject_commit") != receipt.candidate_commit
        or performance.get("candidate_tree") != receipt.candidate_tree
        or performance.get("sample_count") != PERFORMANCE_CHAIN_COUNT
        or performance.get("sample_file_sha256")
        != snapshots["live.g5_performance_samples"].sha256
        or performance.get("thresholds_ms") != PERFORMANCE_THRESHOLDS_MS
        or performance.get("threshold_failures") != []
        or performance.get("qwen_external_call_count")
        != qwen_external_call_count
        or performance.get("qwen_repair_call_count") != qwen_repair_call_count
        or performance.get("route_external_call_count")
        != route_external_call_count
        or performance.get("usable_map_count") != usable_map_count
        or performance.get("editable_partial_result_count")
        != editable_partial_result_count
        or performance.get("public_forbidden_key_count")
        != public_forbidden_key_count
        or performance.get("edit_triggered_route_call_count")
        != edit_triggered_route_call_count
        or performance.get("orphan_database_count") != 0
        or performance.get("raw_request_or_response_retained") is not False
        or performance.get("blind_inputs_read") != 0
        or performance.get("blind_truth_read") != 0
        or performance.get("human_evidence") is not False
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
        "performance_sample_count": PERFORMANCE_CHAIN_COUNT,
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
        "sealed.inference_outputs",
        "sealed.prediction_envelope",
        "sealed.runtime",
        "sealed.reference_input",
    }
    _require_keys(snapshots, required, receipt.component)
    source_cases = _jsonl(
        snapshots["sealed.inputs"], TextCardInputCase, "sealed inputs"
    )
    predictions = _jsonl(
        snapshots["sealed.predictions"], TextCardPrediction, "sealed predictions"
    )
    inference_outputs = _jsonl(
        snapshots["sealed.inference_outputs"],
        AgentInferenceCaseOutputV2,
        "sealed inference outputs",
    )
    runtime = _json(snapshots["sealed.runtime"], "sealed runtime")
    reference_input = _json(
        snapshots["sealed.reference_input"], "sealed reference input"
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
        model_panel_bytes = _git_blob(
            root,
            receipt.candidate_commit,
            "backend/eval_data/trip_text_cards_agent_v2/qwen_model_panel.json",
        )
        model_panel = json.loads(model_panel_bytes)
        provider_binding_bytes = _git_blob(
            root,
            receipt.candidate_commit,
            "backend/eval_data/trip_text_cards_agent_v2/provider_binding.json",
        )
        provider_binding = json.loads(provider_binding_bytes)
    except ValueError as exc:
        raise CandidateComponentVerificationError(
            "invalid sealed blind artifact"
        ) from exc
    candidates = model_panel.get("candidates")
    low_latency = [
        item
        for item in candidates
        if isinstance(item, dict)
        and item.get("role") == "LOW_LATENCY_CANDIDATE"
    ] if isinstance(candidates, list) else []
    qwen_provider = provider_binding.get("qwen")
    if (
        len(low_latency) != 1
        or not isinstance(low_latency[0].get("exact_model_id"), str)
        or not isinstance(qwen_provider, dict)
    ):
        raise CandidateComponentVerificationError(
            "sealed Qwen candidate binding is invalid"
        )
    expected_qwen = {
        "exact_model_id": low_latency[0]["exact_model_id"],
        "endpoint_sha256": qwen_provider.get("inference_endpoint_sha256"),
        "model_panel_sha256": _sha256(model_panel_bytes),
        "prompt_sha256": _sha256(
            _git_blob(
                root,
                receipt.candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md",
            )
        ),
        "schema_sha256": _sha256(
            _git_blob(
                root,
                receipt.candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/qwen_semantic_draft.schema.json",
            )
        ),
        "config_sha256": _sha256(
            _git_blob(
                root,
                receipt.candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_config.json",
            )
        ),
        "provider_binding_sha256": _sha256(provider_binding_bytes),
    }
    if (
        len(source_cases) != 18
        or any(case.split != "frozen_blind" for case in source_cases)
        or truth.human_evidence
        or truth.attestation.subject_commit != receipt.candidate_commit
        or truth.attestation.subject_tree != receipt.candidate_tree
        or envelope.candidate_commit != receipt.candidate_commit
        or envelope.candidate_tree != receipt.candidate_tree
        or envelope.split != "frozen_blind"
        or envelope.model_binding_sha256 != expected_qwen["model_panel_sha256"]
        or envelope.prompt_sha256 != expected_qwen["prompt_sha256"]
        or envelope.schema_sha256 != expected_qwen["schema_sha256"]
        or envelope.config_sha256 != expected_qwen["config_sha256"]
        or envelope.provider_binding_sha256
        != expected_qwen["provider_binding_sha256"]
        or envelope.predictions_sha256
        != snapshots["sealed.predictions"].sha256
        or envelope.inference_outputs_sha256
        != snapshots["sealed.inference_outputs"].sha256
        or envelope.inference_receipt_bundle_sha256
        != snapshots["sealed.runtime"].sha256
        or [item.text_card_prediction for item in inference_outputs]
        != predictions
        or [item.destination_prediction for item in inference_outputs]
        != envelope.destination_predictions
        or runtime.get("candidate_commit") != receipt.candidate_commit
        or runtime.get("candidate_tree") != receipt.candidate_tree
        or runtime.get("input_sha256") != snapshots["sealed.inputs"].sha256
        or runtime.get("predictions_sha256")
        != snapshots["sealed.predictions"].sha256
        or runtime.get("inference_outputs_sha256")
        != snapshots["sealed.inference_outputs"].sha256
        or runtime.get("reference_input_sha256")
        != snapshots["sealed.reference_input"].sha256
        or runtime.get("case_count") != 18
        or runtime.get("qwen_external_call_count") != 18
        or runtime.get("qwen_repair_call_count") != 0
        or not isinstance(runtime.get("amap_external_call_count"), int)
        or int(runtime["amap_external_call_count"]) < 1
        or runtime.get("blind_truth_read") != 0
        or runtime.get("raw_request_or_response_retained") is not False
        or runtime.get("human_evidence") is not False
        or runtime.get("verdict") != "CAPTURE_COMPLETE"
        or reference_input.get("candidate_commit") != receipt.candidate_commit
        or reference_input.get("candidate_tree") != receipt.candidate_tree
        or reference_input.get("input_sha256") != snapshots["sealed.inputs"].sha256
        or reference_input.get("case_count") != 18
        or reference_input.get("provider_effect_case_count") != 18
        or reference_input.get("candidate_predictions_visible") is not False
        or reference_input.get("raw_provider_response_retained") is not False
        or truth.attestation.input_bundle_sha256
        != snapshots["sealed.reference_input"].sha256
        or truth.attestation.prompt_sha256
        != _sha256(
            _git_blob(
                root,
                receipt.candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/prompts/adjudication.md",
            )
        )
        or truth.attestation.output_schema_sha256
        != _sha256(
            _git_blob(
                root,
                receipt.candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/sealed_agent_reference.schema.json",
            )
        )
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
        reference_cases = reference_input.get("cases")
        if (
            not isinstance(reference_cases, list)
            or [
                item.get("case_id")
                for item in reference_cases
                if isinstance(item, dict)
            ]
            != [case.case_id for case in source_cases]
            or reference_input.get("provider_binding_sha256")
            != _sha256(
                _git_blob(
                    root,
                    receipt.candidate_commit,
                    "backend/eval_data/trip_text_cards_agent_v2/provider_binding.json",
                )
            )
        ):
            raise ValueError("sealed Provider reference coverage drifted")
        facts_by_case: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
        unique_runtime_effects: dict[str, ProviderRuntimeEffectReceipt] = {}
        for source_case, reference_case in zip(
            source_cases, reference_cases, strict=True
        ):
            if (
                not isinstance(reference_case, dict)
                or reference_case.get("source_sha256")
                != source_case.normalized_input_sha256
            ):
                raise ValueError("sealed Provider reference source drifted")
            effects = reference_case.get("provider_effects")
            if not isinstance(effects, list):
                raise ValueError("sealed Provider reference effects are invalid")
            case_facts: dict[tuple[str, str], dict[str, Any]] = {}
            for effect in effects:
                if (
                    not isinstance(effect, dict)
                    or effect.get("query_is_role_neutral") is not True
                ):
                    raise ValueError("sealed Provider query selected a candidate role")
                runtime_effect = ProviderRuntimeEffectReceipt.model_validate(
                    effect.get("provider_runtime_effect")
                )
                provider_receipt = ProviderReceiptRef.model_validate(
                    effect.get("provider_receipt")
                )
                canonical_raw = effect.get("canonical_place")
                canonical_place = (
                    AgentCanonicalPlaceLabel.model_validate(canonical_raw)
                    if canonical_raw is not None
                    else None
                )
                if (
                    runtime_effect.effect_id != provider_receipt.runtime_effect_id
                    or provider_receipt.runtime_effect_receipt_sha256
                    != _canonical_sha256(runtime_effect.model_dump(mode="json"))
                    or provider_receipt.provider_binding_sha256
                    != reference_input["provider_binding_sha256"]
                    or provider_receipt.provider_binding_sha256
                    != runtime_effect.provider_binding_sha256
                    or provider_receipt.request_sha256
                    != runtime_effect.request_sha256
                    or provider_receipt.response_sha256
                    != runtime_effect.response_sha256
                    or provider_receipt.resolution_status
                    != runtime_effect.resolution_status
                    or provider_receipt.queried_city
                    != runtime_effect.queried_city
                    or provider_receipt.queried_source_name
                    != runtime_effect.queried_source_name
                    or provider_receipt.observed_at
                    != runtime_effect.completed_at
                    or provider_receipt.accepted_source_name
                    != (
                        provider_receipt.queried_source_name
                        if runtime_effect.resolution_status == "MATCHED"
                        else None
                    )
                    or provider_receipt.queried_city
                    != effect.get("queried_city")
                    or provider_receipt.queried_source_name
                    != effect.get("queried_source_name")
                    or (canonical_place is None)
                    != (provider_receipt.resolution_status != "MATCHED")
                    or (
                        canonical_place is not None
                        and (
                            canonical_place.provider_receipt != provider_receipt
                            or canonical_place.place_id != runtime_effect.place_id
                            or canonical_place.name != runtime_effect.name
                            or canonical_place.city != runtime_effect.city
                            or canonical_place.category != runtime_effect.category
                        )
                    )
                ):
                    raise ValueError("sealed Provider reference fact drifted")
                prior = unique_runtime_effects.setdefault(
                    runtime_effect.effect_id, runtime_effect
                )
                if prior != runtime_effect:
                    raise ValueError("sealed Provider runtime effect is inconsistent")
                key = (
                    provider_receipt.queried_city,
                    provider_receipt.queried_source_name,
                )
                if key in case_facts:
                    raise ValueError("duplicate sealed Provider reference fact")
                case_facts[key] = {
                    "provider_runtime_effect": runtime_effect,
                    "provider_receipt": provider_receipt.model_dump(mode="json"),
                    "canonical_place": (
                        canonical_place.model_dump(mode="json")
                        if canonical_place is not None
                        else None
                    ),
                }
            facts_by_case[source_case.case_id] = case_facts
        if sum(
            effect.external_call_count for effect in unique_runtime_effects.values()
        ) != int(runtime["amap_external_call_count"]):
            raise ValueError("sealed AMap call count drifted from reference facts")
        qwen_external_calls = 0
        qwen_repair_calls = 0
        for source_case, inference_output in zip(
            source_cases, inference_outputs, strict=True
        ):
            prediction = inference_output.text_card_prediction
            binding = prediction.provider_binding
            inference_binding = binding.get("inference_binding")
            effects = binding.get("provider_effects")
            if (
                inference_output.case_id != source_case.case_id
                or inference_output.source_sha256
                != source_case.normalized_input_sha256
                or prediction.source_sha256 != source_case.normalized_input_sha256
                or binding.get("execution_mode") != "LIVE"
                or binding.get("raw_request_or_response_retained") is not False
                or _forbidden_raw_keys(binding)
                or not isinstance(inference_binding, dict)
                or not isinstance(effects, list)
                or len(effects) != len(prediction.mentions)
                or binding.get("provider_effects_sha256")
                != _canonical_sha256(effects)
            ):
                raise ValueError("sealed inference output binding drifted")
            calls = inference_binding.get("calls")
            if (
                inference_binding.get("provider") != "QWEN"
                or inference_binding.get("execution_mode") != "LIVE"
                or inference_binding.get("exact_model_id")
                != expected_qwen["exact_model_id"]
                or inference_binding.get("endpoint_sha256")
                != expected_qwen["endpoint_sha256"]
                or inference_binding.get("model_panel_sha256")
                != expected_qwen["model_panel_sha256"]
                or inference_binding.get("prompt_sha256")
                != expected_qwen["prompt_sha256"]
                or inference_binding.get("prompt_artifact_sha256")
                != expected_qwen["prompt_sha256"]
                or inference_binding.get("schema_sha256")
                != expected_qwen["schema_sha256"]
                or inference_binding.get("schema_artifact_sha256")
                != expected_qwen["schema_sha256"]
                or inference_binding.get("config_sha256")
                != expected_qwen["config_sha256"]
                or inference_binding.get("config_artifact_sha256")
                != expected_qwen["config_sha256"]
                or inference_binding.get("max_concurrency") != 1
                or inference_binding.get("deadline_ms") != 7000
                or inference_binding.get("max_output_tokens") != 768
                or inference_binding.get("external_calls") != 1
                or inference_binding.get("repair_call_count") != 0
                or inference_binding.get("raw_request_or_response_retained")
                is not False
                or not isinstance(calls, list)
                or len(calls) != 1
            ):
                raise ValueError("sealed Qwen per-case binding drifted")
            call = calls[0]
            if (
                not isinstance(call, dict)
                or call.get("outcome") != "RESPONSE_RECEIVED"
                or not _is_sha256(call.get("request_sha256"))
                or not _is_sha256(call.get("response_sha256"))
                or not isinstance(call.get("input_tokens"), int)
                or int(call["input_tokens"]) < 0
                or not isinstance(call.get("output_tokens"), int)
                or int(call["output_tokens"]) < 0
                or _timestamp(call.get("completed_at"))
                < _timestamp(call.get("started_at"))
                or inference_binding.get("input_tokens")
                != call.get("input_tokens")
                or inference_binding.get("output_tokens")
                != call.get("output_tokens")
            ):
                raise ValueError("sealed Qwen call receipt drifted")
            qwen_external_calls += int(inference_binding["external_calls"])
            qwen_repair_calls += int(inference_binding["repair_call_count"])
            for mention, effect in zip(prediction.mentions, effects, strict=True):
                if (
                    not isinstance(effect, dict)
                    or effect.get("raw_text") != mention.raw_text
                    or effect.get("span_start") != mention.span_start
                    or effect.get("span_end") != mention.span_end
                    or effect.get("role") != mention.role
                    or effect.get("day_index") != mention.day_index
                    or effect.get("atomic_place_name")
                    != mention.atomic_place_name
                    or effect.get("eligible_for_place_search")
                    != mention.eligible_for_place_search
                    or effect.get("resolution_status")
                    != mention.resolution_status
                ):
                    raise ValueError("sealed candidate Provider projection drifted")
                if not mention.eligible_for_place_search:
                    continue
                expected_fact = facts_by_case[source_case.case_id].get(
                    (prediction.destination_name, mention.atomic_place_name or "")
                )
                if expected_fact is None:
                    raise ValueError("sealed candidate used an unbound Provider query")
                provider_receipt = ProviderReceiptRef.model_validate(
                    expected_fact["provider_receipt"]
                )
                expected_status = {
                    "MATCHED": "AUTO_MATCHED",
                    "AMBIGUOUS": "NEEDS_CONFIRMATION",
                    "UNRESOLVED": "UNRESOLVED",
                }[provider_receipt.resolution_status]
                runtime_effect = expected_fact["provider_runtime_effect"]
                resolver_receipt = effect.get("resolver_receipt")
                place = effect.get("place")
                canonical = expected_fact["canonical_place"]
                called_hashes_match = (
                    runtime_effect.external_call_count == 0
                    or (
                        isinstance(resolver_receipt, dict)
                        and resolver_receipt.get("request_sha256")
                        == runtime_effect.request_sha256
                        and resolver_receipt.get("response_sha256")
                        == runtime_effect.response_sha256
                    )
                )
                if (
                    mention.resolution_status != expected_status
                    or not isinstance(resolver_receipt, dict)
                    or not called_hashes_match
                    or resolver_receipt.get("external_calls")
                    != runtime_effect.external_call_count
                    or (place is None) != (canonical is None)
                    or (
                        isinstance(place, dict)
                        and isinstance(canonical, dict)
                        and (
                            place.get("canonical_place_id")
                            != canonical.get("place_id")
                            or place.get("name") != canonical.get("name")
                            or place.get("category") != canonical.get("category")
                            or mention.canonical_place_id
                            != canonical.get("place_id")
                            or mention.canonical_city != canonical.get("city")
                            or mention.canonical_category
                            != canonical.get("category")
                        )
                    )
                ):
                    raise ValueError("sealed candidate Provider fact drifted")
        if (
            qwen_external_calls != int(runtime["qwen_external_call_count"])
            or qwen_repair_calls != int(runtime["qwen_repair_call_count"])
        ):
            raise ValueError("sealed Qwen aggregate drifted")
        for case in truth.agent_reference_cases:
            validate_agent_case_annotation(case, source_by_id[case.case_id])
            for mention in case.mentions:
                if not mention.executable_place:
                    continue
                provider_receipt = mention.provider_resolution_receipt
                if provider_receipt is None:
                    raise ValueError("sealed executable place lost Provider receipt")
                expected = facts_by_case[case.case_id].get(
                    (
                        provider_receipt.queried_city,
                        provider_receipt.queried_source_name,
                    )
                )
                actual_canonical = (
                    mention.canonical_place.model_dump(mode="json")
                    if mention.canonical_place is not None
                    else None
                )
                if (
                    expected is None
                    or provider_receipt.model_dump(mode="json")
                    != expected["provider_receipt"]
                    or actual_canonical != expected["canonical_place"]
                ):
                    raise ValueError("sealed truth used an unbound Provider fact")
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
