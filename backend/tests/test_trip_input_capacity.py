"""The supported 160 items remain usable; overflow is never a retry/POI task."""
import asyncio
import json

import pytest

from app.trip_understanding.experience_inference import ExperienceQwenProvider
from app.trip_understanding.failures import (
    CAPACITY_EXCEEDED_MESSAGE, INPUT_CAPACITY_EXCEEDED,
    DAY_CAPACITY_EXCEEDED_MESSAGE, INPUT_DAY_CAPACITY_EXCEEDED,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_trip_capacity_readback import capacity_source, CapacityModelClient, CapacityPlaces
from tests.test_trip_understanding_v3_api import _client
from tests.test_experience_inference import Client, provider
from app.api import trip_understandings_v3


def capacity_pipeline(count, days):
    _, groups = capacity_source(160, days)
    if count == 161:
        groups[-1].append("容量测试161公园")
    source = "北京容量测试，所有地点是模拟测试资料。\n" + "\n".join(
        f"Day{day}：" + "、".join(names) + "。" for day, names in enumerate(groups, 1))

    class Model(CapacityModelClient):
        calls = 0

        async def create(self, **kwargs):
            self.calls += 1
            return await super().create(**kwargs)

    class Places(CapacityPlaces):
        calls = 0

        async def resolve(self, **kwargs):
            self.calls += 1
            return await super().resolve(**kwargs)

    model, places = Model(groups), Places()
    provider = ExperienceQwenProvider(api_key="test", base_url="https://capacity.invalid/v1",
                                     model="controlled-capacity-model", client=model)
    return source, TripUnderstandingPipeline(provider, places), model, places


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [1, 14])
async def test_exactly_160_items_still_reach_public_result(days):
    source, pipeline, model, places = capacity_pipeline(160, days)
    result = (await pipeline.run(source)).public_result
    assert result.status == "READY" and result.coverage.complete
    assert len(result.days) == days
    assert sum(len(day.activities) for day in result.days) == 160
    assert result.days[-1].activities[-1].name == "容量测试160公园"
    assert places.calls == 160
    assert model.calls == (1 if days == 1 else 15)


@pytest.mark.parametrize("days", [1, 14])
def test_161_items_return_explicit_split_message_through_worker_and_api(days):
    source, pipeline, model, places = capacity_pipeline(161, days)
    client, repository, _ = _client()
    created = client.post("/api/v3/trip-understandings", headers={"Idempotency-Key": f"over-capacity-{days}"},
                          json={"mode": "FULL", "source": {"type": "TEXT", "text": source}})
    assert created.status_code == 202
    asyncio.run(TripUnderstandingWorker(repository, full_pipeline=pipeline).run_once("capacity-worker"))
    response = client.get(created.json()["result_url"])
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": INPUT_CAPACITY_EXCEEDED, "message": CAPACITY_EXCEEDED_MESSAGE}
    assert "160" in response.json()["detail"]["message"]
    assert "分成多份行程" in response.json()["detail"]["message"]
    events = client.get(created.json()["events_url"]).text
    assert CAPACITY_EXCEEDED_MESSAGE in events
    assert places.calls == 0
    assert model.calls == (1 if days == 1 else 15)
    assert next(iter(repository.jobs.values()))["status"] == "FAILED"
    assert not repository.results


@pytest.mark.parametrize("returned_days", [14, 15])
def test_fifteen_source_days_cannot_become_a_truncated_fourteen_day_result(returned_days):
    source = "北京15日行程。\n" + "\n".join(f"Day{d}：测试{d:03d}公园。" for d in range(1, 16))
    content = json.dumps(dict(destination="北京", activities=[
        dict(source_quote=f"测试{d:03d}公园", place_name=f"测试{d:03d}公园", day_index=d, role="PLANNED")
        for d in range(1, returned_days + 1)]))
    model = Client(content, content)
    client, repository, _ = _client()
    created = client.post("/api/v3/trip-understandings", headers={"Idempotency-Key": "fifteen-days"},
                          json={"mode": "FULL", "source": {"type": "TEXT", "text": source}})
    pipeline = TripUnderstandingPipeline(provider(model), CapacityPlaces())
    asyncio.run(TripUnderstandingWorker(repository, full_pipeline=pipeline).run_once("capacity-worker"))
    response = client.get(created.json()["result_url"])
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": INPUT_DAY_CAPACITY_EXCEEDED, "message": DAY_CAPACITY_EXCEEDED_MESSAGE}
    assert len(model.calls) == 1
    assert not repository.results


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_kind", ["duplicates", "invented_day"])
async def test_model_mistakes_do_not_turn_small_input_into_a_capacity_failure(invalid_kind):
    source = "北京Day1：测试公园。"
    item = dict(source_quote="测试公园", place_name="测试公园", day_index=1, role="PLANNED")
    valid = dict(destination="北京", activities=[item])
    invalid = {**valid, "activities": [item] * 161 if invalid_kind == "duplicates" else [{**item, "day_index": 15}]}
    model = Client(json.dumps(invalid), json.dumps(valid))
    result = (await TripUnderstandingPipeline(provider(model), CapacityPlaces()).run(source)).public_result
    assert result.status == "READY" and result.coverage.complete
    assert len(result.days) == 1 and len(result.days[0].activities) == 1
    assert len(model.calls) == 2


