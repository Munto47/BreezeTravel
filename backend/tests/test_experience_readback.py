from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException, Request
from httpx import ASGITransport, AsyncClient

from app.api import trip_understandings_v3 as api
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from app.trip_understanding.pipeline import canonical_sha256
from tests.test_experience_v3_journey import repository_for


def application(repository):
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.include_router(api.account_router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repository

    async def optional_user(request: Request):
        return request.headers.get("x-test-user")

    async def required_user(request: Request):
        user = await optional_user(request)
        if not user:
            raise HTTPException(status_code=401)
        return user

    app.dependency_overrides[api.get_optional_user] = optional_user
    app.dependency_overrides[api.get_current_user] = required_user
    return app


async def create_ready(client, repository, key, mode="FULL", *, dated_optional=False):
    body = {"mode": mode}
    if mode == "FULL":
        body["source"] = {"type": "TEXT", "text": DEMO_SOURCE_TEXT}
    response = await client.post("/api/v3/trip-understandings", json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 202
    now = datetime.now(timezone.utc)
    job = await repository.claim_next(worker_id="readback-test", now=now, lease_seconds=30)
    output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
    # Explicit calendar labels and an undated optional place must coexist.
    output.public_result.days[0].label = "9月12日"
    if dated_optional:
        for activity in output.activities:
            if activity.compiled.mention.role == "OPTIONAL":
                activity.compiled.mention.day_index = 1
    await repository.complete_job(job, output, now=now)
    return "/api/v3/trip-understandings/" + response.json()["public_resource_id"], job


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("mode", ["FULL", "DEMO"])
@pytest.mark.asyncio
async def test_private_source_and_supplementary_follow_edits_and_cannot_return_after_deletion(kind, mode):
    async with repository_for(kind) as repository:
        transport = ASGITransport(app=application(repository))
        async with AsyncClient(transport=transport, base_url="http://test") as owner, AsyncClient(transport=transport, base_url="http://test") as stranger:
            base, job = await create_ready(owner, repository, "private-source", mode, dated_optional=mode == "FULL")
            result = await owner.get(base + "/result")
            assert result.json()["is_demo"] == (mode == "DEMO")
            assert result.json()["updated_at"]
            source = await owner.get(base + "/source")
            assert source.status_code == 200 and source.headers["cache-control"] == "no-store"
            assert source.json()["text"] == DEMO_SOURCE_TEXT
            assert len(source.json()["activities"]) == 6
            for item in source.json()["activities"]:
                assert set(item) == {"activity_token", "name", "quote"}
                assert item["quote"] in DEMO_SOURCE_TEXT
            supplemental = await owner.get(base + "/supplementary")
            assert supplemental.status_code == 200 and supplemental.headers["cache-control"] == "no-store"
            items = [item for day in supplemental.json()["days"] for item in day["items"]]
            assert {item["name"] for item in items} == {"南锣鼓巷", "北京环球影城"}
            assert {item["role"] for item in items} == {"OPTIONAL", "EXCLUDED"}
            assert all(set(item) == {"name", "time_hint", "role"} for item in items)
            assert supplemental.json()["days"][-1]["day_index"] is None
            assert supplemental.json()["days"][-1]["day_label"] == "未指定日期"
            if mode == "FULL":
                assert supplemental.json()["days"][0]["day_index"] == 1
                assert supplemental.json()["days"][0]["day_label"] == "9月12日"
            for suffix in ("/source", "/supplementary"):
                response = await stranger.get(base + suffix)
                assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
            unrelated = await stranger.post("/api/v3/trip-understandings", json={"mode": "DEMO"}, headers={"Idempotency-Key": "other-session"})
            assert unrelated.status_code == 202
            for suffix in ("/source", "/supplementary"):
                assert (await stranger.get(base + suffix)).status_code == 404
            before_map = await owner.get(base + "/map-renders/latest")
            assert len(before_map.json()["points"]) == 6
            token = result.json()["days"][0]["activities"][0]["activity_token"]
            edited = await owner.post(base + "/commands", json={"command_type": "ACTIVITY_TIME_SET", "activity_token": token, "start_time": "09:15"},
                headers={"If-Match": result.headers["etag"], "Idempotency-Key": "edit-private-source"})
            assert edited.status_code == 200
            changed = await owner.get(base + "/result")
            current_token = changed.json()["days"][0]["activities"][0]["activity_token"]
            assert current_token != token
            assert datetime.fromisoformat(changed.json()["updated_at"].replace("Z", "+00:00")) >= datetime.fromisoformat(result.json()["updated_at"].replace("Z", "+00:00"))
            assert current_token in {item["activity_token"] for item in (await owner.get(base + "/source")).json()["activities"]}
            assert (await owner.get(base + "/supplementary")).json() == supplemental.json()
            assert (await owner.delete(base + "/source", headers={"Idempotency-Key": "erase-source"})).status_code == 204
            assert (await owner.get(base + "/source")).json() == {"status": "DELETED", "text": None, "activities": []}
            assert (await owner.get(base + "/supplementary")).json() == {"status": "DELETED", "days": []}
            after = await owner.get(base + "/result")
            assert after.json()["is_demo"] == (mode == "DEMO")
            undone = await owner.post(base + "/commands", json={"command_type": "UNDO"}, headers={"If-Match": after.headers["etag"], "Idempotency-Key": "undo-private-source"})
            assert undone.status_code == 200
            assert (await owner.get(base + "/source")).json()["status"] == "DELETED"
            assert (await owner.get(base + "/supplementary")).json()["days"] == []
            if kind == "memory":
                assert job.understanding_id not in repository.source_readback_mentions
            assert (await owner.delete(base, headers={"Idempotency-Key": "erase-trip"})).status_code == 204
            assert (await owner.get(base + "/source")).status_code == 410


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_source_expiry_hides_text_and_optional_arrangements_before_cleanup(kind):
    async with repository_for(kind) as repository:
        async with AsyncClient(transport=ASGITransport(app=application(repository)), base_url="http://test") as client:
            base, job = await create_ready(client, repository, "source-expiry")
            past = datetime.now(timezone.utc) - timedelta(seconds=1)
            if kind == "postgres":
                await repository._pool.execute("UPDATE trip_understanding_sources SET retention_until=$2 WHERE understanding_id=$1", job.understanding_id, past)
            else:
                repository.source_expiries[job.job_id] = past
            assert (await client.get(base + "/result")).status_code == 200
            assert (await client.get(base + "/source")).json() == {"status": "UNAVAILABLE", "text": None, "activities": []}
            assert (await client.get(base + "/supplementary")).json() == {"status": "UNAVAILABLE", "days": []}
            # Saving extends the trip lifetime, but cannot revive an already expired import.
            client.headers["x-test-user"] = "experience-owner"
            claim = await client.post(base + "/claim", headers={"Idempotency-Key": "claim-expired-source"})
            assert claim.status_code == 200
            base = "/api/v3/trip-understandings/" + claim.json()["public_resource_id"]
            assert (await client.get(base + "/source")).json()["status"] == "UNAVAILABLE"
            assert (await client.get(base + "/supplementary")).json()["days"] == []
            await repository.purge_expired_private_data(now=datetime.now(timezone.utc), limit=100)
            assert (await client.get(base + "/source")).json()["text"] is None
            assert (await client.get(base + "/supplementary")).json()["days"] == []


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_account_trip_seek_pagination_is_private_reopenable_and_filters_expired_deleted_unfinished(kind):
    async with repository_for(kind) as repository:
        if kind == "postgres":
            await repository._pool.execute("INSERT INTO users(user_id,nickname) VALUES ('other-owner','Other')")
        transport = ASGITransport(app=application(repository))
        async with AsyncClient(transport=transport, base_url="http://test", headers={"x-test-user": "experience-owner"}) as owner, AsyncClient(transport=transport, base_url="http://test", headers={"x-test-user": "other-owner"}) as other, AsyncClient(transport=transport, base_url="http://test") as anonymous:
            assert (await anonymous.get("/api/v3/me/trips")).status_code == 401
            bases = []
            for index in range(3):
                base, _ = await create_ready(owner, repository, f"owner-trip-{index}")
                bases.append(base)
            demo, _ = await create_ready(owner, repository, "owner-demo", "DEMO")
            claimed = await owner.post(demo + "/claim", headers={"Idempotency-Key": "claim-list-demo"})
            assert claimed.status_code == 200
            claimed_id = claimed.json()["public_resource_id"]
            bases.append("/api/v3/trip-understandings/" + claimed_id)
            foreign, _ = await create_ready(other, repository, "foreign-trip")
            old = datetime.now(timezone.utc) - timedelta(days=3)
            expired = await repository.create_full(owner_user_id="experience-owner", source_text=DEMO_SOURCE_TEXT, idempotency_key="old-trip",
                request_hash=canonical_sha256("old-trip"), now=old, retention_days=1)
            old_job = await repository.claim_next(worker_id="old-trip", now=old, lease_seconds=30)
            await repository.complete_job(old_job, await build_demo_pipeline().run(DEMO_SOURCE_TEXT), now=old)
            expired_base = "/api/v3/trip-understandings/" + expired.accepted.public_resource_id
            assert (await owner.get(expired_base + "/source")).status_code == 410
            deleted = bases.pop(0)
            assert (await owner.delete(deleted, headers={"Idempotency-Key": "delete-listed"})).status_code == 204
            pending = await owner.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": DEMO_SOURCE_TEXT}}, headers={"Idempotency-Key": "pending-list"})
            assert pending.status_code == 202
            # Equal timestamps exercise the deterministic second ordering key.
            same_time = datetime.now(timezone.utc) - timedelta(minutes=1)
            if kind == "postgres":
                await repository._pool.execute("UPDATE trip_understandings SET updated_at=$1 WHERE owner_user_id='experience-owner'", same_time)
            else:
                for row in repository.resources.values():
                    if row["owner_user_id"] == "experience-owner":
                        row["updated_at"] = same_time
            response = await owner.get("/api/v3/me/trips?limit=2")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            first = response.json()
            assert len(first["items"]) == 2 and first["next_cursor"]
            second = (await owner.get("/api/v3/me/trips", params={"limit": 2, "cursor": first["next_cursor"]})).json()
            assert len(second["items"]) == 1 and second["next_cursor"] is None
            items = first["items"] + second["items"]
            assert [item["public_resource_id"] for item in items] == sorted([base.rsplit("/", 1)[1] for base in bases], reverse=True)
            assert next(item for item in items if item["public_resource_id"] == claimed_id)["is_demo"]
            for item in items:
                assert set(item) == {"public_resource_id", "title", "city", "day_count", "updated_at", "expires_at", "is_demo"}
                assert item["city"] == "北京" and item["day_count"] == 3
            assert (await other.get("/api/v3/me/trips", params={"cursor": first["next_cursor"]})).status_code == 400
            assert (await owner.get("/api/v3/me/trips", params={"cursor": first["next_cursor"][:-4] + "bad!"})).status_code == 400
            for limit in (0, 51):
                assert (await owner.get("/api/v3/me/trips", params={"limit": limit})).status_code == 422
            assert [item["public_resource_id"] for item in (await other.get("/api/v3/me/trips")).json()["items"]] == [foreign.rsplit("/", 1)[1]]
            # A fresh browser needs account authentication, not the anonymous cookie.
            async with AsyncClient(transport=transport, base_url="http://test", headers={"x-test-user": "experience-owner"}) as returning:
                for item in items:
                    base = "/api/v3/trip-understandings/" + item["public_resource_id"]
                    assert (await returning.get(base + "/result")).status_code == 200
                    assert (await returning.get(base + "/source")).json()["status"] == "AVAILABLE"
                    assert (await other.get(base + "/source")).status_code == 404
