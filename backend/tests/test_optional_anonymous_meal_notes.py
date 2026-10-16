"""Optional anonymous arrangements retain their source role across public projections."""
from datetime import datetime, timezone

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, proposal_from_draft
from app.trip_understanding.models import (
    ActivityMoveCommand, CreateFullRequest, DestinationBasis, RedoCommand, ResolvedActivity, UndoCommand,
)
from app.trip_understanding.pipeline import EvidenceCompiler, PublicResultProjector, TripUnderstandingPipeline, _projection_omissions
from app.trip_understanding.service import TripUnderstandingApplicationService
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_anonymous_meal_context import fixture
from tests.test_experience_v3_journey import repository_for


SOURCE = "广州\nDay1\n先到沙面。\n午餐：吃面。\n如果散步后还有空，晚上吃饭，门店待选。\n随后去越秀公园。"


def meal_plan(source=SOURCE, *, day=1, role="OPTIONAL", quote="晚上吃饭", evidence=None):
    return proposal_from_draft(source, SemanticDraft.model_validate({"destination": "广州", "activities": [
        {"source_quote": "沙面", "place_name": "沙面", "role": "PLANNED", "day_index": 1},
        {"source_quote": "吃面", "place_name": None, "role": "PLANNED", "day_index": 1,
         "category": "餐饮", "meal_role": "LUNCH"},
        {"source_quote": quote, "place_name": None, "role": role, "day_index": day,
         "category": "餐饮", "meal_role": "DINNER", "role_evidence": evidence},
        {"source_quote": "越秀公园", "place_name": "越秀公园", "role": "PLANNED", "day_index": 1},
    ]}), allow_partial=True)


class RecordingPlaces(FixedReplayPlaces):
    def __init__(self):
        self.names = []

    async def resolve(self, **kwargs):
        self.names.append(kwargs["atomic_place_name"])
        return await super().resolve(**kwargs)


async def build_result(source=SOURCE, *, plan=None, projector=None):
    places = RecordingPlaces()
    output = await TripUnderstandingPipeline(None, places, projector=projector, relative_only=True).run(
        source, prepared_plan=plan or meal_plan(source))
    return output, places


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["PLANNED", "OPTIONAL", "REFERENCE", "EXCLUDED"])
async def test_guangzhou_optional_meal_preserves_condition_position_and_role(role):
    output, places = await build_result(plan=meal_plan(role=role, evidence="如果散步后还有空，晚上吃饭，门店待选"))
    day = output.public_result.days[0]
    assert [card.name for card in day.activities] == ["沙面", "越秀公园"]
    assert places.names == ["沙面", "越秀公园"]
    assert [slot.meal_role for slot in day.meal_slots] == (["LUNCH", "DINNER"] if role == "PLANNED" else ["LUNCH"])
    assert day.alternatives == []
    if role == "OPTIONAL":
        note, = day.source_notes
        assert note.text == "备选用餐：如果散步后还有空，晚上吃饭，门店待选"
        assert note.position == 1
        assert output.public_result.coverage.complete
    elif role == "EXCLUDED":
        assert [note.text for note in day.source_notes] == ["已取消：晚上吃饭"]
    else:
        assert day.source_notes == []


@pytest.mark.asyncio
async def test_unbound_role_evidence_is_not_displayed_as_a_meal_condition():
    output, _ = await build_result(plan=meal_plan(evidence="若酒店提供免费晚餐"))
    assert output.public_result.days[0].source_notes == []
    assert output.public_result.coverage.complete is False
    assert output.public_result.coverage.unprocessed_count >= 1