def test_capacity_code_uses_the_current_failed_job_even_if_display_copy_changes(monkeypatch):
    source, pipeline, _, _ = capacity_pipeline(161, 1)
    client, repository, _ = _client()
    created = client.post("/api/v3/trip-understandings", headers={"Idempotency-Key": "capacity-copy-change"},
                          json={"mode": "FULL", "source": {"type": "TEXT", "text": source}})
    asyncio.run(TripUnderstandingWorker(repository, full_pipeline=pipeline).run_once("capacity-worker"))
    job = next(iter(repository.jobs.values()))
    events = repository.events[job["understanding_id"]]
    events[-1] = events[-1].model_copy(update={"payload": events[-1].payload.model_copy(
        update={"message": "这次没有整理完成，可以重新尝试"})})
    monkeypatch.setattr(trip_understandings_v3, "public_failure_message", lambda category: "文案改版：请拆分行程。")
    response = client.get(created.json()["result_url"])
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": INPUT_CAPACITY_EXCEEDED, "message": "文案改版：请拆分行程。"}
    # A later revision must not inherit an older failed job's capacity category.
    repository.resources[created.json()["public_resource_id"]]["current_revision"] += 1
    assert client.get(created.json()["result_url"]).json()["detail"]["code"] == "UNDERSTANDING_FAILED"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_capacity_category_survives_postgres_authorized_fresh_reader_and_changed_event_copy(monkeypatch):
    import httpx
    from datetime import datetime, timezone
    from fastapi import FastAPI
    from app.trip_understanding.models import CreateFullRequest
    from app.trip_understanding import repository as repository_module
    from app.trip_understanding.repository import PostgresTripUnderstandingRepository
    from app.trip_understanding.service import TripUnderstandingApplicationService
    from app.trip_understanding.source_crypto import SourceCipher
    from app.utils.auth import get_optional_user
    from tests.test_experience_v3_journey import repository_for

    # repository_for creates and drops its own random test database only.
    async with repository_for("postgres") as repository:
        source, pipeline, _, _ = capacity_pipeline(161, 14)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({
            "mode": "FULL", "source": {"type": "TEXT", "text": source}}),
            owner_user_id="experience-owner", idempotency_key="pg-capacity-category")
        # Change the display copy before the immutable event is written.
        monkeypatch.setattr(repository_module, "public_failure_message", lambda category: "这次没有整理完成，可以重新尝试")
        await TripUnderstandingWorker(repository, full_pipeline=pipeline).run_once("pg-capacity-worker")
        reader = PostgresTripUnderstandingRepository(repository._pool, SourceCipher("experience-controlled-test-secret"))
        resource = await reader.authorize(created.accepted.public_resource_id,
            capability_hash=None, user_id="experience-owner", now=datetime.now(timezone.utc))
        assert resource.state == "FAILED" and resource.failure_category == INPUT_CAPACITY_EXCEEDED
        assert "failure_category" not in resource.model_dump()
        events = await reader.list_events(resource, after_event_id=0)
        assert events[-1].payload.message == "这次没有整理完成，可以重新尝试"
        app = FastAPI()
        app.include_router(trip_understandings_v3.router, prefix="/api")
        app.dependency_overrides[trip_understandings_v3.get_trip_understanding_repository] = lambda: reader
        app.dependency_overrides[get_optional_user] = lambda: "experience-owner"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(created.accepted.result_url)
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == INPUT_CAPACITY_EXCEEDED
