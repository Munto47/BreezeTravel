from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screenshot_batches_v3, trip_understandings_v3
from app.config import clear_settings_cache
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from app.trip_understanding.screenshot_batch import (
    CleanupAttempt,
    ScreenshotStagingCancelledError,
    StagedBatch,
    StagedScreenshot,
)
from app.trip_understanding.screenshot_ocr import (
    RawOcrLine,
    ScreenshotOcrEngineBindingV1,
)
from app.trip_understanding.worker import TripUnderstandingWorker
from app.utils.auth import get_current_user, get_optional_user


PNG = b"\x89PNG\r\n\x1a\n" + b"g04-fixture-pixels"
PNG_SECOND = b"\x89PNG\r\n\x1a\n" + b"g04-fixture-pixels-second"


@pytest.fixture(autouse=True)
def _reset_settings_cache_after_test():
    """Keep per-test screenshot roots out of later application lifespans."""

    clear_settings_cache()
    yield
    clear_settings_cache()


class FixtureOcrEngine:
    name = "g04-api-fixture"
    version = "1"

    def __init__(self, outcomes: tuple[str, ...] = ("SUCCESS",)) -> None:
        self.outcomes = outcomes
        self.calls = 0

    @property
    def binding(self) -> ScreenshotOcrEngineBindingV1:
        return ScreenshotOcrEngineBindingV1.create(
            engine=self.name,
            engine_version=self.version,
            configuration={"evidence_tier": "AUTOMATED_FIXTURE"},
        )

    async def recognize(self, image_path: Path) -> tuple[RawOcrLine, ...]:
        del image_path
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if outcome == "FAIL":
            raise ValueError("fixture OCR failure")
        if outcome == "NO_TEXT":
            return ()
        return (
            RawOcrLine(
                text="北京三日行程",
                confidence=0.99,
                bbox=((20, 20), (500, 20), (500, 90), (20, 90)),
            ),
            RawOcrLine(
                text="Day 1 故宫博物院 景山公园",
                confidence=0.99,
                bbox=((20, 120), (900, 120), (900, 190), (20, 190)),
            ),
        )


def _build_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    engine: FixtureOcrEngine | None = None,
) -> tuple[TestClient, InMemoryTripUnderstandingRepository, FastAPI]:
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(tmp_path / "screenshots"))
    clear_settings_cache()
    repository = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(screenshot_batches_v3.router, prefix="/api")
    app.include_router(trip_understandings_v3.router, prefix="/api")
    app.dependency_overrides[
        screenshot_batches_v3.get_screenshot_batch_repository
    ] = lambda: repository
    app.dependency_overrides[
        trip_understandings_v3.get_trip_understanding_repository
    ] = lambda: repository
    app.dependency_overrides[get_current_user] = lambda: "user-a"
    app.dependency_overrides[get_optional_user] = lambda: "user-a"
    app.dependency_overrides[screenshot_batches_v3.get_screenshot_ocr_engine] = (
        lambda: engine or FixtureOcrEngine()
    )
    return TestClient(app), repository, app


