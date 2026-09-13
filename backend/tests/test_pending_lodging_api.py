"""The private recovery read/search/confirm flow never publishes an unconfirmed hotel name."""
from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api import trip_understandings_v3 as api
from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
from app.trip_understanding.repository import InMemoryTripUnderstandingRepository
from tests.test_pending_lodging_recovery import SOURCE, pending_output
from tests.test_experience_v3_journey import repository_for


@pytest.mark.asyncio
async def test_private_scope_recovery_search_binding_confirm_constraint_replace_delete_and_undo():
    repo = InMemoryTripUnderstandingRepository()
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repo
    searches = []
    async def search(**values):
        searches.append(values)
        return [CandidatePlace(canonical_place_id="amap:test-hotel", city="北京", name="星河酒店",
            category="住宿", area_or_address="受控地址", position=GCJ02Position(longitude=116.4, latitude=39.91))]
    app.dependency_overrides[api.get_place_candidate_search] = lambda: search
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as owner, AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as stranger:
        created = await owner.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}, headers={"Idempotency-Key": "pending-api"})
        assert created.status_code == 202
        base = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
        now = datetime.now(timezone.utc)
        job = await repo.claim_next(worker_id="pending-api", now=now, lease_seconds=60)
        await repo.complete_job(job, await pending_output(), now=now)
        result = await owner.get(base + "/result")
        assert "星河酒店" not in result.text
        token = result.json()["pending_lodgings"][0]["pending_token"]
        private = await owner.get(base + "/supplementary")
        assert private.json()["pending_lodgings"] == [] and "星河酒店" not in private.text
        private = await owner.get(base + "/supplementary?include_pending_lodgings=true")
        assert private.headers["cache-control"] == "no-store"
        assert private.json()["pending_lodgings"][0]["name"] == "星河酒店"
        assert (await stranger.get(base + "/supplementary?include_pending_lodgings=true")).status_code == 404
        assert searches == []
        intent = {"kind": "NIGHTS", "overnight_days": [2]}
        query = {"pending_token": token, "intent": intent, "query": "星河酒店", "city": "北京"}
        assert (await owner.post(base + "/place-candidates", json=query)).status_code == 428
        candidate_response = await owner.post(base + "/place-candidates", json=query, headers={"If-Match": result.headers["etag"]})
        assert candidate_response.status_code == 200 and searches == [{"city": "北京", "query": "星河酒店", "category_hint": "住宿"}]
        credential = candidate_response.json()["candidates"][0]["candidate_token"]
        command = {"command_type": "LODGING_RECOVER", "pending_token": token, "candidate_token": credential, "intent": intent}
        wrong = await owner.post(base + "/commands", json={**command, "intent": {"kind": "WHOLE_TRIP"}}, headers={"If-Match": result.headers["etag"], "Idempotency-Key": "wrong-scope"})
        assert wrong.status_code == 409
        saved = await owner.post(base + "/commands", json=command, headers={"If-Match": result.headers["etag"], "Idempotency-Key": "recover"})
        assert saved.status_code == 200
        result = await owner.get(base + "/result")
        assert result.json()["pending_lodgings"] == [] and result.json()["coverage"]["complete"]
        constraint = result.json()["lodging_constraints"][0]
        assert constraint["overnight_days"] == [2]
        assert not any(card["name"] == "星河酒店" for day in result.json()["days"] for card in day["activities"])
        candidates = await owner.post(base + "/place-candidates", json={"activity_token": constraint["activity_token"], "query": "星河酒店", "city": "北京"})
        assert candidates.status_code == 200
        confirmed = await owner.post(base + "/commands", json={"command_type": "PLACE_CONFIRM", "activity_token": constraint["activity_token"],
            "candidate_token": candidates.json()["candidates"][0]["candidate_token"]}, headers={"If-Match": result.headers["etag"], "Idempotency-Key": "replace-constraint"})
        assert confirmed.status_code == 200
        result = await owner.get(base + "/result")
        removed = await owner.post(base + "/commands", json={"command_type": "ACTIVITY_DELETE", "activity_token": result.json()["lodging_constraints"][0]["activity_token"]},
            headers={"If-Match": result.headers["etag"], "Idempotency-Key": "remove-constraint"})
        assert removed.status_code == 200
        result = await owner.get(base + "/result")
        assert result.json()["lodging_constraints"] == []
        undone = await owner.post(base + "/commands", json={"command_type": "UNDO"}, headers={"If-Match": result.headers["etag"], "Idempotency-Key": "undo-constraint"})
        assert undone.status_code == 200
        assert (await owner.get(base + "/result")).json()["lodging_constraints"][0]["overnight_days"] == [2]


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("city_state", ["single", "cross_city", "unknown", "conflicting_destination"])
@pytest.mark.asyncio
async def test_pending_search_follows_only_a_corroborated_single_city_or_requires_explicit_choice(kind, city_state):
    async with repository_for(kind) as repo:
        app = FastAPI()
        app.include_router(api.router, prefix="/api")
        app.dependency_overrides[api.get_trip_understanding_repository] = lambda: repo
        searches = []

        async def search(**values):
            searches.append(values)
            return []

        app.dependency_overrides[api.get_place_candidate_search] = lambda: search
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            created = await client.post("/api/v3/trip-understandings", json={"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}},
                headers={"Idempotency-Key": "pending-city-api"})
            assert created.status_code == 202
            base = "/api/v3/trip-understandings/" + created.json()["public_resource_id"]
            now = datetime.now(timezone.utc)
            job = await repo.claim_next(worker_id="pending-city-api", now=now, lease_seconds=60)
            output = await pending_output()
            # Controlled API preconditions: the source hotel has no city hint;
            # current verified cards may agree, span cities, or remain unknown.
            output.activities[0].compiled.mention.city_hint = None
            if city_state == "cross_city":
                output.public_result.days[2].activities[0].city = "上海"
            elif city_state == "unknown":
                output.public_result.days[2].activities[0].city = None
            elif city_state == "conflicting_destination":
                next(item for item in output.public_result.assumptions if item.key == "destination").value = "暂按 上海"
            await repo.complete_job(job, output, now=now)
            result = await client.get(base + "/result")
            private = await client.get(base + "/supplementary?include_pending_lodgings=true")
            assert private.json()["pending_lodgings"][0]["city"] is None
            query = {"pending_token": result.json()["pending_lodgings"][0]["pending_token"],
                "intent": {"kind": "NIGHTS", "overnight_days": [2]}, "query": "星河酒店"}
            response = await client.post(base + "/place-candidates", json=query, headers={"If-Match": result.headers["etag"]})
            if city_state == "single":
                assert response.status_code == 200
                assert searches == [{"city": "北京", "query": "星河酒店", "category_hint": "住宿"}]
            else:
                assert response.status_code == 422 and response.json()["detail"]["code"] == "CITY_REQUIRED"
                assert response.headers["cache-control"] == "no-store" and searches == []
                explicit = await client.post(base + "/place-candidates", json={**query, "city": "北京"},
                    headers={"If-Match": result.headers["etag"]})
                assert explicit.status_code == 200 and searches[0]["city"] == "北京"
            assert (await client.get(base + "/result")).headers["etag"] == result.headers["etag"]
