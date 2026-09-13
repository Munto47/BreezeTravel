"""Real immutable storage; model/place replies are existing fixed fixtures."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.trip_understanding.candidates import issue_candidate
from app.trip_understanding.dining import dining_binding
from app.trip_understanding.edit_history import advance_edit_history
from app.trip_understanding.errors import (
    CommandTargetChangedError, IdempotencyConflictError, ResourceAccessDeniedError,
    RevisionConflictError, SourceUnavailableError,
)
from app.trip_understanding.models import (
    ActivityMoveCommand, AlternativeInsertCommand, CreateFullRequest, DiningInsertCommand,
    PlaceConfirmCommand, RedoCommand, TripUnderstandingCommand, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.repository import PostgresTripUnderstandingRepository
from app.trip_understanding.route_geometry import InMemoryRouteGeometryCache
from app.trip_understanding.service import TripUnderstandingApplicationService
from app.trip_understanding.source_crypto import SourceCipher
from tests.test_experience_v3_journey import repository_for, create, finish, refresh
from tests.test_meal_slot_selection import build_meal_result, fixture, selected_restaurant, assert_selected
from tests.test_stay_manual_refresh import finish_stay


def test_history_has_explicit_legacy_boundary_and_never_uses_snapshot_flags():
    # An old version promises at most the existing single undo; an old undone
    # result must not make its preceding UNDO record into a guessed redo.
    source, history = advance_edit_history(9, {}, can_undo=True, command_type="UNDO")
    assert source == 8 and history == {"undo": [], "redo": [9]}
    with pytest.raises(CommandTargetChangedError):
        advance_edit_history(10, {}, can_undo=False, command_type="REDO")
    source, history = advance_edit_history(10, {"edit_history": history}, can_undo=False, command_type="REDO")
    assert source == 9 and history == {"undo": [10], "redo": []}
    _, branched = advance_edit_history(11, {"edit_history": {"undo": [2], "redo": [5]}},
        can_undo=False, command_type="ACTIVITY_MOVE")
    assert branched == {"undo": [2, 11], "redo": []}
    for bad in ({"undo": [11], "redo": []}, {"undo": [True], "redo": []}, {}, "bad"):
        with pytest.raises(CommandTargetChangedError):
            advance_edit_history(11, {"edit_history": bad}, can_undo=True, command_type="UNDO")
    assert isinstance(TypeAdapter(TripUnderstandingCommand).validate_python({"command_type": "REDO"}), RedoCommand)
    with pytest.raises(ValidationError):
        TypeAdapter(TripUnderstandingCommand).validate_python({"command_type": "REDO", "revision": 2})


def reader_for(repo, kind):
    return (PostgresTripUnderstandingRepository(repo._pool, SourceCipher("experience-controlled-test-secret"),
        InMemoryRouteGeometryCache()) if kind == "postgres" else repo)


def business_view(result):
    """Compare all business fields; normalize only renewed opaque references."""
    tokens = {}
    for day_index, day in enumerate(result.days):
        for index, card in enumerate(day.activities):
            tokens[card.activity_token] = f"card:{day_index}:{index}"
        for index, alt in enumerate(day.alternatives):
            tokens[alt.activity_token] = f"alternative:{day_index}:{index}"

    def clean(value, key=""):
        if isinstance(value, dict):
            return {k: clean(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(item, key) for item in value]
        return tokens.get(value, value) if isinstance(value, str) else value

    return clean({"days": [day.model_dump(mode="json") for day in result.days],
        "assumptions": [item.model_dump(mode="json") for item in result.assumptions],
        "coverage": result.coverage.model_dump(mode="json"),
        "lodging_constraints": [item.model_dump(mode="json") for item in result.lodging_constraints]})


async def identity_view(repo, resource, now):
    return [(point.name, point.position.model_dump() if point.position else None)
        for point in (await repo.get_map_view(resource, now=now)).points]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_two_edits_two_undos_two_redos_restore_meal_details_and_identity_after_account_reload(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": fixture()["source"]}}), owner_user_id="experience-owner",
            idempotency_key="history-create", now=now)
        job = await repo.claim_next(worker_id="history-fixed", now=now, lease_seconds=60)
        output = await build_meal_result()
        assert sum(len(card.source_details) for day in output.public_result.days for card in day.activities) >= 6
        await repo.complete_job(job, output, now=now)

        async def read():
            reader = reader_for(repo, kind)
            resource = await reader.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            return reader, resource, await reader.get_result(resource)

        reader, resource, stored = await read()
        old_payload = stored.result.model_dump(mode="json")
        old_payload.pop("can_redo")
        assert UserFacingTripResult.model_validate(old_payload).can_redo is False
        states = [(business_view(stored.result), await identity_view(reader, resource, now))]
        assert not stored.result.can_undo and not stored.result.can_redo
        token = stored.result.days[0].activities[0].activity_token
        candidate = issue_candidate(selected_restaurant(), public_resource_id=resource.public_resource_id,
            activity_token=dining_binding(token), expected_etag=stored.opaque_etag, now=now)
        await service.apply_command(resource, DiningInsertCommand(command_type="DINING_INSERT",
            after_activity_token=token, candidate_token=candidate.candidate_token, meal_role="LUNCH"),
            expected_etag=stored.opaque_etag, idempotency_key="history-meal", now=now)
        reader, resource, stored = await read()
        assert_selected(stored.result)
        states.append((business_view(stored.result), await identity_view(reader, resource, now)))
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=2, target_position=0),
            expected_etag=stored.opaque_etag, idempotency_key="history-move", now=now)
        reader, resource, stored = await read()
        states.append((business_view(stored.result), await identity_view(reader, resource, now)))
        original_result_ids = []
        for index, (command, expected, can_undo, can_redo) in enumerate([
            (UndoCommand(command_type="UNDO"), 1, True, True),
            (UndoCommand(command_type="UNDO"), 0, False, True),
            (RedoCommand(command_type="REDO"), 1, True, True),
            (RedoCommand(command_type="REDO"), 2, True, False),
        ]):
            etag = stored.opaque_etag
            outcome = await service.apply_command(resource, command, expected_etag=etag,
                idempotency_key=f"history-nav-{index}", now=now)
            replay = await service.apply_command(resource, command, expected_etag=etag,
                idempotency_key=f"history-nav-{index}", now=now)
            assert replay.replayed and replay.opaque_etag == outcome.opaque_etag
            with pytest.raises(IdempotencyConflictError):
                await service.apply_command(resource,
                    RedoCommand(command_type="REDO") if isinstance(command, UndoCommand) else UndoCommand(command_type="UNDO"),
                    expected_etag=etag, idempotency_key=f"history-nav-{index}", now=now)
            with pytest.raises(RevisionConflictError):
                await service.apply_command(resource, command, expected_etag=etag,
                    idempotency_key=f"history-stale-{index}", now=now)
            reader, resource, stored = await read()
            original_result_ids.append(resource.current_result_id)
            assert (business_view(stored.result), await identity_view(reader, resource, now)) == states[expected]
            assert (stored.result.can_undo, stored.result.can_redo) == (can_undo, can_redo)
            assert stored.result.map.status == "NEEDS_UPDATE"
        assert len(set(original_result_ids)) == 4
        with pytest.raises(ResourceAccessDeniedError):
            await reader.authorize(resource.public_resource_id, capability_hash=None, user_id="different-owner", now=now)
        # Deletion is a privacy operation outside edit history; neither direction
        # restores the erased source, while saved structured arrangements survive.
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="erase", now=now)
        for index, command in enumerate([UndoCommand(command_type="UNDO"), RedoCommand(command_type="REDO")]):
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f"after-erase-{index}", now=now)
            reader, resource, stored = await read()
            assert (business_view(stored.result), await identity_view(reader, resource, now)) == states[1 if index == 0 else 2]
            with pytest.raises(SourceUnavailableError):
                await repo.load_source(job, now=now)
        if kind == "postgres":
            assert await repo._pool.fetchval("SELECT count(*) FROM trip_understanding_results WHERE understanding_id=$1",
                resource.understanding_id) == 9
            assert json.loads(await repo._pool.fetchval("SELECT proposal_json FROM trip_understanding_revisions WHERE understanding_id=$1 AND revision=10",
                resource.understanding_id))["edit_history"]["redo"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_hotel_selection_is_a_real_history_edit_and_new_selection_discards_redo(kind):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "hotel-history", now), now)
        service = TripUnderstandingApplicationService(repo)
        await finish_stay(repo, now)
        resource, initial = await refresh(repo, resource, now)
        chosen = (await repo.get_stay_view(resource)).candidates[0]
        await service.select_stay(resource, candidate_token=chosen.candidate_token, expected_etag=initial.opaque_etag,
            idempotency_key="choose-history", now=now)
        resource, stored = await refresh(repo, resource, now)
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=1, target_position=1),
            expected_etag=stored.opaque_etag, idempotency_key="move-after-stay", now=now)
        for index, (kind_name, selected) in enumerate([("UNDO", True), ("UNDO", False), ("REDO", True), ("REDO", True)]):
            resource, stored = await refresh(repo, resource, now)
            command = TypeAdapter(TripUnderstandingCommand).validate_python({"command_type": kind_name})
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f"stay-nav-{index}", now=now)
            reader = reader_for(repo, kind)
            resource, stored = await refresh(reader, resource, now)
            view = await reader.get_stay_view(resource)
            assert [item.name for item in view.candidates if item.selected] == ([chosen.name] if selected else [])
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="branch-undo", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert stored.result.can_redo
        await service.select_stay(resource, candidate_token=chosen.candidate_token, expected_etag=stored.opaque_etag,
            idempotency_key="branch-stay", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert stored.result.can_undo and not stored.result.can_redo
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, RedoCommand(command_type="REDO"), expected_etag=stored.opaque_etag,
                idempotency_key="old-branch-redo", now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_optional_adoption_confirmation_and_branch_keep_authoritative_details(kind):
    from tests.test_alternative_insert import build_alternative_insert_result, SOURCE, DETAILS
    from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": SOURCE}}), owner_user_id="experience-owner",
            idempotency_key="optional-history", now=now)
        job = await repo.claim_next(worker_id="optional-history", now=now, lease_seconds=60)
        await repo.complete_job(job, await build_alternative_insert_result(), now=now)

        async def read():
            reader = reader_for(repo, kind)
            resource = await reader.authorize(created.accepted.public_resource_id,
                capability_hash=None, user_id="experience-owner", now=now)
            return resource, await reader.get_result(resource)

        resource, stored = await read()
        await service.apply_command(resource, AlternativeInsertCommand(command_type="ALTERNATIVE_INSERT", day_index=1,
            alternative_token=stored.result.days[0].alternatives[0].activity_token, position=0),
            expected_etag=stored.opaque_etag, idempotency_key="optional-add", now=now)
        resource, stored = await read()
        parent = stored.result.days[0].activities[0]
        place = CandidatePlace(canonical_place_id="fixed-history-place", name=parent.name, city="上海", category="景点",
            area_or_address="固定地址", position=GCJ02Position(longitude=121.48, latitude=31.22))
        candidate = issue_candidate(place, public_resource_id=resource.public_resource_id, activity_token=parent.activity_token,
            expected_etag=stored.opaque_etag, now=now)
        await service.apply_command(resource, PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=parent.activity_token,
            candidate_token=candidate.candidate_token), expected_etag=stored.opaque_etag, idempotency_key="optional-confirm", now=now)
        for index, (direction, card_status) in enumerate([("UNDO", "NEEDS_CONFIRMATION"), ("UNDO", None), ("REDO", "NEEDS_CONFIRMATION"), ("REDO", "READY")]):
            resource, stored = await read()
            await service.apply_command(resource, TypeAdapter(TripUnderstandingCommand).validate_python({"command_type": direction}),
                expected_etag=stored.opaque_etag, idempotency_key=f"optional-nav-{index}", now=now)
            resource, stored = await read()
            assert len(stored.result.days[0].alternatives) == 1
            assert [d.model_dump() for d in stored.result.days[0].alternatives[0].source_details] == DETAILS
            assert [card.status for card in stored.result.days[0].activities] == ([card_status] if card_status else [])
            if card_status:
                assert [d.model_dump() for d in stored.result.days[0].activities[0].source_details] == DETAILS
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="optional-branch-undo", now=now)
        resource, stored = await read()
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[0].activities[0].activity_token, target_day_index=2, target_position=0),
            expected_etag=stored.opaque_etag, idempotency_key="optional-new-edit", now=now)
        resource, stored = await read()
        assert not stored.result.can_redo
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, RedoCommand(command_type="REDO"), expected_etag=stored.opaque_etag,
                idempotency_key="optional-old-redo", now=now)


def test_redo_api_uses_existing_cookie_etag_idempotency_and_privacy_boundaries():
    from fastapi.testclient import TestClient
    from app.trip_understanding.worker import TripUnderstandingWorker
    from tests.test_trip_understanding_v3_api import _client
    client, repository, app = _client()
    created = client.post("/api/v3/trip-understandings", json={"mode": "DEMO"}, headers={"Idempotency-Key": "api-history"})
    assert created.status_code == 202
    asyncio.run(TripUnderstandingWorker(repository).run_once("api-history-worker"))
    path = f"/api/v3/trip-understandings/{created.json()['public_resource_id']}"
    current = client.get(path + "/result")
    move = {"command_type": "ACTIVITY_MOVE", "activity_token": current.json()["days"][0]["activities"][0]["activity_token"],
        "target_day_index": 1, "target_position": 1}
    for index, command in enumerate([move, {"command_type": "UNDO"}, {"command_type": "REDO"}]):
        headers = {"If-Match": current.headers["ETag"], "Idempotency-Key": f"api-nav-{index}"}
        response = client.post(path + "/commands", json=command, headers=headers)
        assert response.status_code == 200
        assert client.post(path + "/commands", json=command, headers=headers).headers["Idempotency-Replayed"] == "true"
        assert client.post(path + "/commands", json=command, headers={**headers, "Idempotency-Key": f"api-stale-{index}"}).status_code == 409
        current = client.get(path + "/result")
        assert current.json()["can_redo"] == (index == 1)
        assert "edit_history" not in current.text
    assert TestClient(app).post(path + "/commands", json={"command_type": "UNDO"},
        headers={"If-Match": current.headers["ETag"], "Idempotency-Key": "outsider"}).status_code in {403, 404}


@pytest.mark.asyncio
async def test_postgres_suggestion_storage_uses_the_same_history_and_clears_redo():
    # Exercise the separate PostgreSQL suggestion persistence boundary with a
    # relative command. This is not evidence that a suggestion was generated.
    async with repository_for("postgres") as repo:
        now = datetime.now(timezone.utc)
        resource = await finish(repo, await create(repo, "suggestion-history", now), now)
        service = TripUnderstandingApplicationService(repo)
        resource, original = await refresh(repo, resource, now)
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=original.result.days[0].activities[0].activity_token, target_day_index=1, target_position=1),
            expected_etag=original.opaque_etag, idempotency_key="suggestion-before", now=now)
        resource, changed = await refresh(repo, resource, now)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=changed.opaque_etag,
            idempotency_key="suggestion-before-undo", now=now)
        resource, stored = await refresh(repo, resource, now)
        assert stored.result.can_redo
        command = ActivityMoveCommand(command_type="ACTIVITY_MOVE", activity_token=stored.result.days[0].activities[1].activity_token,
            target_day_index=2, target_position=0)
        async with repo._pool.acquire() as conn, conn.transaction():
            current = await repo._lock_current_result(conn, resource)
            await repo._persist_understanding_mutation(conn, resource=resource, current=current, command=command,
                request_hash="f" * 64, now=now)
        resource, changed = await refresh(reader_for(repo, "postgres"), resource, now)
        assert changed.result.can_undo and not changed.result.can_redo
        expected = business_view(changed.result)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=changed.opaque_etag,
            idempotency_key="suggestion-undo", now=now)
        resource, restored = await refresh(repo, resource, now)
        assert business_view(restored.result) == business_view(original.result)
        await service.apply_command(resource, RedoCommand(command_type="REDO"), expected_etag=restored.opaque_etag,
            idempotency_key="suggestion-redo", now=now)
        resource, replayed = await refresh(repo, resource, now)
        assert business_view(replayed.result) == expected