def _files(*values: bytes):
    return [
        ("screenshots", (f"private-{index}.png", value, "image/png"))
        for index, value in enumerate(values)
    ]


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key.casefold()
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def test_upload_consume_replay_and_public_projection_are_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FixtureOcrEngine()
    client, repository, _ = _build_client(tmp_path, monkeypatch, engine=engine)

    uploaded = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "upload-one"},
        files=_files(PNG),
    )
    assert uploaded.status_code == 201
    assert uploaded.headers["cache-control"] == "no-store"
    payload = uploaded.json()
    assert set(payload) == {"batch_ref", "expires_at", "outcome", "message"}
    assert payload["outcome"] == "COMPLETE"
    assert len(payload["batch_ref"]) == 43
    serialized_upload = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("private-0.png", "bbox", "confidence", "paddle", "receipt"):
        assert forbidden not in serialized_upload.casefold()
    root = tmp_path / "screenshots"
    assert root.is_dir()
    assert list(root.iterdir()) == []

    receipt_count = len(repository.screenshot_cleanup_receipts)
    stage_calls = 0

    async def forbidden_replay_stager(*args, **kwargs):
        nonlocal stage_calls
        del args, kwargs
        stage_calls += 1
        raise AssertionError("terminal idempotency replay consumed request.stream")

    monkeypatch.setattr(
        screenshot_batches_v3,
        "stage_screenshot_multipart",
        forbidden_replay_stager,
    )
    replay = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "upload-one"},
        files=_files(PNG),
    )
    assert replay.status_code == 201
    assert replay.headers["idempotency-replayed"] == "true"
    assert replay.json() == payload
    assert engine.calls == 1
    assert stage_calls == 0
    assert len(repository.screenshot_cleanup_receipts) == receipt_count
    assert list(root.iterdir()) == []

    # Standard idempotency semantics bind the owner/key pair. Once terminal,
    # the application does not read or reinterpret a replacement body.
    reused_key = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "upload-one"},
        files=_files(PNG_SECOND),
    )
    assert reused_key.status_code == 201
    assert reused_key.headers["idempotency-replayed"] == "true"
    assert reused_key.json() == payload
    assert stage_calls == 0
    assert engine.calls == 1
    assert len(repository.screenshot_cleanup_receipts) == receipt_count

    created = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "trip-from-screenshot"},
        json={
            "mode": "FULL",
            "source": {"type": "SCREENSHOT_BATCH", "batch_ref": payload["batch_ref"]},
        },
    )
    assert created.status_code == 202
    assert payload["batch_ref"] not in created.text
    assert set(created.json()) == {
        "public_resource_id",
        "status",
        "message",
        "result_url",
        "events_url",
    }
    replayed_create = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "trip-from-screenshot"},
        json={
            "mode": "FULL",
            "source": {"type": "SCREENSHOT_BATCH", "batch_ref": payload["batch_ref"]},
        },
    )
    assert replayed_create.status_code == 202
    assert replayed_create.headers["idempotency-replayed"] == "true"
    assert replayed_create.json() == created.json()
    reused = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "trip-from-screenshot-again"},
        json={
            "mode": "FULL",
            "source": {"type": "SCREENSHOT_BATCH", "batch_ref": payload["batch_ref"]},
        },
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "SCREENSHOT_BATCH_ALREADY_USED"

    asyncio.run(TripUnderstandingWorker(repository).run_once("g04-api-worker"))
    result = client.get(created.json()["result_url"])
    assert result.status_code == 200
    assert payload["batch_ref"] not in result.text
    forbidden_public_keys = {
        "source",
        "span",
        "span_start",
        "span_end",
        "bbox",
        "confidence",
        "model",
        "provider",
        "receipt",
        "hash",
        "revision",
    }
    assert forbidden_public_keys.isdisjoint(_walk_keys(result.json()))


def test_upload_contract_rejects_login_json_magic_and_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, app = _build_client(tmp_path, monkeypatch)
    app.dependency_overrides.pop(get_current_user)
    assert client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "anonymous"},
        files=_files(PNG),
    ).status_code == 401
    app.dependency_overrides[get_current_user] = lambda: "user-a"

    json_response = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "json"},
        json={"screenshots": ["base64"]},
    )
    assert json_response.status_code == 415
    assert json_response.json()["detail"]["code"] == "MULTIPART_REQUIRED"
    invalid_magic = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "bad-magic"},
        files=_files(b"not-a-png"),
    )
    assert invalid_magic.status_code == 422
    assert invalid_magic.json()["detail"]["code"] == "SCREENSHOT_BATCH_INVALID"
    too_many = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "too-many"},
        files=_files(*(PNG + bytes([index]) for index in range(7))),
    )
    assert too_many.status_code == 422
    assert too_many.json()["detail"]["code"] == "SCREENSHOT_BATCH_INVALID"
    assert list((tmp_path / "screenshots").iterdir()) == []


