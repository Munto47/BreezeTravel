from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from evals.agent_gate_v1.live_export import (
    LiveEvidenceExportError,
    _read_typed_effects,
)
from evals.trip_text_cards_agent_v2.contracts import AgentInferenceCaseOutputV2
from evals.trip_text_cards_v1.contracts import TextCardPrediction, canonical_sha256
from evals.trip_text_cards_v1.validator import load_cases


pytestmark = pytest.mark.integration

DATA_ROOT = Path("eval_data/trip_text_cards_v1")
REGISTRY_CONTRACT = Path("eval_data/agent_gate_v1/live_evidence_registry_contract.sql")


def _admin_dsn() -> str:
    return os.getenv(
        "TEST_DATABASE_ADMIN_URL",
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres",
    )


def _inference_output() -> AgentInferenceCaseOutputV2:
    source = load_cases(DATA_ROOT)["dev"][0]
    city_start = source.input_text.index("北京")
    prediction = TextCardPrediction.model_validate(
        {
            "schema_version": "g01-text-card-prediction-v1",
            "dataset_version": "g01-text-card-dataset-v1",
            "case_id": source.case_id,
            "source_sha256": source.normalized_input_sha256,
            "destination_name": "北京",
            "provider_binding": {"provider": "AMAP", "binding": "live-test"},
            "mentions": [],
            "public_result": {},
            "measurement_scope": "LOCAL_PIPELINE_ONLY",
            "first_progress_ms": None,
            "cards_ready_ms": None,
        }
    )
    return AgentInferenceCaseOutputV2.model_validate(
        {
            "schema_version": "g01-agent-inference-case-output-v2",
            "case_id": source.case_id,
            "source_sha256": source.normalized_input_sha256,
            "text_card_prediction": prediction.model_dump(mode="json"),
            "destination_prediction": {
                "case_id": source.case_id,
                "destination_name": "北京",
                "destination_basis": "EXPLICIT",
                "evidence_span_start": city_start,
                "evidence_span_end": city_start + 2,
                "evidence_raw_text": "北京",
            },
        }
    )


