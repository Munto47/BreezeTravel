"""Supplementary exports identify existing notes without merging source occurrences."""
from datetime import datetime, timezone

import pytest

from app.trip_understanding.models import ActivityMoveCommand, CreateFullRequest, RedoCommand, UndoCommand
from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.readback import _retained_alternative_ids
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


async def named_source_output():
    source = ("北京。\nDay1：青溪公园。如果下雨，把青溪公园替换成星河博物馆。\n"
              "Day2：青溪桥。若有余力可去星河博物馆。\n其他说明：不去远山乐园。")
    draft = SemanticDraft.model_validate(dict(destination="北京", activities=[
        dict(source_quote="青溪公园", place_name="青溪公园", role="PLANNED", day_index=1),
        dict(source_quote="星河博物馆", place_name="星河博物馆", role="OPTIONAL", day_index=1,
            conditional_replacement=dict(target_quote="青溪公园", target_occurrence=1,
                condition_quote="如果下雨", evidence="青溪公园。如果下雨，把青溪公园替换成星河博物馆")),
        dict(source_quote="青溪桥", place_name="青溪桥", role="PLANNED", day_index=2),
        dict(source_quote="星河博物馆", occurrence=2, place_name="星河博物馆", role="OPTIONAL", day_index=2),
        dict(source_quote="远山乐园", place_name="远山乐园", role="EXCLUDED", day_index=None),
    ]))
    plan = proposal_from_draft(source, draft, allow_partial=True)
    output, _ = await build_result(source, plan=plan)
    return source, output


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("legacy_occurrence", [False, True])
async def test_named_alternative_identity_is_source_bound_and_survives_token_refresh(kind, legacy_occurrence):
    source, output = await named_source_output()
    if legacy_occurrence:
        for day in output.public_result.days:
            for alternative in day.alternatives:
                alternative.source_occurrence_id = None
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service, public_id = await save_result(repo, source, output, now)
        resource, stored, supplementary = await read(repo, public_id, now)
        alternatives = [day.alternatives[0] for day in stored.result.days]
        assert alternatives[0].name == alternatives[1].name
        assert alternatives[0].alternative_id != alternatives[1].alternative_id
        assert alternatives[0].replacement_condition == "如果下雨"
        assert alternatives[0].replaces_name == "青溪公园"

        def references(view):
            return [item.source_alternative_id for day in view.days if day.day_index is not None
                for item in day.items if item.role == "OPTIONAL"]

        expected = [alternative.alternative_id for alternative in alternatives]
        assert references(supplementary) == expected
        old_token = alternatives[0].activity_token
        for key, command in [
            ("move", ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=stored.result.days[1].activities[0].activity_token, target_day_index=1, target_position=1)),
            ("undo", UndoCommand(command_type="UNDO")), ("redo", RedoCommand(command_type="REDO")),
        ]:
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f"named-alternative-{key}", now=now)
            resource, stored, supplementary = await read(repo, public_id, now)
            assert references(supplementary) == expected
            assert stored.result.days[0].alternatives[0].replacement_condition == "如果下雨"
        assert stored.result.days[0].alternatives[0].activity_token != old_token


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_claimed_trip_keeps_alternative_and_undated_note_identity_after_edit(kind):
    source, output = await named_source_output()
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": source}}), owner_user_id=None, capability_hash="a" * 64,
            idempotency_key="anonymous-named-source", now=now)
        job = await repo.claim_next(worker_id="anonymous-named-source", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        claimed = await service.claim_demo(created.accepted.public_resource_id, capability_hash="a" * 64,
            user_id="experience-owner", idempotency_key="claim-named-source", now=now)
        public_id = claimed.claimed.public_resource_id
        assert public_id != created.accepted.public_resource_id
        resource, stored, supplementary = await read(repo, public_id, now)
        note, = stored.result.days[0].source_notes
        alternatives = [day.alternatives[0] for day in stored.result.days]
        expected_ids = [item.alternative_id for item in alternatives]
        old_token = alternatives[0].activity_token
        await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
            activity_token=stored.result.days[1].activities[0].activity_token, target_day_index=1, target_position=1),
            expected_etag=stored.opaque_etag, idempotency_key="claimed-named-move", now=now)
        _, stored, supplementary = await read(repo, public_id, now)
        assert stored.result.days[0].alternatives[0].activity_token != old_token
        assert [item.source_alternative_id for day in supplementary.days for item in day.items
            if item.role == "OPTIONAL"] == expected_ids
        excluded, = [item for day in supplementary.days if day.day_index is None for item in day.items]
        assert excluded.source_note_id == note.note_id
        assert stored.result.days[0].source_notes == [note]
        assert note.text == "已取消（原文未指定 Day）：远山乐园"
        assert stored.result.days[0].alternatives[0].replacement_condition == "如果下雨"
        assert stored.result.days[0].alternatives[0].replaces_name == "青溪公园"
        if kind == "postgres":
            import json
            pool = await repo._get_pool()
            proposal = await pool.fetchval("""SELECT r.proposal_json FROM trip_understanding_revisions r
                JOIN trip_understandings u ON u.understanding_id=r.understanding_id AND u.current_revision=r.revision
                WHERE u.public_resource_id=$1""", public_id)
            assert "retained_source_plan" in json.loads(proposal)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("variant", ["exact", "ordinary_day_note", "duplicate_id", "different_identity"])
async def test_undated_exclusion_only_references_its_unique_explicit_undated_note(kind, variant):
    source, output = await named_source_output()
    note, = output.public_result.days[0].source_notes
    assert note.text == "已取消（原文未指定 Day）：远山乐园"
    expected = note.note_id
    if variant == "ordinary_day_note":
        note.text = "已取消：远山乐园"
    elif variant == "duplicate_id":
        output.public_result.days[1].source_notes.append(note.model_copy())
    elif variant == "different_identity":
        note.note_id = "another-source-note"
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        _, public_id = await save_result(repo, source, output, now)
        _, stored, supplementary = await read(repo, public_id, now)
        item, = [item for day in supplementary.days if day.day_index is None for item in day.items]
        assert item.name == "远山乐园" and item.role == "EXCLUDED"
        assert item.source_note_id == (expected if variant == "exact" else None)
        assert item.source_alternative_id is None
        assert len(stored.result.days[0].source_notes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["name_only", "different_source", "ambiguous", "other_day"])
async def test_same_name_does_not_establish_alternative_export_identity(variant):
    from app.trip_understanding.source_occurrence import source_occurrence_id
    source, output = await named_source_output()
    activity = next(item for item in output.activities if item.compiled.mention.role == "OPTIONAL")
    mention = activity.compiled.mention
    row = dict(day_index=mention.day_index, sequence_index=mention.sequence_index, role="OPTIONAL",
        mention_text=mention.raw_text, atomic_place_name=mention.atomic_place_name,
        public_activity_token=activity.compiled.public_activity_token, source_occurrence_id=source_occurrence_id(source, mention))
    result = output.public_result
    alternative = result.days[0].alternatives[0]
    if variant == "name_only":
        alternative.activity_token = "unrelated-public-token-00000"
        alternative.alternative_id = "unrelated-alternative"
        alternative.source_occurrence_id = None
    elif variant == "different_source":
        alternative.source_occurrence_id = "different-source-occurrence"
    elif variant == "ambiguous":
        result.days[0].alternatives.append(alternative.model_copy(update={"alternative_id": "second-match"}))
    else:
        result.days[0].alternatives = []
        result.days[1].alternatives.append(alternative)
    assert _retained_alternative_ids([row], result) == {}
