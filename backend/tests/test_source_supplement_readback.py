"""Fixed two-answer visits survive the real worker and disposable PostgreSQL.

The model and place transports are fixed. Only persistence, version edits and
source erasure use real services; repository_for creates its own temporary DB.
"""
from datetime import datetime, timezone

import pytest

from app.trip_understanding.errors import SourceUnavailableError
from app.trip_understanding.models import ActivityMoveCommand, CreateFullRequest, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_city_evidence_recovery import FixedCities
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_supplement_budget import Client, provider
from tests.test_source_visit_supplement import ROWS, SOURCE, row


SOURCE_WITH_PURPOSES = SOURCE.replace("随后去景山公园。", "随后去景山公园，仅看外观，不入园。")
FIRST = {"destination": "北京", "day_labels": [None, None], "activities": [
    {"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day,
     "occurrence": occurrence, "category": "景点", "city": "北京", "city_evidence": "北京"}
    for name, day, occurrence in [("故宫博物院", 1, 1), ("景山公园", 1, 1), ("故宫博物院", 2, 2)]
]}
SECOND = {"city_fields": [], "source_visits": [*ROWS,
    row(1, "EXTERIOR_ONLY", "景山公园", "仅看外观，不入园")]}
VISIT_DETAILS = [
    {"name": "入口：午门", "optional": False},
    {"name": "太和殿", "optional": False},
    {"name": "珍宝馆", "optional": True},
    {"name": "出口：神武门", "optional": False},
]
EXTERIOR_DETAILS = [{"name": "仅看外观，不入内部", "optional": False}]
PICKUP_DETAILS = [{"name": "仅取物，不参观", "optional": False}]
ORIGINAL = [
    [("故宫博物院", VISIT_DETAILS), ("景山公园", EXTERIOR_DETAILS)],
    [("故宫博物院", PICKUP_DETAILS)],
]
MOVED = [
    [("景山公园", EXTERIOR_DETAILS)],
    [("故宫博物院", VISIT_DETAILS), ("故宫博物院", PICKUP_DETAILS)],
]


def visible_schedule(result):
    return [[(card.name, [detail.model_dump(mode="json") for detail in card.source_details])
             for card in day.activities] for day in result.days]


@pytest.mark.asyncio
async def test_two_answer_source_supplement_survives_postgres_versions_and_source_erasure(monkeypatch):
    async with repository_for("postgres") as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({
            "mode": "FULL", "source": {"type": "TEXT", "text": SOURCE_WITH_PURPOSES},
        }), owner_user_id="experience-owner", idempotency_key="supplement-create", now=now)
        captured_jobs = []
        original_load_source = repository.load_source

        async def capture_source(job, *, now):
            captured_jobs.append(job)
            return await original_load_source(job, now=now)

        monkeypatch.setattr(repository, "load_source", capture_source)
        client = Client(FIRST, SECOND)
        places = FixedCities()
        worker = TripUnderstandingWorker(repository,
            full_pipeline=TripUnderstandingPipeline(provider(client), places))
        assert await worker.run_once("source-supplement-worker", now=now)
        assert len(client.calls) == 2
        assert not client.responses
        # Two visits reuse the same place lookup, while remaining separate cards.
        assert places.calls == [("北京", "故宫博物院"), ("北京", "景山公园")]
        assert len(captured_jobs) == 1
        loaded = await original_load_source(captured_jobs[0], now=now)
        assert loaded.text == SOURCE_WITH_PURPOSES
        assert loaded.initial_plan is None  # Worker must run the current two-answer provider.

        # A separate reader cannot rely on the worker's in-memory PipelineOutput.
        reader = PostgresTripUnderstandingRepository(repository._pool,
            SourceCipher("experience-controlled-test-secret"), InMemoryRouteGeometryCache())

        async def read(expected):
            resource = await reader.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            stored = await reader.get_result(resource)
            assert stored is not None
            assert visible_schedule(stored.result) == expected
            assert sum(len(day.activities) for day in stored.result.days) == 3
            assert stored.result.coverage.confirmed_place_count == 3
            supplementary = await reader.get_supplementary_view(resource, now=now)
            # The optional internal visit belongs only in its parent details.
            assert all(item.name != "珍宝馆" for day in supplementary.days for item in day.items)
            return resource, stored

        resource, stored = await read(ORIGINAL)
        assert stored.result.coverage.complete
        initial_result_id = resource.current_result_id

        async def move_parent(resource, stored, key):
            await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=stored.result.days[0].activities[0].activity_token,
                target_day_index=2, target_position=0), expected_etag=stored.opaque_etag,
                idempotency_key=key, now=now)

        await move_parent(resource, stored, "supplement-move")
        resource, stored = await read(MOVED)
        assert resource.current_result_id != initial_result_id
        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=stored.opaque_etag, idempotency_key="supplement-undo", now=now)
        resource, stored = await read(ORIGINAL)

        await move_parent(resource, stored, "supplement-move-before-erasure")
        resource, stored = await read(MOVED)
        await service.delete_source(resource, user_id="experience-owner",
            idempotency_key="supplement-erase-source", now=now)
        with pytest.raises(SourceUnavailableError):
            await reader.load_source(captured_jobs[0], now=now)
        resource, stored = await read(MOVED)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=stored.opaque_etag, idempotency_key="supplement-undo-after-erasure", now=now)
        await read(ORIGINAL)
        # Undo restores structured itinerary details, never the deleted raw text.
        for loader in (reader.load_source, original_load_source):
            with pytest.raises(SourceUnavailableError):
                await loader(captured_jobs[0], now=now)
        assert len(client.calls) == 2 and len(places.calls) == 2
