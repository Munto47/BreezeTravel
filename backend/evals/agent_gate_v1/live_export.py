from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from pydantic import BaseModel

from evals.agent_gate_v1.authority import (
    git_blob_sha256,
)
from evals.agent_gate_v1.contracts import DetachedAuthoritySignature
from evals.agent_gate_v1.host_tools import trusted_host_tool
from evals.agent_gate_v1.signing import verify_payload_signature
from evals.trip_text_cards_agent_v2.contracts import (
    AgentInferenceCaseOutputV2,
    InferenceDatabaseEffectRecord,
    InferenceDatabaseExportReceipt,
    InferenceEffectReceipt,
    InferenceHttpExchangeReceipt,
    InferenceHttpReceiptBundle,
    InferenceRuntimeReceiptBundle,
    ProviderDatabaseEffectRecord,
    ProviderDatabaseExportReceipt,
    ProviderHttpExchangeReceipt,
    ProviderHttpReceiptBundle,
    ProviderPlaceReceiptRecord,
    ProviderReceiptIndex,
    ProviderRuntimeEffectReceipt,
    ProviderRuntimeReceiptBundle,
)
from evals.trip_text_cards_v1.contracts import canonical_sha256


class LiveEvidenceExportError(ValueError):
    pass


LiveLane = Literal["AMAP", "QWEN"]

# Only typed Provider-effect rows are authoritative. No generic JSON artifact,
# preassembled receipt, aggregate, query, table, or payload is caller supplied.
AMAP_EFFECT_QUERY = """\
SELECT split, effect_id, effect_key_sha256, provider_binding_sha256,
       request_sha256, response_sha256, provider_request_id_sha256,
       http_status, resolution_status, accepted_source_name,
       place_id, place_name, city, category, accepted_source_names,
       started_at, completed_at
FROM trip_g01_amap_provider_effects
WHERE evidence_run_id = %s AND candidate_commit = %s
ORDER BY effect_id
"""

QWEN_EFFECT_QUERY = """\
SELECT split, effect_id, case_id, input_sha256, request_sha256,
       response_sha256, provider_request_id_sha256, http_status,
       output_sha256, inference_output_json, input_tokens, output_tokens,
       latency_ms, repair_call_count, provider_binding_sha256,
       model_binding_sha256, exact_model_id, region, endpoint_sha256,
       prompt_sha256, schema_sha256, config_sha256, started_at, completed_at
FROM trip_g01_qwen_inference_effects
WHERE evidence_run_id = %s AND candidate_commit = %s
ORDER BY case_id
"""

LIVE_EFFECT_QUERIES: dict[LiveLane, str] = {
    "AMAP": AMAP_EFFECT_QUERY,
    "QWEN": QWEN_EFFECT_QUERY,
}


def _git(root: Path, *args: str, text: bool = True):
    result = subprocess.run(
        [trusted_host_tool("git"), "-C", str(root), *args],
        check=False,
        capture_output=True,
        text=text,
    )
    if result.returncode != 0:
        raise LiveEvidenceExportError(f"Git live-export readback failed: {' '.join(args)}")
    return result.stdout.strip() if text else result.stdout


def _git_json(root: Path, commit: str, path: str) -> dict[str, Any]:
    try:
        value = json.loads(_git(root, "show", f"{commit}:{path}", text=False))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveEvidenceExportError(f"candidate JSON is invalid: {path}") from exc
    if not isinstance(value, dict):
        raise LiveEvidenceExportError(f"candidate JSON must be an object: {path}")
    return value


