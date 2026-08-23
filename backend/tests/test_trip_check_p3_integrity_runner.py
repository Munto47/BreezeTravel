from __future__ import annotations

import json

import pytest

from app.config import Settings
from evals.trip_check_v1.input_provider_integrity_runner import (
    live_credentials_ready,
    run_live_matrix,
    run_snapshot_matrix,
)


@pytest.mark.asyncio
async def test_snapshot_matrix_covers_three_cities_and_three_partial_failures(tmp_path):
    manifest = await run_snapshot_matrix(commit_sha="a" * 40, output=tmp_path / "snapshot")

    assert manifest["status"] == "PASS"
    assert manifest["canonical_case_count"] == 6
    assert manifest["canonical_cases_passed"] == 6
    assert manifest["cities"] == ["北京", "上海", "杭州"]
    assert set(manifest["route_modes"]) == {"walking", "transit", "bicycling", "driving"}
    assert manifest["network_call_count"] == 0
    assert all(item["replay_equal"] for item in manifest["cases"])
    readback = json.loads((tmp_path / "snapshot" / "provider_integrity_manifest.json").read_text("utf-8"))
    assert readback == manifest


@pytest.mark.asyncio
async def test_live_matrix_stays_not_run_without_all_credentials(tmp_path):
    settings = Settings(
        runtime_profile="test",
        amap_api_key="",
        qweather_api_key="",
        qweather_private_key="",
        brave_api_key="",
    )
    ready, missing = live_credentials_ready(settings)
    manifest = await run_live_matrix(
        commit_sha="b" * 40,
        output=tmp_path / "live",
        settings=settings,
    )

    assert ready is False
    assert set(missing) == {"AMAP_API_KEY", "QWEATHER_CREDENTIALS", "BRAVE_API_KEY"}
    assert manifest["status"] == "NOT_RUN"
    assert manifest["actual_receipt_count"] == 0
