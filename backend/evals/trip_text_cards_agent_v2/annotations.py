from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from evals.agent_gate_v1.authority import (
    load_anchored_authority_policy,
    load_candidate_current_goal_binding,
)
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.path_security import ArtifactSnapshot, read_external_snapshot
from evals.agent_gate_v1.signing import unsigned_payload, verify_payload_signature
from evals.trip_text_cards_agent_v2.contracts import (
    AgentAdjudicationBundle,
    AgentAnnotationBundle,
    AgentCaseAnnotation,
    AgentInferenceCaseOutputV2,
    InferenceDatabaseExportReceipt,
    InferenceHttpReceiptBundle,
    InferenceRuntimeReceiptBundle,
    ProviderDatabaseExportReceipt,
    ProviderHttpReceiptBundle,
    ProviderReceiptIndex,
    ProviderRuntimeReceiptBundle,
    agent_input_bundle_sha256,
    validate_agent_case_annotation,
)
from evals.trip_text_cards_v1.contracts import (
    TextCardInputCase,
    TextCardPrediction,
    canonical_sha256,
)


class AgentAnnotationValidationError(ValueError):
    pass


_QWEN_CORE_EXPORTER_PATH = "backend/scripts/export_g01_qwen_live_receipts.py"
_AMAP_CORE_EXPORTER_PATH = "backend/scripts/export_g01_amap_live_receipts.py"


def _external_snapshot(
    path: Path,
    repository_root: Path,
    artifact_snapshots: dict[Path, ArtifactSnapshot] | None,
) -> ArtifactSnapshot:
    if artifact_snapshots is None:
        return read_external_snapshot(path, repository_root)
    resolved = path.resolve(strict=True)
    snapshot = artifact_snapshots.get(resolved)
    if snapshot is None or snapshot.path.resolve(strict=True) != resolved:
        raise AgentAnnotationValidationError(
            "required artifact was not frozen in the supplied snapshot set"
        )
    return snapshot


def _read_external_model(
    path: Path,
    model_type,
    repository_root: Path,
    artifact_snapshots: dict[Path, ArtifactSnapshot] | None = None,
):
    snapshot = _external_snapshot(path, repository_root, artifact_snapshots)
    try:
        value = model_type.model_validate_json(snapshot.content)
    except ValueError as exc:
        raise AgentAnnotationValidationError(
            f"invalid agent annotation artifact {snapshot.path.name}: {exc}"
        ) from exc
    return snapshot, value


def _git_tree(repository_root: Path, commit: str) -> str:
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(repository_root), "show", "-s", "--format=%T", commit],
        check=False,
        capture_output=True,
        text=True,
    )
    tree = result.stdout.strip()
    if result.returncode != 0 or len(tree) != 40:
        raise AgentAnnotationValidationError("candidate commit does not exist in the repository")
    return tree


def _git_blob_sha256(repository_root: Path, commit: str, path: str) -> str:
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(repository_root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AgentAnnotationValidationError(f"candidate artifact is absent: {path}")
    return hashlib.sha256(result.stdout).hexdigest()


def _git_blob_json(repository_root: Path, commit: str, path: str) -> dict[str, Any]:
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(repository_root), "show", f"{commit}:{path}"],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise AgentAnnotationValidationError(f"candidate artifact is absent: {path}")
    try:
        value = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AgentAnnotationValidationError(
            f"candidate artifact is not valid JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise AgentAnnotationValidationError(
            f"candidate artifact must be a JSON object: {path}"
        )
    return value


def _require_frozen_provider_binding(
    repository_root: Path,
    candidate_commit: str,
    *,
    lane: str,
    exact_model_id: str | None = None,
    region: str | None = None,
    endpoint_sha256: str | None = None,
) -> str:
    path = "backend/eval_data/trip_text_cards_agent_v2/provider_binding.json"
    binding = _git_blob_json(repository_root, candidate_commit, path)
    if binding.get("status") != "FROZEN":
        raise AgentAnnotationValidationError("candidate Provider binding is not frozen")
    if lane == "AMAP":
        required = (
            binding.get("amap_place_search"),
            binding.get("amap_walking"),
            binding.get("amap_transit"),
        )
        if any(
            not isinstance(value, str) or "PENDING" in value or not value.strip()
            for value in required
        ):
            raise AgentAnnotationValidationError("candidate AMap binding is incomplete")
    elif lane == "QWEN":
        qwen = binding.get("qwen")
        if (
            not isinstance(qwen, dict)
            or qwen.get("region") != region
            or qwen.get("inference_endpoint_sha256") != endpoint_sha256
            or qwen.get("model_selection_binding") != "QWEN_MODEL_PANEL"
        ):
            raise AgentAnnotationValidationError("candidate Qwen Provider binding disagrees")
        del exact_model_id  # exact selection is independently bound by qwen_model_panel.json
    else:
        raise AgentAnnotationValidationError("unknown live Provider binding lane")
    return _git_blob_sha256(repository_root, candidate_commit, path)


def _parse_jsonl_snapshot(snapshot, model_type):
    values = []
    try:
        text = snapshot.content.decode("utf-8")
        if not text or not text.endswith("\n"):
            raise ValueError("JSONL must be non-empty and LF-terminated")
        for line in text.splitlines():
            values.append(model_type.model_validate_json(line))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AgentAnnotationValidationError(
            f"invalid inference artifact {snapshot.path.name}: {exc}"
        ) from exc
    return values


