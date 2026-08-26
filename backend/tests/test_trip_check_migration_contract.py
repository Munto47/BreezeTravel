from pathlib import Path


MIGRATIONS = Path(__file__).parents[1] / "app" / "db" / "migrations"


def test_trip_check_migrations_are_append_only_and_sequential():
    expected = [
        "022_trip_brief_revisions.sql",
        "023_trip_check_runs.sql",
        "024_advice_bundles.sql",
    ]
    assert [path.name for path in sorted(MIGRATIONS.glob("02[2-4]_*.sql"))] == expected


def test_trip_brief_migration_carries_confirmation_and_privacy_contracts():
    sql = (MIGRATIONS / "022_trip_brief_revisions.sql").read_text(encoding="utf-8")
    for required in (
        "trip_brief_revisions",
        "trip_brief_field_sources",
        "temporary_asset_cleanup_receipts",
        "INFERRED",
        "NO_PREFERENCE",
        "CONFIRMED",
    ):
        assert required in sql


def test_trip_check_run_migration_separates_checkpoint_and_side_effect_receipts():
    sql = (MIGRATIONS / "023_trip_check_runs.sql").read_text(encoding="utf-8")
    for required in (
        "trip_check_runs",
        "trip_check_stage_attempts",
        "trip_check_side_effect_receipts",
        "trip_check_run_events",
        "trip_check_commands",
        "config_hash",
    ):
        assert required in sql
    assert "UNIQUE (run_id, side_effect_key)" in sql


def test_advice_migration_reuses_existing_repair_and_candidate_protocols():
    sql = (MIGRATIONS / "024_advice_bundles.sql").read_text(encoding="utf-8")
    assert "REFERENCES suggestion_sets(suggestion_set_id)" in sql
    assert "REFERENCES repair_options(repair_id)" in sql
    assert "trip_check_postcheck_lineage" in sql
