from __future__ import annotations

from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_intakes as trip_intakes_api
from app.importing.screenshots import OcrBoundingBox, OcrTextLine
from app.trip_intake.materialization import InMemoryTripIntakeMaterializationRepository
from app.trip_intake.repository import InMemoryTripIntakeRepository
from app.utils.auth import get_current_user


class ControlledOcr:
    name = "controlled-ocr"
    version = "1"

    async def recognize(self, image_path):
        assert image_path.exists()
        return [
            OcrTextLine(
                text="2027年10月1日到10月7日去成都，12人",
                confidence=0.98,
                box=OcrBoundingBox(x_min=0, y_min=0, x_max=100, y_max=20),
            )
        ]


def _client(monkeypatch):
    intake_repository = InMemoryTripIntakeRepository()
    materialization_repository = InMemoryTripIntakeMaterializationRepository()
    app = FastAPI()
    app.include_router(trip_intakes_api.router, prefix="/api")
    app.dependency_overrides[trip_intakes_api.get_trip_intake_repository] = lambda: intake_repository
    app.dependency_overrides[
        trip_intakes_api.get_trip_intake_materialization_repository
    ] = lambda: materialization_repository
    app.dependency_overrides[trip_intakes_api.get_trip_intake_ocr_engine] = lambda: ControlledOcr()
    app.dependency_overrides[get_current_user] = lambda: "intake-user"
    monkeypatch.setattr(trip_intakes_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app)


def test_text_intake_confirm_materialize_and_recover(monkeypatch) -> None:
    client = _client(monkeypatch)
    created = client.post(
        "/api/rooms/intake-room/trip-intakes",
        json={
            "source_type": "MANUAL_TEXT",
            "raw_text": "2027年10月1日到10月7日去成都，12人。第六天：成都博物馆",
        },
    )
    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    intake_id = created.json()["intake_id"]
    assert created.json()["extraction"]["party_size"]["total"]["min"] == 12

    missing_headers = client.post(
        f"/api/trip-intakes/{intake_id}/revisions/1/confirm",
        headers={"Idempotency-Key": "confirm-1"},
    )
    assert missing_headers.status_code == 428

    confirmed = client.post(
        f"/api/trip-intakes/{intake_id}/revisions/1/confirm",
        headers={"If-Match": '"1"', "Idempotency-Key": "confirm-1"},
    )
    assert confirmed.status_code == 200
    assert confirmed.headers["etag"] == '"2"'
    assert confirmed.json()["status"] == "READY"

    materialized = client.post(
        f"/api/trip-intakes/{intake_id}/revisions/2/materialize",
        headers={"If-Match": '"2"', "Idempotency-Key": "materialize-1"},
    )
    replay = client.post(
        f"/api/trip-intakes/{intake_id}/revisions/2/materialize",
        headers={"If-Match": '"2"', "Idempotency-Key": "materialize-1"},
    )
    assert materialized.status_code == replay.status_code == 200
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert (
        replay.json()["materialization"]["materialization_id"]
        == materialized.json()["materialization"]["materialization_id"]
    )
    assert materialized.json()["materialization"]["brief"]["traveler_count"] == 12
    assert materialized.json()["materialization"]["workspace"]["city"] == "成都市"

    latest = client.get("/api/rooms/intake-room/trip-intakes/latest")
    assert latest.status_code == 200
    assert latest.json()["revision"] == 2


def test_screenshot_ocr_sources_remain_separate(monkeypatch) -> None:
    client = _client(monkeypatch)
    created = client.post(
        "/api/rooms/intake-room/trip-intakes/screenshots",
        json={"ocr_texts": ["2027年10月1日到10月7日去成都", "同行12人，玩7天😊"]},
    )
    assert created.status_code == 201
    payload = created.json()
    assert payload["source_type"] == "SCREENSHOT_OCR"
    assert len(payload["sources"]) == 2
    party_span = payload["extraction"]["party_size"]["total"]["evidence"][0]
    assert party_span["source_id"] == payload["sources"][1]["source_id"]


def test_user_correction_becomes_a_new_evidence_source_before_confirmation(monkeypatch) -> None:
    client = _client(monkeypatch)
    created = client.post(
        "/api/rooms/intake-room/trip-intakes",
        json={"source_type": "MANUAL_TEXT", "raw_text": "想出去玩，目的地和人数还没定"},
    ).json()
    intake_id = created["intake_id"]
    patched = client.patch(
        f"/api/trip-intakes/{intake_id}/revisions/1",
        headers={"If-Match": '"1"', "Idempotency-Key": "correct-core-fields"},
        json={
            "confirmed_values": {
                "city": "成都",
                "start_date": "2027-10-01",
                "end_date": "2027-10-12",
                "party_size": 1,
            }
        },
    )
    assert patched.status_code == 200, patched.text
    payload = patched.json()
    assert payload["revision"] == 2
    assert len(payload["sources"]) == 2
    assert payload["extraction"]["locations"]["status"] == "EXACT"
    assert payload["extraction"]["party_size"]["total"]["min"] == 1
    source_id = payload["sources"][1]["source_id"]
    assert payload["extraction"]["temporal"]["date_range"]["evidence"][0]["source_id"] == source_id
    assert payload["extraction"]["temporal"]["days"]["quantifier"] == "EXACT"
    assert payload["extraction"]["temporal"]["days"]["min"] == 12
    assert payload["extraction"]["temporal"]["days"]["max"] == 12
    assert all(
        issue["field_path"] != "temporal.days"
        for issue in payload["extraction"]["issues"]
    )

    confirmed = client.post(
        f"/api/trip-intakes/{intake_id}/revisions/2/confirm",
        headers={"If-Match": '"2"', "Idempotency-Key": "confirm-corrected"},
    )
    assert confirmed.status_code == 200, confirmed.text


def test_incomplete_intake_confirmation_returns_domain_message(monkeypatch) -> None:
    client = _client(monkeypatch)
    created = client.post(
        "/api/rooms/intake-room/trip-intakes",
        json={"source_type": "MANUAL_TEXT", "raw_text": "想去北京看看，日期和人数还没定"},
    ).json()

    response = client.post(
        f"/api/trip-intakes/{created['intake_id']}/revisions/1/confirm",
        headers={"If-Match": '"1"', "Idempotency-Key": "confirm-incomplete"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "TRIP_INTAKE_NOT_READY",
        "message": "行程草稿尚未满足确认条件，请先补全并保存目的城市、完整日期和正整数人数",
    }
    assert "pydantic.dev" not in response.text


def test_raw_screenshot_is_ocr_processed_then_only_receipt_metadata_is_persisted(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.post(
        "/api/rooms/intake-room/trip-intakes/screenshots",
        files={"screenshots": ("trip.png", b"\x89PNG\r\n\x1a\ncontrolled", "image/png")},
    )
    assert response.status_code == 201, response.text
    source = response.json()["sources"][0]
    assert source["text"].endswith("12人")
    assert source["metadata"]["ocr_engine"] == "controlled-ocr"
    assert source["metadata"]["raw_asset_retained"] is False
    assert "content" not in source["metadata"]
