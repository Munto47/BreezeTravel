"""Independent source cases for restricted additions, not acceptance samples."""
import pytest

from app.trip_understanding.bounded_supplement import build_supplement_patch, validate_supplement
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import ActivityDeleteCommand, ActivityTextEditCommand, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_source_visit_supplement import plan


SOURCE = "北京。\nDay1：故宫博物院，馆内重点参观太和殿。之后去景山公园。不去北海公园。若有余力去颐和园。\nDay2：再访故宫博物院，只在门外取行李，不进展厅。"


async def fixture():
    original = plan(SOURCE, [("故宫博物院", 1, 1), ("故宫博物院", 2, 2)])
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(SOURCE, prepared_plan=original)
    return original, output.public_result


def add(name="景山公园", op_id="missing", **kwargs):
    return dict(operation_id=op_id, operation="ADD_VISIT", activity=dict(
        source_quote=name, place_name=name, day_index=1, role="PLANNED", category="景点"), **kwargs)


@pytest.mark.asyncio
async def test_illegal_operations_leave_independent_source_additions_and_base_intact():
    original, current = await fixture()
    original_snapshot, snapshot = original.model_dump(), current.model_dump()
    first = current.days[0].activities[0]
    missing = add(after_visit_id=first.visit_id)
    rows = [dict(operation_id="rename", operation="RENAME", name="新名字"),
        dict(operation_id="malformed", operation={}), missing,
        dict(operation_id="extra", operation="ADD_VISIT", activity=missing["activity"], delete_visit_id=first.visit_id),
        dict(operation_id="detail", operation="ADD_DETAIL", parent_visit_id=first.visit_id,
            details=[dict(kind="VISIT", source_quote="太和殿", optional=False, evidence="故宫博物院，馆内重点参观太和殿")])]
    result = validate_supplement(SOURCE, original, current, rows)
    assert [item.operation.operation_id for item in result.accepted] == ["missing", "detail"]
    assert [item.mentions[0].atomic_place_name for item in result.accepted] == ["景山公园", "太和殿"]
    assert {item["reason"] for item in result.rejected} == {"OPERATION_NOT_ALLOWED", "INVALID_OPERATION"}
    assert original.model_dump() == original_snapshot and current.model_dump() == snapshot


@pytest.mark.asyncio
async def test_deleted_revisit_and_manually_changed_parent_cannot_be_restored():
    original, current = await fixture()
    first = current.days[0].activities[0]
    revisit = current.days[1].activities[0]
    deleted = apply_public_command(current, ActivityDeleteCommand(command_type="ACTIVITY_DELETE", activity_token=first.activity_token)).result
    row = add("故宫博物院")
    result = validate_supplement(SOURCE, original, deleted, [row])
    assert result.rejected == [{"operation_id": "missing", "reason": "USER_DECISION"}]
    assert deleted.days[1].activities[0].source_occurrence_id == revisit.source_occurrence_id
    edited = apply_public_command(current, ActivityTextEditCommand(command_type="ACTIVITY_TEXT_EDIT",
        activity_token=first.activity_token, name="国家博物馆")).result
    detail = dict(operation_id="detail", operation="ADD_DETAIL", parent_visit_id=first.visit_id,
        details=[dict(kind="VISIT", source_quote="太和殿", optional=False, evidence="故宫博物院，馆内重点参观太和殿")])
    assert validate_supplement(SOURCE, original, edited, [detail]).rejected[0]["reason"] == "USER_DECISION"
    restored = apply_public_command(edited, UndoCommand(command_type="UNDO"), undo_result=current).result
    assert len(validate_supplement(SOURCE, original, restored, [detail]).accepted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["北海公园", "颐和园", "太和殿"])
async def test_cancelled_and_optional_source_cannot_be_added_to_mainline(name):
    original, current = await fixture()
    result = validate_supplement(SOURCE, original, current, [add(name, after_visit_id=current.days[0].activities[0].visit_id)])
    assert not result.accepted
    assert result.rejected[0]["reason"] in {"SOURCE_REJECTED", "ROLE_NOT_ALLOWED"}


@pytest.mark.asyncio
async def test_unknown_and_wrong_day_position_do_not_get_guessed():
    original, current = await fixture()
    result = validate_supplement(SOURCE, original, current, [add(op_id="unknown", after_visit_id="unknown"),
        add(op_id="different-day", after_visit_id=current.days[1].activities[0].visit_id), add(op_id="unanchored")])
    assert not result.accepted
    assert [item["reason"] for item in result.rejected] == ["POSITION_CHANGED", "POSITION_CHANGED", "POSITION_REQUIRED"]


