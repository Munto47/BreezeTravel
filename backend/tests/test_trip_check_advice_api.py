from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_check_advice as advice_api
from app.itineraries.models import TripDateRange, TripWorkspace
from app.itineraries.repositories import InMemoryItineraryRepository
from app.trip_check.advice import InMemoryAdviceRepository
from app.trip_check.models import AdviceAction, AdviceBundle
from app.utils.auth import get_current_user


def test_advice_read_is_workspace_scoped_and_returns_persisted_bundle(monkeypatch):
    itinerary_repository = InMemoryItineraryRepository()
    advice_repository = InMemoryAdviceRepository()
    workspace = TripWorkspace(
        workspace_id="advice-workspace",
        room_id="advice-room",
        city="北京",
        trip_date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
        created_by="advice-user",
    )
    bundle = AdviceBundle(
        advice_bundle_id="advice-bundle",
        workspace_id=workspace.workspace_id,
        run_id="advice-run",
        report_id="advice-report",
        itinerary_revision=1,
        brief_revision=1,
        evidence_snapshot_id="advice-snapshot",
        actions=[
            AdviceAction(
                advice_id="advice-action",
                finding_id="finding-1",
                action="将景山公园顺延到故宫博物院结束后",
                expected_impact="消除时间重叠",
                uncertainty="受现场排队时间影响，仍需预留缓冲",
                evidence_fact_ids=["fact-1"],
            )
        ],
        created_at=datetime(2026, 8, 23, tzinfo=timezone.utc),
    )

    async def setup():
        await itinerary_repository.create_workspace(workspace)
        await advice_repository.save_bundle(bundle, brief_id="brief-1")

    asyncio.run(setup())
    app = FastAPI()
    app.include_router(advice_api.router, prefix="/api")
    app.dependency_overrides[advice_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[advice_api.get_advice_repository] = lambda: advice_repository
    app.dependency_overrides[get_current_user] = lambda: "advice-user"
    access_check = AsyncMock(return_value=None)
    monkeypatch.setattr(advice_api, "require_room_member", access_check)

    with TestClient(app) as client:
        response = client.get(
            f"/api/trip-workspaces/{workspace.workspace_id}/reports/{bundle.report_id}/advice"
        )
        missing = client.get(
            f"/api/trip-workspaces/{workspace.workspace_id}/reports/missing-report/advice"
        )

    assert response.status_code == 200
    assert response.json() == bundle.model_dump(mode="json")
    assert missing.status_code == 404
    access_check.assert_awaited()
