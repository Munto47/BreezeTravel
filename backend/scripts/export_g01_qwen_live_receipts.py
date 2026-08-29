"""Export CORE Qwen evidence to repository-external, write-once artifacts.

The G07 HARDENED entry remains fail-closed and cannot invoke this G01 exporter.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import subprocess
from collections.abc import Mapping
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg

from app.trip_understanding.models import (
    InferenceProposal,
    PlaceResolutionOutcome,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.qwen_provider import qwen_effective_run_config_sha256
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.source_crypto import SourceCipher
from evals.agent_gate_v1.authority import load_worktree_current_goal_binding
from evals.agent_gate_v1.path_security import (
    read_external_snapshot,
    write_external_bytes_exclusive,
)
from evals.trip_text_cards_agent_v2.annotations import (
    _require_frozen_provider_binding,
    validate_provider_receipt_assets,
)
from evals.trip_text_cards_agent_v2.contracts import (
    AgentDestinationPrediction,
    AgentInferenceCaseOutputV2,
    AgentPredictionRunEnvelope,
    InferenceDatabaseEffectRecord,
    InferenceDatabaseExportReceipt,
    InferenceEffectReceipt,
    InferenceHttpExchangeReceipt,
    InferenceHttpReceiptBundle,
    InferenceRuntimeReceiptBundle,
    ProviderReceiptIndex,
    ProviderRuntimeReceiptBundle,
)
from evals.trip_text_cards_agent_v2.split_loader import load_agent_split
from evals.trip_text_cards_v1.contracts import PredictedMention, TextCardPrediction


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_v1"
AGENT_DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_agent_v2"
EXPORTER_PATH = "backend/scripts/export_g01_qwen_live_receipts.py"
_APPLICATION_EXPORT_QUERY = """
SELECT revision.understanding_id,
       source.content_hash AS source_sha256,
       revision.destination_json,
       revision.inference_binding_json,
       result.public_json,
       side_effect.effect_key,
       side_effect.provider_binding_json,
       side_effect.created_at
FROM trip_understanding_revisions revision
JOIN trip_understanding_sources source
  ON source.source_id = revision.source_id
JOIN trip_understanding_results result
  ON result.understanding_id = revision.understanding_id
 AND result.revision = revision.revision
JOIN trip_understanding_jobs job
  ON job.understanding_id = revision.understanding_id
JOIN trip_understanding_side_effect_receipts side_effect
  ON side_effect.job_id = job.job_id
WHERE revision.understanding_id = ANY($1::text[])
  AND revision.revision > 1
ORDER BY revision.understanding_id
""".strip()
_ACTIVITY_EXPORT_QUERY = """
SELECT activity.understanding_id,
       activity.activity_id,
       claim.span_start,
       claim.span_end,
       activity.mention_text,
       activity.role,
       activity.day_index,
       activity.atomic_place_name,
       activity.eligible_for_place_search,
       activity.resolution_status,
       activity.canonical_place_id,
       activity.resolver_receipt_json
FROM trip_understanding_activities activity
JOIN trip_understanding_source_claims claim
  ON claim.understanding_id = activity.understanding_id
 AND claim.revision = activity.revision
 AND claim.activity_id = activity.activity_id
