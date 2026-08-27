from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import trip_check_runs as runs_api
from app.itineraries.hash_service import with_content_hash
from app.itineraries.models import (
    ItineraryDay,
    ItineraryRevisionContent,
    RevisionSource,
    TripDateRange,
    TripWorkspace,
)
from app.itineraries.repositories import InMemoryItineraryRepository
from app.trip_check.briefs import InMemoryTripBriefRepository, TripBriefApplicationService, TripBriefParser
from app.trip_check.models import RunBudget, RunSpec, TripBriefStatus, TripCheckRunStatus
from app.trip_check.runs import InMemoryTripCheckRunRepository
from app.importing.models import ImportSourceType, ImportStatus, ItineraryImport
from app.utils.auth import get_current_user


def _run_spec(**updates):
    payload = {
        "commit_sha": "e7597ca",
        "prompt_version": "none-p1",
        "model_version": "none-p1",
        "provider_version": "fixture-v1",
        "rule_set_version": "audit-v1",
        "execution_mode": "fixture",
        "dataset_hash": "a" * 64,
        "snapshot_hash": "b" * 64,
        "fault_profile": "none",
        "random_seed": 7,
        "budget": RunBudget(timeout_seconds=30).model_dump(mode="json"),
    }
    payload.update(updates)
    return RunSpec.model_validate(payload)


def _client(monkeypatch):
    itinerary_repository = InMemoryItineraryRepository()
    workspace = TripWorkspace(
        workspace_id="run-workspace",
        room_id="run-room",
        city="北京",
        trip_date_range=TripDateRange(start=date(2026, 10, 1), end=date(2026, 10, 2)),
        created_by="run-user",
    )
    asyncio.run(itinerary_repository.create_workspace(workspace))
    revision = with_content_hash(
        ItineraryRevisionContent(
            itinerary_id="itinerary-1",
            workspace_id=workspace.workspace_id,
            revision=1,
            source_type=RevisionSource.IMPORT,
            city=workspace.city,
            date_range=workspace.trip_date_range,
            days=[ItineraryDay(day_index=0), ItineraryDay(day_index=1)],
            created_by="run-user",
        )
    )
    asyncio.run(itinerary_repository.attach_initial_revision(workspace.workspace_id, revision))

    brief_repository = InMemoryTripBriefRepository()
    imported = ItineraryImport(
        import_id="import-1",
        workspace_id=workspace.workspace_id,
        source_type=ImportSourceType.MANUAL_TEXT,
        raw_text="北京2人，第1天故宫，第2天颐和园",
        parse_version="test",
        status=ImportStatus.READY,
        created_by="run-user",
    )
    draft = TripBriefParser().parse(workspace=workspace, itinerary_import=imported, actor_user_id="run-user")
    asyncio.run(brief_repository.save_import_brief(draft))
    confirmed, _ = asyncio.run(
        TripBriefApplicationService(brief_repository).confirm(
            workspace_id=workspace.workspace_id,
            revision=1,
            actor_user_id="run-user",
            idempotency_key="confirm-brief",
        )
    )
    assert confirmed.status == TripBriefStatus.CONFIRMED

    run_repository = InMemoryTripCheckRunRepository()
    app = FastAPI()
    app.include_router(runs_api.router, prefix="/api")
    app.dependency_overrides[runs_api.get_itinerary_repository] = lambda: itinerary_repository
    app.dependency_overrides[runs_api.get_trip_brief_repository] = lambda: brief_repository
    app.dependency_overrides[runs_api.get_trip_check_run_repository] = lambda: run_repository
    app.dependency_overrides[get_current_user] = lambda: "run-user"
    monkeypatch.setattr(runs_api, "require_room_member", AsyncMock(return_value=None))
    return TestClient(app), run_repository