def test_partial_no_text_cross_owner_and_expired_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = FixtureOcrEngine(("SUCCESS", "FAIL"))
    client, repository, app = _build_client(tmp_path, monkeypatch, engine=engine)
    partial = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "partial"},
        files=_files(PNG, PNG_SECOND),
    )
    assert partial.status_code == 201
    assert partial.json()["outcome"] == "PARTIAL"

    app.dependency_overrides[get_optional_user] = lambda: "user-b"
    cross_owner = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "cross-owner"},
        json={
            "mode": "FULL",
            "source": {
                "type": "SCREENSHOT_BATCH",
                "batch_ref": partial.json()["batch_ref"],
            },
        },
    )
    assert cross_owner.status_code == 404
    assert cross_owner.json()["detail"]["code"] == "SCREENSHOT_BATCH_NOT_FOUND"

    app.dependency_overrides[get_optional_user] = lambda: "user-a"
    row = next(iter(repository.screenshot_batches.values()))
    row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = client.post(
        "/api/v3/trip-understandings",
        headers={"Idempotency-Key": "expired"},
        json={
            "mode": "FULL",
            "source": {
                "type": "SCREENSHOT_BATCH",
                "batch_ref": partial.json()["batch_ref"],
            },
        },
    )
    assert expired.status_code == 410
    assert expired.json()["detail"]["code"] == "SCREENSHOT_BATCH_EXPIRED"

    no_text_client, _, _ = _build_client(
        tmp_path / "no-text",
        monkeypatch,
        engine=FixtureOcrEngine(("NO_TEXT",)),
    )
    no_text = no_text_client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "no-text"},
        files=_files(PNG),
    )
    assert no_text.status_code == 422
    assert no_text.json()["detail"]["code"] == "SCREENSHOT_TEXT_NOT_FOUND"
    assert "batch_ref" not in no_text.text


def test_local_recovery_is_bounded_to_random_stale_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "screenshots"
    monkeypatch.setenv("SCREENSHOT_BATCH_TEMP_ROOT", str(root))
    clear_settings_cache()
    stale = root / ("a" * 64)
    stale.mkdir(parents=True)
    (stale / ("b" * 64)).write_bytes(PNG)
    unrelated = root / "do-not-touch"
    unrelated.mkdir()
    (unrelated / "user-file").write_text("preserve", encoding="utf-8")

    result = screenshot_batches_v3.recover_orphaned_local_screenshot_files(
        minimum_age_seconds=0,
    )

    assert result.summary() == {
        "directories_removed": 1,
        "files_removed": 1,
        "failures": 0,
    }
    assert len(result.batches) == 1
    assert result.batches[0].batch_locator == "a" * 64
    assert result.batches[0].asset_locators == ("b" * 64,)
    assert result.batches[0].attempts[-1].terminal_reason == "CRASH_RECOVERY"
    assert result.batches[0].attempts[-1].deleted_locators == ("b" * 64,)
    assert not stale.exists()
    assert (unrelated / "user-file").read_text(encoding="utf-8") == "preserve"


def test_cleanup_retry_preserves_failed_attempt_but_finalizes_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository, _ = _build_client(tmp_path, monkeypatch)
    original_unlink = Path.unlink
    failed_once = False

    def flaky_unlink(path: Path, *args, **kwargs):
        nonlocal failed_once
        if len(path.name) == 64 and not failed_once:
            failed_once = True
            raise PermissionError("fixture first-attempt denial")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    response = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "cleanup-retry"},
        files=_files(PNG),
    )

    assert response.status_code == 201
    statuses = [
        item["cleanup_status"] for item in repository.screenshot_cleanup_receipts
    ]
    assert statuses == ["DELETE_FAILED", "DELETED"]
    batch = next(iter(repository.screenshot_batches.values()))
    assert batch["status"] == "READY"
    assert batch["asset_cleanup_statuses"] == {0: "CLEANED"}


