from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import e2e


class _Acquire:
    def __init__(self, connection):
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, *_args):
        return None


class _Pool:
    def __init__(self):
        self.connection = SimpleNamespace(execute=AsyncMock())

    def acquire(self):
        return _Acquire(self.connection)


def _request(host: str):
    return SimpleNamespace(client=SimpleNamespace(host=host))


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    monkeypatch.setattr(e2e, "_cleanup_secret_consumed", False)
    monkeypatch.setattr(e2e.settings, "runtime_profile", "local_fixture")
    monkeypatch.setattr(e2e.settings, "e2e_restart_gate_mode", True)
    monkeypatch.setattr(e2e.settings, "e2e_cleanup_secret", "a" * 32)


@pytest.mark.asyncio
async def test_cleanup_is_single_use_and_uses_parameterized_repository_boundary(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(e2e, "get_pool", AsyncMock(return_value=pool))
    body = e2e.CleanupRequest(
        room_id="e2e-dual-restart-room-1-abcdef12",
        emails=["e2e+dual-a-1-abcdef12@example.com"],
    )

    assert await e2e.cleanup(body, _request("127.0.0.1"), "a" * 32) == {
        "ok": True,
        "room_count": 1,
        "email_count": 1,
    }
    assert pool.connection.execute.await_count == 2
    with pytest.raises(HTTPException) as replay:
        await e2e.cleanup(body, _request("127.0.0.1"), "a" * 32)
    assert replay.value.status_code == 404


@pytest.mark.asyncio
async def test_cleanup_batches_nine_isolated_rooms_in_one_single_use_call(monkeypatch):
    pool = _Pool()
    monkeypatch.setattr(e2e, "get_pool", AsyncMock(return_value=pool))
    room_ids = [f"e2e-g5-restart-{index}-abcdef12" for index in range(9)]
    body = e2e.CleanupRequest(
        room_ids=room_ids,
        emails=["e2e+g5-a-abcdef12@example.com", "e2e+g5-b-abcdef12@example.com"],
    )

    result = await e2e.cleanup(body, _request("::1"), "a" * 32)

    assert result == {"ok": True, "room_count": 9, "email_count": 2}
    first_call = pool.connection.execute.await_args_list[0]
    assert first_call.args[0] == "DELETE FROM rooms WHERE room_id = ANY($1::text[])"
    assert first_call.args[1] == room_ids


def test_cleanup_request_rejects_duplicate_or_missing_room_targets():
    with pytest.raises(ValueError):
        e2e.CleanupRequest()
    with pytest.raises(ValueError):
        e2e.CleanupRequest(room_id="e2e-one", room_ids=["e2e-one"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "profile", "host", "secret"),
    [
        (False, "local_fixture", "127.0.0.1", "a" * 32),
        (True, "public", "127.0.0.1", "a" * 32),
        (True, "local_fixture", "172.20.0.1", "a" * 32),
        (True, "local_fixture", "127.0.0.1", "b" * 32),
        (True, "local_fixture", "127.0.0.1", "short"),
    ],
)
async def test_cleanup_is_hidden_outside_explicit_loopback_restart_gate(
    monkeypatch, mode, profile, host, secret
):
    monkeypatch.setattr(e2e.settings, "e2e_restart_gate_mode", mode)
    monkeypatch.setattr(e2e.settings, "runtime_profile", profile)
    monkeypatch.setattr(e2e, "get_pool", AsyncMock())
    body = e2e.CleanupRequest(room_id="e2e-dual-restart-room-1-abcdef12")

    with pytest.raises(HTTPException) as rejected:
        await e2e.cleanup(body, _request(host), secret)
    assert rejected.value.status_code == 404
    e2e.get_pool.assert_not_awaited()


def test_loopback_parser_rejects_hostnames_and_non_loopback_addresses():
    assert e2e._is_loopback(_request("127.0.0.1"))
    assert e2e._is_loopback(_request("::1"))
    assert not e2e._is_loopback(_request("localhost"))
    assert not e2e._is_loopback(_request("203.0.113.7"))
