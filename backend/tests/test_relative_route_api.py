"""Relative route HTTP contract through the real router and in-memory repository.

The source/provider/pipeline/map helper and AMap adapter use fixed transports.
No service is started and no live model/place/route request is made.
"""
import json
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from app.api import trip_understandings_v3 as api
from app.trip_understanding.amap_route import AmapRouteProvider
from app.utils.auth import get_optional_user
from tests.test_relative_route_flow import NAMES, SOURCE, create_trip, snapshot
from tests.test_relative_route_options import FixedAmap
from tests.test_trip_understanding_v3_api import _client


@pytest_asyncio.fixture
async def trip_api():
    unused_client, repo, app = _client()
    unused_client.close()
    actor = {"user": "experience-owner"}
    app.dependency_overrides[get_optional_user] = lambda: actor["user"]
    resource, stored = await create_trip(repo, key="relative-route-http")
    fixture = FixedAmap()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fixture.respond)) as route_http:
        provider = AmapRouteProvider(api_key="fixed-test-not-a-real-key", client=route_http)
        app.dependency_overrides[api.get_relative_route_provider] = lambda: provider
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
            yield SimpleNamespace(http=http, app=app, repo=repo, resource=resource, stored=stored, actor=actor,
                calls=fixture.calls, base=f"/api/v3/trip-understandings/{resource.public_resource_id}",
                headers={"If-Match": f'"{stored.opaque_etag}"', "Idempotency-Key": "route-preview"})


def public_only(response):
    text = json.dumps(response.json(), ensure_ascii=False)
    assert SOURCE not in text
    assert all(word not in text for word in ("source_order", "source_quote", "scope_quote", "scope_start",
        "span_start", "span_end", "hard_precedence", "member_visit_ids", "order_assessment"))


async def current(t):
    response = await t.http.get(t.base + "/result")
    assert response.status_code == 200, response.text
    public_only(response)
    return response


async def preview(t):
    response = await t.http.post(t.base + "/changes/preview", json={"day_index": 1}, headers=t.headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "AVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"] == t.headers["If-Match"]
    public_only(response)
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize("missing,expected,code", [
    ("If-Match", 428, "IF_MATCH_REQUIRED"),
    ("Idempotency-Key", 400, "IDEMPOTENCY_KEY_REQUIRED"),
])
async def test_day_preview_requires_both_preconditions_without_query_or_write(trip_api, missing, expected, code):
    t = trip_api
    before = await current(t)
    headers = {key: value for key, value in t.headers.items() if key != missing}
    response = await t.http.post(t.base + "/changes/preview", json={"day_index": 1}, headers=headers)
    assert response.status_code == expected and response.json()["detail"]["code"] == code
    after = await current(t)
    assert after.headers["etag"] == before.headers["etag"] and after.json() == before.json()
    assert not t.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"day_index": 1, "check_token": "legacy-check-token-0000000000"},
    {"day_index": True}, {"day_index": 0}, {"day_index": 15}])
async def test_preview_requires_one_valid_typed_target(trip_api, payload):
    t = trip_api
    before = await snapshot(t.repo, t.resource, "memory")
    response = await t.http.post(t.base + "/changes/preview", json=payload, headers=t.headers)
    assert response.status_code == 422
    assert (await snapshot(t.repo, t.resource, "memory")).revision == before.revision
    assert not t.calls


@pytest.mark.asyncio
async def test_preview_rejects_old_etag_and_reusing_a_key_for_another_day(trip_api):
    t = trip_api
    before = await current(t)
    stale = await t.http.post(t.base + "/changes/preview", json={"day_index": 1},
        headers={"If-Match": '"tu3_not-the-current-result"', "Idempotency-Key": "old-preview-version"})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "CHECK_CHANGED"
    assert not t.calls
    await preview(t)
    changed = await t.http.post(t.base + "/changes/preview", json={"day_index": 2}, headers=t.headers)
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "REQUEST_CHANGED"
    assert len(t.calls) == 6 and (await current(t)).json() == before.json()