def test_staging_cleanup_failure_is_privacy_blocked_and_never_returns_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, repository, _ = _build_client(tmp_path, monkeypatch)
    original_unlink = Path.unlink

    def blocked_unlink(path: Path, *args, **kwargs):
        if len(path.name) == 64:
            raise PermissionError("fixture persistent denial")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked_unlink)
    response = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "cleanup-blocked"},
        files=_files(b"not-a-png"),
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "SCREENSHOT_CLEANUP_RETRY_REQUIRED"
    assert "batch_ref" not in response.text
    batch = next(iter(repository.screenshot_batches.values()))
    assert batch["status"] == "PRIVACY_BLOCKED"


def test_wait_for_wrapped_staging_timeout_persists_inner_cleanup_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCREENSHOT_STAGING_DEADLINE_SECONDS", "0.001")
    client, repository, _ = _build_client(tmp_path, monkeypatch)
    batch_locator = "a" * 64
    asset_locator = "b" * 64
    staged = StagedBatch(
        batch_locator=batch_locator,
        temp_root=tmp_path,
        directory=tmp_path / batch_locator,
        screenshots=(
            StagedScreenshot(
                part_index=0,
                media_type="image/png",
                byte_size=len(PNG),
                sha256="c" * 64,
                locator=asset_locator,
                path=tmp_path / batch_locator / asset_locator,
            ),
        ),
        body_bytes=len(PNG),
        total_file_bytes=len(PNG),
    )
    cleanup = CleanupAttempt(
        attempt_number=1,
        terminal_reason="TIMED_OUT",
        attempted_at=datetime.now(timezone.utc),
        deleted_locators=(asset_locator,),
        already_absent_locators=(),
        failed_locators=(),
        remaining_locators=(),
        error_categories=(),
        directory_removed=True,
        succeeded=True,
    )

    async def controlled_stager(*args, **kwargs):
        del args, kwargs
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError as exc:
            raise ScreenshotStagingCancelledError(
                "controlled wait_for cancellation",
                batch=staged,
                cleanup_attempts=(cleanup,),
            ) from exc

    monkeypatch.setattr(
        screenshot_batches_v3,
        "stage_screenshot_multipart",
        controlled_stager,
    )
    response = client.post(
        "/api/v3/screenshot-batches",
        headers={"Idempotency-Key": "wrapped-timeout"},
        files=_files(PNG),
    )

    assert response.status_code == 504
    assert response.json()["detail"]["code"] == "SCREENSHOT_PROCESSING_TIMED_OUT"
    assert "batch_ref" not in response.text
    batch = next(iter(repository.screenshot_batches.values()))
    assert batch["status"] == "TIMED_OUT"
    assert repository.screenshot_cleanup_receipts[0]["terminal_reason"] == "TIMED_OUT"
    assert repository.screenshot_cleanup_receipts[0]["cleanup_status"] == "DELETED"


def test_concurrent_same_upload_has_one_ocr_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingOcrEngine(FixtureOcrEngine):
        async def recognize(self, image_path: Path) -> tuple[RawOcrLine, ...]:
            started.set()
            await asyncio.to_thread(release.wait, 5)
            return await super().recognize(image_path)

    engine = BlockingOcrEngine()
    client, repository, app = _build_client(tmp_path, monkeypatch, engine=engine)
    second_client = TestClient(app)

    def upload_first():
        return client.post(
            "/api/v3/screenshot-batches",
            headers={"Idempotency-Key": "concurrent-upload"},
            files=_files(PNG),
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(upload_first)
        assert started.wait(5)
        duplicate = second_client.post(
            "/api/v3/screenshot-batches",
            headers={"Idempotency-Key": "concurrent-upload"},
            files=_files(PNG),
        )
        release.set()
        winner = pending.result(timeout=10)

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "REQUEST_IN_PROGRESS"
    assert winner.status_code == 201
    assert engine.calls == 1
    assert len(repository.screenshot_batches) == 1
    assert len(repository.screenshot_cleanup_receipts) == 1
