from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import imports as imports_api
from app.importing.repositories import InMemoryImportRepository
from app.importing.screenshots import (
    InMemoryScreenshotAssetRepository,
    OcrBoundingBox,
    OcrTextLine,
)
from app.importing.upload_batches import (
    InMemoryScreenshotUploadBatchRepository,
    ScreenshotUploadBatchService,
)
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.trip_check.briefs import InMemoryTripBriefRepository
from app.utils.auth import get_current_user


PNG = b"\x89PNG\r\n\x1a\nminiapp-batch-fixture"


class FakeOcrEngine:
    name = "batch_fixture_ocr"
    version = "v1"

    def __init__(self):
        self.calls = 0

    async def recognize(self, image_path: Path) -> list[OcrTextLine]:
        self.calls += 1
        assert image_path.exists()
        return [
            OcrTextLine(
                text="第1天：北京 2人 09:00-11:00 颐和园",
                confidence=0.96,
                box=OcrBoundingBox(x_min=1, y_min=1, x_max=300, y_max=40),
            )
        ]


class FakeProvider:
    async def search(self, *, query: str, city: str):
        return [
            {
                "place_id": "summer-palace",
                "name": query,
                "city": city,
                "category": "attraction",
                "address": "受控地址",
                "coords": {"lng": 116.27, "lat": 39.99},
                "retrieval_provider": "controlled_test",
                "retrieval_request_hash": "1" * 64,
                "retrieval_response_hash": "2" * 64,
                "retrieval_observed_at": "2026-08-23T00:00:00+00:00",
                "execution_mode": "fixture",
            }
        ]


def _client(monkeypatch):
    itinerary_repository = InMemoryItineraryRepository()
    workspace = TripWorkspace(
        workspace_id="workspace-batch-api",
        room_id="room-batch-api",
        city="北京",
        trip_date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        created_by="batch-user",
    )
    asyncio.run(itinerary_repository.create_workspace(workspace))
    import_repository = InMemoryImportRepository(itinerary_repository)
    command_repository = InMemoryCreationCommandRepository()
    brief_repository = InMemoryTripBriefRepository()
    asset_repository = InMemoryScreenshotAssetRepository()
    batch_repository = InMemoryScreenshotUploadBatchRepository()
    ocr_engine = FakeOcrEngine()
    app = FastAPI()
    app.include_router(imports_api.router, prefix="/api")
    app.dependency_overrides[imports_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[imports_api.get_import_repository] = lambda: import_repository
    app.dependency_overrides[imports_api.get_entity_candidate_provider] = lambda: FakeProvider()
    app.dependency_overrides[imports_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[imports_api.get_trip_brief_repository] = lambda: brief_repository
    app.dependency_overrides[imports_api.get_screenshot_asset_repository] = lambda: asset_repository
    app.dependency_overrides[imports_api.get_screenshot_upload_batch_repository] = lambda: batch_repository
    app.dependency_overrides[imports_api.get_ocr_engine] = lambda: ocr_engine
    app.dependency_overrides[get_current_user] = lambda: "batch-user"
    monkeypatch.setattr(imports_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), asset_repository, batch_repository, ocr_engine


def _create(client: TestClient, count: int = 2):
    return client.post(
        "/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches",
        headers={"Idempotency-Key": "create-batch"},
        json={"expected_count": count},
    )


def _upload(client: TestClient, batch_id: str, position: int, version: int, key: str):
    return client.post(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}/files/{position}",
        headers={"Idempotency-Key": key, "If-Match": str(version)},
        files={"file": (f"{position}.png", PNG + bytes([position]), "image/png")},
    )


def test_batch_requires_all_files_then_commits_once_and_replays(monkeypatch):
    client, asset_repository, _, engine = _client(monkeypatch)
    created = _create(client)
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    batch_id = created.json()["batch_id"]

    first = _upload(client, batch_id, 0, 1, "upload-0")
    replay = _upload(client, batch_id, 0, 1, "upload-0")
    assert first.status_code == replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert first.json()["uploaded_positions"] == [0]
    assert len(asset_repository.assets) == 1

    incomplete = client.post(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}/commit",
        headers={"Idempotency-Key": "commit", "If-Match": "2"},
    )
    assert incomplete.status_code == 409
    assert incomplete.json()["detail"]["uploaded_count"] == 1
    assert engine.calls == 0

    second = _upload(client, batch_id, 1, 2, "upload-1")
    assert second.status_code == 200
    assert second.json()["uploaded_positions"] == [0, 1]

    committed = client.post(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}/commit",
        headers={"Idempotency-Key": "commit", "If-Match": "3"},
    )
    repeated = client.post(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}/commit",
        headers={"Idempotency-Key": "commit", "If-Match": "3"},
    )
    assert committed.status_code == repeated.status_code == 201
    assert repeated.headers["Idempotency-Replayed"] == "true"
    assert committed.json()["batch"]["status"] == "SUCCEEDED"
    assert committed.json()["batch"]["result_import_id"]
    assert engine.calls == 2
    assert all(asset.state == "CLEANED" for asset in asset_repository.assets.values())


