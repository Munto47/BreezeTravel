from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import imports
from app.config import Settings


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