def test_run_create_read_events_resume_and_idempotency(monkeypatch):
    client, _ = _client(monkeypatch)
    scheduler = Mock()
    monkeypatch.setattr(runs_api, "_schedule_execution", scheduler)
    body = {
        "itinerary_revision": 1,
        "brief_revision": 2,
        "run_spec": _run_spec().model_dump(mode="json"),
    }
    missing = client.post("/api/trip-workspaces/run-workspace/trip-check-runs", json=body)
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"

    headers = {"Idempotency-Key": "create-run"}
    created = client.post("/api/trip-workspaces/run-workspace/trip-check-runs", headers=headers, json=body)
    replay = client.post("/api/trip-workspaces/run-workspace/trip-check-runs", headers=headers, json=body)
    assert created.status_code == replay.status_code == 201
    assert created.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    scheduler.assert_called_once()
    run = created.json()
    assert run["stage"] == "COLLECT_EVIDENCE"
    assert run["completed_stages"] == ["PARSE", "WAIT_BRIEF_CONFIRMATION", "RESOLVE_PLACES"]
    assert run["status"] == "WAITING"

    changed_body = {
        **body,
        "run_spec": _run_spec(provider_version="fixture-v2").model_dump(mode="json"),
    }
    reused = client.post(
        "/api/trip-workspaces/run-workspace/trip-check-runs",
        headers=headers,
        json=changed_body,
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "IDEMPOTENCY_KEY_REUSED"

    read = client.get(f"/api/trip-check-runs/{run['run_id']}")
    assert read.status_code == 200
    assert read.headers["etag"] == '"1"'

    events = client.get(f"/api/trip-check-runs/{run['run_id']}/events")
    assert events.status_code == 200
    assert "event: run_created" in events.text
    assert "id: 1" in events.text
    empty_reconnect = client.get(
        f"/api/trip-check-runs/{run['run_id']}/events",
        headers={"Last-Event-ID": "1"},
    )
    assert empty_reconnect.text == ""

    mismatch = client.post(
        f"/api/trip-check-runs/{run['run_id']}/resume",
        headers={"If-Match": '"1"', "Idempotency-Key": "resume-mismatch"},
        json={"config_hash": "f" * 64},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "RUN_CONFIG_MISMATCH"

    missing_if_match = client.post(
        f"/api/trip-check-runs/{run['run_id']}/resume",
        headers={"Idempotency-Key": "resume-without-if-match"},
        json={"config_hash": run["config_hash"]},
    )
    assert missing_if_match.status_code == 428
    assert missing_if_match.json()["detail"]["code"] == "IF_MATCH_REQUIRED"

    resume_headers = {"If-Match": '"1"', "Idempotency-Key": "resume-run"}
    resumed = client.post(
        f"/api/trip-check-runs/{run['run_id']}/resume",
        headers=resume_headers,
        json={"config_hash": run["config_hash"]},
    )
    resumed_replay = client.post(
        f"/api/trip-check-runs/{run['run_id']}/resume",
        headers=resume_headers,
        json={"config_hash": run["config_hash"]},
    )
    assert resumed.status_code == resumed_replay.status_code == 200
    assert resumed.json()["status"] == "RUNNING"
    assert resumed.json()["version"] == 2
    assert resumed.json()["lease_owner"].startswith("worker:")
    assert resumed_replay.json() == resumed.json()
    assert resumed_replay.headers["Idempotency-Replayed"] == "true"

    stale = client.post(
        f"/api/trip-check-runs/{run['run_id']}/resume",
        headers={"If-Match": '"1"', "Idempotency-Key": "resume-stale-version"},
        json={"config_hash": run["config_hash"]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "TRIP_CHECK_RUN_CONFLICT"


def test_run_rejects_unconfirmed_brief_and_active_lease_resume(monkeypatch):
    client, repository = _client(monkeypatch)
    body = {
        "itinerary_revision": 1,
        "brief_revision": 1,
        "run_spec": _run_spec().model_dump(mode="json"),
    }
    unconfirmed = client.post(
        "/api/trip-workspaces/run-workspace/trip-check-runs",
        headers={"Idempotency-Key": "unconfirmed"},
        json=body,
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["detail"]["code"] == "TRIP_BRIEF_CONFIRMATION_REQUIRED"

    body["brief_revision"] = 2
    created = client.post(
        "/api/trip-workspaces/run-workspace/trip-check-runs",
        headers={"Idempotency-Key": "create"},
        json=body,
    ).json()
    repository.runs[created["run_id"]] = repository.runs[created["run_id"]].model_copy(
        update={
            "status": TripCheckRunStatus.RUNNING,
            "lease_owner": "worker:active",
            "lease_until": datetime.now(timezone.utc).replace(year=2099),
        }
    )
    active = client.post(
        f"/api/trip-check-runs/{created['run_id']}/resume",
        headers={"If-Match": '"1"', "Idempotency-Key": "active-resume"},
        json={"config_hash": created["config_hash"]},
    )
    assert active.status_code == 409
    assert active.json()["detail"]["code"] == "RUN_NOT_RESUMABLE"
