from __future__ import annotations

from pathlib import Path

import pytest

from app.db import connection


class _Acquire:
    def __init__(self, value: object) -> None:
        self.value = value

    async def __aenter__(self) -> object:
        return self.value

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection_value: object) -> None:
        self.connection_value = connection_value

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection_value)


class _Connection:
    def __init__(self, applied: set[str]) -> None:
        self.applied = applied

    async def fetchval(self, _query: str) -> bool:
        return True

    async def fetch(self, _query: str, expected: list[str]):
        return [
            {"filename": filename}
            for filename in expected
            if filename in self.applied
        ]


def _image_migrations() -> set[str]:
    return {
        path.name
        for path in Path("app/db/migrations").glob("*.sql")
    }


@pytest.mark.asyncio
async def test_schema_check_requires_every_migration_in_the_current_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied = _image_migrations()
    applied.remove("031_day_index_trip_bridge.sql")
    fake_pool = _Pool(_Connection(applied))

    async def get_fake_pool() -> _Pool:
        return fake_pool

    monkeypatch.setattr(connection, "get_pool", get_fake_pool)
    with pytest.raises(RuntimeError, match="031_day_index_trip_bridge.sql"):
        await connection.check_schema_version()


@pytest.mark.asyncio
async def test_schema_check_accepts_the_exact_complete_image_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_pool = _Pool(_Connection(_image_migrations()))

    async def get_fake_pool() -> _Pool:
        return fake_pool

    monkeypatch.setattr(connection, "get_pool", get_fake_pool)
    assert await connection.check_schema_version() is None


@pytest.mark.asyncio
async def test_schema_check_rejects_a_required_filename_absent_from_the_image() -> None:
    with pytest.raises(RuntimeError, match="required migration is absent from image"):
        await connection.check_schema_version(
            "032_reserved_future_migration.sql"
        )


def test_cleanup_receipts_are_delete_immutable_and_not_cascade_owned() -> None:
    migration = (
        Path(connection.__file__).resolve().parent
        / "migrations"
        / "034_trip_understanding_screenshot_batches.sql"
    ).read_text(encoding="utf-8")
    receipt_table = migration.split(
        "CREATE TABLE IF NOT EXISTS trip_understanding_screenshot_cleanup_receipts (",
        1,
    )[1].split(");", 1)[0]

    assert "REFERENCES" not in receipt_table
    assert "ON DELETE CASCADE" not in receipt_table
    assert (
        "BEFORE UPDATE OR DELETE ON "
        "trip_understanding_screenshot_cleanup_receipts"
    ) in migration
