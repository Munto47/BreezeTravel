from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import asyncpg

from app.trip_understanding.amap_place import AmapPlaceResolver
from app.trip_understanding.amap_route import AmapRouteProvider
from app.trip_understanding.map_render import MapRenderer
from app.trip_understanding.map_worker import MapRenderWorker
from app.trip_understanding.models import ActivityMoveCommand
from app.trip_understanding.pipeline import canonical_sha256
from app.trip_understanding.qwen_provider import QwenStructuredInferenceProvider
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.source_crypto import SourceCipher
from app.trip_understanding.full_text import build_full_text_pipeline
from evals.agent_gate_v1.path_security import write_external_bytes_exclusive


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
PANEL_PATH = (
    BACKEND_ROOT
    / "eval_data"
    / "trip_text_cards_agent_v2"
    / "qwen_model_panel.json"
)
SOURCE_TEXT = """北京三日长攻略
Day 1：上午先去故宫博物院，下午步行到景山公园。
Day 2：安排天坛公园和前门大街，顺序按正文。
Day 3：先到北海公园，再去恭王府。
中国国家博物馆只是参考，南锣鼓巷有空再考虑，途中经过王府井地铁站但不游览，明确不去北京环球影城。
预约说明：热门地点需要提前确认。详情链接 https://example.invalid/g01/live-persistence?place=故宫博物院 不是地点。
日期和人数尚未确定，请保留为可编辑假设。
"""
FORBIDDEN_PUBLIC_KEYS = {
    "raw_text",
    "source",
    "source_id",
    "span",
    "span_start",
    "span_end",
    "offset",
    "confidence",
    "model",
    "provider",
    "uuid",
    "hash",
    "revision",
    "receipt",
    "run",
    "stage",
}


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _candidate(role: str) -> tuple[dict[str, object], bytes]:
    panel_bytes = PANEL_PATH.read_bytes()
    panel = json.loads(panel_bytes)
    values = panel.get("candidates")
    if not isinstance(values, list):
        raise ValueError("Qwen model panel has no candidates")
    for value in values:
        if isinstance(value, dict) and value.get("role") == role:
            return value, panel_bytes
    raise ValueError(f"Qwen model panel has no {role} candidate")


def _price(candidate: dict[str, object], price_type: str) -> float | None:
    pricing = candidate.get("pricing")
    if not isinstance(pricing, list):
        return None
    for band in pricing:
        if not isinstance(band, dict):
            continue
        prices = band.get("prices")
        if not isinstance(prices, list):
            continue
        for row in prices:
            if isinstance(row, dict) and row.get("type") == price_type:
                try:
                    return float(row["price"])
                except (KeyError, TypeError, ValueError):
                    return None
    return None


