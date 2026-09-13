"""Saved collaboration order and global alternatives survive the actual worker.

PostgreSQL tests use fresh controlled databases; place responses are fixtures.
No model inference is allowed for the new structured import.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from app.trip_understanding.collaboration_import import CollaborationRouteUnavailableError, prepare_collaboration_import
from app.trip_understanding.errors import IdempotencyConflictError, ResourceAccessDeniedError, SourceUnavailableError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver, DeterministicTextInferenceProvider
from app.trip_understanding.models import ActivityMoveCommand, SourceSemanticPlan, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.worker import TripUnderstandingWorker
from tests.test_collaboration_import import _saved_route
from tests.test_experience_v3_journey import repository_for


def imported_route(route=None):
    if route is None:
        route = _saved_route()
        route["backupPool"] = [{"name": "颐和园", "category": "attraction", "placeId": "untrusted-old-identity"}]
    return prepare_collaboration_import(user_id="experience-owner", room_id="controlled-room",
        saved_itinerary_id="controlled-saved", city="北京", itinerary_data=route, idempotency_key="same-import")


class NoInference:
    async def propose(self, _source):
        raise AssertionError("Saved collaboration semantics must never be reinterpreted by a model")


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_all_selected_places_survive_worker_save_edit_undo_and_private_source_deletion(kind):
    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        prepared = imported_route()
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_from_collaboration(prepared, owner_user_id="experience-owner", now=now)
        replay = await service.create_from_collaboration(prepared, owner_user_id="experience-owner", now=now)
        assert replay.replayed and replay.accepted == created.accepted
        changed = _saved_route()
        changed["backupPool"] = [{"name": "北海公园", "category": "attraction"}]
        with pytest.raises(IdempotencyConflictError):
            await service.create_from_collaboration(imported_route(changed), owner_user_id="experience-owner", now=now)

        captured = []
        load_source = repository.load_source

        async def remember_source(job, *, now):
            source = await load_source(job, now=now)
            captured.append((job, source))
            return source

        repository.load_source = remember_source
        if kind == "postgres":
            row = await repository._pool.fetchrow("SELECT proposal_json,inference_binding_json FROM trip_understanding_revisions WHERE revision=1")
            proposal = json.loads(row["proposal_json"])
            assert isinstance(proposal["initial_plan"], str)
            assert all(name not in str(row) for name in ("故宫博物院", "景山公园", "颐和园"))
        pipeline = TripUnderstandingPipeline(NoInference(), ControlledSnapshotPlaceResolver())
        assert await TripUnderstandingWorker(repository, full_pipeline=pipeline).run_once("structured-import", now=now)
        assert isinstance(captured[0][1].initial_plan, SourceSemanticPlan)
        assert [(mention.atomic_place_name, mention.day_index, mention.role.value)
                for mention in captured[0][1].initial_plan.mentions] == [
                    ("故宫博物院", 1, "PLANNED"), ("景山公园", 1, "PLANNED"), ("颐和园", None, "OPTIONAL")]

        async def read_expected(expected_names):
            resource = await service.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            stored = await repository.get_result(resource)
            assert stored is not None and stored.result.status == "READY"
            assert stored.result.coverage.complete
            assert [[card.name for card in day.activities] for day in stored.result.days] == [expected_names]
            assert all(not day.alternatives for day in stored.result.days)
            supplementary = await repository.get_supplementary_view(resource, now=now)
            assert len(supplementary.days) == 1
            group = supplementary.days[0]
            assert group.day_index is None and group.day_label == "未指定日期"
            assert [(item.name, item.role) for item in group.items] == [("颐和园", "OPTIONAL")]
            return resource, stored

        resource, original = await read_expected(["故宫博物院", "景山公园"])
        # September relative-only scope: legacy saved clocks no longer become new visit constraints.
        assert [(card.start_time, card.end_time) for card in original.result.days[0].activities] == [(None, None), (None, None)]
        assert all(card.timing_source == "UNSPECIFIED" and not card.locked and not card.fixed_commitment
                   for card in original.result.days[0].activities)
        assert (await repository.get_source_view(resource, now=now)).text == prepared.source_text
        with pytest.raises(ResourceAccessDeniedError):
            await service.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="other-owner", now=now)
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=original.result.days[0].activities[0].activity_token, target_day_index=1, target_position=1),
            expected_etag=original.opaque_etag, idempotency_key="move", now=now)
        resource, edited = await read_expected(["景山公园", "故宫博物院"])
        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=edited.opaque_etag, idempotency_key="undo", now=now)
        resource, undone = await read_expected(["故宫博物院", "景山公园"])
        assert len({original.opaque_etag, edited.opaque_etag, undone.opaque_etag}) == 3
        before_delete = await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=undone.result.days[0].activities[0].activity_token, target_day_index=1, target_position=1),
            expected_etag=undone.opaque_etag, idempotency_key="move-before-delete", now=now)
        await repository.delete_source(resource, user_id="experience-owner", idempotency_key="erase-source",
            request_hash="d" * 64, now=now)
        assert (await repository.get_supplementary_view(resource, now=now)).days == []
        with pytest.raises(SourceUnavailableError):
            await load_source(captured[0][0], now=now)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"),
            expected_etag=before_delete.opaque_etag, idempotency_key="undo-after-delete", now=now)
        assert (await repository.get_supplementary_view(resource, now=now)).days == []
        if kind == "postgres":
            assert await repository._pool.fetchval("SELECT encrypted_content IS NULL FROM trip_understanding_sources")
            assert await repository._pool.fetchval("SELECT count(*) FROM trip_understanding_activities WHERE role='OPTIONAL'") == 0


@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.asyncio
async def test_initial_plan_cannot_outlive_source_retention(kind):
    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        await TripUnderstandingApplicationService(repository).create_from_collaboration(imported_route(),
            owner_user_id="experience-owner", now=now)
        job = await repository.claim_next(worker_id="retention-check", now=now, lease_seconds=60)
        assert (await repository.load_source(job, now=now)).initial_plan is not None
        with pytest.raises(SourceUnavailableError):
            await repository.load_source(job, now=now + timedelta(days=31))


@pytest.mark.asyncio
async def test_historical_import_without_initial_plan_uses_existing_inference_entry():
    prepared = replace(imported_route(_saved_route()), initial_plan=None)
    calls = []

    class HistoricalInference(DeterministicTextInferenceProvider):
        async def propose(self, source):
            calls.append(source)
            return await super().propose(source)

    async with repository_for("memory") as repository:
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_from_collaboration(prepared, owner_user_id="experience-owner")
        assert await TripUnderstandingWorker(repository, full_pipeline=TripUnderstandingPipeline(
            HistoricalInference(), ControlledSnapshotPlaceResolver())).run_once("legacy-import")
        resource = await service.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner")
        assert await repository.get_result(resource) is not None
        assert calls == [prepared.source_text]


@pytest.mark.parametrize("change", ["too_many", "invalid", "too_many_days", "bad_slots"])
def test_import_rejects_invalid_or_oversize_selected_places_without_silent_skips(change):
    route = _saved_route()
    if change == "too_many":
        route["backupPool"] = [{"name": f"测试{index}公园"} for index in range(159)]
    elif change == "invalid":
        route["backupPool"] = [{"name": "颐和园"}, {"name": "https://invalid.example"}]
    elif change == "too_many_days":
        route["days"] *= 15
    else:
        route["days"][0]["slots"] = "bad saved structure"
    with pytest.raises(CollaborationRouteUnavailableError) as raised:
        imported_route(route)
    if change == "invalid":
        assert raised.value.code == "COLLABORATION_INVALID_PLACE"
    elif change in {"too_many", "too_many_days"}:
        assert raised.value.code == "COLLABORATION_ROUTE_TOO_LARGE"


@pytest.mark.asyncio
async def test_prepared_plan_rejects_wrong_source_and_inconsistent_global_alternatives():
    prepared = imported_route()
    with pytest.raises(ValueError):
        await TripUnderstandingPipeline(NoInference(), ControlledSnapshotPlaceResolver()).run(
            prepared.source_text + "改写", prepared_plan=prepared.initial_plan)
    with pytest.raises(ValueError):
        SourceSemanticPlan.model_validate({**prepared.initial_plan.model_dump(),
            "unassigned_alternative_ids": [prepared.initial_plan.mentions[0].mention_id]})


def test_import_name_boundary_matches_pipeline_and_place_service():
    from app.trip_understanding.pipeline import atomic_place_rejection_reason
    route = _saved_route()
    supported = "名称" * 19 + "公园"
    assert len(supported) == 40 and atomic_place_rejection_reason(supported) is None
    route["backupPool"] = [{"name": supported}]
    assert imported_route(route).initial_plan.mentions[-1].atomic_place_name == supported
    route["backupPool"] = [{"name": "大" + supported}]
    assert atomic_place_rejection_reason("大" + supported) == "TOO_LONG"
    with pytest.raises(CollaborationRouteUnavailableError) as error:
        imported_route(route)
    assert error.value.code == "COLLABORATION_INVALID_PLACE"
