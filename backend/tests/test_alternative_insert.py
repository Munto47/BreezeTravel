"""A saved optional parent can be added with its authoritative visit details.

Model replies/places are fixed; PostgreSQL cases use an isolated disposable DB.
"""
from datetime import datetime, timezone
import json

import pytest
from pydantic import TypeAdapter, ValidationError, model_serializer

from app.trip_understanding.commands import apply_public_command, refresh_choice_selection_tokens
from app.trip_understanding.errors import CommandTargetChangedError, ResourceAccessDeniedError, RevisionConflictError
from app.trip_understanding.models import (
    ActivityCardView, ActivityInsertCommand, AlternativeInsertCommand, CreateFullRequest,
    PlaceConfirmCommand, TripUnderstandingCommand, UndoCommand, UserFacingTripResult,
)
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_experience_v3_journey import repository_for
from tests.test_semantic_supplement_budget import Client, provider


SOURCE = "上海。\nDay1：备选青岚乐园，必玩：云海航船、星光环线。\nDay2：备选晨光公园。"
DETAILS = [{"name": "云海航船", "optional": False}, {"name": "星光环线", "optional": False}]


async def build_alternative_insert_result():
    first = dict(destination="上海", day_labels=["Day1", "Day2"], activities=[
        dict(source_quote=name, place_name=name, role="OPTIONAL", day_index=day, category="景点",
             city="上海", city_evidence="上海")
        for name, day in (("青岚乐园", 1), ("晨光公园", 2))
    ])
    second = dict(city_fields=[], source_visits=[
        dict(parent_index=0, kind="VISIT", source_quote=item["name"], optional=False,
             evidence="必玩：云海航船、星光环线") for item in DETAILS
    ])
    client = Client(first, second)
    output = await TripUnderstandingPipeline(provider(client, deadline=5), FixedReplayPlaces()).run(SOURCE)
    assert len(client.calls) == 2 and output.resolution_receipt["attempted_count"] == 0
    assert [d.model_dump(mode="json") for d in output.public_result.days[0].alternatives[0].source_details] == DETAILS
    return output


def insertion(public, *, day=1, position=0):
    return AlternativeInsertCommand(command_type="ALTERNATIVE_INSERT", day_index=day, position=position,
        alternative_token=public.days[0].alternatives[0].activity_token)


async def build_alternative_insert_states():
    from app.trip_understanding.candidates import CandidatePlace, GCJ02Position

    before = (await build_alternative_insert_result()).public_result
    after = apply_public_command(before, insertion(before)).result
    place = CandidatePlace(canonical_place_id="fixed-optional-parent-identity", name="青岚乐园", city="上海",
        category="景点", area_or_address="固定验证地址", position=GCJ02Position(longitude=121.48, latitude=31.22))
    confirmed = apply_public_command(after, PlaceConfirmCommand(command_type="PLACE_CONFIRM",
        activity_token=after.days[0].activities[0].activity_token,
        candidate_token="fixed-signed-candidate-is-validated-separately-in-postgres-test"), confirmed_place=place).result
    undone = apply_public_command(confirmed, UndoCommand(command_type="UNDO"), undo_result=after).result
    undo_insert = apply_public_command(after, UndoCommand(command_type="UNDO"), undo_result=before).result
    assert [d.model_dump(mode="json") for d in confirmed.days[0].activities[0].source_details] == DETAILS
    return {key: value.model_dump(mode="json") for key, value in (
        ("before", before), ("inserted", after), ("confirmed", confirmed), ("undo", undone), ("undo_insert", undo_insert))}


@pytest.mark.parametrize("field,value", [("source_details", DETAILS), ("name", "另一景点"), ("meal_role", "LUNCH"),
    ("day_index", True), ("position", "0")])
def test_wire_accepts_only_source_token_and_strict_position_not_client_visit_fields(field, value):
    payload = dict(command_type="ALTERNATIVE_INSERT", day_index=1, position=0,
                   alternative_token="fixed-alternative-token-00001")
    assert isinstance(TypeAdapter(TripUnderstandingCommand).validate_python(payload), AlternativeInsertCommand)
    with pytest.raises(ValidationError):
        TypeAdapter(TripUnderstandingCommand).validate_python({**payload, field: value})


