from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import imports
from app.config import Settings


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def test_public_runtime_rejects_default_missing_or_reused_private_secrets() -> None:
    with pytest.raises(ValidationError, match="explicit 32-character"):
        Settings(_env_file=None, runtime_profile="public")

    shared = "s" * 32
    with pytest.raises(ValidationError, match="must be independent"):
        Settings(
            _env_file=None,
            runtime_profile="public",
            jwt_secret_key=shared,
            trip_understanding_cookie_signing_key=shared,
            trip_understanding_source_encryption_key="e" * 32,
        )

    settings = Settings(
        _env_file=None,
        runtime_profile="public",
        jwt_secret_key="j" * 32,
        trip_understanding_cookie_signing_key="c" * 32,
        trip_understanding_source_encryption_key="e" * 32,
    )
    assert settings.runtime_profile == "public"


def test_public_legacy_import_api_is_hidden_unless_diagnostics_are_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "diagnostic-key-" + "k" * 32
    monkeypatch.setattr(
        imports,
        "get_settings",
        lambda: SimpleNamespace(
            runtime_profile="public",
            legacy_import_diagnostics_enabled=False,
            legacy_import_diagnostics_key=key,
        ),
    )
    with pytest.raises(HTTPException) as hidden:
        imports.require_legacy_import_diagnostic_access(key)
    assert hidden.value.status_code == 404
    assert key not in str(hidden.value.detail)

    monkeypatch.setattr(
        imports,
        "get_settings",
        lambda: SimpleNamespace(
            runtime_profile="public",
            legacy_import_diagnostics_enabled=True,
            legacy_import_diagnostics_key=key,
        ),
    )
    with pytest.raises(HTTPException) as wrong_key:
        imports.require_legacy_import_diagnostic_access("wrong")
    assert wrong_key.value.status_code == 404
    assert imports.require_legacy_import_diagnostic_access(key) is None


def test_non_public_legacy_import_api_remains_available_for_local_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        imports,
        "get_settings",
        lambda: SimpleNamespace(
            runtime_profile="local_fixture",
            legacy_import_diagnostics_enabled=False,
            legacy_import_diagnostics_key="",
        ),
    )
    assert imports.require_legacy_import_diagnostic_access(None) is None


def test_public_runtime_mounts_only_current_user_facing_api() -> None:
    script = """
import json
import asyncio
from app.main import app
schema = app.openapi()
print(json.dumps({
    'paths': sorted(schema.get('paths', {})),
    'schemas': sorted(schema.get('components', {}).get('schemas', {})),
    'info': schema.get('info', {}),
    'health': asyncio.run(__import__('app.main', fromlist=['health_check']).health_check()),
}))
"""
    environment = os.environ.copy()
    environment.update(
        {
            "RUNTIME_PROFILE": "public",
            "JWT_SECRET_KEY": "j" * 32,
            "TRIP_UNDERSTANDING_COOKIE_SIGNING_KEY": "c" * 32,
            "TRIP_UNDERSTANDING_SOURCE_ENCRYPTION_KEY": "e" * 32,
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    paths = set(payload["paths"])
    assert not any(
        path.startswith(prefix)
        for path in paths
        for prefix in (
            "/api/chat",
            "/api/share/",
            "/api/trip-workspaces",
            "/api/audits",
            "/api/trip-intakes",
            "/api/trip-check-runs",
            "/api/user/rooms",
            "/api/user/itineraries",
            "/api/auth/test-login",
            "/metrics",
        )
    )
    assert {
        "WorkspaceSnapshot",
        "ItineraryRevision",
        "EvidenceSnapshot",
        "RepairOption",
        "SharedWorkspaceView",
    }.isdisjoint(payload["schemas"])
    assert "/api/v3/trip-understandings" in paths
    assert "/api/v3/me/travel-data" in paths
    assert payload["health"] == {"status": "ok"}
    public_schema = json.dumps(payload, ensure_ascii=False)
    assert not any(
        token in public_schema
        for token in ("LangGraph", "ReAct", "Critic", "Advanced RAG", "MCP Server")
    )


def test_test_profile_keeps_legacy_routes_for_compatibility_regression() -> None:
    from app.main import app

    paths = app.openapi()["paths"]
    assert "/api/chat" in paths
    assert "/api/trip-workspaces/{workspace_id}/snapshot" in paths
    assert "/api/audits/{audit_id}/evidence" in paths
    assert "/api/auth/test-login" in paths
    assert "/metrics" in paths