@pytest.mark.asyncio
async def test_saved_shanghai_semantics_keep_branch_meal_without_promoting_it():
    saved = fixture()
    plan = proposal_from_draft(saved["source"], SemanticDraft.model_validate_json(saved["response"]), allow_partial=True)
    output, _ = await build_result(saved["source"], plan=plan)
    day = output.public_result.days[2]
    notes = [note for note in day.source_notes if "南京西路周边" in note.text]
    assert len(notes) == 1
    assert notes[0].text.startswith("方案A · 备选用餐：")
    assert "南京西路周边吃饭" in notes[0].text
    assert day.meal_slots == [] and day.activities == []
    assert [len(day.meal_slots) for day in output.public_result.days[:2]] == [1, 2]
    assert not output.public_result.coverage.complete


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [True, False])
async def test_repeated_quote_deduplicates_only_same_source_position_and_branch(typed):
    source = SOURCE.replace("随后去越秀公园。", "随后去越秀公园。\n晚上吃饭。")
    plan = meal_plan(source)
    if not typed:
        plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": None, "meal_role": None})
            if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    meal = next(item for item in plan.mentions if item.role == "OPTIONAL")
    repeated = meal.model_copy(update={"mention_id": "duplicate", "sequence_index": meal.sequence_index + 1})
    other_branch = meal.model_copy(update={"mention_id": "other-branch", "branch_id": "b", "branch_label": "方案 B"})
    start = source.rindex("晚上吃饭")
    second_visit = meal.model_copy(update={"mention_id": "second-meal", "span_start": start,
        "span_end": start + len(meal.raw_text), "sequence_index": 10})
    plan = plan.model_copy(update={"mentions": [*plan.mentions, repeated, other_branch, second_visit]})
    output, _ = await build_result(source, plan=plan)
    notes = output.public_result.days[0].source_notes
    label = "备选用餐" if typed else "原文备选"
    assert [(note.text, note.position) for note in notes] == [
        (f"{label}：晚上吃饭", 1), (f"方案 B · {label}：晚上吃饭", 1), (f"{label}：晚上吃饭", 2)]
    assert output.public_result.coverage.complete


@pytest.mark.asyncio
async def test_optional_meal_keeps_bounded_preference_and_internal_role_without_new_card():
    source = SOURCE.replace("如果散步后还有空，晚上吃饭，门店待选。", "晚餐：晚上吃饭，8分饱，不吃辣；下午：另去散步。")
    plan = meal_plan(source)
    parent = plan.mentions[0]
    plan = plan.model_copy(update={"mentions": [item.model_copy(update={
        "parent_mention_id": parent.mention_id, "relation_type": "INTERNAL_DETAIL"})
        if item.role == "OPTIONAL" else item for item in plan.mentions]})
    output, places = await build_result(source, plan=plan)
    day = output.public_result.days[0]
    assert [(note.text, note.position) for note in day.source_notes] == [("备选用餐：晚餐：晚上吃饭，8分饱，不吃辣", 1)]
    assert [slot.meal_role for slot in day.meal_slots] == ["LUNCH"]
    assert places.names == ["沙面", "越秀公园"]


class DropMealNotes(PublicResultProjector):
    def project(self, *args, **kwargs):
        result = super().project(*args, **kwargs)
        for day in result.days:
            day.source_notes = []
        return result


@pytest.mark.asyncio
async def test_optional_meal_without_public_exit_stays_unfinished():
    output, _ = await build_result(projector=DropMealNotes())
    assert output.public_result.coverage.complete is False
    assert output.public_result.coverage.unprocessed_count == 1
    assert any(issue.category == "PUBLIC_PROJECTION_OMISSION" and issue.field == "source_notes"
               for issue in output.proposal.diagnostics)


@pytest.mark.asyncio
@pytest.mark.parametrize("quote", ["晚上吃饭", "晚餐"])
async def test_optional_meal_with_missing_model_category_keeps_source_note_and_order(quote):
    source = ("广州两日游。\nDay1：先到沙面，再去陈家祠。\n"
              "晚餐：可选，如果还有空，晚上吃饭，门店待选，不吃辣。\nDay2：先到越秀公园，再去广州塔。")
    rows = [{"source_quote": name, "place_name": name, "role": "PLANNED", "day_index": day}
            for name, day in [("沙面", 1), ("陈家祠", 1), ("越秀公园", 2), ("广州塔", 2)]]
    rows.insert(2, {"source_quote": quote, "place_name": None, "role": "OPTIONAL", "day_index": 1})
    plan = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "广州", "activities": rows}),
                               allow_partial=True)
    meal = plan.mentions[2]
    assert meal.category_hint == "地点" and meal.meal_role is None
    output, places = await build_result(source, plan=plan)
    assert [[card.name for card in day.activities] for day in output.public_result.days] == [
        ["沙面", "陈家祠"], ["越秀公园", "广州塔"]]
    assert places.names == ["沙面", "陈家祠", "越秀公园", "广州塔"]
    note, = output.public_result.days[0].source_notes
    assert note.text == "原文备选：晚餐：可选，如果还有空，晚上吃饭，门店待选，不吃辣"
    assert note.position == 2
    assert all(not day.meal_slots for day in output.public_result.days)
    assert output.public_result.days[1].source_notes == []
    assert output.proposal.mentions[2].role == "OPTIONAL" and output.proposal.mentions[2].day_index == 1
    assert output.public_result.coverage.complete
    omitted, _ = await build_result(source, plan=plan, projector=DropMealNotes())
    assert not omitted.public_result.coverage.complete
    assert omitted.public_result.coverage.unprocessed_count == 1


