from __future__ import annotations

import hashlib
import hmac

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.auth import get_wechat_auth_service
from app.authentication.wechat import (
    HttpxWechatSessionProvider,
    InMemoryWechatIdentityRepository,
    WechatAuthError,
    WechatAuthService,
    WechatIdentity,
    WechatSession,
)
from app.main import app


class StubProvider:
    def __init__(self, openid: str = "openid-not-stored") -> None:
        self.openid = openid
        self.codes: list[str] = []

    async def exchange(self, code: str) -> WechatSession:
        self.codes.append(code)
        return WechatSession(openid=self.openid)


@pytest.mark.asyncio
async def test_wechat_identity_is_stable_and_only_hmac_is_retained():
    provider = StubProvider()
    repository = InMemoryWechatIdentityRepository()
    service = WechatAuthService(
        app_id="test-app",
        identity_hash_key="separate-hmac-key",
        provider=provider,
        repository=repository,
    )

    created = await service.login(code="first-code", nickname="  小风  ")
    repeated = await service.login(code="second-code", nickname="ignored")

    assert created.user_id == repeated.user_id
    assert created.nickname == repeated.nickname == "小风"
    assert created.is_new_user is True
    assert repeated.is_new_user is False
    expected = hmac.new(b"separate-hmac-key", b"openid-not-stored", hashlib.sha256).hexdigest()
    assert list(repository.identities) == [("test-app", expected)]
    assert "openid-not-stored" not in repr(repository.identities)


@pytest.mark.asyncio
async def test_wechat_auth_fails_closed_without_identity_configuration():
    service = WechatAuthService(
        app_id="",
        identity_hash_key="",
        provider=StubProvider(),
        repository=InMemoryWechatIdentityRepository(),
    )
    with pytest.raises(WechatAuthError) as caught:
        await service.login(code="unused")
    assert caught.value.code == "WECHAT_AUTH_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_wechat_provider_maps_invalid_code_without_leaking_provider_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["js_code"] == "expired-code"
        return httpx.Response(200, json={"errcode": 40029, "errmsg": "invalid code with private detail"})

    provider = HttpxWechatSessionProvider(
        app_id="test-app",
        app_secret="test-secret",
        endpoint="https://wechat.invalid/code2session",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(WechatAuthError) as caught:
        await provider.exchange("expired-code")
    assert caught.value.code == "WECHAT_LOGIN_CODE_INVALID"
    assert "private detail" not in caught.value.message
    assert "test-secret" not in caught.value.message


@pytest.mark.asyncio
async def test_wechat_provider_maps_transport_failure_without_retry():
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError("offline", request=request)

    provider = HttpxWechatSessionProvider(
        app_id="test-app",
        app_secret="test-secret",
        endpoint="https://wechat.invalid/code2session",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(WechatAuthError) as caught:
        await provider.exchange("one-shot-code")
    assert caught.value.code == "WECHAT_AUTH_PROVIDER_UNAVAILABLE"
    assert calls == 1


def test_wechat_login_endpoint_returns_existing_jwt_shape():
    class StubService:
        async def login(self, *, code: str, nickname: str | None = None) -> WechatIdentity:
            assert code == "fresh-code"
            assert nickname == "小风"
            return WechatIdentity(user_id="wechat-user", nickname="小风", is_new_user=True)

    app.dependency_overrides[get_wechat_auth_service] = lambda: StubService()
    try:
        response = TestClient(app).post(
            "/api/auth/wechat/login",
            json={"code": "fresh-code", "nickname": "小风"},
        )
    finally:
        app.dependency_overrides.pop(get_wechat_auth_service, None)
    assert response.status_code == 200
    assert response.json()["user_id"] == "wechat-user"
    assert response.json()["is_new_user"] is True
    assert isinstance(response.json()["token"], str)
