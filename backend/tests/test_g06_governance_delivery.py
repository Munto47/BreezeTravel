from __future__ import annotations

import json
from pathlib import Path

import yaml

from app.trip_understanding.memory_share import (
    DataConsentView,
    FeedbackRequest,
    PreferenceMemoryView,
    ShareProjectionView,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/core-mainline.yml"
MIGRATION_PATH = (
    REPOSITORY_ROOT / "backend/app/db/migrations/033_user_memory_and_feedback.sql"
)


def test_g06_public_contracts_are_bounded_and_purpose_specific() -> None:
    assert set(DataConsentView.model_json_schema()["properties"]) == {
        "memory_enabled",
        "feedback_enabled",
        "training_eval_enabled",
    }
    assert set(PreferenceMemoryView.model_json_schema()["properties"]) == {
        "walking_tolerance_minutes",
        "preferred_start_time",
        "dining_preferences",
        "hotel_preferences",
        "intensity",
    }
    assert set(FeedbackRequest.model_json_schema()["properties"]) == {
        "event_type",
        "subject_type",
        "subject_ref",
    }
    share_fields = set(ShareProjectionView.model_json_schema()["properties"])
    assert share_fields == {
        "title",
        "destination",
        "schedule",
        "party_size",
        "days",
        "accommodation",
        "message",
    }
    assert not share_fields & {
        "revision",
        "receipt",
        "hash",
        "source",
        "confidence",
        "provider",
        "finding",
    }


def test_g06_033_is_additive_minimal_and_contains_no_raw_content_columns() -> None:
    sql = MIGRATION_PATH.read_text(encoding="utf-8")
    for table in (
        "g06_data_consents",
        "g06_preference_profiles",
        "g06_feedback_events",
        "g06_share_links",
        "g06_share_sessions",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "DEFAULT FALSE" in sql
    assert "ON DELETE CASCADE" in sql
    assert "projection_json" in sql
    assert "secret_hash" in sql and "capability_hash" in sql
    lowered = sql.lower()
    for forbidden in (
        "source_text",
        "raw_text",
        "screenshot_bytes",
        "chat_message",
        "full_source",
        "pgvector",
        "create extension",
    ):
        assert forbidden not in lowered
    migrations = sorted(path.name for path in MIGRATION_PATH.parent.glob("*.sql"))
    assert migrations.index("033_user_memory_and_feedback.sql") < migrations.index(
        "034_trip_understanding_screenshot_batches.sql"
    )


def test_g06_required_ci_jobs_are_explicit_and_fail_closed() -> None:
    jobs = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))["jobs"]
    g06_jobs = (
        "g06_memory_share_targeted",
        "g06_postgresql",
        "g06_browser_e2e",
    )
    for job_name in g06_jobs:
        assert jobs[job_name]["name"] == job_name
        assert jobs[job_name]["needs"] == "core-mainline-preflight"
        assert "sequence == '6'" in jobs[job_name]["if"]
        assert "work_kind != 'GOAL_TRANSITION'" in jobs[job_name]["if"]
    aggregator = jobs["core-mainline"]
    assert set(g06_jobs).issubset(set(aggregator["needs"]))
    enforcement = aggregator["steps"][0]["run"]
    assert 'expected_g06_result="success"' in enforcement
    assert 'expected_g06_result="skipped"' in enforcement
    for result_name in (
        "G06_MEMORY_SHARE_TARGETED_RESULT",
        "G06_POSTGRESQL_RESULT",
        "G06_BROWSER_E2E_RESULT",
    ):
        assert result_name in enforcement
    assert "frontend_build" in aggregator["needs"]


def test_g06_binding_keeps_privacy_boundaries_and_historical_exception_exact() -> None:
    goal = (REPOSITORY_ROOT / "docs/governance/CURRENT_GOAL.md").read_text(
        encoding="utf-8"
    )
    binding = json.loads(
        (REPOSITORY_ROOT / "docs/governance/current_goal_binding.json").read_text(
            encoding="utf-8"
        )
    )
    registry = json.loads(
        (REPOSITORY_ROOT / "docs/governance/current_work_packages.json").read_text(
            encoding="utf-8"
        )
    )
    assert binding["goal_id"] == registry["active_goal_id"] == "TC-VNEXT-G06-MEMORY-SHARE"
    assert binding["status"] == "IN_PROGRESS"
    assert registry["active_slice"]["work_kind"] == "PRODUCT"
    for token in (
        "记忆默认关闭",
        "产品记忆不等于训练同意",
        "`/share/{share_ref}#s=<secret>`",
        "清空全部旅行数据",
    ):
        assert token in goal

    g04 = json.loads(
        (REPOSITORY_ROOT / "docs/governance/gate-results/G04.product-delivery.json").read_text(
            encoding="utf-8"
        )
    )["historical_compatibility"]
    assert g04["approved_failure_count"] == 2
    assert g04["unexpected_failure_count"] == 0
    assert g04["full_pytest_pass"] is False
    assert g04["removal_deadline"] == "BEFORE_G07_EXACT_BINDING_ACCEPTANCE"
