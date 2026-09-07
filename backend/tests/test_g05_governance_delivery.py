from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

import yaml

from governance.core_mainline import validate_delivery_receipt
from app.trip_understanding.models import KnowledgeSuggestionView


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / "backend/eval_data/g05_knowledge/admission_v1.json"
ORACLE_PATH = REPOSITORY_ROOT / "backend/eval_data/g05_knowledge/ablation_oracle_v1.json"
WORKFLOW_PATH = REPOSITORY_ROOT / ".github/workflows/core-mainline.yml"
MIGRATION_PATH = REPOSITORY_ROOT / "backend/app/db/migrations/032_knowledge_claims.sql"
DELIVERY_PATH = REPOSITORY_ROOT / "docs/governance/gate-results/G05.product-delivery.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_g05_frozen_source_and_ablation_bindings_are_exact() -> None:
    assert _sha256(MANIFEST_PATH) == (
        "f0776f865441af9a857ffb6c755e2407ae83ce1d2d1ffaba786cfe54998f3940"
    )
    assert _sha256(ORACLE_PATH) == (
        "d0bcf0691c98bac40bd4713f26487c88d8645766c791eef77d1ca161aceaefc7"
    )

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    dispositions = {
        item["claim_type"]: item["disposition"]
        for item in manifest["claim_type_dispositions"]
    }
    assert dispositions == {
        "TYPICAL_DURATION": "EXPLICIT_GAP",
        "SUITABLE_TIME": "CLAIM_AVAILABLE",
        "NIGHT_VIEW": "CLAIM_AVAILABLE",
        "SEASON": "CLAIM_AVAILABLE",
        "RESERVATION_ADVICE": "CLAIM_AVAILABLE",
    }
    assert len(manifest["places"]) == 18
    assert {item["city"] for item in manifest["places"]} == {"北京", "上海", "杭州"}
    assert all(
        urlparse(source["canonical_url"]).scheme == "https"
        and urlparse(source["terms_url"]).scheme == "https"
        for source in manifest["sources"]
    )
    not_ready = {
        (source["source_key"], source["version"])
        for source in manifest["sources"]
        if source["admission_status"] == "NOT_READY"
    }
    assert not {
        (claim["source_key"], claim["source_version"])
        for claim in manifest["claims"]
    } & not_ready
    serialized = json.dumps(manifest, ensure_ascii=False).lower()
    for forbidden in ("raw_html", "raw_body", "page_text", "full_text", "小红书"):
        assert forbidden not in serialized


def test_g05_public_projection_and_032_remain_bounded() -> None:
    public_fields = set(KnowledgeSuggestionView.model_json_schema()["properties"])
    assert public_fields == {
        "type",
        "text",
        "source_name",
        "source_url",
        "freshness",
    }
    assert not public_fields & {
        "claim_id",
        "claim_revision_id",
        "receipt",
        "license_status",
        "confidence",
        "reviewer",
    }

    migration = MIGRATION_PATH.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS knowledge_source_versions" in migration
    assert "CREATE TABLE IF NOT EXISTS knowledge_claim_versions" in migration
    assert "CREATE TABLE IF NOT EXISTS knowledge_usage_receipts" in migration
    assert "pgvector" not in migration.lower()
    assert "create extension" not in migration.lower()
    migrations = sorted(
        path.name for path in MIGRATION_PATH.parent.glob("*.sql")
    )
    assert migrations.index("032_knowledge_claims.sql") < migrations.index(
        "034_trip_understanding_screenshot_batches.sql"
    )


def test_g05_required_ci_jobs_are_explicit_and_fail_closed() -> None:
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")
    jobs = yaml.safe_load(workflow_text)["jobs"]
    g05_jobs = (
        "g05_knowledge_targeted",
        "g05_postgresql",
        "g05_browser_e2e",
    )
    for job_name in g05_jobs:
        assert jobs[job_name]["name"] == job_name
        assert jobs[job_name]["needs"] == "core-mainline-preflight"
        assert "sequence == '5'" in jobs[job_name]["if"]
        assert "work_kind != 'GOAL_TRANSITION'" in jobs[job_name]["if"]
    assert (
        jobs["g05_postgresql"]["services"]["postgres"]["image"]
        == "pgvector/pgvector:0.8.1-pg16"
    )

    aggregator = jobs["core-mainline"]
    assert set(g05_jobs).issubset(set(aggregator["needs"]))
    enforcement = aggregator["steps"][0]["run"]
    assert 'expected_g05_result="success"' in enforcement
    assert 'expected_g05_result="skipped"' in enforcement
    for result_name in (
        "G05_KNOWLEDGE_TARGETED_RESULT",
        "G05_POSTGRESQL_RESULT",
        "G05_BROWSER_E2E_RESULT",
    ):
        assert result_name in enforcement
    assert "frontend_build" in aggregator["needs"]


def test_g04_historical_exception_is_not_expanded_by_g05() -> None:
    receipt = json.loads(
        (
            REPOSITORY_ROOT
            / "docs/governance/gate-results/G04.product-delivery.json"
        ).read_text(encoding="utf-8")
    )
    exception = receipt["historical_compatibility"]
    assert exception["status"] == "PASS_WITH_APPROVED_HISTORICAL_EXCEPTION"
    assert exception["approved_failure_count"] == 2
    assert exception["unexpected_failure_count"] == 0
    assert exception["full_pytest_pass"] is False
    assert exception["removal_deadline"] == "BEFORE_G07_EXACT_BINDING_ACCEPTANCE"


def test_g05_delivery_receipt_binds_exact_product_and_required_checks() -> None:
    receipt = json.loads(DELIVERY_PATH.read_text(encoding="utf-8"))

    assert receipt["product_fingerprint"] == (
        "5e3838abbd35e500a6b505067d63fcf281b90d9d7127bc15cc92f34103a7881b"
    )
    assert receipt["checks"] == {
        "core_mainline_contract": "PASS",
        "g05_knowledge_targeted": "PASS",
        "g05_postgresql": "PASS",
        "frontend_build": "PASS",
        "g05_browser_e2e": "PASS",
    }
    assert receipt["remote_ci"] == {
        "workflow": "Product mainline",
        "run_id": 33386769272,
        "run_url": "https://github.com/Munto47/BreezeTravel/actions/runs/33386769272",
        "pull_request": 18,
        "head_commit": "9dcd911c85688cc8b5783a37e8c03f6cee413baa",
        "product_commit": "363daed34d25b991ad9699a7381ac0d64e658e8b",
        "core_mainline": "PASS",
    }
    assert receipt["historical_compatibility"]["approved_failure_count"] == 2
    assert receipt["historical_compatibility"]["full_pytest_pass"] is False
    assert validate_delivery_receipt(REPOSITORY_ROOT, 5)["verdict"] == "PASS"