@pytest.mark.asyncio
async def test_duplicate_operation_ids_and_source_occurrences_are_rejected_without_losing_sibling():
    original, current = await fixture()
    first = current.days[0].activities[0]
    rows = [add(op_id="duplicate", after_visit_id=first.visit_id)] * 2
    rows.extend([add(op_id="one", after_visit_id=first.visit_id), add(op_id="two", after_visit_id=first.visit_id)])
    result = validate_supplement(SOURCE, original, current, rows)
    assert [item.operation.operation_id for item in result.accepted] == ["one"]
    assert [item["reason"] for item in result.rejected] == ["DUPLICATE_OPERATION_ID", "DUPLICATE_OPERATION_ID", "ALREADY_EXTRACTED"]


@pytest.mark.asyncio
async def test_detail_cannot_reassign_parent_and_bad_detail_does_not_erase_legal_sibling():
    original, current = await fixture()
    first, revisit = current.days[0].activities[0], current.days[1].activities[0]
    legal = dict(kind="VISIT", source_quote="太和殿", optional=False, evidence="故宫博物院，馆内重点参观太和殿")
    rows = [dict(operation_id="wrong-parent", operation="ADD_DETAIL", parent_visit_id=revisit.visit_id, details=[legal]),
        dict(operation_id="mixed", operation="ADD_DETAIL", parent_visit_id=first.visit_id, details=[dict(legal, parent_index=1), legal])]
    result = validate_supplement(SOURCE, original, current, rows)
    assert [item.operation.operation_id for item in result.accepted] == ["mixed"]
    assert len(result.accepted[0].mentions) == 1
    assert result.accepted[0].mentions[0].parent_mention_id == original.mentions[0].mention_id
    assert [item["reason"] for item in result.rejected] == ["DETAIL_REJECTED", "DETAIL_PARTIALLY_REJECTED"]


@pytest.mark.asyncio
async def test_correction_is_only_a_suggestion_and_must_quote_retained_source():
    original, current = await fixture()
    before = current.model_dump()
    row = dict(operation_id="check", operation="SUGGEST_CORRECTION", target_visit_id=current.days[0].activities[0].visit_id,
        source_quote="景山公园", evidence="之后去景山公园")
    result = validate_supplement(SOURCE, original, current, [row, dict(row, operation_id="invented", evidence="去一个新的地方")])
    assert len(result.accepted) == 1 and not result.accepted[0].mentions
    assert result.rejected[0]["reason"] == "SOURCE_NOT_UNIQUE"
    assert current.model_dump() == before
    with pytest.raises(ValueError, match="source does not match"):
        validate_supplement(SOURCE + "改动", original, current, [row])


class CountingPlaces(FixedReplayPlaces):
    def __init__(self):
        self.queries = []

    async def resolve(self, **kwargs):
        self.queries.append(kwargs["atomic_place_name"])
        return await super().resolve(**kwargs)


@pytest.mark.asyncio
async def test_patch_queries_only_new_main_visit_and_preserves_existing_cards_and_revisit():
    original, current = await fixture()
    first = current.days[0].activities[0]
    first.note = "保留我的备注"
    snapshots = [card.model_dump() for day in current.days for card in day.activities]
    missing = add(after_visit_id=first.visit_id)
    option = add("颐和园", "option")
    option["operation"], option["activity"]["role"] = "ADD_ALTERNATIVE", "OPTIONAL"
    places = CountingPlaces()
    patch = await build_supplement_patch(SOURCE, original, current, [missing, option], TripUnderstandingPipeline(None, places))
    assert places.queries == ["景山公园"]
    assert [card.name for day in patch.result.days for card in day.activities] == ["故宫博物院", "景山公园", "故宫博物院"]
    assert [card.model_dump() for day in patch.result.days for card in day.activities if card.name == "故宫博物院"] == snapshots
    assert patch.result.days[0].alternatives[0].name == "颐和园"
    assert patch.result.days[0].alternatives[0].insertion_position is None
    assert patch.result.coverage.confirmed_place_count == 3
    assert patch.routes_changed and patch.changed_days == ["Day 1"]
    assert not patch.validation.rejected