def validate_inference_runtime_receipt_assets(
    *,
    inference_runtime_receipt_bundle_path: Path,
    repository_root: Path,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_goal_id: str,
    require_live_provider_evidence: bool,
    artifact_snapshots: dict[Path, ArtifactSnapshot] | None = None,
) -> tuple[InferenceRuntimeReceiptBundle, dict[str, Any]]:
    if _git_tree(repository_root, expected_candidate_commit) != expected_candidate_tree:
        raise AgentAnnotationValidationError("expected candidate tree does not match the Git commit")
    runtime_snapshot, runtime = _read_external_model(
        inference_runtime_receipt_bundle_path,
        InferenceRuntimeReceiptBundle,
        repository_root,
        artifact_snapshots,
    )
    if (
        runtime.goal_id != expected_goal_id
        or runtime.candidate_commit != expected_candidate_commit
        or runtime.candidate_tree != expected_candidate_tree
    ):
        raise AgentAnnotationValidationError("Qwen runtime candidate binding mismatch")
    if require_live_provider_evidence and (
        runtime.execution_mode != "LIVE"
        or runtime.evidence_level != "LIVE_PROVIDER_EVIDENCE"
    ):
        raise AgentAnnotationValidationError("live Qwen evidence is required for this lane")
    if runtime.execution_mode != "LIVE":
        return runtime, {
            "inference_runtime_receipt_bundle_sha256": runtime_snapshot.sha256,
            "qwen_live_effect_count": 0,
            "evidence_level": runtime.evidence_level,
        }

    goal_binding = load_candidate_current_goal_binding(
        repository_root,
        expected_candidate_commit,
    )
    anchored = None
    expected_exporter_path = _QWEN_CORE_EXPORTER_PATH
    if goal_binding.gate_profile == "HARDENED_CANDIDATE_GATE":
        anchored = load_anchored_authority_policy(
            repository_root,
            expected_candidate_commit,
        )
        if (
            runtime.authority_policy_sha256 != anchored.sha256
            or runtime.authority_signature is None
        ):
            raise AgentAnnotationValidationError("Qwen runtime authority binding mismatch")
        verify_payload_signature(
            payload=unsigned_payload(runtime),
            signature=runtime.authority_signature,
            manifest=anchored.manifest,
            expected_role="QWEN_LIVE_EXPORTER",
        )
        expected_exporter_path = anchored.manifest.live_exporter_paths[
            "QWEN_LIVE_EXPORTER"
        ]
    elif (
        runtime.authority_policy_sha256 is not None
        or runtime.authority_signature is not None
    ):
        raise AgentAnnotationValidationError(
            "CORE Qwen runtime cannot claim HARDENED authority evidence"
        )
    if (
        runtime.exporter_path is None
        or runtime.exporter_sha256 is None
        or runtime.exporter_path != expected_exporter_path
        or _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            runtime.exporter_path,
        )
        != runtime.exporter_sha256
    ):
        raise AgentAnnotationValidationError("Qwen exporter binding mismatch")

    expected_qwen_bindings = {
        "model_binding_sha256": _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            "backend/eval_data/trip_text_cards_agent_v2/qwen_model_panel.json",
        ),
        "prompt_sha256": _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md",
        ),
        "schema_sha256": _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            "backend/eval_data/trip_text_cards_agent_v2/qwen_semantic_draft.schema.json",
        ),
        "config_sha256": _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_config.json",
        ),
        "provider_binding_sha256": _require_frozen_provider_binding(
            repository_root,
            expected_candidate_commit,
            lane="QWEN",
            exact_model_id=runtime.exact_model_id,
            region=runtime.region,
            endpoint_sha256=runtime.endpoint_sha256,
        ),
    }
    if any(
        getattr(runtime, field) != expected
        for field, expected in expected_qwen_bindings.items()
    ):
        raise AgentAnnotationValidationError(
            "Qwen runtime is not bound to the candidate model/prompt/schema/config"
        )
    model_panel = _git_blob_json(
        repository_root,
        expected_candidate_commit,
        "backend/eval_data/trip_text_cards_agent_v2/qwen_model_panel.json",
    )
    if (
        model_panel.get("status") != "FROZEN"
        or model_panel.get("frozen_candidate") != runtime.exact_model_id
        or model_panel.get("region") != runtime.region
        or runtime.exact_model_id
        not in {
            item.get("exact_model_id")
            for item in model_panel.get("candidates", [])
            if isinstance(item, dict)
        }
    ):
        raise AgentAnnotationValidationError("Qwen runtime model panel binding mismatch")

    required_paths = (
        runtime.predictions_path,
        runtime.inference_outputs_path,
        runtime.database_export_receipt_path,
        runtime.provider_http_receipt_bundle_path,
    )
    if any(value is None for value in required_paths):
        raise AgentAnnotationValidationError("Qwen live runtime raw artifact paths are incomplete")
    predictions_snapshot = _external_snapshot(
        Path(runtime.predictions_path), repository_root, artifact_snapshots
    )
    outputs_snapshot = _external_snapshot(
        Path(runtime.inference_outputs_path), repository_root, artifact_snapshots
    )
    database_snapshot, database = _read_external_model(
        Path(runtime.database_export_receipt_path),
        InferenceDatabaseExportReceipt,
        repository_root,
        artifact_snapshots,
    )
    http_snapshot, http = _read_external_model(
        Path(runtime.provider_http_receipt_bundle_path),
        InferenceHttpReceiptBundle,
        repository_root,
        artifact_snapshots,
    )
    if (
        predictions_snapshot.sha256 != runtime.predictions_sha256
        or outputs_snapshot.sha256 != runtime.inference_outputs_sha256
        or database_snapshot.sha256 != runtime.database_export_receipt_sha256
        or http_snapshot.sha256 != runtime.provider_http_receipt_bundle_sha256
    ):
        raise AgentAnnotationValidationError("Qwen runtime raw artifact hash mismatch")

    predictions = _parse_jsonl_snapshot(predictions_snapshot, TextCardPrediction)
    outputs = _parse_jsonl_snapshot(outputs_snapshot, AgentInferenceCaseOutputV2)
    prediction_projection = [item.text_card_prediction for item in outputs]
    if predictions != prediction_projection:
        raise AgentAnnotationValidationError("Qwen predictions are not the output projection")
    if [item.case_id for item in outputs] != [item.case_id for item in runtime.effects]:
        raise AgentAnnotationValidationError("Qwen output and runtime case order mismatch")
    for output, effect in zip(outputs, runtime.effects, strict=True):
        if canonical_sha256(output.model_dump(mode="json")) != effect.output_sha256:
            raise AgentAnnotationValidationError("Qwen runtime effect output hash mismatch")

    for artifact in (database, http):
        if anchored is not None:
            if (
                artifact.authority_policy_sha256 != anchored.sha256
                or artifact.authority_signature is None
            ):
                raise AgentAnnotationValidationError(
                    "Qwen child receipt authority mismatch"
                )
            verify_payload_signature(
                payload=unsigned_payload(artifact),
                signature=artifact.authority_signature,
                manifest=anchored.manifest,
                expected_role="QWEN_LIVE_EXPORTER",
            )
        elif (
            artifact.authority_policy_sha256 is not None
            or artifact.authority_signature is not None
        ):
            raise AgentAnnotationValidationError(
                "CORE Qwen child receipt cannot claim HARDENED authority evidence"
            )
        if (
            artifact.goal_id != expected_goal_id
            or artifact.candidate_commit != expected_candidate_commit
            or artifact.candidate_tree != expected_candidate_tree
            or artifact.model_binding_sha256 != runtime.model_binding_sha256
            or artifact.provider_binding_sha256 != runtime.provider_binding_sha256
            or artifact.execution_mode != "LIVE"
        ):
            raise AgentAnnotationValidationError("Qwen child receipt binding mismatch")
    if database.source_registry != "POSTGRESQL_INFERENCE_EFFECT_REGISTRY":
        raise AgentAnnotationValidationError("Qwen live database source registry mismatch")

    database_by_id = {item.effect_id: item for item in database.effects}
    http_by_id = {item.effect_id: item for item in http.exchanges}
    runtime_by_id = {item.effect_id: item for item in runtime.effects}
    if set(database_by_id) != set(runtime_by_id) or set(http_by_id) != set(runtime_by_id):
        raise AgentAnnotationValidationError("Qwen database/HTTP/runtime effect sets disagree")
    for effect_id, effect in runtime_by_id.items():
        database_effect = database_by_id[effect_id]
        http_effect = http_by_id[effect_id]
        if (
            database_effect.case_id,
            database_effect.request_sha256,
            database_effect.response_sha256,
            database_effect.output_sha256,
            database_effect.started_at,
            database_effect.completed_at,
        ) != (
            effect.case_id,
            effect.request_sha256,
            effect.response_sha256,
            effect.output_sha256,
            effect.started_at,
            effect.completed_at,
        ):
            raise AgentAnnotationValidationError("Qwen database effect disagrees with runtime")
        if (
            http_effect.case_id,
            http_effect.request_sha256,
            http_effect.response_sha256,
            http_effect.provider_request_id_sha256,
            http_effect.completed_at,
        ) != (
            effect.case_id,
            effect.request_sha256,
            effect.response_sha256,
            effect.provider_request_id_sha256,
            effect.completed_at,
        ):
            raise AgentAnnotationValidationError("Qwen HTTP exchange disagrees with runtime")
    return runtime, {
        "inference_runtime_receipt_bundle_sha256": runtime_snapshot.sha256,
        "predictions_sha256": predictions_snapshot.sha256,
        "inference_outputs_sha256": outputs_snapshot.sha256,
        "database_export_receipt_sha256": database_snapshot.sha256,
        "provider_http_receipt_bundle_sha256": http_snapshot.sha256,
        "qwen_live_effect_count": len(runtime.effects),
        "evidence_level": "LIVE_PROVIDER_EVIDENCE",
    }