@pytest.mark.asyncio
async def test_fixed_provider_details_are_copied_without_selecting_any_other_option():
    current = (await build_alternative_insert_result()).public_result
    original = current.model_dump(mode="json")
    command = insertion(current)
    changed = apply_public_command(current, command)
    assert current.model_dump(mode="json") == original
    card = changed.result.days[0].activities[0]
    assert card.name == "青岚乐园" and card.status == "NEEDS_CONFIRMATION"
    assert [d.model_dump(mode="json") for d in card.source_details] == DETAILS
    assert not changed.result.days[1].activities
    assert [d.alternatives[0].name for d in changed.result.days] == ["青岚乐园", "晨光公园"]
    assert not any(d.choice_selections for d in changed.result.days)
    assert changed.result.days[0].alternatives[0].activity_token != command.alternative_token
    assert changed.inserted_token == card.activity_token
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(changed.result, command)
    restored = apply_public_command(changed.result, UndoCommand(command_type="UNDO"), undo_result=current).result
    assert not any(day.activities for day in restored.days)
    assert [d.model_dump(mode="json") for d in restored.days[0].alternatives[0].source_details] == DETAILS


@pytest.mark.asyncio
async def test_existing_public_meal_time_and_details_are_copied_as_one_visit():
    current = (await build_alternative_insert_result()).public_result
    alternative = current.days[0].alternatives[0]
    # This tests the public command contract, not model extraction of meals.
    alternative.category = "餐饮"
    alternative.meal_role = "LUNCH"
    alternative.start_time, alternative.end_time = "13:00", "13:40"
    alternative.visit_duration_minutes = 40
    alternative.timing_source = "USER"
    alternative.locked = alternative.fixed_commitment = True
    changed = apply_public_command(current, insertion(current)).result
    card = changed.days[0].activities[0]
    for field in ("name", "city", "category", "meal_role", "start_time", "end_time", "visit_duration_minutes",
                  "timing_source", "locked", "fixed_commitment", "source_details"):
        assert getattr(card, field) == getattr(alternative, field)
    card.source_details[0].name = "独立编辑"
    assert current.days[0].alternatives[0].source_details[0].name == "云海航船"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["wrong_day", "missing_day", "missing_token", "duplicate_same_day", "duplicate_other_day", "position"])
async def test_wrong_day_stale_ambiguous_and_invalid_position_never_insert(case):
    current = (await build_alternative_insert_result()).public_result
    payload = insertion(current).model_dump()
    if case == "wrong_day":
        payload["day_index"] = 2
    elif case == "missing_day":
        payload["day_index"] = 3
    elif case == "missing_token":
        payload["alternative_token"] = "old-version-alternative-token-0001"
    elif case == "position":
        payload["position"] = 1
    else:
        current.days[0 if case == "duplicate_same_day" else 1].alternatives.append(current.days[0].alternatives[0].model_copy(deep=True))
    before = current.model_dump()
    with pytest.raises(CommandTargetChangedError):
        apply_public_command(current, AlternativeInsertCommand.model_validate(payload))
    assert current.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("count,allowed", [(159, True), (160, False)])