@pytest.mark.asyncio
async def test_preview_and_adopt_require_resource_owner_even_with_a_valid_credential(trip_api):
    t = trip_api
    offered = await preview(t)
    token = offered.json()["options"][0]["change_token"]
    before = await snapshot(t.repo, t.resource, "memory")
    calls = list(t.calls)
    for actor in (None, "another-account"):
        t.actor["user"] = actor
        for endpoint, payload in (("preview", {"day_index": 1}), ("adopt", {"change_token": token})):
            response = await t.http.post(t.base + "/changes/" + endpoint, json=payload,
                headers={**t.headers, "Idempotency-Key": f"unauthorized-{actor}-{endpoint}"})
            assert response.status_code == 404 and response.json()["detail"]["code"] == "RESOURCE_NOT_FOUND"
            public_only(response)
    t.actor["user"] = "experience-owner"
    assert (await snapshot(t.repo, t.resource, "memory")).revision == before.revision
    assert t.calls == calls


@pytest.mark.asyncio
async def test_compare_adopt_and_new_get_agree_and_duplicate_requests_do_not_repeat_effects(trip_api):
    t = trip_api
    before = await current(t)
    offered = await preview(t)
    option, = offered.json()["options"]
    assert option["before"] == list(NAMES[:4])
    assert option["after"] == [NAMES[i] for i in (0, 2, 1, 3)]
    assert option["minutes_saved"] == 45 and len(t.calls) == 6
    repeated = await preview(t)
    assert repeated.json() == offered.json() and repeated.headers["Idempotency-Replayed"] == "true"
    assert len(t.calls) == 6 and (await current(t)).json() == before.json()
    headers = {**t.headers, "Idempotency-Key": "adopt-route-once"}
    applied = await t.http.post(t.base + "/changes/adopt", json={"change_token": option["change_token"]}, headers=headers)
    assert applied.status_code == 200 and applied.json()["status"] == "APPLIED", applied.text
    public_only(applied)
    readback = await current(t)
    assert readback.headers["etag"] == applied.headers["etag"] != before.headers["etag"]
    assert [[c["name"] for c in day["activities"]] for day in readback.json()["days"]] == [option["after"], list(NAMES[4:])]
    assert readback.json()["map"]["status"] == "NEEDS_UPDATE"
    assert readback.json()["can_undo"] is True and readback.json()["can_redo"] is False
    state = await snapshot(t.repo, t.resource, "memory")
    duplicate = await t.http.post(t.base + "/changes/adopt", json={"change_token": option["change_token"]}, headers=headers)
    assert duplicate.status_code == 200 and duplicate.headers["Idempotency-Replayed"] == "true"
    assert (await snapshot(t.repo, t.resource, "memory")).revision == state.revision
    assert (await current(t)).json() == readback.json() and len(t.calls) == 6


@pytest.mark.asyncio
async def test_old_tampered_and_other_resource_credentials_never_write(trip_api):
    t = trip_api
    option = (await preview(t)).json()["options"][0]
    token = option["change_token"]
    other, other_stored = await create_trip(t.repo, key="relative-route-http-other")
    other_before = await snapshot(t.repo, other, "memory")
    other_response = await t.http.post(f"/api/v3/trip-understandings/{other.public_resource_id}/changes/adopt",
        json={"change_token": token}, headers={"If-Match": f'"{other_stored.opaque_etag}"', "Idempotency-Key": "wrong-resource"})
    assert other_response.status_code == 409
    assert (await snapshot(t.repo, other, "memory")).revision == other_before.revision
    middle = len(token) // 2
    altered = token[:middle] + ("A" if token[middle] != "A" else "B") + token[middle + 1:]
    original = await snapshot(t.repo, t.resource, "memory")
    tampered = await t.http.post(t.base + "/changes/adopt", json={"change_token": altered},
        headers={**t.headers, "Idempotency-Key": "tampered"})
    assert tampered.status_code == 409
    assert (await snapshot(t.repo, t.resource, "memory")).revision == original.revision
    applied = await t.http.post(t.base + "/changes/adopt", json={"change_token": token},
        headers={**t.headers, "Idempotency-Key": "apply-current"})
    assert applied.status_code == 200
    changed = await snapshot(t.repo, t.resource, "memory")
    for index, tag in enumerate((t.headers["If-Match"], applied.headers["etag"])):
        stale = await t.http.post(t.base + "/changes/adopt", json={"change_token": token},
            headers={"If-Match": tag, "Idempotency-Key": f"old-preview-{index}"})
        assert stale.status_code == 409
        public_only(stale)
    assert (await snapshot(t.repo, t.resource, "memory")).revision == changed.revision
    assert len(t.calls) == 6