def validate_provider_receipt_assets(
    *,
    split: str,
    provider_receipt_index_path: Path,
    provider_runtime_receipt_bundle_path: Path,
    repository_root: Path,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_goal_id: str,
    expected_provider_binding_sha256: str,
    expected_runtime_receipt_bundle_sha256: str,
    expected_database_export_receipt_sha256: str,
    expected_provider_http_receipt_bundle_sha256: str,
    require_live_provider_evidence: bool,
    artifact_snapshots: dict[Path, ArtifactSnapshot] | None = None,
) -> tuple[ProviderReceiptIndex, ProviderRuntimeReceiptBundle, dict[str, Any]]:
    if _git_tree(repository_root, expected_candidate_commit) != expected_candidate_tree:
        raise AgentAnnotationValidationError("expected candidate tree does not match the Git commit")

    provider_index_snapshot, provider_index = _read_external_model(
        provider_receipt_index_path,
        ProviderReceiptIndex,
        repository_root,
        artifact_snapshots,
    )

    runtime_snapshot, runtime_bundle = _read_external_model(
        provider_runtime_receipt_bundle_path,
        ProviderRuntimeReceiptBundle,
        repository_root,
        artifact_snapshots,
    )
    runtime_bundle_sha256 = runtime_snapshot.sha256
    if runtime_bundle_sha256 != expected_runtime_receipt_bundle_sha256:
        raise AgentAnnotationValidationError("provider runtime receipt bundle expected hash mismatch")

    if (
        provider_index.goal_id != expected_goal_id
        or runtime_bundle.goal_id != expected_goal_id
    ):
        raise AgentAnnotationValidationError("Provider receipt Goal binding mismatch")
    if provider_index.split != split:
        raise AgentAnnotationValidationError("provider receipt index split mismatch")
    if (
        provider_index.subject_commit != expected_candidate_commit
        or provider_index.subject_tree != expected_candidate_tree
    ):
        raise AgentAnnotationValidationError("provider receipt index candidate binding mismatch")
    if provider_index.provider_binding_sha256 != expected_provider_binding_sha256:
        raise AgentAnnotationValidationError("provider receipt index config binding mismatch")
    if provider_index.runtime_receipt_bundle_sha256 != runtime_bundle_sha256:
        raise AgentAnnotationValidationError("provider runtime receipt bundle hash mismatch")
    if (
        runtime_bundle.candidate_commit != expected_candidate_commit
        or runtime_bundle.candidate_tree != expected_candidate_tree
    ):
        raise AgentAnnotationValidationError("provider runtime receipt candidate binding mismatch")
    if runtime_bundle.provider_binding_sha256 != expected_provider_binding_sha256:
        raise AgentAnnotationValidationError("provider runtime binding mismatch")
    execution_modes = {
        provider_index.execution_mode,
        runtime_bundle.execution_mode,
    }
    evidence_levels = {
        provider_index.evidence_level,
        runtime_bundle.evidence_level,
    }
    if len(execution_modes) != 1 or len(evidence_levels) != 1:
        raise AgentAnnotationValidationError("provider index and runtime evidence modes disagree")
    if require_live_provider_evidence and (
        runtime_bundle.execution_mode != "LIVE"
        or runtime_bundle.evidence_level != "LIVE_PROVIDER_EVIDENCE"
    ):
        raise AgentAnnotationValidationError("live Provider evidence is required for this lane")
    if require_live_provider_evidence:
        candidate_provider_binding_sha256 = _require_frozen_provider_binding(
            repository_root,
            expected_candidate_commit,
            lane="AMAP",
        )
        if expected_provider_binding_sha256 != candidate_provider_binding_sha256:
            raise AgentAnnotationValidationError(
                "AMap expected binding was not derived from the candidate artifact"
            )
    anchored_policy = None
    if runtime_bundle.execution_mode == "LIVE":
        goal_binding = load_candidate_current_goal_binding(
            repository_root,
            expected_candidate_commit,
        )
        expected_exporter_path = _AMAP_CORE_EXPORTER_PATH
        if goal_binding.gate_profile == "HARDENED_CANDIDATE_GATE":
            try:
                anchored_policy = load_anchored_authority_policy(
                    repository_root,
                    expected_candidate_commit,
                )
            except ValueError as exc:
                raise AgentAnnotationValidationError(str(exc)) from exc
            for artifact in (provider_index, runtime_bundle):
                if artifact.authority_policy_sha256 != anchored_policy.sha256:
                    raise AgentAnnotationValidationError(
                        "live Provider authority policy mismatch"
                    )
                if artifact.authority_signature is None:
                    raise AgentAnnotationValidationError(
                        "live Provider authority signature is missing"
                    )
                verify_payload_signature(
                    payload=unsigned_payload(artifact),
                    signature=artifact.authority_signature,
                    manifest=anchored_policy.manifest,
                    expected_role="AMAP_LIVE_EXPORTER",
                )
            expected_exporter_path = anchored_policy.manifest.live_exporter_paths[
                "AMAP_LIVE_EXPORTER"
            ]
        elif any(
            artifact.authority_policy_sha256 is not None
            or artifact.authority_signature is not None
            for artifact in (provider_index, runtime_bundle)
        ):
            raise AgentAnnotationValidationError(
                "CORE AMap receipts cannot claim HARDENED authority evidence"
            )
        if (
            runtime_bundle.exporter_path is None
            or runtime_bundle.exporter_sha256 is None
            or runtime_bundle.exporter_path != expected_exporter_path
            or _git_blob_sha256(
                repository_root,
                expected_candidate_commit,
                runtime_bundle.exporter_path,
            )
            != runtime_bundle.exporter_sha256
        ):
            raise AgentAnnotationValidationError("AMap live exporter binding mismatch")
    if runtime_bundle.generated_at > provider_index.frozen_at:
        raise AgentAnnotationValidationError("provider index was frozen before runtime receipts existed")

    database_snapshot, database_export = _read_external_model(
        Path(runtime_bundle.database_export_receipt_path),
        ProviderDatabaseExportReceipt,
        repository_root,
        artifact_snapshots,
    )
    http_snapshot, http_bundle = _read_external_model(
        Path(runtime_bundle.provider_http_receipt_bundle_path),
        ProviderHttpReceiptBundle,
        repository_root,
        artifact_snapshots,
    )
    database_export_sha256 = database_snapshot.sha256
    http_receipt_sha256 = http_snapshot.sha256
    if (
        database_export_sha256 != expected_database_export_receipt_sha256
        or database_export_sha256 != runtime_bundle.database_export_receipt_sha256
    ):
        raise AgentAnnotationValidationError("provider database export receipt hash mismatch")
    if (
        http_receipt_sha256 != expected_provider_http_receipt_bundle_sha256
        or http_receipt_sha256 != runtime_bundle.provider_http_receipt_bundle_sha256
    ):
        raise AgentAnnotationValidationError("provider HTTP receipt bundle hash mismatch")
    if anchored_policy is not None:
        for artifact in (database_export, http_bundle):
            if artifact.authority_policy_sha256 != anchored_policy.sha256:
                raise AgentAnnotationValidationError("live Provider child policy mismatch")
            if artifact.authority_signature is None:
                raise AgentAnnotationValidationError("live Provider child signature is missing")
            verify_payload_signature(
                payload=unsigned_payload(artifact),
                signature=artifact.authority_signature,
                manifest=anchored_policy.manifest,
                expected_role="AMAP_LIVE_EXPORTER",
            )
    elif any(
        artifact.authority_policy_sha256 is not None
        or artifact.authority_signature is not None
        for artifact in (database_export, http_bundle)
    ):
        raise AgentAnnotationValidationError(
            "CORE AMap child receipts cannot claim HARDENED authority evidence"
        )
    expected_asset_binding = (
        expected_candidate_commit,
        expected_candidate_tree,
        expected_provider_binding_sha256,
    )
    if (
        database_export.goal_id,
        database_export.candidate_commit,
        database_export.candidate_tree,
        database_export.provider_binding_sha256,
    ) != (expected_goal_id, *expected_asset_binding):
        raise AgentAnnotationValidationError("provider database export candidate binding mismatch")
    if (
        http_bundle.goal_id,
        http_bundle.candidate_commit,
        http_bundle.candidate_tree,
        http_bundle.provider_binding_sha256,
    ) != (expected_goal_id, *expected_asset_binding):
        raise AgentAnnotationValidationError("provider HTTP receipt candidate binding mismatch")
    if (
        database_export.execution_mode != runtime_bundle.execution_mode
        or http_bundle.execution_mode != runtime_bundle.execution_mode
    ):
        raise AgentAnnotationValidationError("Provider artifact execution modes disagree")
    if runtime_bundle.execution_mode == "LIVE":
        hardened = goal_binding.gate_profile == "HARDENED_CANDIDATE_GATE"
        expected_database_source = (
            "POSTGRESQL_PROVIDER_EFFECT_REGISTRY"
            if hardened
            else "POSTGRESQL_APPLICATION_TABLES"
        )
        expected_runtime_source = (
            "PERSISTED_PROVIDER_EFFECT_REGISTRY"
            if hardened
            else "PERSISTED_APPLICATION_TABLES"
        )
        if (
            database_export.source_registry != expected_database_source
            or runtime_bundle.source_runtime != expected_runtime_source
        ):
            raise AgentAnnotationValidationError("AMap live persistence source mismatch")

    receipt_by_effect = {item.runtime_effect_id: item for item in provider_index.receipts}
    effect_by_id = {item.effect_id: item for item in runtime_bundle.effects}
    if set(receipt_by_effect) != set(effect_by_id):
        raise AgentAnnotationValidationError(
            "provider index and runtime bundle must reference the same effects exactly once"
        )
    database_by_id = {item.effect_id: item for item in database_export.effects}
    http_by_id = {item.effect_id: item for item in http_bundle.exchanges}
    if set(effect_by_id) != set(database_by_id) or set(effect_by_id) != set(http_by_id):
        raise AgentAnnotationValidationError(
            "runtime, database, and HTTP receipts must cover identical effects"
        )
    for effect_id, receipt in receipt_by_effect.items():
        effect = effect_by_id[effect_id]
        database_effect = database_by_id[effect_id]
        http_effect = http_by_id[effect_id]
        if receipt.receipt_ref != effect.effect_id:
            raise AgentAnnotationValidationError("provider receipt_ref must identify the runtime effect")
        if receipt.observed_at != effect.completed_at:
            raise AgentAnnotationValidationError("provider receipt observation time mismatch")
        if receipt.runtime_effect_receipt_sha256 != canonical_sha256(
            effect.model_dump(mode="json")
        ):
            raise AgentAnnotationValidationError("provider runtime effect receipt hash mismatch")
        expected_facts = (
            receipt.provider_binding_sha256,
            receipt.request_sha256,
            receipt.response_sha256,
            receipt.resolution_status,
            receipt.queried_source_name,
            receipt.queried_city,
            receipt.place_id,
            receipt.name,
            receipt.city,
            receipt.category,
            receipt.accepted_source_names,
        )
        actual_facts = (
            effect.provider_binding_sha256,
            effect.request_sha256,
            effect.response_sha256,
            effect.resolution_status,
            effect.queried_source_name,
            effect.queried_city,
            effect.place_id,
            effect.name,
            effect.city,
            effect.category,
            effect.accepted_source_names,
        )
        if expected_facts != actual_facts:
            raise AgentAnnotationValidationError("provider index does not match runtime effect facts")
        database_facts = (
            database_effect.effect_key_sha256,
            database_effect.provider_binding_sha256,
            database_effect.request_sha256,
            database_effect.response_sha256,
            database_effect.resolution_status,
            database_effect.external_call_count,
            database_effect.started_at,
            database_effect.completed_at,
            database_effect.persisted_status,
        )
        runtime_database_facts = (
            effect.effect_key_sha256,
            effect.provider_binding_sha256,
            effect.request_sha256,
            effect.response_sha256,
            effect.resolution_status,
            effect.external_call_count,
            effect.started_at,
            effect.completed_at,
            effect.status,
        )
        if database_facts != runtime_database_facts:
            raise AgentAnnotationValidationError("database export does not match runtime effect")
        if (
            http_effect.request_sha256,
            http_effect.response_sha256,
            http_effect.external_call_count,
            http_effect.completed_at,
        ) != (
            effect.request_sha256,
            effect.response_sha256,
            effect.external_call_count,
            effect.completed_at,
        ):
            raise AgentAnnotationValidationError("HTTP receipt does not match runtime effect")

    return provider_index, runtime_bundle, {
        "provider_receipt_index_sha256": provider_index_snapshot.sha256,
        "provider_runtime_receipt_bundle_sha256": runtime_bundle_sha256,
        "provider_binding_sha256": expected_provider_binding_sha256,
        "database_export_receipt_sha256": runtime_bundle.database_export_receipt_sha256,
        "provider_http_receipt_bundle_sha256": (
            runtime_bundle.provider_http_receipt_bundle_sha256
        ),
        "live_provider_evidence_verified": (
            runtime_bundle.evidence_level == "LIVE_PROVIDER_EVIDENCE"
        ),
        "evidence_level": runtime_bundle.evidence_level,
    }


