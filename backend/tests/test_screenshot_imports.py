from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import imports as imports_api
from app.importing.errors import OcrProcessingError
from app.importing.repositories import InMemoryImportRepository
from app.importing.screenshots import (
    AssetCleanupReceipt,
    InMemoryScreenshotAssetRepository,
    OcrBoundingBox,
    OcrTextLine,
    PaddleOcrEngine,
    ScreenshotAssetCleanupService,
    TemporaryAssetRecord,
)
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.trip_check.briefs import InMemoryTripBriefRepository
from app.utils.auth import get_current_user


PNG = b"\x89PNG\r\n\x1a\ncontrolled-png-fixture"


class FakeOcrEngine:
    name = "controlled_ocr_fixture"
    version = "fixture-v1"

    def __init__(
        self,
        *,
        confidence: float = 0.62,
        fail: bool = False,
        decorative_footer: bool = False,
        decorative_header: bool = False,
        wrapped_place: bool = False,
        single_ascii_noise: str | None = None,
    ):
        self.confidence = confidence
        self.fail = fail
        self.decorative_footer = decorative_footer
        self.decorative_header = decorative_header
        self.wrapped_place = wrapped_place
        self.single_ascii_noise = single_ascii_noise
        self.calls = 0

    async def recognize(self, image_path: Path) -> list[OcrTextLine]:
        self.calls += 1
        assert image_path.exists()
        if self.fail:
            raise OcrProcessingError("controlled OCR failure")
        lines = []
        if self.decorative_header:
            lines.append(
                OcrTextLine(
                    text="行程备忘",
                    confidence=0.99,
                    box=OcrBoundingBox(x_min=1, y_min=2, x_max=120, y_max=40),
                    requires_confirmation=False,
                )
            )
        content_y = 100 if self.decorative_header else 2
        lines.append(
            OcrTextLine(
                text=(
                    "第1天：北京 2人 地铁 09:00-11:00 颐和"
                    if self.wrapped_place
                    else "第1天：北京 2人 地铁 09:00-11:00 颐和园"
                ),
                confidence=self.confidence,
                box=OcrBoundingBox(x_min=1, y_min=content_y, x_max=300, y_max=content_y + 38),
                requires_confirmation=self.confidence < 0.85,
            )
        )
        if self.wrapped_place:
            lines.append(
                OcrTextLine(
                    text="园",
                    confidence=self.confidence,
                    box=OcrBoundingBox(x_min=1, y_min=content_y + 42, x_max=40, y_max=content_y + 80),
                    requires_confirmation=self.confidence < 0.85,
                )
            )
        if self.decorative_footer:
            if self.single_ascii_noise is not None:
                lines.append(
                    OcrTextLine(
                        text=self.single_ascii_noise,
                        confidence=0.51,
                        box=OcrBoundingBox(x_min=1, y_min=1000, x_max=30, y_max=1038),
                        requires_confirmation=True,
                    )
                )
            lines.append(
                OcrTextLine(
                    text="#1826",
                    confidence=0.99,
                    box=OcrBoundingBox(x_min=900, y_min=1100, x_max=980, y_max=1140),
                    requires_confirmation=False,
                )
            )
        return lines


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


