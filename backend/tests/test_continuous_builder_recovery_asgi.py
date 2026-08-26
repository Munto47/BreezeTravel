from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import audits as audits_api
from app.api import trip_workspaces as workspaces_api
from app.audit.repositories import InMemoryAuditRepository
from app.itineraries.repositories import InMemoryItineraryRepository
from app.members.repositories import InMemoryMemberConstraintRepository
from app.operations.repositories import InMemoryCreationCommandRepository
from app.suggestions.repositories import InMemorySuggestionRepository
from app.utils.auth import get_current_user
from evals.continuous.http_builder import (
    _run_concurrent_edit_contract,
    _run_drag_button_equivalence,
)
from evals.continuous.http_import import HttpResponse, _Recorder


DATASET = Path(__file__).resolve().parents[1] / "eval_data" / "dual_entry_v1" / "regression.inputs.jsonl"


class _AsgiTransport:
    def __init__(self, client: TestClient):
        self.client = client

    def request(self, method, url, *, headers, json_body, timeout_seconds):
        del timeout_seconds
        response = self.client.request(method, url, headers=dict(headers), json=json_body)
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = response.text
        return HttpResponse(response.status_code, dict(response.headers), body)


def _case() -> dict:
    return next(
        json.loads(line)
        for line in DATASET.read_text(encoding="utf-8").splitlines()
        if '"case_id":"reg.bj.builder.drag-button-equivalence-recovery"' in line
    )


def _recorder(monkeypatch) -> _Recorder:
    itineraries = InMemoryItineraryRepository()
    audits = InMemoryAuditRepository(itineraries.workspaces)
    members = InMemoryMemberConstraintRepository(itineraries)
    commands = InMemoryCreationCommandRepository()
    suggestions = InMemorySuggestionRepository(itineraries)
    app = FastAPI()

    @app.post("/api/room")
    def create_room():
        # Room creation is still a public HTTP boundary in this isolated ASGI
        # product test; only its unrelated persistence implementation is
        # replaced because the recovery contract never reads room state.
        return {"status": "ok"}

    app.include_router(workspaces_api.router, prefix="/api")
    app.include_router(audits_api.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: "recovery-asgi-user"
    app.dependency_overrides[workspaces_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[workspaces_api.get_audit_repository] = lambda: audits
    app.dependency_overrides[workspaces_api.get_member_constraint_repository] = lambda: members
    app.dependency_overrides[workspaces_api.get_suggestion_repository] = lambda: suggestions
    app.dependency_overrides[audits_api.get_itinerary_repository] = lambda: itineraries
    app.dependency_overrides[audits_api.get_audit_repository] = lambda: audits
    app.dependency_overrides[audits_api.get_member_constraint_repository] = lambda: members
    app.dependency_overrides[audits_api.get_creation_command_repository] = lambda: commands
    monkeypatch.setattr(workspaces_api, "require_room_member", AsyncMock(return_value=None))
    monkeypatch.setattr(audits_api, "require_room_member", AsyncMock(return_value=None))
    return _Recorder(_AsgiTransport(TestClient(app)), "http://testserver", 3)


def test_recovery_contracts_execute_only_public_product_http(monkeypatch):
    recorder = _recorder(monkeypatch)
    case = _case()

    equivalence = _run_drag_button_equivalence(
        recorder,
        case=case,
        bearer_token="test-token",
        provider_anchor=None,
        run_namespace="asgi-recovery-contract",
    )
    concurrency = _run_concurrent_edit_contract(
        recorder,
        case=case,
        bearer_token="test-token",
        provider_anchor=None,
        run_namespace="asgi-recovery-contract",
    )

    assert equivalence["status"] == "PASS"
    assert equivalence["outputs_equivalent"] is True
    assert equivalence["normalized_revision_semantic_hash_equal"] is True
    assert equivalence["raw_revision_content_hash_equal"] is False
    assert equivalence["failure_rollback_equivalent"] is True
    assert equivalence["incremental_full_audit_semantic_parity"] is True
    assert concurrency["status"] == "PASS"
    assert sorted(item["status_code"] for item in concurrency["client_statuses"].values()) == [200, 409]
    assert concurrency["loser_explicit_reload"] is True
    assert recorder.transactions
    request_indexes = [item["request_index"] for item in recorder.transactions]
    assert len(request_indexes) == len(set(request_indexes))
    assert all(item["path"].startswith("/api/") for item in recorder.transactions)
    assert not any("sql" in item["step"].casefold() for item in recorder.transactions)
    assert not any("domain" in item["step"].casefold() for item in recorder.transactions)