def _case_semantics(case: AgentCaseAnnotation) -> dict[str, Any]:
    mentions = []
    for mention in sorted(case.mentions, key=lambda item: (item.span_start, item.span_end, item.role)):
        value = mention.model_dump(mode="json")
        value.pop("mention_id", None)
        mentions.append(value)
    return {
        "case_id": case.case_id,
        "source_sha256": case.source_sha256,
        "destination_name": case.destination_name,
        "destination_basis": case.destination_basis,
        "destination_evidence_span_start": case.destination_evidence_span_start,
        "destination_evidence_span_end": case.destination_evidence_span_end,
        "destination_evidence_raw_text": case.destination_evidence_raw_text,
        "mentions": mentions,
    }


def _conflict_sha256(left: AgentCaseAnnotation, right: AgentCaseAnnotation) -> str:
    pair = sorted((_case_semantics(left), _case_semantics(right)), key=canonical_sha256)
    return canonical_sha256({"case_id": left.case_id, "independent_annotations": pair})


def _validate_agent_annotation_snapshot(
    path: Path,
    *,
    split: str,
    source_cases: list[TextCardInputCase],
    repository_root: Path,
) -> tuple[ArtifactSnapshot, AgentAnnotationBundle]:
    snapshot, bundle = _read_external_model(path, AgentAnnotationBundle, repository_root)
    if bundle.split != split:
        raise AgentAnnotationValidationError("agent annotation bundle split mismatch")
    expected_ids = [case.case_id for case in source_cases]
    if [case.case_id for case in bundle.cases] != expected_ids:
        raise AgentAnnotationValidationError("agent annotation bundle must cover the split in source order")
    source_by_id = {case.case_id: case for case in source_cases}
    for case in bundle.cases:
        try:
            validate_agent_case_annotation(case, source_by_id[case.case_id])
        except ValueError as exc:
            raise AgentAnnotationValidationError(str(exc)) from exc
    return snapshot, bundle