def _serialized_model(value: BaseModel) -> bytes:
    return (
        json.dumps(
            value.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _serialized_jsonl(values: list[BaseModel]) -> bytes:
    return (
        "\n".join(
            json.dumps(
                item.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in values
        )
        + "\n"
    ).encode("utf-8")


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _externally_signed_model(
    *,
    model_type,
    unsigned: dict[str, Any],
    lane: LiveLane,
    authority_signature: DetachedAuthoritySignature,
    manifest,
):
    role = "AMAP_LIVE_EXPORTER" if lane == "AMAP" else "QWEN_LIVE_EXPORTER"
    try:
        verify_payload_signature(
            payload=unsigned,
            signature=authority_signature,
            manifest=manifest,
            expected_role=role,
        )
    except ValueError as exc:
        raise LiveEvidenceExportError("external live-export signature is invalid") from exc
    return model_type.model_validate(
        {
            **unsigned,
            "authority_signature": authority_signature.model_dump(mode="json"),
        }
    )


def _load_candidate_bindings(
    root: Path,
    candidate_commit: str,
    lane: LiveLane,
) -> dict[str, Any]:
    provider_path = "backend/eval_data/trip_text_cards_agent_v2/provider_binding.json"
    provider = _git_json(root, candidate_commit, provider_path)
    if provider.get("status") != "FROZEN":
        raise LiveEvidenceExportError("candidate Provider binding is not FROZEN")
    values: dict[str, Any] = {
        "provider_binding_sha256": git_blob_sha256(root, candidate_commit, provider_path)
    }
    if lane == "AMAP":
        required = (
            provider.get("amap_place_search"),
            provider.get("amap_walking"),
            provider.get("amap_transit"),
        )
        if any(
            not isinstance(item, str) or not item.strip() or "PENDING" in item
            for item in required
        ):
            raise LiveEvidenceExportError("candidate AMap binding is incomplete")
        return values

    panel_path = "backend/eval_data/trip_text_cards_agent_v2/qwen_model_panel.json"
    panel = _git_json(root, candidate_commit, panel_path)
    qwen = provider.get("qwen")
    if not isinstance(qwen, dict):
        raise LiveEvidenceExportError("candidate Qwen Provider binding must be structured")
    exact_model_id = qwen.get("exact_model_id")
    region = qwen.get("region")
    endpoint_sha256 = qwen.get("endpoint_sha256")
    if (
        panel.get("status") != "FROZEN"
        or panel.get("frozen_candidate") != exact_model_id
        or panel.get("region") != region
        or not isinstance(exact_model_id, str)
        or not isinstance(region, str)
        or not isinstance(endpoint_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", endpoint_sha256)
    ):
        raise LiveEvidenceExportError("candidate Qwen model panel is not exactly frozen")
    values.update(
        {
            "exact_model_id": exact_model_id,
            "region": region,
            "endpoint_sha256": endpoint_sha256,
            "model_binding_sha256": git_blob_sha256(root, candidate_commit, panel_path),
            "prompt_sha256": git_blob_sha256(
                root,
                candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_prompt.md",
            ),
            "schema_sha256": git_blob_sha256(
                root,
                candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/agent_inference_case_output.schema.json",
            ),
            "config_sha256": git_blob_sha256(
                root,
                candidate_commit,
                "backend/eval_data/trip_text_cards_agent_v2/qwen_inference_config.json",
            ),
        }
    )
    return values


def _read_typed_effects(
    *,
    database_url: str,
    evidence_run_id: str,
    lane: LiveLane,
    candidate_commit: str,
) -> tuple[list[dict[str, Any]], str, str]:
    if not re.fullmatch(r"G01-LIVE-[A-Z0-9-]{8,100}", evidence_run_id):
        raise LiveEvidenceExportError("invalid G01 live evidence run ID")
    query = LIVE_EFFECT_QUERIES[lane]
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        with connection.transaction():
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            identity = connection.execute(
                """
                SELECT current_database() AS database_name,
                       COALESCE(inet_server_addr()::text, 'local') AS server_address,
                       COALESCE(inet_server_port(), 0) AS server_port,
                       txid_current_snapshot()::text AS transaction_snapshot
                """
            ).fetchone()
            rows = connection.execute(query, (evidence_run_id, candidate_commit)).fetchall()
    if identity is None:
        raise LiveEvidenceExportError("database identity readback failed")
    if not rows:
        raise LiveEvidenceExportError(f"no typed {lane} live Provider effects were found")
    typed_rows = [dict(row) for row in rows]
    database_instance_sha256 = canonical_sha256(
        {
            "database_name": identity["database_name"],
            "server_address": identity["server_address"],
            "server_port": identity["server_port"],
        }
    )
    transaction_snapshot_sha256 = canonical_sha256(
        {
            "transaction_snapshot": identity["transaction_snapshot"],
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "typed_rows": _jsonable(typed_rows),
        }
    )
    return typed_rows, database_instance_sha256, transaction_snapshot_sha256


def _single_split(rows: list[dict[str, Any]]) -> str:
    splits = {str(row["split"]) for row in rows}
    if len(splits) != 1:
        raise LiveEvidenceExportError("one live export cannot mix evaluation splits")
    split = splits.pop()
    if split not in {"dev", "validation", "frozen_blind"}:
        raise LiveEvidenceExportError("typed live effects contain an invalid split")
    return split


def _paths(output_dir: Path, lane: LiveLane, evidence_run_id: str) -> dict[str, Path]:
    prefix = f"{evidence_run_id.lower()}-{lane.lower()}"
    names = (
        {
            "DATABASE": f"{prefix}-database.json",
            "HTTP": f"{prefix}-http.json",
            "RUNTIME": f"{prefix}-runtime.json",
            "INDEX": f"{prefix}-index.json",
        }
        if lane == "AMAP"
        else {
            "PREDICTIONS": f"{prefix}-predictions.jsonl",
            "INFERENCE_OUTPUTS": f"{prefix}-outputs.jsonl",
            "DATABASE": f"{prefix}-database.json",
            "HTTP": f"{prefix}-http.json",
            "RUNTIME": f"{prefix}-runtime.json",
        }
    )
    return {key: output_dir / value for key, value in names.items()}


def _amap_effect_models(rows: list[dict[str, Any]], binding: dict[str, Any]):
    database_effects: list[ProviderDatabaseEffectRecord] = []
    exchanges: list[ProviderHttpExchangeReceipt] = []
    runtime_effects: list[ProviderRuntimeEffectReceipt] = []
    receipts: list[ProviderPlaceReceiptRecord] = []
    for row in rows:
        if row["provider_binding_sha256"] != binding["provider_binding_sha256"]:
            raise LiveEvidenceExportError("AMap effect used a different Provider binding")
        runtime = ProviderRuntimeEffectReceipt.model_validate(
            {
                "effect_id": row["effect_id"],
                "effect_key_sha256": row["effect_key_sha256"],
                "provider": "AMAP",
                "execution_mode": "LIVE",
                "provider_binding_sha256": row["provider_binding_sha256"],
                "request_sha256": row["request_sha256"],
                "response_sha256": row["response_sha256"],
                "resolution_status": row["resolution_status"],
                "place_id": row["place_id"],
                "name": row["place_name"],
                "city": row["city"],
                "category": row["category"],
                "accepted_source_names": list(row["accepted_source_names"] or []),
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "status": "SUCCEEDED",
                "raw_response_in_repository": False,
            }
        )
        database_effects.append(
            ProviderDatabaseEffectRecord.model_validate(
                {
                    "effect_id": runtime.effect_id,
                    "effect_key_sha256": runtime.effect_key_sha256,
                    "provider_binding_sha256": runtime.provider_binding_sha256,
                    "request_sha256": runtime.request_sha256,
                    "response_sha256": runtime.response_sha256,
                    "resolution_status": runtime.resolution_status,
                    "started_at": runtime.started_at,
                    "completed_at": runtime.completed_at,
                    "persisted_status": "SUCCEEDED",
                }
            )
        )
        exchanges.append(
            ProviderHttpExchangeReceipt.model_validate(
                {
                    "effect_id": runtime.effect_id,
                    "request_sha256": runtime.request_sha256,
                    "response_sha256": runtime.response_sha256,
                    "http_status": row["http_status"],
                    "provider_status": "SUCCESS",
                    "provider_request_id_sha256": row["provider_request_id_sha256"],
                    "completed_at": runtime.completed_at,
                    "raw_response_retained": False,
                }
            )
        )
        receipts.append(
            ProviderPlaceReceiptRecord.model_validate(
                {
                    "receipt_id": runtime.effect_id,
                    "provider": "AMAP",
                    "execution_mode": "LIVE",
                    "provider_binding_sha256": runtime.provider_binding_sha256,
                    "receipt_ref": runtime.effect_id,
                    "runtime_effect_id": runtime.effect_id,
                    "runtime_effect_receipt_sha256": canonical_sha256(
                        runtime.model_dump(mode="json")
                    ),
                    "request_sha256": runtime.request_sha256,
                    "response_sha256": runtime.response_sha256,
                    "observed_at": runtime.completed_at,
                    "resolution_status": runtime.resolution_status,
                    "accepted_source_name": row["accepted_source_name"],
                    "place_id": runtime.place_id,
                    "name": runtime.name,
                    "city": runtime.city,
                    "category": runtime.category,
                    "accepted_source_names": runtime.accepted_source_names,
                }
            )
        )
        runtime_effects.append(runtime)
    return database_effects, exchanges, runtime_effects, receipts


def _build_amap(
    *,
    rows: list[dict[str, Any]],
    paths: dict[str, Path],
    root: Path,
    commit: str,
    tree: str,
    exporter_path: str,
    exporter_sha256: str,
    binding: dict[str, Any],
    database_instance_sha256: str,
    transaction_snapshot_sha256: str,
    authority_signatures: dict[str, DetachedAuthoritySignature],
    manifest,
    policy_sha256: str,
) -> dict[str, bytes]:
    split = _single_split(rows)
    database_effects, exchanges, runtime_effects, receipts = _amap_effect_models(
        rows, binding
    )
    now = datetime.now(UTC)
    common = {
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "provider_binding_sha256": binding["provider_binding_sha256"],
        "execution_mode": "LIVE",
        "authority_policy_sha256": policy_sha256,
    }
    database = _externally_signed_model(
        model_type=ProviderDatabaseExportReceipt,
        unsigned={
            **common,
            "schema_version": "g01-amap-database-export-receipt-v2",
            "source_registry": "POSTGRESQL_PROVIDER_EFFECT_REGISTRY",
            "query_sha256": hashlib.sha256(AMAP_EFFECT_QUERY.encode()).hexdigest(),
            "transaction_snapshot_sha256": transaction_snapshot_sha256,
            "database_instance_sha256": database_instance_sha256,
            "exported_at": now,
            "effects": [item.model_dump(mode="json") for item in database_effects],
        },
        lane="AMAP",
        authority_signature=authority_signatures["DATABASE"],
        manifest=manifest,
    )
    http = _externally_signed_model(
        model_type=ProviderHttpReceiptBundle,
        unsigned={
            **common,
            "schema_version": "g01-amap-http-receipt-bundle-v2",
            "captured_at": now,
            "exchanges": [item.model_dump(mode="json") for item in exchanges],
        },
        lane="AMAP",
        authority_signature=authority_signatures["HTTP"],
        manifest=manifest,
    )
    database_bytes = _serialized_model(database)
    http_bytes = _serialized_model(http)
    runtime = _externally_signed_model(
        model_type=ProviderRuntimeReceiptBundle,
        unsigned={
            **common,
            "schema_version": "g01-amap-runtime-receipt-bundle-v2",
            "database_export_receipt_path": str(paths["DATABASE"].resolve()),
            "database_export_receipt_sha256": hashlib.sha256(database_bytes).hexdigest(),
            "provider_http_receipt_bundle_path": str(paths["HTTP"].resolve()),
            "provider_http_receipt_bundle_sha256": hashlib.sha256(http_bytes).hexdigest(),
            "generated_at": now,
            "generated_by": "G01_AMAP_LIVE_RECEIPT_EXPORTER",
            "source_runtime": "PERSISTED_PROVIDER_EFFECT_REGISTRY",
            "evidence_level": "LIVE_PROVIDER_EVIDENCE",
            "exporter_path": exporter_path,
            "exporter_sha256": exporter_sha256,
            "effects": [item.model_dump(mode="json") for item in runtime_effects],
        },
        lane="AMAP",
        authority_signature=authority_signatures["RUNTIME"],
        manifest=manifest,
    )
    runtime_bytes = _serialized_model(runtime)
    index = _externally_signed_model(
        model_type=ProviderReceiptIndex,
        unsigned={
            "schema_version": "g01-text-card-provider-receipt-index-v2",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "dataset_version": "g01-text-card-dataset-v1",
            "split": split,
            "subject_commit": commit,
            "subject_tree": tree,
            "provider_binding_sha256": binding["provider_binding_sha256"],
            "execution_mode": "LIVE",
            "evidence_level": "LIVE_PROVIDER_EVIDENCE",
            "runtime_receipt_bundle_sha256": hashlib.sha256(runtime_bytes).hexdigest(),
            "frozen_at": now,
            "receipts": [item.model_dump(mode="json") for item in receipts],
            "authority_policy_sha256": policy_sha256,
        },
        lane="AMAP",
        authority_signature=authority_signatures["INDEX"],
        manifest=manifest,
    )
    return {
        "DATABASE": database_bytes,
        "HTTP": http_bytes,
        "RUNTIME": runtime_bytes,
        "INDEX": _serialized_model(index),
    }


def _qwen_effect_models(rows: list[dict[str, Any]], binding: dict[str, Any]):
    outputs: list[AgentInferenceCaseOutputV2] = []
    database_effects: list[InferenceDatabaseEffectRecord] = []
    exchanges: list[InferenceHttpExchangeReceipt] = []
    runtime_effects: list[InferenceEffectReceipt] = []
    required = {
        key: binding[key]
        for key in (
            "provider_binding_sha256",
            "model_binding_sha256",
            "exact_model_id",
            "region",
            "endpoint_sha256",
            "prompt_sha256",
            "schema_sha256",
            "config_sha256",
        )
    }
    for row in rows:
        if any(row[key] != value for key, value in required.items()):
            raise LiveEvidenceExportError("Qwen effect used a different frozen runtime binding")
        output = AgentInferenceCaseOutputV2.model_validate(row["inference_output_json"])
        if (
            output.case_id != row["case_id"]
            or canonical_sha256(output.model_dump(mode="json")) != row["output_sha256"]
        ):
            raise LiveEvidenceExportError("persisted Qwen output identity or hash mismatch")
        runtime = InferenceEffectReceipt.model_validate(
            {
                "effect_id": row["effect_id"],
                "case_id": row["case_id"],
                "input_sha256": row["input_sha256"],
                "request_sha256": row["request_sha256"],
                "response_sha256": row["response_sha256"],
                "provider_request_id_sha256": row["provider_request_id_sha256"],
                "output_sha256": row["output_sha256"],
                "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"],
                "latency_ms": row["latency_ms"],
                "repair_call_count": row["repair_call_count"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "status": "SUCCEEDED",
            }
        )
        database_effects.append(
            InferenceDatabaseEffectRecord.model_validate(
                {
                    "effect_id": runtime.effect_id,
                    "case_id": runtime.case_id,
                    "request_sha256": runtime.request_sha256,
                    "response_sha256": runtime.response_sha256,
                    "output_sha256": runtime.output_sha256,
                    "started_at": runtime.started_at,
                    "completed_at": runtime.completed_at,
                    "persisted_status": "SUCCEEDED",
                }
            )
        )
        exchanges.append(
            InferenceHttpExchangeReceipt.model_validate(
                {
                    "effect_id": runtime.effect_id,
                    "case_id": runtime.case_id,
                    "request_sha256": runtime.request_sha256,
                    "response_sha256": runtime.response_sha256,
                    "provider_request_id_sha256": runtime.provider_request_id_sha256,
                    "http_status": row["http_status"],
                    "completed_at": runtime.completed_at,
                    "raw_response_retained": False,
                }
            )
        )
        outputs.append(output)
        runtime_effects.append(runtime)
    return outputs, database_effects, exchanges, runtime_effects


def _build_qwen(
    *,
    rows: list[dict[str, Any]],
    paths: dict[str, Path],
    root: Path,
    commit: str,
    tree: str,
    exporter_path: str,
    exporter_sha256: str,
    binding: dict[str, Any],
    database_instance_sha256: str,
    transaction_snapshot_sha256: str,
    authority_signatures: dict[str, DetachedAuthoritySignature],
    manifest,
    policy_sha256: str,
) -> dict[str, bytes]:
    split = _single_split(rows)
    outputs, database_effects, exchanges, runtime_effects = _qwen_effect_models(
        rows, binding
    )
    predictions = [item.text_card_prediction for item in outputs]
    predictions_bytes = _serialized_jsonl(predictions)
    outputs_bytes = _serialized_jsonl(outputs)
    now = datetime.now(UTC)
    common = {
        "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
        "candidate_commit": commit,
        "candidate_tree": tree,
        "model_binding_sha256": binding["model_binding_sha256"],
        "provider_binding_sha256": binding["provider_binding_sha256"],
        "execution_mode": "LIVE",
        "authority_policy_sha256": policy_sha256,
    }
    database = _externally_signed_model(
        model_type=InferenceDatabaseExportReceipt,
        unsigned={
            **common,
            "schema_version": "g01-qwen-database-export-receipt-v1",
            "source_registry": "POSTGRESQL_INFERENCE_EFFECT_REGISTRY",
            "query_sha256": hashlib.sha256(QWEN_EFFECT_QUERY.encode()).hexdigest(),
            "transaction_snapshot_sha256": transaction_snapshot_sha256,
            "database_instance_sha256": database_instance_sha256,
            "exported_at": now,
            "effects": [item.model_dump(mode="json") for item in database_effects],
        },
        lane="QWEN",
        authority_signature=authority_signatures["DATABASE"],
        manifest=manifest,
    )
    http = _externally_signed_model(
        model_type=InferenceHttpReceiptBundle,
        unsigned={
            **common,
            "schema_version": "g01-qwen-http-receipt-bundle-v1",
            "captured_at": now,
            "exchanges": [item.model_dump(mode="json") for item in exchanges],
        },
        lane="QWEN",
        authority_signature=authority_signatures["HTTP"],
        manifest=manifest,
    )
    database_bytes = _serialized_model(database)
    http_bytes = _serialized_model(http)
    runtime = _externally_signed_model(
        model_type=InferenceRuntimeReceiptBundle,
        unsigned={
            **common,
            "schema_version": "g01-qwen-inference-receipt-bundle-v2",
            "dataset_version": "g01-text-card-dataset-v1",
            "split": split,
            "provider": "QWEN",
            "evidence_level": "LIVE_PROVIDER_EVIDENCE",
            "region": binding["region"],
            "endpoint_sha256": binding["endpoint_sha256"],
            "exact_model_id": binding["exact_model_id"],
            "prompt_sha256": binding["prompt_sha256"],
            "schema_sha256": binding["schema_sha256"],
            "config_sha256": binding["config_sha256"],
            "predictions_sha256": hashlib.sha256(predictions_bytes).hexdigest(),
            "inference_outputs_sha256": hashlib.sha256(outputs_bytes).hexdigest(),
            "predictions_path": str(paths["PREDICTIONS"].resolve()),
            "inference_outputs_path": str(paths["INFERENCE_OUTPUTS"].resolve()),
            "database_export_receipt_path": str(paths["DATABASE"].resolve()),
            "database_export_receipt_sha256": hashlib.sha256(database_bytes).hexdigest(),
            "provider_http_receipt_bundle_path": str(paths["HTTP"].resolve()),
            "provider_http_receipt_bundle_sha256": hashlib.sha256(http_bytes).hexdigest(),
            "generated_at": now,
            "generated_by": "G01_QWEN_LIVE_RECEIPT_EXPORTER",
            "raw_request_or_response_in_repository": False,
            "exporter_path": exporter_path,
            "exporter_sha256": exporter_sha256,
            "effects": [item.model_dump(mode="json") for item in runtime_effects],
        },
        lane="QWEN",
        authority_signature=authority_signatures["RUNTIME"],
        manifest=manifest,
    )
    return {
        "PREDICTIONS": predictions_bytes,
        "INFERENCE_OUTPUTS": outputs_bytes,
        "DATABASE": database_bytes,
        "HTTP": http_bytes,
        "RUNTIME": _serialized_model(runtime),
    }


def export_live_lane(
    *,
    lane: LiveLane,
    evidence_run_id: str,
    candidate_commit: str,
    output_dir: Path,
    repository_root: Path,
    custody_registry_path: Path,
    registry_anchor_receipt_path: Path,
    live_run_mint_path: Path,
) -> dict[str, str]:
    # The former direct-DSN implementation could sign arbitrary typed rows and
    # therefore did not prove an external Provider call. Keep the lower-level
    # serializers for controlled contract tests, but make the formal entrypoint
    # fail closed until the custody-pinned registry anchor, one-shot live mint,
    # and purpose-specific signed HTTP capture chain is implemented end to end.
    del (
        lane,
        evidence_run_id,
        candidate_commit,
        output_dir,
        repository_root,
        custody_registry_path,
        registry_anchor_receipt_path,
        live_run_mint_path,
    )
    raise LiveEvidenceExportError(
        "formal live export is NOT_RUN until custody-pinned registry, one-shot "
        "mint, and signed direct-HTTPS capture verification are available"
    )
