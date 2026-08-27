from pathlib import Path

from app.config import Settings


MIGRATION = Path("app/db/migrations/026_trip_intake_v2.sql")
LINEAGE_MIGRATION = Path("app/db/migrations/027_trip_intake_revision_lineage.sql")


def test_runtime_requires_trip_intake_v2_migration() -> None:
    assert Settings().required_migration == "028_trip_understanding_v3.sql"


def test_lineage_migration_removes_only_the_conflicting_room_intake_constraint() -> None:
    sql = LINEAGE_MIGRATION.read_text(encoding="utf-8")

    assert "ALTER TABLE trip_intake_revisions" in sql
    assert (
        "DROP CONSTRAINT IF EXISTS trip_intake_revisions_room_id_intake_id_key"
        in sql
    )
    assert "DROP TABLE" not in sql
    assert "DROP COLUMN" not in sql


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


def test_historical_p6_fingerprint_remains_bound_through_miniapp_025() -> None:
    runner = Path("evals/trip_check_v1/p6/postgres_runner.py").read_text(encoding="utf-8")
    assert 'LATEST_MIGRATION = "025_miniapp_identity_and_upload_batches.sql"' in runner
    assert "if path.name <= LATEST_MIGRATION" in runner
