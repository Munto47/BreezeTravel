"""Supplementary exports identify existing notes without merging source occurrences."""
from datetime import datetime, timezone

import pytest

from app.trip_understanding.models import ActivityMoveCommand, CreateFullRequest, RedoCommand, UndoCommand
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.test_experience_v3_journey import repository_for
from tests.test_optional_anonymous_meal_notes import SOURCE, DropMealNotes, build_result, meal_plan


async def save_result(repo, source, output, now):
    service = TripUnderstandingApplicationService(repo)
    created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
        "source": {"type": "TEXT", "text": source}}), owner_user_id="experience-owner",
        idempotency_key="source-notes-create", now=now)
    job = await repo.claim_next(worker_id="source-notes", now=now, lease_seconds=60)
    await repo.complete_job(job, output, now=now)
    return service, created.accepted.public_resource_id


async def read(repo, public_id, now):
    resource = await repo.authorize(public_id, capability_hash=None, user_id="experience-owner", now=now)
    return resource, await repo.get_result(resource), await repo.get_supplementary_view(resource, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("variant", ["meal", "untyped", "cancelled"])
async def test_source_note_identity_survives_saved_edits_and_disappears_with_source(kind, variant):
    plan = meal_plan(role="EXCLUDED" if variant == "cancelled" else "OPTIONAL")
    if variant == "untyped":
        plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"meal_role": None, "category_hint": None})
            if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    output, _ = await build_result(plan=plan)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service, public_id = await save_result(repo, SOURCE, output, now)
        resource, stored, supplementary = await read(repo, public_id, now)
        expected_note, = stored.result.days[0].source_notes
        item, = supplementary.days[0].items
        assert item.source_note_id == expected_note.note_id
        assert item.role == ("EXCLUDED" if variant == "cancelled" else "OPTIONAL")
        assert item.name == "晚上吃饭"
        for key, command in [
            ("move", ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=stored.result.days[0].activities[-1].activity_token, target_day_index=1, target_position=0)),
            ("undo", UndoCommand(command_type="UNDO")),
            ("redo", RedoCommand(command_type="REDO")),
        ]:
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f"source-note-{key}", now=now)
            resource, stored, supplementary = await read(repo, public_id, now)
            assert stored.result.days[0].source_notes == [expected_note]
            assert supplementary.days[0].items[0].source_note_id == expected_note.note_id
        await repo.delete_source(resource, user_id="experience-owner", idempotency_key="source-notes-delete",
            request_hash="b" * 64, now=now)
        _, stored, supplementary = await read(repo, public_id, now)
        assert stored.result.days[0].source_notes == [expected_note]
        assert supplementary.status == "DELETED" and not supplementary.days


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_same_words_at_distinct_source_positions_keep_distinct_export_identities(kind):
    source = SOURCE + "\n晚上吃饭。"
    plan = meal_plan(source)
    first = next(mention for mention in plan.mentions if mention.role == "OPTIONAL")
    start = source.rindex("晚上吃饭")
    second = first.model_copy(update={"mention_id": "later-optional-meal", "span_start": start,
        "span_end": start + len(first.raw_text), "sequence_index": 10})
    plan = plan.model_copy(update={"mentions": [*plan.mentions, second]})
    output, _ = await build_result(source, plan=plan)
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        _, public_id = await save_result(repo, source, output, now)
        _, stored, supplementary = await read(repo, public_id, now)
        notes = stored.result.days[0].source_notes
        assert len(notes) == 2 and notes[0].text == notes[1].text
        assert notes[0].note_id != notes[1].note_id
        assert [item.source_note_id for item in supplementary.days[0].items] == [note.note_id for note in notes]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_source_content_without_visible_note_does_not_get_a_deduplication_reference(kind):
    output, _ = await build_result(projector=DropMealNotes())
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        _, public_id = await save_result(repo, SOURCE, output, now)
        _, stored, supplementary = await read(repo, public_id, now)
        assert not stored.result.days[0].source_notes
        item, = supplementary.days[0].items
        assert item.name == "晚上吃饭" and item.source_note_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_claimed_anonymous_trip_keeps_the_note_reference(kind):
    output, _ = await build_result()
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": SOURCE}}), owner_user_id=None, capability_hash="a" * 64,
            idempotency_key="anonymous-source-notes", now=now)
        job = await repo.claim_next(worker_id="anonymous-source-notes", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        claimed = await service.claim_demo(created.accepted.public_resource_id, capability_hash="a" * 64,
            user_id="experience-owner", idempotency_key="claim-source-notes", now=now)
        _, stored, supplementary = await read(repo, claimed.claimed.public_resource_id, now)
        note, = stored.result.days[0].source_notes
        item, = supplementary.days[0].items
        assert item.source_note_id == note.note_id