@pytest.mark.asyncio
async def test_untyped_optional_meal_keeps_an_independent_day_and_condition():
    source = "广州\nDay1：沙面。\nDay2：越秀公园。\n午餐：若行程允许，可以用餐，门店待选，不吃辣。\n随后去广州塔。"
    plan = proposal_from_draft(source, SemanticDraft.model_validate({"destination": "广州", "activities": [
        {"source_quote": "沙面", "place_name": "沙面", "role": "PLANNED", "day_index": 1},
        {"source_quote": "越秀公园", "place_name": "越秀公园", "role": "PLANNED", "day_index": 2},
        {"source_quote": "用餐", "place_name": None, "role": "OPTIONAL", "day_index": 2},
        {"source_quote": "广州塔", "place_name": "广州塔", "role": "PLANNED", "day_index": 2},
    ]}), allow_partial=True)
    output, places = await build_result(source, plan=plan)
    assert output.public_result.days[0].source_notes == []
    note, = output.public_result.days[1].source_notes
    assert note.text == "原文备选：午餐：若行程允许，可以用餐，门店待选，不吃辣"
    assert note.position == 1
    assert all(not day.meal_slots for day in output.public_result.days)
    assert places.names == ["沙面", "越秀公园", "广州塔"]


@pytest.mark.asyncio
@pytest.mark.parametrize("clause,quote", [
    ("如果还有空，饭后散步。", "饭后散步"),
    ("如果还有空，晚餐后散步。", "晚餐"),
    ("如果还有空，吃饭后散步。", "吃饭"),
    ("如果还有空，晚餐后的自由活动。", "自由活动"),
    ("晚餐：不吃饭。", "吃饭"),
    ("晚餐：不要去吃饭。", "吃饭"),
    ("晚餐：不在这里吃饭。", "吃饭"),
    ("晚餐：不要在晚上吃饭。", "吃饭"),
    ("晚餐：取消晚上吃饭。", "晚上吃饭"),
    ("晚餐：参观吃饭博物馆。", "吃饭"),
    ("如果还有空，看看饭后公园。", "饭后公园"),
])
async def test_untyped_anonymous_actions_keep_neutral_source_notes_without_becoming_meals(clause, quote):
    source = SOURCE.replace("如果散步后还有空，晚上吃饭，门店待选。", clause)
    plan = meal_plan(source, quote=quote, evidence=clause.rstrip("。"))
    plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": "地点", "meal_role": None})
        if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    output, places = await build_result(source, plan=plan)
    note, = output.public_result.days[0].source_notes
    assert note.text == "原文备选：" + clause.rstrip("。")
    assert note.position == 1
    assert output.public_result.days[0].alternatives == []
    assert [slot.meal_role for slot in output.public_result.days[0].meal_slots] == ["LUNCH"]
    assert places.names == ["沙面", "越秀公园"]


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", [True, False])
async def test_oversized_optional_meal_is_not_silently_truncated(typed):
    quote = "晚上吃饭，" + "餐饮偏好" * 160
    source = SOURCE.replace("晚上吃饭", quote)
    plan = meal_plan(source, quote=quote)
    if not typed:
        plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": None, "meal_role": None})
            if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    output, _ = await build_result(source, plan=plan)
    assert output.public_result.days[0].source_notes == []
    assert output.public_result.coverage.complete is False
    assert output.public_result.coverage.unprocessed_count >= 1


@pytest.mark.parametrize("source_text", ["", SOURCE.replace("晚上吃饭", "夜间吃饭")])
@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.parametrize("day", [1, None])
def test_anonymous_optional_without_a_valid_source_anchor_is_a_projection_omission(source_text, typed, day):
    plan = meal_plan(day=day)
    if not typed:
        plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": None, "meal_role": None})
            if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    compiled, _, _ = EvidenceCompiler().compile(SOURCE, plan)
    result = PublicResultProjector().project("广州", DestinationBasis.EXPLICIT,
        [ResolvedActivity(compiled=item, resolution_status="NOT_ELIGIBLE") for item in compiled],
        include_alternatives=True, source_text=source_text)
    assert result.days[0].source_notes == []
    issues = _projection_omissions(plan, compiled, result, source_text=source_text)
    assert [(issue.category, issue.field) for issue in issues] == [("PUBLIC_PROJECTION_OMISSION", "source_notes")]


