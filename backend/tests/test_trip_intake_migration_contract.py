from pathlib import Path

from app.config import Settings


MIGRATION = Path("app/db/migrations/025_trip_intake_v2.sql")


def test_runtime_requires_trip_intake_v2_migration() -> None:
    assert Settings().required_migration == "025_trip_intake_v2.sql"


def test_migration_adds_immutable_intake_lineage_and_relaxes_only_product_scope_checks() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "trip_intake_revisions",
        "trip_intake_field_sources",
        "trip_intake_commands",
        "trip_intake_materializations",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "UNIQUE (intake_id, idempotency_key)" in sql
    assert "UNIQUE (intake_id, intake_revision)" in sql
    assert "trip_brief_revisions_source_intake_fk" in sql
    assert "trip_workspaces_city_nonblank_check" in sql
    assert "trip_brief_revisions_traveler_positive_check" in sql
    assert "suggestion_sets_day_index_nonnegative_check" in sql
    assert "reject_trip_intake_fact_update" in sql
    assert "SCREENSHOT_OCR" in sql


def test_historical_p6_fingerprint_remains_bound_through_024() -> None:
    runner = Path("evals/trip_check_v1/p6/postgres_runner.py").read_text(encoding="utf-8")
    assert 'LATEST_MIGRATION = "024_advice_bundles.sql"' in runner
    assert "if path.name <= LATEST_MIGRATION" in runner