def test_batch_rejects_missing_precondition_and_version_conflict(monkeypatch):
    client, _, _, _ = _client(monkeypatch)
    batch_id = _create(client, count=1).json()["batch_id"]
    missing = client.post(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}/files/0",
        headers={"Idempotency-Key": "missing-version"},
        files={"file": ("0.png", PNG, "image/png")},
    )
    conflict = _upload(client, batch_id, 0, 9, "wrong-version")
    assert missing.status_code == 428
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "SCREENSHOT_UPLOAD_BATCH_VERSION_CONFLICT"


def test_cancel_deletes_staged_original_and_is_idempotent(monkeypatch):
    client, asset_repository, _, _ = _client(monkeypatch)
    batch_id = _create(client, count=1).json()["batch_id"]
    uploaded = _upload(client, batch_id, 0, 1, "upload-before-cancel")
    assert uploaded.status_code == 200
    cancelled = client.delete(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}",
        headers={"Idempotency-Key": "cancel", "If-Match": "2"},
    )
    replay = client.delete(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}",
        headers={"Idempotency-Key": "cancel", "If-Match": "2"},
    )
    assert cancelled.status_code == replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert cancelled.json()["batch"]["status"] == "CANCELLED"
    assert next(iter(asset_repository.assets.values())).state == "CLEANED"


def test_upload_rejects_idempotency_conflict_and_duplicate_position(monkeypatch):
    client, asset_repository, _, _ = _client(monkeypatch)
    batch_id = _create(client, count=2).json()["batch_id"]
    first = _upload(client, batch_id, 0, 1, "same-upload-key")
    changed = client.post(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}/files/0",
        headers={"Idempotency-Key": "same-upload-key", "If-Match": "1"},
        files={"file": ("changed.png", PNG + b"changed", "image/png")},
    )
    duplicate = _upload(client, batch_id, 0, 2, "different-upload-key")
    assert first.status_code == 200
    assert changed.status_code == 409
    assert changed.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert duplicate.status_code == 409
    assert len(asset_repository.assets) == 1


def test_cancel_cleanup_failure_stays_privacy_blocked(monkeypatch):
    client, asset_repository, batch_repository, _ = _client(monkeypatch)
    batch_id = _create(client, count=1).json()["batch_id"]
    assert _upload(client, batch_id, 0, 1, "upload-locked").status_code == 200
    original_unlink = Path.unlink

    def fail_unlink(self, missing_ok=False):
        raise PermissionError("controlled lock")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    cancelled = client.delete(
        f"/api/trip-workspaces/workspace-batch-api/screenshot-upload-batches/{batch_id}",
        headers={"Idempotency-Key": "cancel-locked", "If-Match": "2"},
    )
    assert cancelled.status_code == 500
    assert cancelled.json()["detail"]["code"] == "PRIVACY_BLOCKED"
    assert batch_repository.batches[batch_id].status == "PRIVACY_BLOCKED"
    asset = next(iter(asset_repository.assets.values()))
    assert asset.state == "CLEANUP_FAILED"
    monkeypatch.setattr(Path, "unlink", original_unlink)
    original_unlink(Path(asset.storage_locator), missing_ok=True)


def test_expired_batch_recovery_deletes_original_and_marks_expired(monkeypatch):
    client, asset_repository, batch_repository, _ = _client(monkeypatch)
    batch_id = _create(client, count=1).json()["batch_id"]
    assert _upload(client, batch_id, 0, 1, "upload-expiring").status_code == 200
    service = ScreenshotUploadBatchService(
        repository=batch_repository,
        asset_repository=asset_repository,
    )
    recovered = asyncio.run(
        service.recover_expired(now=datetime.now(timezone.utc) + timedelta(hours=1))
    )
    assert recovered == [batch_id]
    assert batch_repository.batches[batch_id].status == "EXPIRED"
    assert next(iter(asset_repository.assets.values())).state == "CLEANED"
