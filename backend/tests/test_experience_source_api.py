from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api import trip_understandings_v3 as api
from app.trip_understanding.demo import DEMO_SOURCE_TEXT, build_demo_pipeline
from tests.test_experience_source_deletion import PRIVATE_QUOTE, assert_source_erased
from tests.test_experience_v3_journey import repository_for


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("mode", ["FULL", "DEMO"])
@pytest.mark.asyncio
async def test_anonymous_source_delete_retains_trip_and_rejects_other_sessions(kind, mode):
    async with repository_for(kind) as repository:
        app = FastAPI()
        app.include_router(api.router, prefix="/api")
        app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repository
        transport = ASGITransport(app=app)
        async with (
            AsyncClient(transport=transport, base_url="http://test") as owner,
            AsyncClient(transport=transport, base_url="http://test") as stranger,
        ):
            body = {"mode": mode}
            if mode == "FULL":
                body["source"] = {"type": "TEXT", "text": DEMO_SOURCE_TEXT}
            created = await owner.post(
                "/api/v3/trip-understandings", json=body,
                headers={"Idempotency-Key": "create-owner"},
            )
            assert created.status_code == 202
            base = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
            now = datetime.now(timezone.utc)
            job = await repository.claim_next(worker_id="source-api", now=now, lease_seconds=30)
            output = await build_demo_pipeline().run(DEMO_SOURCE_TEXT)
            for activity in output.activities:
                activity.resolver_receipt["raw_provider_response"] = PRIVATE_QUOTE
            await repository.complete_job(job, output, now=now)
            before = await owner.get(base + "/result")
            before_map = await owner.get(base + "/map-renders/latest")
            assert before.status_code == before_map.status_code == 200
            assert before.json()["ownership"] == "ANONYMOUS"
            assert len([point for point in before_map.json()["points"] if point["position"]]) == 6
            if kind == "postgres":
                assert await repository._pool.fetchval(
                    "SELECT count(*) FROM trip_understanding_source_claims WHERE understanding_id=$1",
                    job.understanding_id,
                ) > 0
            else:
                assert repository.sources

            # Neither absence of a cookie nor another valid anonymous session grants access.
            headers = {"Idempotency-Key": "delete-owner-source"}
            assert (await stranger.delete(base + "/source", headers=headers)).status_code == 404
            other = await stranger.post(
                "/api/v3/trip-understandings", json={"mode": "DEMO"},
                headers={"Idempotency-Key": "create-stranger"},
            )
            assert other.status_code == 202
            assert (await stranger.delete(base + "/source", headers=headers)).status_code == 404
            deleted = await owner.delete(base + "/source", headers=headers)
            assert deleted.status_code == 204
            assert deleted.headers["cache-control"] == "no-store"
            await assert_source_erased(repository, kind, job.understanding_id)
            after = await owner.get(base + "/result")
            after_map = await owner.get(base + "/map-renders/latest")
            assert after.status_code == after_map.status_code == 200
            assert after.json() == before.json()
            assert after.headers["etag"] == before.headers["etag"]
            assert after_map.json()["points"] == before_map.json()["points"]
            replay = await owner.delete(base + "/source", headers=headers)
            assert replay.status_code == 204
            assert replay.headers["idempotency-replayed"] == "true"
            assert (await stranger.delete(base + "/source", headers=headers)).status_code == 404
