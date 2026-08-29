from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import asyncpg

from app.constraints.amap_types import classify_amap_type
from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.models import (
    ActivityRole,
    DestinationBasis,
    InferenceProposal,
    ProposedMention,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline, canonical_sha256
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.source_crypto import SourceCipher
from evals.agent_gate_v1.authority import load_worktree_current_goal_binding
from evals.agent_gate_v1.path_security import write_external_bytes_exclusive
from evals.trip_text_cards_agent_v2.annotations import _require_frozen_provider_binding
from evals.trip_text_cards_agent_v2.contracts import (
    ProviderDatabaseEffectRecord,
    ProviderDatabaseExportReceipt,
    ProviderHttpExchangeReceipt,
    ProviderHttpReceiptBundle,
    ProviderPlaceReceiptRecord,
    ProviderReceiptIndex,
    ProviderRuntimeEffectReceipt,
    ProviderRuntimeReceiptBundle,
)
from evals.trip_text_cards_agent_v2.split_loader import load_agent_split


# CORE writes only redacted, repository-external evidence; it never claims HARDENED
# authority and never reads signer keys.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
DATA_ROOT = BACKEND_ROOT / "eval_data" / "trip_text_cards_v1"
EXPORTER_PATH = "backend/scripts/export_g01_amap_live_receipts.py"
_DEEP_CITIES = frozenset({"北京", "上海", "杭州"})
_CATEGORY_LABELS = {
    PlaceCategory.ATTRACTION: "景点",
    PlaceCategory.FOOD: "餐饮",
    PlaceCategory.HOTEL: "住宿",
    PlaceCategory.TRANSPORT: "交通节点",
    PlaceCategory.UNKNOWN: "地点",
}
_CATALOG_PATTERNS = (
    re.compile(r"第1天上午先到(?P<a>[^，。；]+)，午后步行到(?P<b>[^，。；]+)，"),
    re.compile(r"第2天安排(?P<a>[^，。；]+)和(?P<b>[^，。；]+)，"),
    re.compile(r"第3天先去(?P<a>[^，。；]+)，再到(?P<b>[^，。；]+)结束当天"),
    re.compile(r"Day 1 确定游览(?P<a>[^，。；]+)，随后前往(?P<b>[^，。；]+)；"),
    re.compile(r"Day 2 上午看(?P<a>[^，。；]+)，下午安排(?P<b>[^，。；]+)；"),
    re.compile(r"Day 3 把(?P<a>[^，。；]+)放在前面，再去(?P<b>[^，。；]+)。"),
    re.compile(r"第一天的确定行程是(?P<a>[^，。；]+)与(?P<b>[^，。；]+)；"),
    re.compile(r"第二天先(?P<a>[^，。；]+)后(?P<b>[^，。；]+)；"),
    re.compile(r"第三天依次到(?P<a>[^，。；]+)、(?P<b>[^，。；]+)。"),
    re.compile(r"[。；](?P<a>[^；。“”]{1,40})只是从另一篇攻略里听说的参考项"),
    re.compile(r"[。；](?P<a>[^；。“”]{1,40})仅在时间充裕时作为备选"),
    re.compile(r"行程途中会经过(?P<a>[^，。；]{1,40})，但不在那里游览"),
    re.compile(r"这次明确不去(?P<a>[^，。；]{1,40})。"),
    re.compile(r"如果当天太累，(?P<a>[^，。；]{1,40})可以完全不去"),
    re.compile(r"只是路过(?P<a>[^，。；]{1,40})换乘"),
    re.compile(r"网友曾提到(?P<a>[^，。；]{1,40})，但这不是本次安排"),
    re.compile(r"已经决定排除(?P<a>[^，。；]{1,40})。"),
)
_APPLICATION_EXPORT_QUERY = """
SELECT activity_id, understanding_id, day_index, sequence_index, role,
       atomic_place_name, category_hint, eligible_for_place_search,
       resolution_status, canonical_place_id, resolver_receipt_json, created_at
FROM trip_understanding_activities
WHERE understanding_id = ANY($1::text[])
ORDER BY atomic_place_name, understanding_id, sequence_index, activity_id
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


def _database_url(admin_url: str, database_name: str) -> str:
    return f"{admin_url.rsplit('/', 1)[0]}/{database_name}"


def _clean_candidate(candidate_commit: str) -> tuple[str, str]:
    if _git("status", "--porcelain"):
        raise ValueError("AMap CORE export requires a clean candidate checkout")
    head = str(_git("rev-parse", "HEAD"))
    if candidate_commit != head:
        raise ValueError("candidate commit must equal the clean checkout HEAD")
    return head, str(_git("rev-parse", f"{candidate_commit}^{{tree}}"))


def _candidate_name(value: str) -> str:
    normalized = value.strip().strip("“”\"' ")
    if (
        not normalized
        or len(normalized) > 40
        or "http://" in normalized.casefold()
        or "https://" in normalized.casefold()
        or any(marker in normalized for marker in "，。！？；\n")
    ):
        raise ValueError("source-only Provider catalog extracted a non-atomic value")
    return normalized


def extract_source_place_candidates(source_text: str) -> tuple[str, ...]:
    """Extract all role-neutral place strings from dev/validation source text."""

    values: list[str] = []
    for pattern in _CATALOG_PATTERNS:
        for match in pattern.finditer(source_text):
            values.extend(
                _candidate_name(value)
                for value in match.groupdict().values()
                if value is not None
            )
    return tuple(dict.fromkeys(values))


def build_source_only_catalog(cases: Iterable[Any]) -> dict[str, tuple[str, ...]]:
    """Flatten all roles so Provider receipts cannot reveal model role decisions."""

    by_city: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        names = extract_source_place_candidates(case.input_text)
        if len(names) != 10:
            raise ValueError(
                f"{case.case_id} source-only Provider catalog expected 10 names, got {len(names)}"
            )
        if any(name not in case.input_text for name in names):
            raise ValueError(f"{case.case_id} Provider catalog name is absent from source")
        for city in case.city_scope:
            by_city[city].update(names)
    return {
        city: tuple(sorted(names, key=lambda value: (value.casefold(), value)))
        for city, names in sorted(by_city.items())
    }


class _CatalogInferenceProvider:
    def __init__(self, *, city: str, names: tuple[str, ...]) -> None:
        self.city = city
        self.names = names

    @property
    def source_text(self) -> str:
        return "\n".join(
            [f"目的地：{self.city}", *(f"Day 1：{name}" for name in self.names)]
        )

    async def propose(self, source_text: str) -> InferenceProposal:
        if source_text != self.source_text:
            raise ValueError("catalog inference source binding mismatch")
        mentions = []
        cursor = 0
        for index, name in enumerate(self.names):
            start = source_text.index(name, cursor)
            end = start + len(name)
            cursor = end
            mentions.append(
                ProposedMention(
                    mention_id=f"catalog-mention-{index + 1}",
                    raw_text=name,
                    span_start=start,
                    span_end=end,
                    role=ActivityRole.PLANNED,
                    day_index=1,
                    sequence_index=index,
                    atomic_place_name=name,
                )
            )
        return InferenceProposal(
            source_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            destination_name=self.city,
            destination_basis=DestinationBasis.EXPLICIT,
            mentions=mentions,
            binding={
                "provider": "SOURCE_ONLY_EVAL_CATALOG",
                "external_calls": 0,
                "candidate_predictions_used": False,
                "role_labels_used": False,
                "blind_inputs_read": 0,
                "blind_truth_read": 0,
            },
        )


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
        for migration in sorted((BACKEND_ROOT / "app" / "db" / "migrations").glob("*.sql")):
            await connection.execute(migration.read_text(encoding="utf-8"))
            await connection.execute(
                "INSERT INTO applied_migrations(filename) VALUES ($1) ON CONFLICT DO NOTHING",
                migration.name,
            )
    finally:
        await connection.close()


def _receipt_detail(receipt: dict[str, Any]) -> dict[str, Any]:
    nested = receipt.get("provider_binding")
    return nested if isinstance(nested, dict) else receipt


def _as_datetime(value: object, fallback: datetime) -> datetime:
    if not isinstance(value, str):
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _category(receipt: dict[str, Any]) -> str:
    return _CATEGORY_LABELS[classify_amap_type(str(receipt.get("typecode") or ""), "")]


def _resolution_status(row: Any, receipt: dict[str, Any]) -> str:
    if row["resolution_status"] == "AUTO_MATCHED":
        return "MATCHED"
    compatible = receipt.get("category_compatible_candidate_count")
    return "AMBIGUOUS" if isinstance(compatible, int) and compatible > 1 else "UNRESOLVED"


def _effect_models(
    rows: list[Any], *, provider_binding_sha256: str
) -> tuple[
    list[ProviderDatabaseEffectRecord],
    list[ProviderHttpExchangeReceipt],
    list[ProviderRuntimeEffectReceipt],
    list[ProviderPlaceReceiptRecord],
]:
    database_effects = []
    exchanges = []
    runtime_effects = []
    receipts = []
    seen_queries: set[tuple[str, str]] = set()
    for row in rows:
        raw_receipt = row["resolver_receipt_json"]
        receipt = json.loads(raw_receipt) if isinstance(raw_receipt, str) else dict(raw_receipt)
        detail = _receipt_detail(receipt)
        query_name = str(row["atomic_place_name"] or "").strip()
        query_city = str(detail.get("city") or "").strip()
        if not query_city or not query_name:
            raise ValueError("persisted Provider catalog row lost its city or atomic name")
        query_key = (query_city, query_name)
        if query_key in seen_queries:
            raise ValueError("persisted Provider catalog contains a duplicate city/name query")
        seen_queries.add(query_key)
        if row["role"] != "PLANNED" or row["eligible_for_place_search"] is not True:
            raise ValueError("Provider catalog persisted a non-executable activity")

        external_calls = int(receipt.get("external_calls", detail.get("external_calls", 0)))
        if external_calls not in {0, 1, 2}:
            raise ValueError(
                "one catalog effect must make at most two deterministic Provider calls"
            )
        request_sha256 = detail.get("request_sha256")
        if not isinstance(request_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", request_sha256):
            request_sha256 = canonical_sha256(
                {
                    "provider": "AMAP_POI_V2",
                    "city": query_city,
                    "query_sha256": detail.get("query_sha256"),
                    "endpoint_sha256": detail.get("endpoint_sha256"),
                    "external_calls": external_calls,
                }
            )
        response_sha256 = detail.get("response_sha256")
        if not isinstance(response_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", response_sha256):
            response_sha256 = canonical_sha256(receipt)

        completed_at = row["created_at"]
        started_at = _as_datetime(detail.get("observed_at"), completed_at)
        if started_at > completed_at:
            latency_ms = detail.get("latency_ms")
            started_at = completed_at - timedelta(
                milliseconds=float(latency_ms) if isinstance(latency_ms, int | float) else 0
            )
        resolution = _resolution_status(row, detail)
        matched = resolution == "MATCHED"
        effect_id = str(row["activity_id"])
        runtime = ProviderRuntimeEffectReceipt.model_validate(
            {
                "effect_id": effect_id,
                "effect_key_sha256": canonical_sha256(
                    {
                        "city": query_city,
                        "query": query_name,
                        "provider_binding_sha256": provider_binding_sha256,
                    }
                ),
                "provider": "AMAP",
                "execution_mode": "LIVE",
                "provider_binding_sha256": provider_binding_sha256,
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "resolution_status": resolution,
                "queried_source_name": query_name,
                "queried_city": query_city,
                "external_call_count": external_calls,
                "place_id": str(row["canonical_place_id"]) if matched else None,
                "name": query_name if matched else None,
                "city": query_city if matched else None,
                "category": _category(detail) if matched else None,
                "accepted_source_names": [query_name] if matched else [],
                "started_at": started_at,
                "completed_at": completed_at,
                "status": "SUCCEEDED",
                "raw_response_in_repository": False,
            }
        )
        database_effects.append(
            ProviderDatabaseEffectRecord.model_validate(
                {
                    "effect_id": effect_id,
                    "effect_key_sha256": runtime.effect_key_sha256,
                    "provider_binding_sha256": provider_binding_sha256,
                    "request_sha256": request_sha256,
                    "response_sha256": response_sha256,
                    "resolution_status": resolution,
                    "external_call_count": external_calls,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "persisted_status": "SUCCEEDED",
                }
            )
        )
        provider_request_id = detail.get("provider_request_id_sha256")
        if not isinstance(provider_request_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", provider_request_id
        ):
            provider_request_id = None
        # A typed primary query may be followed by exactly one untyped rewrite.
        # The combined resolver receipt binds both ordered request/response
        # hashes while exposing the terminal 2xx status.  Keep one logical
        # application effect and report the actual call count.
        successful_call = external_calls > 0 and isinstance(detail.get("http_status"), int)
        exchanges.append(
            ProviderHttpExchangeReceipt.model_validate(
                {
                    "effect_id": effect_id,
                    "request_sha256": request_sha256,
                    "response_sha256": response_sha256,
                    "external_call_count": external_calls,
                    "http_status": detail.get("http_status") if successful_call else None,
                    "provider_status": (
                        "NOT_CALLED"
                        if external_calls == 0
                        else "SUCCESS" if successful_call else "FAILED"
                    ),
                    "provider_request_id_sha256": provider_request_id,
                    "completed_at": completed_at,
                    "raw_response_retained": False,
                }
            )
        )
        receipt_ref = ProviderPlaceReceiptRecord.model_validate(
            {
                "receipt_id": f"amap-receipt-{hashlib.sha256(effect_id.encode()).hexdigest()[:24]}",
                "provider": "AMAP",
                "execution_mode": "LIVE",
                "provider_binding_sha256": provider_binding_sha256,
                "receipt_ref": effect_id,
                "runtime_effect_id": effect_id,
                "runtime_effect_receipt_sha256": canonical_sha256(
                    runtime.model_dump(mode="json")
                ),
                "request_sha256": request_sha256,
                "response_sha256": response_sha256,
                "observed_at": completed_at,
                "resolution_status": resolution,
                "queried_source_name": query_name,
                "queried_city": query_city,
                "accepted_source_name": query_name if matched else None,
                "place_id": runtime.place_id,
                "name": runtime.name,
                "city": runtime.city,
                "category": runtime.category,
                "accepted_source_names": runtime.accepted_source_names,
            }
        )
        runtime_effects.append(runtime)
        receipts.append(receipt_ref)
    return database_effects, exchanges, runtime_effects, receipts


async def _capture(args: argparse.Namespace) -> dict[str, bytes | int]:
    if not os.getenv("AMAP_API_KEY"):
        raise ValueError("existing AMap credential is required")
    binding = load_worktree_current_goal_binding(REPOSITORY_ROOT)
    if binding.goal_sequence != 1 or binding.gate_profile != "CORE_AGENT_GATE":
        raise ValueError("G01 AMap application-table export requires CORE_AGENT_GATE")
    candidate_commit, candidate_tree = _clean_candidate(args.candidate_commit)
    provider_binding_sha256 = _require_frozen_provider_binding(
        REPOSITORY_ROOT, candidate_commit, lane="AMAP"
    )
    cases, split_receipt = load_agent_split(DATA_ROOT, args.split)
    if split_receipt.blind_inputs_read or split_receipt.blind_truth_read:
        raise ValueError("ordinary AMap catalog capture cannot read frozen blind data")
    catalog = build_source_only_catalog(cases)

    database_name = f"breezetravel_g01_amap_{uuid4().hex[:12]}"
    if not re.fullmatch(r"breezetravel_g01_amap_[0-9a-f]{12}", database_name):
        raise ValueError("isolated AMap catalog database name is invalid")
    admin = await asyncpg.connect(args.database_admin_url)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_url = _database_url(args.database_admin_url, database_name)
        await _migrate(database_url)
        pool = await asyncpg.create_pool(database_url, min_size=2, max_size=8)
        repository = PostgresTripUnderstandingRepository(
            pool, SourceCipher("g01-amap-source-only-catalog-isolated-key")
        )
        owner_user_id = str(uuid4())
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO users (user_id, nickname) VALUES ($1, 'G01 AMap catalog')",
                owner_user_id,
            )

        understanding_ids: list[str] = []
        resolver = AmapPlaceResolver(api_key=os.environ["AMAP_API_KEY"])
        for city, names in catalog.items():
            for chunk_index in range(0, len(names), 60):
                chunk = names[chunk_index : chunk_index + 60]
                inference = _CatalogInferenceProvider(city=city, names=chunk)
                source_text = inference.source_text
                created = await repository.create_full(
                    owner_user_id=owner_user_id,
                    source_text=source_text,
                    idempotency_key=(
                        f"{args.evidence_run_id}:{args.split}:{city}:{chunk_index // 60}"
                    ),
                    request_hash=canonical_sha256(
                        {
                            "split": args.split,
                            "city": city,
                            "names_sha256": canonical_sha256(list(chunk)),
                        }
                    ),
                    now=datetime.now(UTC),
                    retention_days=1,
                )
                job = await repository.claim_next(
                    worker_id="g01-amap-source-only-catalog",
                    now=datetime.now(UTC),
                    lease_seconds=60,
                )
                if job is None:
                    raise ValueError("AMap catalog understanding job was not queued")
                source = await repository.load_source(job, now=datetime.now(UTC))
                pipeline = TripUnderstandingPipeline(
                    inference_provider=inference,
                    place_resolver=resolver,
                    max_executable_activities=len(chunk),
                    max_place_concurrency=args.concurrency,
                )
                output = await pipeline.run(source.text)
                await repository.complete_job(job, output, now=datetime.now(UTC))
                resource = await repository.authorize(
                    created.accepted.public_resource_id,
                    capability_hash=None,
                    user_id=owner_user_id,
                    now=datetime.now(UTC),
                )
                understanding_ids.append(resource.understanding_id)

        async with pool.acquire() as connection, connection.transaction():
            identity = await connection.fetchrow(
                """
                SELECT current_database() AS database_name,
                       COALESCE(inet_server_addr()::text, 'LOCAL_SOCKET') AS server_address,
                       COALESCE(inet_server_port(), 0) AS server_port,
                       txid_current_snapshot()::text AS transaction_snapshot
                """
            )
            rows = await connection.fetch(_APPLICATION_EXPORT_QUERY, understanding_ids)
        if identity is None or not rows:
            raise ValueError("AMap application-table readback returned no persisted effects")
        expected_query_count = sum(len(names) for names in catalog.values())
        if len(rows) != expected_query_count:
            raise ValueError(
                f"AMap application-table readback expected {expected_query_count} effects, got {len(rows)}"
            )
        database_effects, exchanges, runtime_effects, receipts = _effect_models(
            list(rows), provider_binding_sha256=provider_binding_sha256
        )
        now = datetime.now(UTC)
        common = {
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "candidate_commit": candidate_commit,
            "candidate_tree": candidate_tree,
            "provider_binding_sha256": provider_binding_sha256,
            "execution_mode": "LIVE",
        }
        query_sha256 = hashlib.sha256(_APPLICATION_EXPORT_QUERY.encode("utf-8")).hexdigest()
        transaction_snapshot_sha256 = canonical_sha256(
            {
                "query_sha256": query_sha256,
                "transaction_snapshot": identity["transaction_snapshot"],
                "effect_receipts": [
                    canonical_sha256(effect.model_dump(mode="json"))
                    for effect in database_effects
                ],
            }
        )
        database = ProviderDatabaseExportReceipt.model_validate(
            {
                **common,
                "schema_version": "g01-amap-database-export-receipt-v2",
                "source_registry": "POSTGRESQL_APPLICATION_TABLES",
                "query_sha256": query_sha256,
                "transaction_snapshot_sha256": transaction_snapshot_sha256,
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
        http = ProviderHttpReceiptBundle.model_validate(
            {
                **common,
                "schema_version": "g01-amap-http-receipt-bundle-v2",
                "captured_at": now,
                "exchanges": [item.model_dump(mode="json") for item in exchanges],
            }
        )
        database_bytes = _serialized(database)
        http_bytes = _serialized(http)
        exporter_bytes = _git("show", f"{candidate_commit}:{EXPORTER_PATH}", text=False)
        runtime = ProviderRuntimeReceiptBundle.model_validate(
            {
                **common,
                "schema_version": "g01-amap-runtime-receipt-bundle-v2",
                "database_export_receipt_path": str(args.paths["DATABASE"].resolve()),
                "database_export_receipt_sha256": _sha256_bytes(database_bytes),
                "provider_http_receipt_bundle_path": str(args.paths["HTTP"].resolve()),
                "provider_http_receipt_bundle_sha256": _sha256_bytes(http_bytes),
                "generated_at": now,
                "generated_by": "G01_AMAP_LIVE_RECEIPT_EXPORTER",
                "source_runtime": "PERSISTED_APPLICATION_TABLES",
                "evidence_level": "LIVE_PROVIDER_EVIDENCE",
                "exporter_path": EXPORTER_PATH,
                "exporter_sha256": _sha256_bytes(exporter_bytes),
                "effects": [item.model_dump(mode="json") for item in runtime_effects],
            }
        )
        runtime_bytes = _serialized(runtime)
        index = ProviderReceiptIndex.model_validate(
            {
                "schema_version": "g01-text-card-provider-receipt-index-v2",
                "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
                "dataset_version": "g01-text-card-dataset-v1",
                "split": args.split,
                "subject_commit": candidate_commit,
                "subject_tree": candidate_tree,
                "provider_binding_sha256": provider_binding_sha256,
                "execution_mode": "LIVE",
                "evidence_level": "LIVE_PROVIDER_EVIDENCE",
                "runtime_receipt_bundle_sha256": _sha256_bytes(runtime_bytes),
                "frozen_at": now,
                "receipts": [item.model_dump(mode="json") for item in receipts],
            }
        )
        return {
            "DATABASE": database_bytes,
            "HTTP": http_bytes,
            "RUNTIME": runtime_bytes,
            "INDEX": _serialized(index),
            "query_count": len(rows),
            "external_call_count": sum(item.external_call_count for item in runtime_effects),
            "matched_count": sum(item.resolution_status == "MATCHED" for item in runtime_effects),
            "other_city_zero_call_count": sum(
                item.queried_city not in _DEEP_CITIES and item.external_call_count == 0
                for item in runtime_effects
            ),
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
    stem = f"{evidence_run_id.lower()}-amap-{split}"
    return {
        "DATABASE": output_dir / f"{stem}-database.json",
        "HTTP": output_dir / f"{stem}-http.json",
        "RUNTIME": output_dir / f"{stem}-runtime.json",
        "INDEX": output_dir / f"{stem}-index.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-run-id", required=True)
    parser.add_argument("--candidate-commit", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--split", required=True, choices=("dev", "validation"))
    parser.add_argument(
        "--database-admin-url",
        default="postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,80}", args.evidence_run_id):
        parser.error("evidence-run-id must be a stable, path-safe identifier")
    if not re.fullmatch(r"[0-9a-f]{40}", args.candidate_commit):
        parser.error("candidate-commit must be a full Git commit")
    if args.concurrency < 1 or args.concurrency > 8:
        parser.error("concurrency must be between 1 and 8")
    args.paths = _paths(args.output_dir, args.evidence_run_id, args.split)
    artifacts = asyncio.run(_capture(args))
    hashes = {}
    for key in ("DATABASE", "HTTP", "RUNTIME", "INDEX"):
        snapshot = write_external_bytes_exclusive(
            args.paths[key], artifacts[key], REPOSITORY_ROOT
        )
        hashes[key.lower()] = snapshot.sha256
    print(
        json.dumps(
            {
                "status": "COMPLETE",
                "split": args.split,
                "query_count": artifacts["query_count"],
                "external_call_count": artifacts["external_call_count"],
                "matched_count": artifacts["matched_count"],
                "other_city_zero_call_count": artifacts["other_city_zero_call_count"],
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