@pytest.mark.parametrize("typed", [True, False])
def test_legacy_projector_without_source_text_keeps_saved_anonymous_quote(typed):
    plan = meal_plan()
    if not typed:
        plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": None, "meal_role": None})
            if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    compiled, _, _ = EvidenceCompiler().compile(SOURCE, plan)
    result = PublicResultProjector().project("广州", DestinationBasis.EXPLICIT,
        [ResolvedActivity(compiled=item, resolution_status="NOT_ELIGIBLE") for item in compiled],
        include_alternatives=True)
    note, = result.days[0].source_notes
    assert note.text == ("备选用餐" if typed else "原文备选") + "：晚上吃饭"
    assert _projection_omissions(plan, compiled, result) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("day", [1, None])
@pytest.mark.parametrize("length", [400, 700])
async def test_optional_heading_with_oversized_source_block_stays_unfinished(day, length):
    source = SOURCE.replace("如果散步后还有空，晚上吃饭，门店待选。", "晚餐：可选，" + "偏好" * length + "。")
    plan = meal_plan(source, day=day, quote="晚餐")
    plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": None, "meal_role": None})
        if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
    output, places = await build_result(source, plan=plan)
    assert output.public_result.days[0].source_notes == []
    assert not output.public_result.coverage.complete
    assert output.public_result.coverage.unprocessed_count == 1
    assert places.names == ["沙面", "越秀公园"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("typed", [True, False])
async def test_optional_meal_notes_survive_save_edit_undo_redo_source_deletion_and_share(kind, typed):
    async with repository_for(kind) as repo:
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": SOURCE}}), owner_user_id="experience-owner",
            idempotency_key="optional-meal-create", now=now)
        job = await repo.claim_next(worker_id="optional-meal-test", now=now, lease_seconds=60)
        plan = meal_plan(evidence="如果散步后还有空，晚上吃饭，门店待选")
        if not typed:
            plan = plan.model_copy(update={"mentions": [mention.model_copy(update={"category_hint": None, "meal_role": None})
                if mention.role == "OPTIONAL" else mention for mention in plan.mentions]})
        output, _ = await build_result(plan=plan)
        await repo.complete_job(job, output, now=now)

        async def read():
            resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
                user_id="experience-owner", now=now)
            return resource, await repo.get_result(resource)

        resource, stored = await read()
        expected = stored.result.days[0].source_notes
        assert len(expected) == 1
        assert expected[0].text.startswith("备选用餐：" if typed else "原文备选：")
        for key, command in [
            ("move", ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=stored.result.days[0].activities[-1].activity_token, target_day_index=1, target_position=0)),
            ("undo", UndoCommand(command_type="UNDO")),
            ("redo", RedoCommand(command_type="REDO")),
        ]:
            await service.apply_command(resource, command, expected_etag=stored.opaque_etag,
                idempotency_key=f"optional-meal-{key}", now=now)
            resource, stored = await read()
            assert stored.result.days[0].source_notes == expected
        await repo.delete_source(resource, user_id="experience-owner", idempotency_key="optional-meal-delete-source",
            request_hash="b" * 64, now=now)
        resource, stored = await read()
        assert stored.result.days[0].source_notes == expected
        share, _ = await repo.create_share(resource, "experience-owner", stored.result,
            idempotency_key="optional-meal-share", expires_in_days=1, signing_key="fixed-share-key", now=now)
        path, secret = share.share_url.split("#s=")
        ref = path.rsplit("/", 1)[1]
        session = await repo.exchange_share_secret(ref, secret, now=now)
        shared = await repo.read_share(ref, session.capability, now=now)
        assert shared.days[0].notes == [note.text for note in expected]
        assert len(shared.days[0].meal_arrangements) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
