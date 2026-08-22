"""Offline checks for planner weather Provider authentication and host routing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agents.planner.nodes import weather_fetcher


class _Response:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return {"code": "200", "daily": [{"fxDate": "2026-08-20"}]}


class _Session:
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _Response()


def _jwt_settings():
    return SimpleNamespace(
        qweather_auth_type="jwt",
        qweather_api_key="",
        qweather_api_host="tenant.re.qweatherapi.com",
        qweather_private_key="configured-private-key",
        qweather_key_id="configured-key-id",
        qweather_project_id="configured-project-id",
    )


def test_jwt_credentials_are_accepted_without_legacy_api_key():
    with patch.object(weather_fetcher, "settings", _jwt_settings()):
        assert weather_fetcher._has_qweather_credentials() is True


def test_incomplete_jwt_credentials_are_rejected():
    config = _jwt_settings()
    config.qweather_project_id = ""
    with patch.object(weather_fetcher, "settings", config):
        assert weather_fetcher._has_qweather_credentials() is False


def test_fetch_uses_custom_host_and_authorization_header_without_key_query_param():
    session = _Session()
    build_headers = AsyncMock(return_value={"Authorization": "Bearer redacted-test-token"})
    with (
        patch.object(weather_fetcher, "settings", _jwt_settings()),
        patch.object(weather_fetcher, "_build_weather_headers", build_headers),
    ):
        daily = asyncio.run(weather_fetcher._fetch_qweather_7d(session, 39.918058, 116.397029))

    assert daily == [{"fxDate": "2026-08-20"}]
    assert session.calls[0][0] == "https://tenant.re.qweatherapi.com/v7/weather/7d"
    assert session.calls[0][1]["headers"] == {"Authorization": "Bearer redacted-test-token"}
    assert "key" not in session.calls[0][1]["params"]