@pytest.mark.asyncio
async def test_details_and_correction_need_no_place_or_model_calls_and_never_rename():
    original, current = await fixture()
    first = current.days[0].activities[0]
    rows = [dict(operation_id="detail", operation="ADD_DETAIL", parent_visit_id=first.visit_id,
        details=[dict(kind="VISIT", source_quote="太和殿", optional=False, evidence="故宫博物院，馆内重点参观太和殿")]),
        dict(operation_id="check", operation="SUGGEST_CORRECTION", target_visit_id=first.visit_id,
            source_quote="故宫博物院", evidence="故宫博物院，馆内重点参观太和殿")]
    places = CountingPlaces()
    patch = await build_supplement_patch(SOURCE, original, current, rows, TripUnderstandingPipeline(None, places))
    assert not places.queries and patch.resolved_additions is None
    assert not patch.routes_changed
    assert [card.name for day in patch.result.days for card in day.activities] == ["故宫博物院", "故宫博物院"]
    assert [detail.name for detail in patch.result.days[0].activities[0].source_details] == ["太和殿"]
    assert patch.result.days[1].activities[0].source_details == current.days[1].activities[0].source_details
    assert patch.result.correction_suggestions[0].target_visit_id == first.visit_id
    assert not current.days[0].activities[0].source_details and not current.correction_suggestions


@pytest.mark.asyncio
async def test_conditional_alternative_keeps_original_specific_target_without_selecting_it():
    source = "上海。\nDay1：先去陆家嘴。若天气不好，将陆家嘴换为上海科技馆。"
    original = plan(source, [("陆家嘴", 1, 1)])
    pipeline = TripUnderstandingPipeline(None, CountingPlaces())
    current = (await pipeline.run(source, prepared_plan=original)).public_result
    option = dict(operation_id="weather", operation="ADD_ALTERNATIVE", activity=dict(
        source_quote="上海科技馆", place_name="上海科技馆", role="OPTIONAL", day_index=1, category="景点"))
    places = CountingPlaces()
    patch = await build_supplement_patch(source, original, current, [option], TripUnderstandingPipeline(None, places))
    assert not places.queries
    assert patch.result.days[0].activities == current.days[0].activities
    alternative = patch.result.days[0].alternatives[0]
    assert alternative.replaces_visit_id == current.days[0].activities[0].visit_id
    assert alternative.replacement_condition == "若天气不好"
    assert not patch.routes_changed


@pytest.mark.asyncio
async def test_two_missing_visits_after_same_saved_anchor_keep_response_order():
    source = "北京。\nDay1：先去天安门广场，之后去景山公园，再去北海公园。"
    original = plan(source, [("天安门广场", 1, 1)])
    current = (await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=original)).public_result
    anchor = current.days[0].activities[0].visit_id
    rows = [add(after_visit_id=anchor), add("北海公园", "last", after_visit_id=anchor)]
    patch = await build_supplement_patch(source, original, current, rows, TripUnderstandingPipeline(None, FixedReplayPlaces()))
    assert not patch.validation.rejected
    assert [card.name for card in patch.result.days[0].activities] == ["天安门广场", "景山公园", "北海公园"]


@pytest.mark.asyncio
async def test_optional_parent_detail_stays_on_unselected_alternative():
    from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
    source = "北京。\nDay1：先去故宫博物院。若有余力去颐和园，园内重点看长廊。"
    original = _proposal_from_live_draft(source, SemanticDraft.model_validate(dict(destination="北京", activities=[
        dict(source_quote=name, place_name=name, day_index=1, role=role, category="景点")
        for name, role in [("故宫博物院", "PLANNED"), ("颐和园", "OPTIONAL")]])))
    current = (await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=original)).public_result
    target = current.days[0].alternatives[0]
    row = dict(operation_id="inside-option", operation="ADD_DETAIL", parent_visit_id=target.alternative_id,
        details=[dict(kind="VISIT", source_quote="长廊", optional=False, evidence="颐和园，园内重点看长廊")])
    places = CountingPlaces()
    patch = await build_supplement_patch(source, original, current, [row], TripUnderstandingPipeline(None, places))
    assert not patch.validation.rejected
    assert not places.queries and not patch.routes_changed
    assert patch.result.days[0].activities == current.days[0].activities
    assert [detail.name for detail in patch.result.days[0].alternatives[0].source_details] == ["长廊"]