@pytest.mark.asyncio
async def test_typed_live_registry_materializes_and_exports_fixed_candidate_rows() -> None:
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")

    database_name = f"breezetravel_agent_gate_{uuid4().hex[:10]}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin = await asyncpg.connect(_admin_dsn())
    candidate_commit = "a" * 40
    other_candidate_commit = "b" * 40
    evidence_run_id = "G01-LIVE-PGREGISTRY1"
    started_at = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)
    completed_at = started_at + timedelta(seconds=1)
    try:
        await admin.execute(f'CREATE DATABASE "{database_name}"')
        database_dsn = f"{_admin_dsn().rsplit('/', 1)[0]}/{database_name}"
        connection = await asyncpg.connect(database_dsn)
        try:
            await connection.execute(REGISTRY_CONTRACT.read_text(encoding="utf-8"))
            await connection.execute(
                """
                INSERT INTO trip_g01_amap_provider_effects (
                    evidence_run_id, candidate_commit, split, effect_id,
                    effect_key_sha256, provider_binding_sha256,
                    request_sha256, response_sha256, provider_request_id_sha256,
                    http_status, resolution_status, accepted_source_name,
                    place_id, place_name, city, category, accepted_source_names,
                    started_at, completed_at
                ) VALUES (
                    $1, $2, 'validation', $3, $4, $5, $6, $7, $8,
                    200, 'MATCHED', '故宫', 'B000A8UIN8', '故宫博物院',
                    '北京', '风景名胜', ARRAY['故宫', '故宫博物院'], $9, $10
                )
                """,
                evidence_run_id,
                candidate_commit,
                "amap-effect-0002",
                "1" * 64,
                "2" * 64,
                "3" * 64,
                "4" * 64,
                "5" * 64,
                started_at,
                completed_at,
            )
            await connection.execute(
                """
                INSERT INTO trip_g01_amap_provider_effects (
                    evidence_run_id, candidate_commit, split, effect_id,
                    effect_key_sha256, provider_binding_sha256,
                    request_sha256, response_sha256, provider_request_id_sha256,
                    http_status, resolution_status, accepted_source_names,
                    started_at, completed_at
                ) VALUES (
                    $1, $2, 'validation', 'amap-effect-other', $3, $4, $5,
                    $6, $7, 200, 'UNRESOLVED', ARRAY[]::TEXT[], $8, $9
                )
                """,
                evidence_run_id,
                other_candidate_commit,
                "6" * 64,
                "2" * 64,
                "7" * 64,
                "8" * 64,
                "9" * 64,
                started_at,
                completed_at,
            )

            inference_output = _inference_output()
            inference_json = inference_output.model_dump(mode="json")
            await connection.execute(
                """
                INSERT INTO trip_g01_qwen_inference_effects (
                    evidence_run_id, candidate_commit, split, effect_id,
                    case_id, input_sha256, request_sha256, response_sha256,
                    provider_request_id_sha256, http_status, output_sha256,
                    inference_output_json, input_tokens, output_tokens,
                    latency_ms, repair_call_count, provider_binding_sha256,
                    model_binding_sha256, exact_model_id, region,
                    endpoint_sha256, prompt_sha256, schema_sha256,
                    config_sha256, started_at, completed_at
                ) VALUES (
                    $1, $2, 'validation', 'qwen-effect-0001', $3, $4, $5,
                    $6, $7, 200, $8, $9::jsonb, 120, 30, 410.5, 0,
                    $10, $11, 'qwen-plus-frozen-test', 'cn-beijing',
                    $12, $13, $14, $15, $16, $17
                )
                """,
                evidence_run_id,
                candidate_commit,
                inference_output.case_id,
                inference_output.source_sha256,
                "a" * 64,
                "b" * 64,
                "c" * 64,
                canonical_sha256(inference_json),
                json.dumps(inference_json, ensure_ascii=False),
                "2" * 64,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                "0" * 64,
                "1" * 64,
                started_at,
                completed_at,
            )

            with pytest.raises(asyncpg.CheckViolationError):
                await connection.execute(
                    """
                    INSERT INTO trip_g01_amap_provider_effects (
                        evidence_run_id, candidate_commit, split, effect_id,
                        effect_key_sha256, provider_binding_sha256,
                        request_sha256, response_sha256,
                        provider_request_id_sha256, http_status,
                        resolution_status, accepted_source_names,
                        started_at, completed_at, raw_response_retained
                    ) VALUES (
                        $1, $2, 'validation', 'invalid-raw-retention', $3, $4,
                        $5, $6, $7, 200, 'UNRESOLVED', ARRAY[]::TEXT[],
                        $8, $9, TRUE
                    )
                    """,
                    evidence_run_id,
                    candidate_commit,
                    "a" * 64,
                    "2" * 64,
                    "3" * 64,
                    "4" * 64,
                    "5" * 64,
                    started_at,
                    completed_at,
                )

            roles = await connection.fetch(
                """
                SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
                       rolreplication, rolbypassrls
                FROM pg_roles
                WHERE rolname = ANY($1::text[])
                ORDER BY rolname
                """,
                [
                    "breezetravel_g01_amap_effect_writer",
                    "breezetravel_g01_live_effect_exporter",
                    "breezetravel_g01_qwen_effect_writer",
                ],
            )
            assert len(roles) == 3
            assert all(
                not row[field]
                for row in roles
                for field in (
                    "rolcanlogin",
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                    "rolbypassrls",
                )
            )

            await connection.execute("SET ROLE breezetravel_g01_amap_effect_writer")
            try:
                await connection.execute(
                    """
                    INSERT INTO trip_g01_amap_provider_effects (
                        evidence_run_id, candidate_commit, split, effect_id,
                        effect_key_sha256, provider_binding_sha256,
                        request_sha256, response_sha256,
                        provider_request_id_sha256, http_status,
                        resolution_status, accepted_source_names,
                        started_at, completed_at
                    ) VALUES (
                        'G01-LIVE-PGWRITER01', $1, 'validation',
                        'amap-writer-effect', $2, $3, $4, $5, $6, 200,
                        'UNRESOLVED', ARRAY[]::TEXT[], $7, $8
                    )
                    """,
                    candidate_commit,
                    "f" * 64,
                    "2" * 64,
                    "3" * 64,
                    "4" * 64,
                    "5" * 64,
                    started_at,
                    completed_at,
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.fetchval(
                        "SELECT count(*) FROM trip_g01_amap_provider_effects"
                    )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(
                        "UPDATE trip_g01_amap_provider_effects SET http_status = 204"
                    )
            finally:
                await connection.execute("RESET ROLE")

            await connection.execute("SET ROLE breezetravel_g01_qwen_effect_writer")
            try:
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(
                        """
                        INSERT INTO trip_g01_amap_provider_effects (
                            evidence_run_id, candidate_commit, split, effect_id,
                            effect_key_sha256, provider_binding_sha256,
                            request_sha256, response_sha256,
                            provider_request_id_sha256, http_status,
                            resolution_status, accepted_source_names,
                            started_at, completed_at
                        ) VALUES (
                            'G01-LIVE-PGCROSS01', $1, 'validation',
                            'amap-cross-effect', $2, $3, $4, $5, $6, 200,
                            'UNRESOLVED', ARRAY[]::TEXT[], $7, $8
                        )
                        """,
                        candidate_commit,
                        "0" * 64,
                        "2" * 64,
                        "3" * 64,
                        "4" * 64,
                        "5" * 64,
                        started_at,
                        completed_at,
                    )
            finally:
                await connection.execute("RESET ROLE")

            await connection.execute("SET ROLE breezetravel_g01_live_effect_exporter")
            try:
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM trip_g01_amap_provider_effects"
                    )
                ) == 3
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(
                        "UPDATE trip_g01_amap_provider_effects SET http_status = 204"
                    )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(
                        "DELETE FROM trip_g01_amap_provider_effects"
                    )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await connection.execute(
                        "TRUNCATE trip_g01_amap_provider_effects"
                    )
            finally:
                await connection.execute("RESET ROLE")

            for mutation in (
                "UPDATE trip_g01_amap_provider_effects SET http_status = 204",
                "DELETE FROM trip_g01_amap_provider_effects",
                "TRUNCATE trip_g01_amap_provider_effects",
            ):
                with pytest.raises(asyncpg.PostgresError, match="append-only"):
                    await connection.execute(mutation)
        finally:
            await connection.close()

        amap_rows, amap_database, amap_snapshot = await asyncio.to_thread(
            _read_typed_effects,
            database_url=database_dsn,
            evidence_run_id=evidence_run_id,
            lane="AMAP",
            candidate_commit=candidate_commit,
        )
        qwen_rows, qwen_database, qwen_snapshot = await asyncio.to_thread(
            _read_typed_effects,
            database_url=database_dsn,
            evidence_run_id=evidence_run_id,
            lane="QWEN",
            candidate_commit=candidate_commit,
        )

        assert [row["effect_id"] for row in amap_rows] == ["amap-effect-0002"]
        assert [row["effect_id"] for row in qwen_rows] == ["qwen-effect-0001"]
        assert qwen_rows[0]["inference_output_json"] == inference_json
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", value)
            for value in (
                amap_database,
                amap_snapshot,
                qwen_database,
                qwen_snapshot,
            )
        )
        assert amap_database == qwen_database
        assert amap_snapshot != qwen_snapshot

        with pytest.raises(LiveEvidenceExportError, match="no typed AMAP"):
            await asyncio.to_thread(
                _read_typed_effects,
                database_url=database_dsn,
                evidence_run_id=evidence_run_id,
                lane="AMAP",
                candidate_commit="c" * 40,
            )
    finally:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        await admin.close()