async def test_capacity_counts_actual_cards_and_never_silently_overflows(count, allowed):
    current = (await build_alternative_insert_result()).public_result
    current.days[0].activities = [ActivityCardView(activity_token=f"capacity-card-token-{index:06d}",
        name=f"固定地点{index}", category="景点", area_or_address="地点待确认", status="NEEDS_CONFIRMATION",
        available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"]) for index in range(count)]
    command = insertion(current, position=count)
    if allowed:
        assert len(apply_public_command(current, command).result.days[0].activities) == 160
    else:
        with pytest.raises(CommandTargetChangedError):
            apply_public_command(current, command)
        assert len(current.days[0].activities) == 160


@pytest.mark.asyncio
async def test_legacy_missing_alternative_token_remains_readable_and_manual_insert_compatible():
    raw = (await build_alternative_insert_result()).public_result.model_dump(mode="json")
    for day in raw["days"]:
        for item in day["alternatives"]:
            item.pop("activity_token")
    current = UserFacingTripResult.model_validate(raw)
    refresh_choice_selection_tokens(current.days, {})
    assert all(d.alternatives[0].activity_token is None for d in current.days)
    added = apply_public_command(current, ActivityInsertCommand(command_type="ACTIVITY_INSERT",
        day_index=1, position=0, name="手动地点")).result
    assert added.days[0].activities[0].name == "手动地点"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_saved_source_add_refresh_undo_etag_and_owner_boundary(kind):
    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner", idempotency_key="optional-details-create", now=now)
        job = await repository.claim_next(worker_id="optional-details-fixed", now=now, lease_seconds=60)
        await repository.complete_job(job, await build_alternative_insert_result(), now=now)

        async def read():
            resource = await repository.authorize(created.accepted.public_resource_id, capability_hash=None,
                user_id="experience-owner", now=now)
            return resource, await repository.get_result(resource)

        resource, stored = await read()
        with pytest.raises(ResourceAccessDeniedError):
            await repository.authorize(resource.public_resource_id, capability_hash=None, user_id="not-the-owner", now=now)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, insertion(stored.result, day=2), expected_etag=stored.opaque_etag,
                idempotency_key="optional-details-cross-day", now=now)
        command, initial_etag, original_result_id = insertion(stored.result), stored.opaque_etag, resource.current_result_id
        applied = await service.apply_command(resource, command, expected_etag=initial_etag,
            idempotency_key="optional-details-add", now=now)
        replay = await service.apply_command(resource, command, expected_etag=initial_etag,
            idempotency_key="optional-details-add", now=now)
        assert replay.replayed and replay.opaque_etag == applied.opaque_etag
        resource, stored = await read()
        assert resource.current_result_id != original_result_id
        assert len(stored.result.days[0].activities) == 1
        card = stored.result.days[0].activities[0]
        assert card.status == "NEEDS_CONFIRMATION" and [d.model_dump(mode="json") for d in card.source_details] == DETAILS
        with pytest.raises(RevisionConflictError):
            await service.apply_command(resource, command, expected_etag=initial_etag,
                idempotency_key="optional-details-stale-etag", now=now)
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key="optional-details-stale-token", now=now)
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="optional-details-undo", now=now)
        resource, stored = await read()
        assert not any(day.activities for day in stored.result.days)
        assert [d.model_dump(mode="json") for d in stored.result.days[0].alternatives[0].source_details] == DETAILS
        if kind == "postgres":
            old = json.loads(await repository._pool.fetchval(
                "SELECT public_json FROM trip_understanding_results WHERE result_id=$1", original_result_id))
            assert not old["days"][0]["activities"]  # immutable old snapshot kept
            assert old["days"][0]["alternatives"][0]["activity_token"] == command.alternative_token
        # Erasing the private source preserves already structured visits and
        # must not require decrypting/reloading the source to add one later.
        await service.delete_source(resource, user_id="experience-owner", idempotency_key="optional-details-source-delete", now=now)
        resource, stored = await read()
        await service.apply_command(resource, insertion(stored.result), expected_etag=stored.opaque_etag,
            idempotency_key="optional-details-add-after-source-delete", now=now)
        resource, stored = await read()
        assert [d.model_dump(mode="json") for d in stored.result.days[0].activities[0].source_details] == DETAILS


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_saved_candidate_confirmation_and_undo_keep_copied_details(kind):
    from app.trip_understanding.candidates import CandidatePlace, GCJ02Position, issue_candidate

    async with repository_for(kind) as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner", idempotency_key="optional-confirm-create", now=now)
        job = await repository.claim_next(worker_id="optional-confirm-fixed", now=now, lease_seconds=60)
        await repository.complete_job(job, await build_alternative_insert_result(), now=now)

        async def read():
            resource = await repository.authorize(created.accepted.public_resource_id, capability_hash=None,
                user_id="experience-owner", now=now)
            return resource, await repository.get_result(resource)

        resource, stored = await read()
        await service.apply_command(resource, insertion(stored.result), expected_etag=stored.opaque_etag,
            idempotency_key="optional-confirm-add", now=now)
        resource, stored = await read()
        selected = CandidatePlace(canonical_place_id="fixed-optional-parent-identity", name="青岚乐园", city="上海",
            category="景点", area_or_address="固定验证地址", position=GCJ02Position(longitude=121.48, latitude=31.22))
        candidate = issue_candidate(selected, public_resource_id=resource.public_resource_id,
            activity_token=stored.result.days[0].activities[0].activity_token, expected_etag=stored.opaque_etag, now=now)
        await service.apply_command(resource, PlaceConfirmCommand(command_type="PLACE_CONFIRM",
            activity_token=stored.result.days[0].activities[0].activity_token, candidate_token=candidate.candidate_token),
            expected_etag=stored.opaque_etag, idempotency_key="optional-confirm-identity", now=now)
        resource, stored = await read()
        assert stored.result.days[0].activities[0].status == "READY"
        assert [d.model_dump(mode="json") for d in stored.result.days[0].activities[0].source_details] == DETAILS
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="optional-confirm-undo", now=now)
        resource, stored = await read()
        assert stored.result.days[0].activities[0].status == "NEEDS_CONFIRMATION"
        assert [d.model_dump(mode="json") for d in stored.result.days[0].activities[0].source_details] == DETAILS


