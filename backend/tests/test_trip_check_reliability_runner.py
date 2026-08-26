from __future__ import annotations

import os

import pytest

from evals.trip_check_v1.reliability_runner import (
    CANONICAL_CASES,
    FORBIDDEN_TRACE_KEYS,
    _json_dump,
    run_reliability_matrix,
)


def test_reliability_matrix_contract_is_fixed_and_redacted():
    assert CANONICAL_CASES == (
        "provider_timeout",
        "partial_field_failure",
        "duplicate_submit",
        "concurrent_revision",
        "terminate_after_evidence",
        "config_drift",
    )
    assert {
        "raw_text",
        "poi_name",
        "prompt",
        "authorization",
        "provider_raw_response",
        "user_id",
    } <= FORBIDDEN_TRACE_KEYS


def test_reliability_json_artifacts_are_lf_normalized(tmp_path):
    artifact = tmp_path / "metrics.json"
    _json_dump(artifact, {"status": "PASS"})
    assert b"\r\n" not in artifact.read_bytes()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_reliability_matrix_is_six_of_six(tmp_path):
    if os.getenv("RUN_SERVICE_INTEGRATION") != "1":
        pytest.skip("set RUN_SERVICE_INTEGRATION=1 with controlled PostgreSQL")
    manifest = await run_reliability_matrix(
        commit_sha="abcdef0",
        output=tmp_path / "reliability",
    )
    assert manifest["status"] == "PASS"
    assert manifest["canonical_cases_passed"] == manifest["canonical_case_count"] == 6
    assert manifest["domain_required_field_coverage"] == 1.0
    assert manifest["domain_otel_association_rate"] == 1.0
    assert manifest["sensitive_attribute_hit_count"] == 0