WHERE activity.understanding_id = ANY($1::text[])
ORDER BY activity.understanding_id, claim.span_start, claim.span_end
""".strip()


def _git(*args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout.strip() if text else result.stdout


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _json_object(value: Any, *, label: str) -> dict[str, Any]:
    """Normalize asyncpg JSONB values while preserving an object-only contract."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _serialized(model: Any) -> bytes:
    return (
        json.dumps(
            model.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _serialized_jsonl(values: list[Any]) -> bytes:
    return b"".join(
        (
            json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        for value in values
    )


def _database_url(admin_url: str, database_name: str) -> str:
    return f"{admin_url.rsplit('/', 1)[0]}/{database_name}"


def _clean_candidate(candidate_commit: str) -> tuple[str, str]:
    if _git("status", "--porcelain"):
        raise ValueError("Qwen CORE export requires a clean candidate checkout")
    head = str(_git("rev-parse", "HEAD"))
    if candidate_commit != head:
        raise ValueError("candidate commit must equal the clean checkout HEAD")
    return head, str(_git("rev-parse", f"{candidate_commit}^{{tree}}"))


async def _migrate(database_url: str) -> None:
    connection = await asyncpg.connect(database_url)
    try:
        await connection.execute(
            (BACKEND_ROOT / "app" / "db" / "init.sql").read_text(encoding="utf-8")
        )
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS applied_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT NOW()
            )
            """
        )
        for migration in sorted(
            (BACKEND_ROOT / "app" / "db" / "migrations").glob("*.sql")
        ):
            await connection.execute(migration.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO applied_migrations(filename) VALUES ($1) ON CONFLICT DO NOTHING",
                migration.name,
            )
    finally:
        await connection.close()


def _parse_time(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or value == "NOT_COMPLETED":
        raise ValueError(f"Qwen {label} timestamp was not captured")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Qwen {label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Qwen {label} timestamp has no timezone")
    return parsed


def _valid_sha256(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


class _FrozenProposalProvider:
    def __init__(self, proposal: InferenceProposal) -> None:
        self.proposal = proposal

    async def propose(self, source_text: str) -> InferenceProposal:
        if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != self.proposal.source_hash:
            raise ValueError("persisted Qwen proposal source binding mismatch")
        return self.proposal


class _ProviderIndexResolver:
    """Replay frozen live AMap decisions without making another Provider call."""

    def __init__(self, index: ProviderReceiptIndex) -> None:
        self.by_key = {
            (receipt.queried_city, receipt.queried_source_name): receipt
            for receipt in index.receipts
        }

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> PlaceResolutionOutcome:
        del category_hint
        receipt = self.by_key.get((city, atomic_place_name))
        if receipt is None:
            return PlaceResolutionOutcome(
                receipt={
                    "provider": "AMAP_FROZEN_RECEIPT_REPLAY",
                    "status": "UNRESOLVED",
                    "city": city,
                    "external_calls": 0,
                    "live_receipt_present": False,
                }
            )
        common = {
            "provider": "AMAP_FROZEN_RECEIPT_REPLAY",
            "status": receipt.resolution_status,
            "city": city,
            "source_receipt_id": receipt.receipt_id,
            "source_runtime_effect_receipt_sha256": (
                receipt.runtime_effect_receipt_sha256
            ),
            "external_calls": 0,
            "live_receipt_present": True,
        }
        if receipt.resolution_status != "MATCHED":
            return PlaceResolutionOutcome(receipt=common)
        if any(
            value is None
            for value in (receipt.place_id, receipt.name, receipt.city, receipt.category)
        ):
            raise ValueError("matched AMap receipt lost canonical place facts")
        provider_binding = {
            **common,
            "status": "AUTO_MATCHED",
            "city": receipt.city,
            "category": receipt.category,
        }
        return PlaceResolutionOutcome(
            place=ResolvedPlace(
                canonical_place_id=receipt.place_id,
                name=receipt.name,
                category=receipt.category,
                area_or_address=f"{receipt.city}·地点详情已确认",
                provider_binding=provider_binding,
            ),
            receipt=provider_binding,
        )


def _load_raw_prediction_rows(
    *,
    path: Path,
    summary_path: Path,
    candidate_commit: str,
    candidate_tree: str,
    split: str,
    source_cases: list[Any],
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    raw_snapshot = read_external_snapshot(path, REPOSITORY_ROOT)
    summary_snapshot = read_external_snapshot(summary_path, REPOSITORY_ROOT)
    try:
        summary = json.loads(summary_snapshot.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Qwen prediction summary is invalid") from exc
    if (
        summary.get("candidate_commit") != candidate_commit
        or summary.get("candidate_tree") != candidate_tree
        or summary.get("raw_predictions_sha256") != raw_snapshot.sha256
        or summary.get("blind_inputs_read") != 0
        or summary.get("blind_truth_read") != 0
        or summary.get("human_evidence") is not False
        or summary.get("raw_request_or_response_retained") is not False
    ):
        raise ValueError("Qwen prediction summary candidate or privacy binding mismatch")
    requested_splits = summary.get("requested_splits")
    provider_effective_config_sha256 = summary.get("effective_config_sha256")
    if (
        summary.get("batch_concurrency") != 1
        or summary.get("provider_max_concurrency") != 1
        or summary.get("deadline_ms") != 7000
        or summary.get("max_output_tokens") != 2048
        or not isinstance(requested_splits, list)
        or not all(isinstance(value, str) for value in requested_splits)
        or not isinstance(provider_effective_config_sha256, str)
        or summary.get("effective_run_config_sha256")
        != qwen_effective_run_config_sha256(
            model_role=str(summary.get("model_role")),
            splits=requested_splits,
            batch_concurrency=1,
            provider_effective_config_sha256=provider_effective_config_sha256,
        )
    ):
        raise ValueError("Qwen prediction summary is not bound to serial execution")
    try:
        all_rows = [
            json.loads(line)
            for line in raw_snapshot.content.decode("utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Qwen raw prediction run is invalid JSONL") from exc
    rows = [row for row in all_rows if row.get("split") == split]
    if [row.get("case_id") for row in rows] != [case.case_id for case in source_cases]:
        raise ValueError("Qwen prediction split does not cover source cases in order")

    expected_artifacts = {
        "model_panel_sha256": _sha256_path(AGENT_DATA_ROOT / "qwen_model_panel.json"),
        "prompt_sha256": _sha256_path(AGENT_DATA_ROOT / "qwen_inference_prompt.md"),
        "schema_sha256": _sha256_path(
            AGENT_DATA_ROOT / "qwen_semantic_draft.schema.json"
        ),
        "config_sha256": _sha256_path(AGENT_DATA_ROOT / "qwen_inference_config.json"),
    }
    if any(summary.get(field) != expected for field, expected in expected_artifacts.items()):
        raise ValueError("Qwen prediction summary artifact binding mismatch")
    exact_model_id = summary.get("exact_model_id")
    if not isinstance(exact_model_id, str):
        raise ValueError("Qwen prediction summary has no exact model ID")
    for row, source in zip(rows, source_cases, strict=True):
        if (
            row.get("status") != "VALID_MODEL_OUTPUT"
            or row.get("schema_valid_model_output") is not True
            or row.get("source_sha256") != source.normalized_input_sha256
        ):
            raise ValueError(f"{source.case_id} is not a valid live Qwen output")
        proposal = InferenceProposal.model_validate(row.get("proposal"))
        runtime_binding = proposal.binding
        calls = runtime_binding.get("calls")
        if (
            proposal.source_hash != source.normalized_input_sha256
            or runtime_binding.get("execution_mode") != "LIVE"
            or runtime_binding.get("exact_model_id") != exact_model_id
            or runtime_binding.get("model_panel_sha256")
            != expected_artifacts["model_panel_sha256"]
            or runtime_binding.get("prompt_sha256") != expected_artifacts["prompt_sha256"]
            or runtime_binding.get("schema_sha256") != expected_artifacts["schema_sha256"]
            or runtime_binding.get("config_sha256") != expected_artifacts["config_sha256"]
            or runtime_binding.get("effective_config_sha256")
            != provider_effective_config_sha256
            or runtime_binding.get("max_concurrency") != 1
            or runtime_binding.get("deadline_ms") != 7000
            or runtime_binding.get("max_output_tokens") != 2048
            or runtime_binding.get("fallback_used") is True
            or runtime_binding.get("raw_request_or_response_retained") is not False
            or not isinstance(calls, list)
            or not 1 <= len(calls) <= 2
        ):
            raise ValueError(f"{source.case_id} Qwen runtime binding mismatch")
        for call in calls:
            if not isinstance(call, dict) or call.get("outcome") != "RESPONSE_RECEIVED":
                raise ValueError(f"{source.case_id} has an incomplete Qwen call")
            _parse_time(call.get("started_at"), label="started_at")
            _parse_time(call.get("completed_at"), label="completed_at")
    return rows, summary, raw_snapshot.sha256


def _provider_assets(
    *,
    args: argparse.Namespace,
    candidate_commit: str,
    candidate_tree: str,
) -> ProviderReceiptIndex:
    runtime_snapshot = read_external_snapshot(args.provider_runtime_receipts, REPOSITORY_ROOT)
    runtime = ProviderRuntimeReceiptBundle.model_validate_json(runtime_snapshot.content)
    if runtime.database_export_receipt_path is None or runtime.provider_http_receipt_bundle_path is None:
        raise ValueError("AMap runtime receipt paths are incomplete")
    database_sha256 = _sha256_path(Path(runtime.database_export_receipt_path))
    http_sha256 = _sha256_path(Path(runtime.provider_http_receipt_bundle_path))
    provider_binding_sha256 = _require_frozen_provider_binding(
        REPOSITORY_ROOT,
        candidate_commit,
        lane="AMAP",
    )
    index, _runtime, _receipt = validate_provider_receipt_assets(
        split=args.split,
        provider_receipt_index_path=args.provider_receipt_index,
        provider_runtime_receipt_bundle_path=args.provider_runtime_receipts,
        repository_root=REPOSITORY_ROOT,
        expected_candidate_commit=candidate_commit,
        expected_candidate_tree=candidate_tree,
        expected_goal_id="TC-VNEXT-G01-TEXT-CARDS",
        expected_provider_binding_sha256=provider_binding_sha256,
        expected_runtime_receipt_bundle_sha256=runtime_snapshot.sha256,
        expected_database_export_receipt_sha256=database_sha256,
        expected_provider_http_receipt_bundle_sha256=http_sha256,
        require_live_provider_evidence=True,
    )
    return index


def _call_effect(
    *,
    case_id: str,
    source_sha256: str,
    binding: dict[str, Any],
    output: AgentInferenceCaseOutputV2,
) -> tuple[
    InferenceEffectReceipt,
    InferenceDatabaseEffectRecord,
    InferenceHttpExchangeReceipt,
]:
    calls = binding.get("calls")
    if not isinstance(calls, list) or not 1 <= len(calls) <= 2:
        raise ValueError("Qwen call chain is incomplete")
    request_hashes = [str(call["request_sha256"]) for call in calls]
    response_hashes = [str(call["response_sha256"]) for call in calls]
    if any(_valid_sha256(value) is None for value in (*request_hashes, *response_hashes)):
        raise ValueError("Qwen call chain lost request or response hash")
    request_sha256 = (
        request_hashes[0]
        if len(request_hashes) == 1
        else canonical_sha256(request_hashes)
    )
    response_sha256 = (
        response_hashes[0]
        if len(response_hashes) == 1
        else canonical_sha256(response_hashes)
    )
    started_at = min(
        _parse_time(call.get("started_at"), label="started_at") for call in calls
    )
    completed_at = max(
        _parse_time(call.get("completed_at"), label="completed_at") for call in calls
    )
    request_ids = [_valid_sha256(call.get("provider_request_id_sha256")) for call in calls]
    provider_request_id_sha256 = (
        request_ids[0]
        if len(request_ids) == 1
        else canonical_sha256(request_ids) if all(request_ids) else None
    )
    statuses = [call.get("http_status") for call in calls]
    http_status: int | str = (
        statuses[0]
        if len(set(statuses)) == 1
        and (isinstance(statuses[0], int) or statuses[0] == "NOT_EXPOSED_BY_SDK")
        else "NOT_EXPOSED_BY_SDK"
    )
    effect_id = f"qwen-{case_id.lower()}-{source_sha256[:16]}"
    output_sha256 = canonical_sha256(output.model_dump(mode="json"))
    runtime = InferenceEffectReceipt.model_validate(
        {
            "effect_id": effect_id,
            "case_id": case_id,
            "input_sha256": source_sha256,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "provider_request_id_sha256": provider_request_id_sha256,
            "output_sha256": output_sha256,
            "input_tokens": int(binding.get("input_tokens", 0)),
            "output_tokens": int(binding.get("output_tokens", 0)),
            "latency_ms": float(binding.get("latency_ms", 0.0)),
            "repair_call_count": int(binding.get("repair_call_count", 0)),
            "started_at": started_at,
            "completed_at": completed_at,
            "status": "SUCCEEDED",
        }
    )
    database = InferenceDatabaseEffectRecord.model_validate(
        {
            "effect_id": effect_id,
            "case_id": case_id,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "output_sha256": output_sha256,
            "started_at": started_at,
            "completed_at": completed_at,
            "persisted_status": "SUCCEEDED",
        }
    )
    http = InferenceHttpExchangeReceipt.model_validate(
        {
            "effect_id": effect_id,
            "case_id": case_id,
            "request_sha256": request_sha256,
            "response_sha256": response_sha256,
            "provider_request_id_sha256": provider_request_id_sha256,
            "http_status": http_status,
            "completed_at": completed_at,
            "raw_response_retained": False,
        }
    )
    return runtime, database, http


def _prediction_from_readback(
    *,
    source: Any,
    revision: Any,
    activities: list[Any],
    provider_binding: dict[str, Any],
) -> AgentInferenceCaseOutputV2:
    destination = _json_object(
        revision["destination_json"], label="persisted destination"
    )
    destination_name = str(destination["name"])
    destination_basis = str(destination["status"])
    mentions = []
    for row in activities:
        receipt = _json_object(
            row["resolver_receipt_json"], label="persisted resolver receipt"
        )
        matched = row["resolution_status"] == "AUTO_MATCHED"
        canonical_city = (
            receipt.get("selected_city") or receipt.get("city") if matched else None
        )
        canonical_category = receipt.get("category") if matched else None
        if matched and (
            not isinstance(canonical_city, str)
            or not isinstance(canonical_category, str)
        ):
            raise ValueError("persisted matched place lost city or category")
        mentions.append(
            PredictedMention(
                span_start=int(row["span_start"]),
                span_end=int(row["span_end"]),
                raw_text=str(row["mention_text"]),
                role=str(row["role"]),
                day_index=row["day_index"],
                atomic_place_name=row["atomic_place_name"],
                eligible_for_place_search=bool(row["eligible_for_place_search"]),
                resolution_status=str(row["resolution_status"]),
                canonical_place_id=(
                    str(row["canonical_place_id"]) if matched else None
                ),
                canonical_city=canonical_city,
                canonical_category=canonical_category,
            )
        )
    prediction = TextCardPrediction(
        case_id=source.case_id,
        source_sha256=source.normalized_input_sha256,
        destination_name=destination_name,
        provider_binding=provider_binding,
        mentions=mentions,
        public_result=_json_object(
            revision["public_json"], label="persisted public result"
        ),
        measurement_scope="LOCAL_PIPELINE_ONLY",
    )
    if destination_basis == "EXPLICIT":
        start = source.input_text.find(destination_name)
        if start < 0:
            raise ValueError("explicit destination is not source-verbatim")
        destination_prediction = AgentDestinationPrediction(
            case_id=source.case_id,
            destination_name=destination_name,
            destination_basis="EXPLICIT",
            evidence_span_start=start,
            evidence_span_end=start + len(destination_name),
            evidence_raw_text=destination_name,
        )
    else:
        destination_prediction = AgentDestinationPrediction(
            case_id=source.case_id,
            destination_name=destination_name,
            destination_basis="SOFT_ASSUMPTION",
        )
    return AgentInferenceCaseOutputV2(
        case_id=source.case_id,
        source_sha256=source.normalized_input_sha256,
        text_card_prediction=prediction,
        destination_prediction=destination_prediction,
    )


async def _capture(args: argparse.Namespace) -> dict[str, bytes | int | str]:
    binding = load_worktree_current_goal_binding(REPOSITORY_ROOT)
    if binding.goal_sequence != 1 or binding.gate_profile != "CORE_AGENT_GATE":
        raise ValueError("G01 Qwen application-table export requires CORE_AGENT_GATE")
    candidate_commit, candidate_tree = _clean_candidate(args.candidate_commit)
    source_cases, split_access = load_agent_split(DATA_ROOT, args.split)
    if split_access.blind_inputs_read or split_access.blind_truth_read:
        raise ValueError("ordinary Qwen export cannot read frozen blind data")
    raw_rows, summary, raw_predictions_sha256 = _load_raw_prediction_rows(
        path=args.raw_predictions,
        summary_path=args.prediction_summary,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
        split=args.split,
        source_cases=source_cases,
    )
    provider_index = _provider_assets(
        args=args,
        candidate_commit=candidate_commit,
        candidate_tree=candidate_tree,
    )
    exact_model_id = str(summary["exact_model_id"])
    provider_binding_sha256 = _require_frozen_provider_binding(
        REPOSITORY_ROOT,
        candidate_commit,
        lane="QWEN",
        exact_model_id=exact_model_id,
        region=str(summary["region"]),
        endpoint_sha256=str(raw_rows[0]["proposal"]["binding"]["endpoint_sha256"]),
    )
    provider_binding = json.loads(
        (AGENT_DATA_ROOT / "provider_binding.json").read_text(encoding="utf-8")
    )
    model_binding_sha256 = _sha256_path(AGENT_DATA_ROOT / "qwen_model_panel.json")
    prompt_sha256 = _sha256_path(AGENT_DATA_ROOT / "qwen_inference_prompt.md")
    schema_sha256 = _sha256_path(AGENT_DATA_ROOT / "qwen_semantic_draft.schema.json")
    config_sha256 = _sha256_path(AGENT_DATA_ROOT / "qwen_inference_config.json")

    database_name = f"breezetravel_g01_qwen_{uuid4().hex[:12]}"
    if not re.fullmatch(r"breezetravel_g01_qwen_[0-9a-f]{12}", database_name):
        raise ValueError("isolated Qwen evidence database name is invalid")
    admin = await asyncpg.connect(args.database_admin_url)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_url = _database_url(args.database_admin_url, database_name)
        await _migrate(database_url)
        pool = await asyncpg.create_pool(database_url, min_size=2, max_size=6)
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g01-qwen-application-table-capture-isolated-key"),
        )
        owner_user_id = str(uuid4())
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO users (user_id, nickname) VALUES ($1, 'G01 Qwen capture')",
                owner_user_id,
            )

        understanding_ids: list[str] = []
        case_by_understanding: dict[str, Any] = {}
        raw_by_case = {row["case_id"]: row for row in raw_rows}
        resolver = _ProviderIndexResolver(provider_index)
        for source in source_cases:
            proposal = InferenceProposal.model_validate(raw_by_case[source.case_id]["proposal"])
            created = await repository.create_full(
                owner_user_id=owner_user_id,
                source_text=source.input_text,
                idempotency_key=f"{args.evidence_run_id}:{args.split}:{source.case_id}",
                request_hash=canonical_sha256(
                    {
                        "case_id": source.case_id,
                        "source_sha256": source.normalized_input_sha256,
                        "raw_predictions_sha256": raw_predictions_sha256,
                    }
                ),
                now=datetime.now(UTC),
                retention_days=1,
            )
            job = await repository.claim_next(
                worker_id="g01-qwen-application-table-capture",
                now=datetime.now(UTC),
                lease_seconds=60,
            )
            if job is None:
                raise ValueError("Qwen application-table job was not queued")
            persisted_source = await repository.load_source(job, now=datetime.now(UTC))
            output = await TripUnderstandingPipeline(
                inference_provider=_FrozenProposalProvider(proposal),
                place_resolver=resolver,
                max_place_concurrency=4,
            ).run(persisted_source.text)
            await repository.complete_job(job, output, now=datetime.now(UTC))
            resource = await repository.authorize(
                created.accepted.public_resource_id,
                capability_hash=None,
                user_id=owner_user_id,
                now=datetime.now(UTC),
            )
            understanding_ids.append(resource.understanding_id)
            case_by_understanding[resource.understanding_id] = source

        async with pool.acquire() as connection, connection.transaction():
            identity = await connection.fetchrow(
                """
                SELECT current_database() AS database_name,
                       COALESCE(inet_server_addr()::text, 'LOCAL_SOCKET') AS server_address,
                       COALESCE(inet_server_port(), 0) AS server_port,
                       txid_current_snapshot()::text AS transaction_snapshot
                """
            )
            revisions = await connection.fetch(
                _APPLICATION_EXPORT_QUERY,
                understanding_ids,
            )
            activity_rows = await connection.fetch(
                _ACTIVITY_EXPORT_QUERY,
                understanding_ids,
            )
        if identity is None or len(revisions) != len(source_cases):
            raise ValueError("Qwen application-table readback is incomplete")
        activities_by_understanding: dict[str, list[Any]] = defaultdict(list)
        for row in activity_rows:
            activities_by_understanding[str(row["understanding_id"])].append(row)
        revision_by_source = {
            str(row["source_sha256"]).strip(): row for row in revisions
        }
        outputs = []
        runtime_effects = []
        database_effects = []
        exchanges = []
        for source in source_cases:
            revision = revision_by_source.get(source.normalized_input_sha256)
            if revision is None:
                raise ValueError(f"{source.case_id} has no application-table readback")
            understanding_id = str(revision["understanding_id"])
            if case_by_understanding.get(understanding_id) != source:
                raise ValueError("Qwen readback understanding/source mapping mismatch")
            persisted_binding = _json_object(
                revision["inference_binding_json"],
                label="persisted inference binding",
            )
            side_effect_binding = _json_object(
                revision["provider_binding_json"],
                label="persisted side-effect binding",
            )
            if side_effect_binding.get("inference") != persisted_binding:
                raise ValueError("Qwen revision and side-effect inference bindings disagree")
            output = _prediction_from_readback(
                source=source,
                revision=revision,
                activities=activities_by_understanding[understanding_id],
                provider_binding=provider_binding,
            )
            runtime, database_effect, http_exchange = _call_effect(
                case_id=source.case_id,
                source_sha256=source.normalized_input_sha256,
                binding=persisted_binding,
                output=output,
            )
            outputs.append(output)
            runtime_effects.append(runtime)
            database_effects.append(database_effect)
            exchanges.append(http_exchange)

        predictions = [item.text_card_prediction for item in outputs]
        predictions_bytes = _serialized_jsonl(predictions)
        outputs_bytes = _serialized_jsonl(outputs)
        now = datetime.now(UTC)
        common = {
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "candidate_commit": candidate_commit,
            "candidate_tree": candidate_tree,
            "model_binding_sha256": model_binding_sha256,
            "provider_binding_sha256": provider_binding_sha256,
            "execution_mode": "LIVE",
        }
        combined_query = f"{_APPLICATION_EXPORT_QUERY}\n{_ACTIVITY_EXPORT_QUERY}"
        query_sha256 = hashlib.sha256(combined_query.encode("utf-8")).hexdigest()
        database = InferenceDatabaseExportReceipt.model_validate(
            {
                **common,
                "schema_version": "g01-qwen-database-export-receipt-v1",
                "source_registry": "POSTGRESQL_APPLICATION_TABLES",
                "query_sha256": query_sha256,
                "transaction_snapshot_sha256": canonical_sha256(
                    {
                        "query_sha256": query_sha256,
                        "transaction_snapshot": identity["transaction_snapshot"],
                        "effects": [
                            canonical_sha256(item.model_dump(mode="json"))
                            for item in database_effects
                        ],
                    }
                ),
                "database_instance_sha256": canonical_sha256(
                    {
                        "database_name": identity["database_name"],
                        "server_address": identity["server_address"],
                        "server_port": identity["server_port"],
                    }
                ),
                "exported_at": now,
                "effects": [item.model_dump(mode="json") for item in database_effects],
            }
        )
        http = InferenceHttpReceiptBundle.model_validate(
            {
                **common,
                "schema_version": "g01-qwen-http-receipt-bundle-v1",
                "captured_at": now,
                "exchanges": [item.model_dump(mode="json") for item in exchanges],
            }
        )
        database_bytes = _serialized(database)
        http_bytes = _serialized(http)
        exporter_bytes = _git("show", f"{candidate_commit}:{EXPORTER_PATH}", text=False)
        runtime = InferenceRuntimeReceiptBundle.model_validate(
            {
                **common,
                "schema_version": "g01-qwen-inference-receipt-bundle-v2",
                "dataset_version": "g01-text-card-dataset-v1",
                "split": args.split,
                "provider": "QWEN",
                "evidence_level": "LIVE_PROVIDER_EVIDENCE",
                "region": str(summary["region"]),
                "endpoint_sha256": str(
                    raw_rows[0]["proposal"]["binding"]["endpoint_sha256"]
                ),
                "exact_model_id": exact_model_id,
                "prompt_sha256": prompt_sha256,
                "schema_sha256": schema_sha256,
                "config_sha256": config_sha256,
                "predictions_sha256": _sha256_bytes(predictions_bytes),
                "inference_outputs_sha256": _sha256_bytes(outputs_bytes),
                "predictions_path": str(args.paths["PREDICTIONS"].resolve()),
                "inference_outputs_path": str(args.paths["OUTPUTS"].resolve()),
                "database_export_receipt_path": str(args.paths["DATABASE"].resolve()),
                "database_export_receipt_sha256": _sha256_bytes(database_bytes),
                "provider_http_receipt_bundle_path": str(args.paths["HTTP"].resolve()),
                "provider_http_receipt_bundle_sha256": _sha256_bytes(http_bytes),
                "exporter_path": EXPORTER_PATH,
                "exporter_sha256": _sha256_bytes(exporter_bytes),
                "generated_at": now,
                "generated_by": "G01_QWEN_LIVE_RECEIPT_EXPORTER",
                "raw_request_or_response_in_repository": False,
                "effects": [item.model_dump(mode="json") for item in runtime_effects],
            }
        )
        runtime_bytes = _serialized(runtime)
        envelope = AgentPredictionRunEnvelope(
            split=args.split,
            candidate_commit=candidate_commit,
            candidate_tree=candidate_tree,
            predictions_sha256=_sha256_bytes(predictions_bytes),
            inference_outputs_sha256=_sha256_bytes(outputs_bytes),
            model_binding_sha256=model_binding_sha256,
            prompt_sha256=prompt_sha256,
            schema_sha256=schema_sha256,
            config_sha256=config_sha256,
            provider_binding_sha256=provider_binding_sha256,
            inference_receipt_bundle_sha256=_sha256_bytes(runtime_bytes),
            generated_at=now,
            destination_predictions=[item.destination_prediction for item in outputs],
        )
        return {
            "PREDICTIONS": predictions_bytes,
            "OUTPUTS": outputs_bytes,
            "DATABASE": database_bytes,
            "HTTP": http_bytes,
            "RUNTIME": runtime_bytes,
            "ENVELOPE": _serialized(envelope),
            "case_count": len(outputs),
            "schema_valid_count": len(outputs),
            "raw_predictions_sha256": raw_predictions_sha256,
        }
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()


def _paths(output_dir: Path, evidence_run_id: str, split: str) -> dict[str, Path]:
    stem = f"{evidence_run_id.lower()}-qwen-{split}"
    return {
        "PREDICTIONS": output_dir / f"{stem}-predictions.jsonl",
        "OUTPUTS": output_dir / f"{stem}-inference-outputs.jsonl",
        "DATABASE": output_dir / f"{stem}-database.json",
        "HTTP": output_dir / f"{stem}-http.json",
        "RUNTIME": output_dir / f"{stem}-runtime.json",
        "ENVELOPE": output_dir / f"{stem}-envelope.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--split", required=True, choices=("dev", "validation"))
    parser.add_argument("--raw-predictions", required=True, type=Path)
    parser.add_argument("--prediction-summary", required=True, type=Path)
    parser.add_argument("--provider-receipt-index", required=True, type=Path)
    parser.add_argument("--provider-runtime-receipts", required=True, type=Path)
    parser.add_argument(
        "--database-admin-url",
        default="postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,80}", args.evidence_run_id):
        parser.error("evidence-run-id must be a stable, path-safe identifier")
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
        parser.error("candidate-commit must be a full Git commit")
    args.paths = _paths(args.output_dir, args.evidence_run_id, args.split)
    artifacts = asyncio.run(_capture(args))
    hashes = {}
    for key in ("PREDICTIONS", "OUTPUTS", "DATABASE", "HTTP", "RUNTIME", "ENVELOPE"):
        snapshot = write_external_bytes_exclusive(
            args.paths[key],
            artifacts[key],
            REPOSITORY_ROOT,
        )
        hashes[key.lower()] = snapshot.sha256
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "split": args.split,
                "case_count": artifacts["case_count"],
                "schema_valid_count": artifacts["schema_valid_count"],
                "raw_predictions_sha256": artifacts["raw_predictions_sha256"],
                "artifact_sha256": hashes,
                "source_runtime": "PERSISTED_APPLICATION_TABLES",
                "blind_inputs_read": 0,
                "blind_truth_read": 0,
                "human_evidence": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