@pytest.mark.asyncio
async def test_postgres_old_snapshot_without_alternative_tokens_remains_readable():
    class LegacyResult(UserFacingTripResult):
        @model_serializer(mode="wrap")
        def old_shape(self, handler):
            payload = handler(self)
            for day in payload["days"]:
                for item in day["alternatives"]:
                    item.pop("activity_token", None)
                    item.pop("source_details", None)
            return payload

    async with repository_for("postgres") as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner", idempotency_key="old-alternative-create", now=now)
        job = await repository.claim_next(worker_id="old-alternative-fixed", now=now, lease_seconds=60)
        output = await build_alternative_insert_result()
        output.public_result = LegacyResult.model_validate(output.public_result.model_dump())
        await repository.complete_job(job, output, now=now)
        resource = await repository.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
        stored = await repository.get_result(resource)
        original = json.loads(await repository._pool.fetchval(
            "SELECT public_json FROM trip_understanding_results WHERE result_id=$1", resource.current_result_id))
        assert all("activity_token" not in item for day in original["days"] for item in day["alternatives"])
        assert all(item.activity_token is None and item.source_details == [] for day in stored.result.days for item in day.alternatives)


@pytest.mark.asyncio
async def test_postgres_159_to_160_add_then_overflow_refused_and_undo_restores_capacity():
    # A controlled current snapshot represents prior manual card additions;
    # this is persisted command capacity, not a semantic extraction claim.
    output = await build_alternative_insert_result()
    output.public_result.days[0].activities = [ActivityCardView(activity_token=f"saved-capacity-card-{index:06d}",
        name=f"已编辑地点{index}", category="景点", area_or_address="地点待确认", status="NEEDS_CONFIRMATION",
        available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"]) for index in range(159)]
    async with repository_for("postgres") as repository:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repository)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL", "source": {"type": "TEXT", "text": SOURCE}}),
            owner_user_id="experience-owner", idempotency_key="optional-capacity-create", now=now)
        job = await repository.claim_next(worker_id="optional-capacity-fixed", now=now, lease_seconds=60)
        await repository.complete_job(job, output, now=now)

        async def read():
            resource = await repository.authorize(created.accepted.public_resource_id, capability_hash=None, user_id="experience-owner", now=now)
            return resource, await repository.get_result(resource)

        resource, stored = await read()
        await service.apply_command(resource, insertion(stored.result, position=159), expected_etag=stored.opaque_etag,
            idempotency_key="optional-capacity-160", now=now)
        resource, stored = await read()
        assert len(stored.result.days[0].activities) == 160
        assert [d.model_dump(mode="json") for d in stored.result.days[0].activities[-1].source_details] == DETAILS
        full_id = resource.current_result_id
        with pytest.raises(CommandTargetChangedError):
            await service.apply_command(resource, insertion(stored.result, position=160), expected_etag=stored.opaque_etag,
                idempotency_key="optional-capacity-overflow", now=now)
        resource, stored = await read()
        assert resource.current_result_id == full_id and len(stored.result.days[0].activities) == 160
        await service.apply_command(resource, UndoCommand(command_type="UNDO"), expected_etag=stored.opaque_etag,
            idempotency_key="optional-capacity-undo", now=now)
        resource, stored = await read()
        assert len(stored.result.days[0].activities) == 159