def validate_agent_annotation_bundle(
    path: Path,
    *,
    split: str,
    source_cases: list[TextCardInputCase],
    repository_root: Path,
) -> AgentAnnotationBundle:
    _snapshot, bundle = _validate_agent_annotation_snapshot(
        path,
        split=split,
        source_cases=source_cases,
        repository_root=repository_root,
    )
    return bundle


def verify_agent_adjudication(
    *,
    split: str,
    source_cases: list[TextCardInputCase],
    first_path: Path,
    second_path: Path,
    adjudication_path: Path,
    provider_receipt_index_path: Path,
    provider_runtime_receipt_bundle_path: Path,
    repository_root: Path,
    expected_candidate_commit: str,
    expected_candidate_tree: str,
    expected_provider_binding_sha256: str,
    expected_runtime_receipt_bundle_sha256: str,
    expected_database_export_receipt_sha256: str,
    expected_provider_http_receipt_bundle_sha256: str,
    require_live_provider_evidence: bool,
) -> tuple[AgentAdjudicationBundle, dict[str, Any]]:
    first_snapshot, first = _validate_agent_annotation_snapshot(
        first_path,
        split=split,
        source_cases=source_cases,
        repository_root=repository_root,
    )
    second_snapshot, second = _validate_agent_annotation_snapshot(
        second_path,
        split=split,
        source_cases=source_cases,
        repository_root=repository_root,
    )
    if first.assignment_id == second.assignment_id:
        raise AgentAnnotationValidationError("agent annotations require distinct assignment IDs")
    if first.attestation.task_id == second.attestation.task_id:
        raise AgentAnnotationValidationError("agent annotations require distinct isolated tasks")
    if {first.attestation.task_role, second.attestation.task_role} != {"ANNOTATOR_A", "ANNOTATOR_B"}:
        raise AgentAnnotationValidationError("annotation panel requires ANNOTATOR_A and ANNOTATOR_B")
    if first.attestation.input_bundle_sha256 != second.attestation.input_bundle_sha256:
        raise AgentAnnotationValidationError("annotators must bind the same input bundle")
    provider_index, _runtime_bundle, provider_verification = validate_provider_receipt_assets(
        split=split,
        provider_receipt_index_path=provider_receipt_index_path,
        provider_runtime_receipt_bundle_path=provider_runtime_receipt_bundle_path,
        repository_root=repository_root,
        expected_candidate_commit=expected_candidate_commit,
        expected_candidate_tree=expected_candidate_tree,
        expected_goal_id="TC-VNEXT-G01-TEXT-CARDS",
        expected_provider_binding_sha256=expected_provider_binding_sha256,
        expected_runtime_receipt_bundle_sha256=expected_runtime_receipt_bundle_sha256,
        expected_database_export_receipt_sha256=(
            expected_database_export_receipt_sha256
        ),
        expected_provider_http_receipt_bundle_sha256=(
            expected_provider_http_receipt_bundle_sha256
        ),
        require_live_provider_evidence=require_live_provider_evidence,
    )
    if first.attestation.subject_commit != expected_candidate_commit:
        raise AgentAnnotationValidationError("annotator candidate commit binding mismatch")
    if first.attestation.subject_tree != expected_candidate_tree:
        raise AgentAnnotationValidationError("annotator candidate tree binding mismatch")

    provider_index_sha256 = provider_verification["provider_receipt_index_sha256"]
    expected_input_bundle_sha256 = agent_input_bundle_sha256(
        split,
        source_cases,
        provider_index_sha256,
    )
    if first.attestation.input_bundle_sha256 != expected_input_bundle_sha256:
        raise AgentAnnotationValidationError("annotator input bundle hash mismatch")
    if first.attestation.prompt_sha256 != second.attestation.prompt_sha256:
        raise AgentAnnotationValidationError("annotators must use the same frozen prompt")
    reference_prompt_path = (
        "backend/eval_data/trip_text_cards_agent_v2/prompts/reference.md"
    )
    annotation_schema_path = (
        "backend/eval_data/trip_text_cards_agent_v2/agent_annotation.schema.json"
    )
    if first.attestation.prompt_sha256 != _git_blob_sha256(
        repository_root,
        expected_candidate_commit,
        reference_prompt_path,
    ):
        raise AgentAnnotationValidationError("annotator prompt hash binding mismatch")
    if {
        first.attestation.output_schema_sha256,
        second.attestation.output_schema_sha256,
    } != {
        _git_blob_sha256(
            repository_root,
            expected_candidate_commit,
            annotation_schema_path,
        )
    }:
        raise AgentAnnotationValidationError("annotator output schema hash binding mismatch")
    if first.attestation.subject_commit != second.attestation.subject_commit:
        raise AgentAnnotationValidationError("annotators must bind the same subject commit")
    if first.attestation.subject_tree != second.attestation.subject_tree:
        raise AgentAnnotationValidationError("annotators must bind the same subject tree")
    if provider_index.frozen_at > min(first.attestation.started_at, second.attestation.started_at):
        raise AgentAnnotationValidationError("annotators started before the provider index was frozen")

    adjudication_snapshot, adjudication = _read_external_model(
        adjudication_path,
        AgentAdjudicationBundle,
        repository_root,
    )
    if adjudication.split != split:
        raise AgentAnnotationValidationError("agent adjudication split mismatch")
    if set(adjudication.source_assignment_ids) != {first.assignment_id, second.assignment_id}:
        raise AgentAnnotationValidationError("agent adjudication assignment binding mismatch")
    source_hashes = {first_snapshot.sha256, second_snapshot.sha256}
    if set(adjudication.source_bundle_sha256) != source_hashes:
        raise AgentAnnotationValidationError("agent adjudication source byte binding mismatch")
    if adjudication.attestation.task_id in {
        first.attestation.task_id,
        second.attestation.task_id,
    }:
        raise AgentAnnotationValidationError("agent adjudicator must use a fresh isolated task")
    if adjudication.attestation.subject_commit != first.attestation.subject_commit:
        raise AgentAnnotationValidationError("agent adjudication subject commit mismatch")
    if adjudication.attestation.subject_tree != first.attestation.subject_tree:
        raise AgentAnnotationValidationError("agent adjudication subject tree mismatch")
    if adjudication.attestation.started_at < max(
        first.attestation.frozen_at,
        second.attestation.frozen_at,
    ):
        raise AgentAnnotationValidationError("agent adjudication cannot start before both source bundles are frozen")
    adjudication_prompt_path = (
        "backend/eval_data/trip_text_cards_agent_v2/prompts/adjudication.md"
    )
    adjudication_schema_path = (
        "backend/eval_data/trip_text_cards_agent_v2/agent_adjudication.schema.json"
    )
    if adjudication.attestation.prompt_sha256 != _git_blob_sha256(
        repository_root,
        expected_candidate_commit,
        adjudication_prompt_path,
    ):
        raise AgentAnnotationValidationError("agent adjudication prompt hash binding mismatch")
    if adjudication.attestation.output_schema_sha256 != _git_blob_sha256(
        repository_root,
        expected_candidate_commit,
        adjudication_schema_path,
    ):
        raise AgentAnnotationValidationError("agent adjudication output schema hash binding mismatch")
    if adjudication.attestation.input_bundle_sha256 != canonical_sha256(
        {
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "candidate_commit": expected_candidate_commit,
            "candidate_tree": expected_candidate_tree,
            "provider_receipt_index_sha256": provider_index_sha256,
            "provider_runtime_receipt_bundle_sha256": (
                expected_runtime_receipt_bundle_sha256
            ),
            "source_bundle_sha256": sorted(source_hashes),
        }
    ):
        raise AgentAnnotationValidationError("agent adjudication input bundle hash mismatch")

    receipt_by_id = {item.receipt_id: item for item in provider_index.receipts}

    expected_ids = [case.case_id for case in source_cases]
    if [case.case_id for case in adjudication.agent_reference_cases] != expected_ids:
        raise AgentAnnotationValidationError("agent reference must cover the split in source order")
    source_by_id = {case.case_id: case for case in source_cases}
    first_by_id = {case.case_id: case for case in first.cases}
    second_by_id = {case.case_id: case for case in second.cases}
    reference_by_id = {case.case_id: case for case in adjudication.agent_reference_cases}
    for case in adjudication.agent_reference_cases:
        try:
            validate_agent_case_annotation(case, source_by_id[case.case_id])
        except ValueError as exc:
            raise AgentAnnotationValidationError(str(exc)) from exc

    expected_conflicts: dict[str, str] = {}
    for case_id in expected_ids:
        left = first_by_id[case_id]
        right = second_by_id[case_id]
        if _case_semantics(left) == _case_semantics(right):
            if _case_semantics(reference_by_id[case_id]) != _case_semantics(left):
                raise AgentAnnotationValidationError(f"{case_id} agreed reference changed during adjudication")
        else:
            expected_conflicts[case_id] = _conflict_sha256(left, right)
    actual_conflicts = {item.case_id: item.conflict_sha256 for item in adjudication.conflicts}
    if actual_conflicts != expected_conflicts:
        raise AgentAnnotationValidationError("agent adjudication conflict set or fingerprint mismatch")

    executable_mentions = sum(
        mention.executable_place
        for case in adjudication.agent_reference_cases
        for mention in case.mentions
    )
    canonical_mentions = sum(
        mention.executable_place and mention.canonical_place is not None
        for case in adjudication.agent_reference_cases
        for mention in case.mentions
    )
    gold_executable_minimum_met = split == "dev" or executable_mentions >= 65
    if not gold_executable_minimum_met:
        raise AgentAnnotationValidationError(f"{split} requires at least 65 executable mentions")
    provider_bound_mentions = sum(
        mention.executable_place and mention.provider_resolution_receipt is not None
        for case in adjudication.agent_reference_cases
        for mention in case.mentions
    )
    if provider_bound_mentions != executable_mentions:
        raise AgentAnnotationValidationError("every executable agent reference requires a live provider receipt")
    for case in adjudication.agent_reference_cases:
        for mention in case.mentions:
            resolution_ref = mention.provider_resolution_receipt
            if resolution_ref is None:
                continue
            receipt = receipt_by_id.get(resolution_ref.receipt_id)
            if receipt is None:
                raise AgentAnnotationValidationError("place resolution references an unknown provider receipt")
            expected_receipt = (
                resolution_ref.provider,
                resolution_ref.execution_mode,
                resolution_ref.provider_binding_sha256,
                resolution_ref.request_sha256,
                resolution_ref.response_sha256,
                resolution_ref.resolution_status,
                resolution_ref.queried_source_name,
                resolution_ref.queried_city,
                resolution_ref.accepted_source_name,
                resolution_ref.receipt_ref,
                resolution_ref.observed_at,
                resolution_ref.authorization_basis,
                resolution_ref.raw_response_in_git,
                resolution_ref.retention,
                resolution_ref.runtime_effect_id,
                resolution_ref.runtime_effect_receipt_sha256,
            )
            actual_receipt = (
                receipt.provider,
                receipt.execution_mode,
                receipt.provider_binding_sha256,
                receipt.request_sha256,
                receipt.response_sha256,
                receipt.resolution_status,
                receipt.queried_source_name,
                receipt.queried_city,
                receipt.accepted_source_name,
                receipt.receipt_ref,
                receipt.observed_at,
                receipt.authorization_basis,
                receipt.raw_response_in_git,
                receipt.retention,
                receipt.runtime_effect_id,
                receipt.runtime_effect_receipt_sha256,
            )
            if expected_receipt != actual_receipt:
                raise AgentAnnotationValidationError("place resolution does not match its provider receipt")
            canonical = mention.canonical_place
            if canonical is None:
                continue
            if mention.raw_text not in receipt.accepted_source_names:
                raise AgentAnnotationValidationError(
                    "matched source text is not a provider-bound accepted place name"
                )
            expected = (
                canonical.place_id,
                canonical.name,
                canonical.city,
                canonical.category,
                canonical.provider_receipt.request_sha256,
                canonical.provider_receipt.response_sha256,
                canonical.provider_receipt.resolution_status,
            )
            actual = (
                receipt.place_id,
                receipt.name,
                receipt.city,
                receipt.category,
                receipt.request_sha256,
                receipt.response_sha256,
                receipt.resolution_status,
            )
            if expected != actual:
                raise AgentAnnotationValidationError("canonical place does not match its provider receipt")

    return adjudication, {
        "schema_version": "g01-text-card-agent-adjudication-verification-receipt-v2",
        "split": split,
        "case_count": len(source_cases),
        "annotator_task_count": 2,
        "adjudicator_task_count": 1,
        "tasks_distinct": True,
        "context_isolation": "PROCESS_AND_PROMPT_ISOLATION",
        "conflict_count": len(expected_conflicts),
        "conflicts_adjudicated": len(expected_conflicts),
        "evidence_span_validity": 1.0,
        "agent_reference_executable_mentions": executable_mentions,
        "provider_bound_executable_mentions": provider_bound_mentions,
        "canonical_provider_bound_mentions": canonical_mentions,
        "gold_executable_minimum_met": gold_executable_minimum_met,
        "source_bundle_sha256": sorted(source_hashes),
        "adjudication_sha256": adjudication_snapshot.sha256,
        **provider_verification,
        "evidence_level": "MULTI_AGENT_SIMULATED_REVIEW",
        "human_evidence": False,
    }