def _collect_keys(value: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            keys.add(str(key).casefold())
            keys.update(_collect_keys(child))
    elif isinstance(value, list):
        for child in value:
            keys.update(_collect_keys(child))
    return keys


def _database_url(admin_url: str, database_name: str) -> str:
    return f"{admin_url.rsplit('/', 1)[0]}/{database_name}"


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


async def _run(args: argparse.Namespace) -> dict[str, object]:
    if _git("status", "--porcelain"):
        raise ValueError("live persistence smoke requires a clean candidate checkout")
    if not os.getenv("QWEN_API_KEY") or not os.getenv("AMAP_API_KEY"):
        raise ValueError("existing Qwen and AMap credentials are required")
    candidate_commit = _git("rev-parse", "HEAD")
    candidate_tree = _git("rev-parse", "HEAD^{tree}")
    candidate, panel_bytes = _candidate(args.model_role)
    model = candidate.get("exact_model_id")
    if not isinstance(model, str) or not model:
        raise ValueError("Qwen exact model ID is unavailable")

    database_name = f"breezetravel_g01_live_{uuid4().hex[:12]}"
    if not re.fullmatch(r"breezetravel_g01_live_[0-9a-f]{12}", database_name):
        raise ValueError("isolated database name is invalid")
    admin = await asyncpg.connect(args.database_admin_url)
    pool: asyncpg.Pool | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_url = _database_url(args.database_admin_url, database_name)
        await _migrate(database_url)
        pool = await asyncpg.create_pool(database_url, min_size=2, max_size=6)
        repository = PostgresTripUnderstandingRepository(
            pool,
            SourceCipher("g01-live-persistence-isolated-test-key"),
        )
        owner_user_id = str(uuid4())
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO users (user_id, nickname) VALUES ($1, 'G01 live persistence smoke')",
                owner_user_id,
            )

        create_started = time.perf_counter()
        created_at = datetime.now(UTC)
        request_hash = canonical_sha256(
            {"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE_TEXT}}
        )
        created = await repository.create_full(
            owner_user_id=owner_user_id,
            source_text=SOURCE_TEXT,
            idempotency_key=f"g01-live-create-{candidate_commit[:12]}",
            request_hash=request_hash,
            now=created_at,
            retention_days=30,
        )
        progress_latency_ms = round((time.perf_counter() - create_started) * 1000, 3)
        job = await repository.claim_next(
            worker_id="g01-live-understanding-smoke",
            now=datetime.now(UTC),
            lease_seconds=30,
        )
        if job is None:
            raise ValueError("live understanding job was not queued")
        source = await repository.load_source(job, now=datetime.now(UTC))
        provider = QwenStructuredInferenceProvider(
            api_key=os.environ["QWEN_API_KEY"],
            base_url=os.getenv(
                "QWEN_API_URL",
                "https://dashscope.aliyuncs.com/compatible-mode/v1",
            ),
            model=model,
            deadline_seconds=7.0,
            max_output_tokens=768,
            input_cny_per_million=_price(candidate, "input_token"),
            output_cny_per_million=_price(candidate, "output_token"),
        )
        pipeline = build_full_text_pipeline(
            provider,
            AmapPlaceResolver(api_key=os.environ["AMAP_API_KEY"]),
            max_place_concurrency=4,
        )
        card_started = time.perf_counter()
        output = await pipeline.run(source.text)
        await repository.complete_job(job, output, now=datetime.now(UTC))
        card_latency_ms = round((time.perf_counter() - card_started) * 1000, 3)

        resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash=None,
            user_id=owner_user_id,
            now=datetime.now(UTC),
        )
        stored = await repository.get_result(resource)
        if stored is None:
            raise ValueError("live cards were not persisted")
        public_payload = stored.result.model_dump(mode="json")
        forbidden_keys = sorted(_collect_keys(public_payload) & FORBIDDEN_PUBLIC_KEYS)
        if forbidden_keys:
            raise ValueError(f"public projection exposed internal keys: {forbidden_keys}")

        async with pool.acquire() as connection:
            activity_rows = await connection.fetch(
                """
                SELECT role, resolution_status, canonical_place_id,
                       resolver_receipt_json
                FROM trip_understanding_activities
                WHERE understanding_id = $1 AND revision = $2
                ORDER BY day_index NULLS LAST, sequence_index, activity_id
                """,
                resource.understanding_id,
                2,
            )
            initial_map_jobs = await connection.fetchval(
                "SELECT COUNT(*) FROM trip_map_render_jobs WHERE understanding_id = $1",
                resource.understanding_id,
            )
        matched_receipts = []
        for row in activity_rows:
            receipt = row["resolver_receipt_json"]
            if isinstance(receipt, str):
                receipt = json.loads(receipt)
            if row["resolution_status"] == "AUTO_MATCHED":
                if not isinstance(receipt, dict):
                    raise ValueError("matched activity has no resolver receipt")
                coordinates = receipt.get("coordinates")
                if not isinstance(coordinates, dict) or not receipt.get("adcode"):
                    raise ValueError("matched activity did not persist coordinates and adcode")
                matched_receipts.append(receipt)
        if len(matched_receipts) < 2 or initial_map_jobs != 1:
            raise ValueError("live place persistence or initial async map enqueue is incomplete")

        map_worker = MapRenderWorker(
            repository,
            renderer=MapRenderer(AmapRouteProvider(api_key=os.environ["AMAP_API_KEY"])),
            lease_seconds=30,
        )
        if not await map_worker.run_once(
            "g01-live-map-smoke",
            now=datetime.now(UTC),
        ):
            raise ValueError("initial map job was not executable")
        map_view = await repository.get_map_view(resource, now=datetime.now(UTC))
        if map_view.status not in {"AVAILABLE", "LIMITED"}:
            raise ValueError("known live route did not produce a usable map result")

        async with pool.acquire() as connection:
            route_effect_count_before_edit = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM trip_map_provider_effect_receipts effect
                JOIN trip_map_render_jobs job ON job.map_job_id = effect.map_job_id
                WHERE job.understanding_id = $1
                """,
                resource.understanding_id,
            )
            route_external_calls_before_edit = await connection.fetchval(
                """
                SELECT COALESCE(SUM(effect.external_call_count), 0)
                FROM trip_map_provider_effect_receipts effect
                JOIN trip_map_render_jobs job ON job.map_job_id = effect.map_job_id
                WHERE job.understanding_id = $1
                """,
                resource.understanding_id,
            )

        first_card = next(
            card
            for day in stored.result.days
            for card in day.activities
        )
        command = ActivityMoveCommand(
            command_type="ACTIVITY_MOVE",
            activity_token=first_card.activity_token,
            target_day_index=2,
            target_position=0,
        )
        command_hash = canonical_sha256(
            {
                "command": command.model_dump(mode="json"),
                "if_match": stored.opaque_etag,
            }
        )
        command_outcome = await repository.apply_command(
            resource,
            command,
            expected_etag=stored.opaque_etag,
            idempotency_key=f"g01-live-edit-{candidate_commit[:12]}",
            request_hash=command_hash,
            now=datetime.now(UTC),
        )
        updated_resource = await repository.authorize(
            created.accepted.public_resource_id,
            capability_hash=None,
            user_id=owner_user_id,
            now=datetime.now(UTC),
        )
        updated = await repository.get_result(updated_resource)
        if updated is None or updated.result.map.status != "NEEDS_UPDATE":
            raise ValueError("card edit did not mark the existing map as needing update")
        async with pool.acquire() as connection:
            route_effect_count_after_edit = await connection.fetchval(
                """
                SELECT COUNT(*)
                FROM trip_map_provider_effect_receipts effect
                JOIN trip_map_render_jobs job ON job.map_job_id = effect.map_job_id
                WHERE job.understanding_id = $1
                """,
                resource.understanding_id,
            )
            route_external_calls_after_edit = await connection.fetchval(
                """
                SELECT COALESCE(SUM(effect.external_call_count), 0)
                FROM trip_map_provider_effect_receipts effect
                JOIN trip_map_render_jobs job ON job.map_job_id = effect.map_job_id
                WHERE job.understanding_id = $1
                """,
                resource.understanding_id,
            )
        if (
            route_effect_count_after_edit != route_effect_count_before_edit
            or route_external_calls_after_edit != route_external_calls_before_edit
        ):
            raise ValueError("card edit triggered an automatic route Provider call")
        if route_effect_count_before_edit < 2 or route_external_calls_before_edit < 2:
            raise ValueError("walking and transit Provider effects were not both persisted")

        binding = output.inference_binding
        primary_binding = binding.get("primary_provider_binding")
        if isinstance(primary_binding, dict):
            binding = primary_binding
        return {
            "schema_version": "g01-live-persistence-smoke-v1",
            "goal_id": "TC-VNEXT-G01-TEXT-CARDS",
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "candidate_commit": candidate_commit,
            "candidate_tree": candidate_tree,
            "model_panel_sha256": hashlib.sha256(panel_bytes).hexdigest(),
            "exact_model_id": model,
            "source_sha256": hashlib.sha256(SOURCE_TEXT.encode("utf-8")).hexdigest(),
            "create_to_progress_ms": progress_latency_ms,
            "create_to_editable_cards_ms": card_latency_ms,
            "public_result_status": stored.result.status,
            "public_day_count": len(stored.result.days),
            "public_card_count": sum(len(day.activities) for day in stored.result.days),
            "public_forbidden_key_count": len(forbidden_keys),
            "qwen_external_calls": binding.get("external_calls"),
            "qwen_repair_calls": binding.get("repair_call_count"),
            "qwen_input_tokens": binding.get("input_tokens"),
            "qwen_output_tokens": binding.get("output_tokens"),
            "qwen_estimated_cost_cny": binding.get("estimated_cost_cny"),
            "qwen_estimated_cost_status": binding.get("estimated_cost_status"),
            "persisted_activity_count": len(activity_rows),
            "persisted_auto_match_count": len(matched_receipts),
            "persisted_coordinate_receipt_count": len(matched_receipts),
            "initial_map_job_count": initial_map_jobs,
            "initial_map_terminal_status": map_view.status,
            "route_effect_count": route_effect_count_before_edit,
            "route_external_call_count": route_external_calls_before_edit,
            "edit_status": command_outcome.applied.status,
            "map_status_after_edit": updated.result.map.status,
            "automatic_route_calls_after_edit": (
                route_external_calls_after_edit - route_external_calls_before_edit
            ),
            "raw_request_or_response_retained": False,
            "database_source": "ISOLATED_POSTGRESQL_APPLICATION_TABLES",
            "isolated_database_destroyed_after_receipt": True,
            "blind_inputs_read": 0,
            "blind_truth_read": 0,
            "human_evidence": False,
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--database-admin-url",
        default="postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )
    parser.add_argument(
        "--model-role",
        choices=("QUALITY_CEILING", "PRODUCTION_CANDIDATE", "LOW_LATENCY_CANDIDATE"),
        default="LOW_LATENCY_CANDIDATE",
    )
    args = parser.parse_args()
    receipt = asyncio.run(_run(args))
    output_bytes = (
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    snapshot = write_external_bytes_exclusive(
        args.output,
        output_bytes,
        REPOSITORY_ROOT,
    )
    print(
        json.dumps(
            {
                "output": str(snapshot.path),
                "receipt_sha256": snapshot.sha256,
                "public_result_status": receipt["public_result_status"],
                "public_card_count": receipt["public_card_count"],
                "persisted_auto_match_count": receipt["persisted_auto_match_count"],
                "initial_map_terminal_status": receipt["initial_map_terminal_status"],
                "automatic_route_calls_after_edit": receipt[
                    "automatic_route_calls_after_edit"
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