@pytest.mark.asyncio
async def test_legacy_check_target_keeps_its_existing_branch_without_new_day_header_requirement(trip_api):
    t = trip_api
    materialized = await t.http.post(t.base + "/materialize", headers={**t.headers, "Idempotency-Key": "prepare-legacy-checks"})
    assert materialized.status_code == 200, materialized.text
    checks = await t.http.get(t.base + "/checks")
    assert checks.status_code == 200
    items = [item for item in checks.json()["items"] if item["can_preview"]]
    assert items, checks.text
    response = await t.http.post(t.base + "/changes/preview", json={"check_token": items[0]["check_token"]},
        headers={"Idempotency-Key": "legacy-preview"})
    assert response.status_code == 200, response.text
    assert "change_token" in response.json() and "options" not in response.json()
    public_only(response)
    # A missing/obsolete legacy check remains the existing CHECK_CHANGED error,
    # rather than being misparsed as a day request or requiring new If-Match.
    missing = await t.http.post(t.base + "/changes/preview", json={"check_token": "old-check-token-no-longer-current"},
        headers={"Idempotency-Key": "legacy-missing"})
    assert missing.status_code == 409 and missing.json()["detail"]["code"] == "CHECK_CHANGED"
    assert not t.calls


@pytest.mark.asyncio
async def test_live_route_dependency_closes_real_client_and_returns_complete_http_body_without_calls(trip_api, monkeypatch):
    t = trip_api
    # A valid old private snapshot has no positive order assessment. The real
    # endpoint must return its no-safe-change body without opening route HTTP.
    state = await snapshot(t.repo, t.resource, "memory")
    t.repo.g03_pipeline_inputs[(t.resource.understanding_id, state.revision)].pop("source_order")
    t.app.dependency_overrides.pop(api.get_relative_route_provider)
    configured = api.get_settings().model_copy(update={
        "trip_understanding_provider_mode": "live", "amap_api_key": "fixed-test-not-a-real-key"})
    monkeypatch.setattr(api, "get_settings", lambda: configured)
    clients = []
    enter, send = httpx.AsyncClient.__aenter__, httpx.AsyncClient.send

    async def enter_client(client):
        clients.append(client)
        return await enter(client)

    async def send_only_asgi(client, request, *args, **kwargs):
        if not isinstance(client._transport, httpx.ASGITransport):
            raise AssertionError("No external HTTP is allowed in this lifecycle regression")
        return await send(client, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__aenter__", enter_client)
    monkeypatch.setattr(httpx.AsyncClient, "send", send_only_asgi)
    response = await t.http.post(t.base + "/changes/preview", json={"day_index": 1}, headers=t.headers)
    assert response.status_code == 200, response.text
    assert response.is_closed and response.is_stream_consumed
    assert response.json()["options"] == []
    assert response.json()["status"] != "AVAILABLE"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"] == t.headers["If-Match"]
    public_only(response)
    assert len(clients) == 1 and clients[0].is_closed
    assert not t.calls