def _client(monkeypatch, *, engine: FakeOcrEngine | None = None):
    itinerary_repository = InMemoryItineraryRepository()
    workspace = TripWorkspace(
        workspace_id="workspace-screenshot-api",
        room_id="room-screenshot-api",
        city="北京",
        trip_date_range=TripDateRange(start=date(2026, 9, 1), end=date(2026, 9, 2)),
        created_by="screenshot-user",
    )
    asyncio.run(itinerary_repository.create_workspace(workspace))
    import_repository = InMemoryImportRepository(itinerary_repository)
    command_repository = InMemoryCreationCommandRepository()
    brief_repository = InMemoryTripBriefRepository()
    asset_repository = InMemoryScreenshotAssetRepository()
    ocr_engine = engine or FakeOcrEngine()
    app = FastAPI()
    app.include_router(imports_api.router, prefix="/api")
    app.dependency_overrides[imports_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[imports_api.get_import_repository] = lambda: import_repository
    app.dependency_overrides[imports_api.get_entity_candidate_provider] = lambda: FakeProvider()
    app.dependency_overrides[imports_api.get_creation_command_repository] = lambda: command_repository
    app.dependency_overrides[imports_api.get_trip_brief_repository] = lambda: brief_repository
    app.dependency_overrides[imports_api.get_screenshot_asset_repository] = lambda: asset_repository
    app.dependency_overrides[imports_api.get_ocr_engine] = lambda: ocr_engine
    app.dependency_overrides[get_current_user] = lambda: "screenshot-user"
    monkeypatch.setattr(imports_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), itinerary_repository, brief_repository, asset_repository, ocr_engine


def test_screenshot_import_cleans_original_and_replays_without_new_assets(monkeypatch):
    client, _, brief_repository, asset_repository, engine = _client(monkeypatch)
    path = "/api/trip-workspaces/workspace-screenshot-api/imports/screenshots"
    headers = {"Idempotency-Key": "screenshot-import-once"}
    files = [("screenshots", ("trip.png", PNG, "image/png"))]

    first = client.post(path, headers=headers, files=files)
    replay = client.post(path, headers=headers, files=files)

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert engine.calls == 1
    assert len(asset_repository.assets) == 1
    asset = next(iter(asset_repository.assets.values()))
    assert asset.state == "CLEANED"
    assert asset.storage_locator == f"deleted://{asset.asset_id}"
    receipt = first.json()["cleanup_receipts"][0]
    assert receipt["cleanup_status"] == "DELETED"
    assert "storage_locator" not in first.text
    itinerary_import = first.json()["itinerary_import"]
    assert itinerary_import["status"] == "NEEDS_RESOLUTION"
    assert itinerary_import["resolutions"][0]["resolution_status"] == "AMBIGUOUS"
    assert itinerary_import["resolutions"][0]["canonical_place_id"] is None
    ocr = first.json()["ocr_receipts"][0]
    assert ocr["engine"] == "controlled_ocr_fixture"
    assert ocr["lines"][0]["requires_confirmation"] is True
    assert ocr["lines"][0]["box"] == {"x_min": 1, "y_min": 2, "x_max": 300, "y_max": 40}
    artifacts = asset_repository.ocr_artifacts[itinerary_import["import_id"]]
    assert artifacts[0].asset_hash == ocr["asset_hash"]
    brief = brief_repository.briefs[("workspace-screenshot-api", 1)]
    assert brief is not None
    assert brief.field_provenance["traveler_count"].confidence == pytest.approx(0.62)


def test_screenshot_idempotency_rejects_different_image_bytes(monkeypatch):
    client, _, _, asset_repository, _ = _client(monkeypatch, engine=FakeOcrEngine(confidence=0.95))
    path = "/api/trip-workspaces/workspace-screenshot-api/imports/screenshots"
    headers = {"Idempotency-Key": "screenshot-conflict"}
    first = client.post(path, headers=headers, files=[("screenshots", ("a.png", PNG, "image/png"))])
    reused = client.post(
        path,
        headers=headers,
        files=[("screenshots", ("b.png", PNG + b"different", "image/png"))],
    )

    assert first.status_code == 201
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(asset_repository.assets) == 1


def test_screenshot_import_retains_decorative_footer_receipt_but_does_not_parse_it_as_stop(monkeypatch):
    engine = FakeOcrEngine(
        confidence=0.95,
        decorative_footer=True,
        decorative_header=True,
        wrapped_place=True,
        single_ascii_noise="a",
    )
    client, _, _, _, _ = _client(monkeypatch, engine=engine)

    response = client.post(
        "/api/trip-workspaces/workspace-screenshot-api/imports/screenshots",
        headers={"Idempotency-Key": "screenshot-decorative-footer"},
        files=[("screenshots", ("trip.png", PNG, "image/png"))],
    )

    assert response.status_code == 201
    body = response.json()
    assert [line["text"] for line in body["ocr_receipts"][0]["lines"]] == [
        "行程备忘",
        "第1天：北京 2人 地铁 09:00-11:00 颐和",
        "园",
        "a",
        "#1826",
    ]
    assert body["itinerary_import"]["raw_text"] == "第1天：北京 2人 地铁 09:00-11:00 颐和园"
    assert len(body["itinerary_import"]["raw_stops"]) == 1
    assert body["itinerary_import"]["status"] == "READY"
    noise = next(
        line for line in body["ocr_receipts"][0]["lines"] if line["text"] == "a"
    )
    assert noise["requires_confirmation"] is True


def test_invalid_screenshot_batch_creates_no_asset(monkeypatch):
    client, _, _, asset_repository, engine = _client(monkeypatch)
    response = client.post(
        "/api/trip-workspaces/workspace-screenshot-api/imports/screenshots",
        headers={"Idempotency-Key": "invalid-screenshot"},
        files=[("screenshots", ("fake.png", b"not-a-png", "image/png"))],
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "SCREENSHOT_BATCH_INVALID"
    assert asset_repository.assets == {}
    assert engine.calls == 0


def test_ocr_failure_still_deletes_original_and_records_failed_terminal_reason(monkeypatch):
    client, _, _, asset_repository, _ = _client(monkeypatch, engine=FakeOcrEngine(fail=True))
    response = client.post(
        "/api/trip-workspaces/workspace-screenshot-api/imports/screenshots",
        headers={"Idempotency-Key": "ocr-failure"},
        files=[("screenshots", ("trip.png", PNG, "image/png"))],
    )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "OCR_PROCESSING_FAILED"
    assert len(asset_repository.cleanup_receipts) == 1
    receipt = next(iter(asset_repository.cleanup_receipts.values()))
    assert receipt.terminal_reason == "FAILED"
    assert receipt.cleanup_status == "DELETED"
    asset = asset_repository.assets[receipt.asset_id]
    assert asset.state == "CLEANED"


def test_cleanup_failure_fails_closed_as_privacy_blocked(monkeypatch):
    client, _, _, asset_repository, _ = _client(monkeypatch, engine=FakeOcrEngine(confidence=0.95))
    original_unlink = Path.unlink

    def fail_unlink(self, missing_ok=False):
        raise PermissionError("controlled locked file")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    response = client.post(
        "/api/trip-workspaces/workspace-screenshot-api/imports/screenshots",
        headers={"Idempotency-Key": "cleanup-failure"},
        files=[("screenshots", ("trip.png", PNG, "image/png"))],
    )

    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "PRIVACY_BLOCKED"
    receipt = next(iter(asset_repository.cleanup_receipts.values()))
    assert receipt.cleanup_status == "DELETE_FAILED"
    assert receipt.cleanup_error_category == "PERMISSIONERROR"
    asset = asset_repository.assets[receipt.asset_id]
    assert asset.state == "CLEANUP_FAILED"
    monkeypatch.setattr(Path, "unlink", original_unlink)
    original_unlink(Path(asset.storage_locator), missing_ok=True)


@pytest.mark.asyncio
async def test_expired_asset_recovery_deletes_file_and_is_idempotent(tmp_path):
    asset_repository = InMemoryScreenshotAssetRepository()
    path = tmp_path / "expired.png"
    path.write_bytes(PNG)
    asset = TemporaryAssetRecord(
        asset_id="expired",
        workspace_id="workspace",
        content_hash="a" * 64,
        media_type="image/png",
        byte_size=len(PNG),
        storage_locator=str(path),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
    )
    await asset_repository.create_assets([asset])
    service = ScreenshotAssetCleanupService(asset_repository, temp_root=tmp_path)

    receipts = await service.recover_expired()
    repeated = await service.recover_expired()

    assert len(receipts) == 1
    assert receipts[0] == AssetCleanupReceipt(
        receipt_id=receipts[0].receipt_id,
        asset_id="expired",
        terminal_reason="TIMED_OUT",
        cleanup_status="DELETED",
        asset_hash="a" * 64,
        cleanup_attempted_at=receipts[0].cleanup_attempted_at,
    )
    assert repeated == []
    assert not path.exists()


@pytest.mark.asyncio
async def test_paddle_ocr_adapter_maps_boxes_and_low_confidence_without_runtime(monkeypatch, tmp_path):
    engine = PaddleOcrEngine(confirmation_threshold=0.85)
    payload = [{
        "res": {
            "rec_texts": ["北京2人", "颐和园"],
            "rec_scores": [0.97, 0.72],
            "rec_boxes": [[1, 2, 120, 30], [1, 32, 100, 60]],
        }
    }]
    monkeypatch.setattr(engine, "_predict", lambda _: payload)
    image = tmp_path / "controlled.png"
    image.write_bytes(PNG)

    lines = await engine.recognize(image)

    assert [item.text for item in lines] == ["北京2人", "颐和园"]
    assert lines[0].box == OcrBoundingBox(x_min=1, y_min=2, x_max=120, y_max=30)
    assert lines[0].requires_confirmation is False
    assert lines[1].requires_confirmation is True


def test_paddle_ocr_adapter_disables_unstable_windows_onednn(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakePipeline:
        def predict(self, image_path: str):
            captured["image_path"] = image_path
            return []

    def fake_paddle_ocr(**kwargs):
        captured.update(kwargs)
        return FakePipeline()

    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCR=fake_paddle_ocr))
    image = tmp_path / "controlled.png"
    image.write_bytes(PNG)

    result = PaddleOcrEngine()._predict(image)

    assert result == []
    assert captured["enable_mkldnn"] is False
    assert captured["image_path"] == str(image)