def build_blank_agent_work_packet(
    *,
    split: str,
    assignment_id: str,
    source_cases: list[TextCardInputCase],
    prompt_sha256: str,
    provider_receipt_index: ProviderReceiptIndex,
    provider_receipt_index_sha256: str,
) -> dict[str, Any]:
    if split not in {"dev", "validation"}:
        raise AgentAnnotationValidationError("ordinary agent work packets cannot contain frozen_blind")
    return {
        "schema_version": "g01-text-card-agent-work-packet-v2",
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "dataset_version": "g01-text-card-dataset-v1",
        "assignment_id": assignment_id,
        "split": split,
        "prompt_sha256": prompt_sha256,
        "input_bundle_sha256": agent_input_bundle_sha256(
            split,
            source_cases,
            provider_receipt_index_sha256,
        ),
        "provider_receipt_index_sha256": provider_receipt_index_sha256,
        "candidate_commit": provider_receipt_index.subject_commit,
        "candidate_tree": provider_receipt_index.subject_tree,
        "provider_binding_sha256": provider_receipt_index.provider_binding_sha256,
        "provider_runtime_receipt_bundle_sha256": (
            provider_receipt_index.runtime_receipt_bundle_sha256
        ),
        "provider_receipts_present": True,
        "provider_receipts": provider_receipt_index.model_dump(mode="json")["receipts"],
        "status": "BLANK_AGENT_WORK_PACKET",
        "candidate_predictions_included": False,
        "peer_labels_included": False,
        "cases": [
            {
                "case_id": case.case_id,
                "source_sha256": case.normalized_input_sha256,
                "input_text": case.input_text,
                "annotation": None,
            }
            for case in source_cases
        ],
    }