@pytest.mark.parametrize("typed", [True, False])
@pytest.mark.parametrize("with_places", [True, False])
async def test_undated_optional_meal_keeps_supplementary_exit(kind, typed, with_places):
    async with repository_for(kind) as repo:
        clause = "晚餐：可选，如果游览结束还有空且同行者愿意，再讨论在哪里吃饭，门店待选；同行者不吃辣、不吃香菜、不喝酒，优先有清淡菜品的店，实际点单前还要确认是否有适合所有人的选择；如没有合适选择就各自回去休息"
        source = SOURCE + "\n补充：\n" + clause + "。"
        plan = meal_plan(source)
        original = next(item for item in plan.mentions if item.role == "OPTIONAL")
        start = source.index("晚餐")
        undated = original.model_copy(update={"mention_id": "undated-meal", "day_index": None,
            "raw_text": "晚餐", "span_start": start, "span_end": start + 2,
            "branch_label": "方案 B", "branch_id": "b"})
        if not typed:
            undated = undated.model_copy(update={"category_hint": None, "meal_role": None})
        plan = plan.model_copy(update={"mentions": [*plan.mentions, undated] if with_places else [undated]})
        output, _ = await build_result(source, plan=plan)
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": source}}), owner_user_id="experience-owner",
            idempotency_key="undated-meal-create", now=now)
        job = await repo.claim_next(worker_id="undated-meal-test", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
            user_id="experience-owner", now=now)
        supplementary = await repo.get_supplementary_view(resource, now=now)
        expected = "方案 B · " + clause
        assert len(expected) > 80
        assert [(item.name, item.role) for day in supplementary.days if day.day_index is None
                for item in day.items] == [(expected, "OPTIONAL")]
        assert not any(clause in note.text for day in output.public_result.days for note in day.source_notes)
        assert output.public_result.coverage.complete is with_places
        assert output.public_result.coverage.unprocessed_count == 0
        if with_places:
            stored = await repo.get_result(resource)
            await service.apply_command(resource, ActivityMoveCommand(command_type="ACTIVITY_MOVE",
                activity_token=stored.result.days[0].activities[-1].activity_token, target_day_index=1, target_position=0),
                expected_etag=stored.opaque_etag, idempotency_key="undated-meal-move", now=now)
            resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
                user_id="experience-owner", now=now)
        assert await repo.get_supplementary_view(resource, now=now) == supplementary
        stored = await repo.get_result(resource)
        share, _ = await repo.create_share(resource, "experience-owner", stored.result,
            idempotency_key="undated-meal-share", expires_in_days=1, signing_key="fixed-share-key", now=now,
            supplementary=supplementary)
        path, secret = share.share_url.split("#s=")
        ref = path.rsplit("/", 1)[1]
        session = await repo.exchange_share_secret(ref, secret, now=now)
        shared = await repo.read_share(ref, session.capability, now=now)
        assert shared.unassigned_alternatives == [expected]
        await repo.delete_source(resource, user_id="experience-owner", idempotency_key="undated-meal-delete-source",
            request_hash="c" * 64, now=now)
        deleted = await repo.get_supplementary_view(resource, now=now)
        assert deleted.status == "DELETED" and deleted.days == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["memory", "postgres"])
async def test_oversized_undated_heading_is_not_returned_as_a_complete_supplementary_item(kind):
    async with repository_for(kind) as repo:
        source = SOURCE.replace("如果散步后还有空，晚上吃饭，门店待选。", "晚餐：可选，" + "餐饮偏好" * 300 + "。")
        plan = meal_plan(source, quote="晚餐", day=None)
        output, places = await build_result(source, plan=plan)
        now = datetime.now(timezone.utc)
        service = TripUnderstandingApplicationService(repo)
        created = await service.create_full(CreateFullRequest.model_validate({"mode": "FULL",
            "source": {"type": "TEXT", "text": source}}), owner_user_id="experience-owner",
            idempotency_key="oversized-undated-create", now=now)
        job = await repo.claim_next(worker_id="oversized-undated-test", now=now, lease_seconds=60)
        await repo.complete_job(job, output, now=now)
        resource = await repo.authorize(created.accepted.public_resource_id, capability_hash=None,
            user_id="experience-owner", now=now)
        stored = await repo.get_result(resource)
        assert not stored.result.coverage.complete and stored.result.coverage.unprocessed_count == 1
        assert stored.result.days[0].source_notes == []
        supplementary = await repo.get_supplementary_view(resource, now=now)
        assert supplementary.status == "AVAILABLE" and supplementary.days == []
        assert places.names == ["沙面", "越秀公园"]
