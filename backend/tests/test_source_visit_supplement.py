"""Source instructions stay attached to the same visit, never extra route stops."""
from copy import deepcopy

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.failures import INPUT_CAPACITY_EXCEEDED
from app.trip_understanding.models import ActivityRole, ProposedMention, SemanticDiagnostic
from app.trip_understanding.pipeline import PublicResultProjector, TripUnderstandingPipeline
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.semantic_page_replays import FixedReplayPlaces


def plan(source, parents):
    return _proposal_from_live_draft(source, SemanticDraft.model_validate(dict(destination="北京", activities=[
        dict(source_quote=name, place_name=name, day_index=day, occurrence=occurrence,
            role="PLANNED", category="景点") for name, day, occurrence in parents])))


def row(parent, kind, quote, evidence, **fields):
    return dict(parent_index=parent, kind=kind, source_quote=quote, evidence=evidence, occurrence=1, optional=False) | fields


def apply(source, before, rows):
    return apply_source_visit_supplement(source, before, rows,
        parent_ids=[m.mention_id for m in before.mentions if not m.parent_mention_id])


SOURCE = ("北京。\nDay1：故宫博物院，午门进、神武门出，重点：太和殿；时间充裕看珍宝馆。随后去景山公园。"
          "\nDay2：再访故宫博物院，只在门外取寄存的行李，不进展厅。")
ROWS = [row(0, "EXIT", "神武门", "午门进、神武门出"),
    row(0, "VISIT", "太和殿", "重点：太和殿"),
    row(0, "VISIT", "珍宝馆", "时间充裕看珍宝馆", optional=True),
    row(0, "ENTRY", "午门", "午门进、神武门出"),
    row(2, "PICKUP_ONLY", "故宫博物院", "只在门外取寄存的行李，不进展厅", occurrence=2)]


@pytest.mark.asyncio
async def test_typed_details_project_in_visit_order_with_parent_facts_and_routes_unchanged():
    before = plan(SOURCE, [("故宫博物院", 1, 1), ("景山公园", 1, 1), ("故宫博物院", 2, 2)])
    snapshot = before.model_dump()
    after = apply(SOURCE, before, ROWS)
    assert before.model_dump() == snapshot
    assert [m.model_dump() for m in after.mentions[:3]] == snapshot["mentions"]
    assert after.binding == before.binding
    assert len(after.mentions) == 8 and not after.diagnostics
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(SOURCE, prepared_plan=after)
    assert output.resolution_receipt["attempted_count"] == 3
    assert [c.name for day in output.public_result.days for c in day.activities] == ["故宫博物院", "景山公园", "故宫博物院"]
    assert [d.model_dump() for d in output.public_result.days[0].activities[0].source_details] == [
        {"name": "入口：午门", "optional": False}, {"name": "太和殿", "optional": False},
        {"name": "珍宝馆", "optional": True}, {"name": "出口：神武门", "optional": False}]
    assert [d.name for d in output.public_result.days[1].activities[0].source_details] == ["仅取物，不参观"]
    assert output.public_result.coverage.complete
    public = output.public_result.model_dump_json()
    for private in ("detail_kind", "parent_index", "source_quote", "role_evidence", "parent_mention_id"):
        assert private not in public


def test_duplicate_source_does_not_append_twice_and_legacy_visit_defaults_remain_compatible():
    before = plan(SOURCE, [("故宫博物院", 1, 1), ("景山公园", 1, 1), ("故宫博物院", 2, 2)])
    first = apply(SOURCE, before, ROWS)
    second = apply(SOURCE, first, ROWS + ROWS)
    assert second.model_dump() == first.model_dump()
    old = first.mentions[4].model_dump(exclude={"detail_kind"})
    assert ProposedMention.model_validate(old).detail_kind is None


@pytest.mark.asyncio
async def test_internal_execution_order_is_not_source_mention_order():
    source = "北京。\nDay1：故宫博物院，园内太和殿、乾清宫均要参观，实际先看乾清宫再看太和殿。"
    before = plan(source, [("故宫博物院", 1, 1)])
    supplement = [row(0, "VISIT", name, "园内太和殿、乾清宫均要参观") for name in ("乾清宫", "太和殿")]
    after = apply(source, before, supplement)
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=after)
    assert [d.name for d in output.public_result.days[0].activities[0].source_details] == ["乾清宫", "太和殿"]
    assert after.mentions[1].span_start > after.mentions[2].span_start


def test_new_details_need_existing_order_anchors_before_inserting_around_old_details():
    source = "北京。\nDay1：故宫博物院，园内先看太和殿，再看乾清宫，最后看珍宝馆。"
    before = plan(source, [("故宫博物院", 1, 1)])
    existing = apply(source, before, [row(0, "VISIT", "太和殿", "园内先看太和殿")])
    new = row(0, "VISIT", "乾清宫", "再看乾清宫")
    unknown_order = apply(source, existing, [new])
    assert unknown_order.mentions == existing.mentions and unknown_order.unprocessed_count == 1
    ordered = apply(source, existing, [row(0, "VISIT", "太和殿", "园内先看太和殿"), new])
    assert len(ordered.mentions) == 3
    assert ordered.mentions[:2] == existing.mentions
    assert ordered.mentions[2].sequence_index > ordered.mentions[1].sequence_index


@pytest.mark.parametrize("source,parents,supplement", [
    ("北京。\nDay1：故宫博物院，园内看太和殿。之后去景山公园，上万春亭。",
        [("故宫博物院", 1, 1), ("景山公园", 1, 1)], row(0, "VISIT", "万春亭", "上万春亭")),
    ("北京。\nDay1：景山公园，园内路线：万春亭，俯瞰故宫全景。", [("景山公园", 1, 1)],
        row(0, "VISIT", "故宫", "俯瞰故宫全景")),
    ("北京。\nDay1：故宫博物院。\nDay2：景山公园，园内看万春亭。",
        [("故宫博物院", 1, 1), ("景山公园", 2, 1)], row(0, "VISIT", "万春亭", "园内看万春亭")),
    ("北京。\nDay1：故宫博物院，园内看太和殿，取消珍宝馆。", [("故宫博物院", 1, 1)],
        row(0, "VISIT", "珍宝馆", "取消珍宝馆")),
    ("北京。\nDay1：故宫博物院，园内看太和殿，若有时间看珍宝馆。", [("故宫博物院", 1, 1)],
        row(0, "VISIT", "珍宝馆", "若有时间看珍宝馆")),
    ("北京。\nDay1：故宫博物院，午门进、神武门出。", [("故宫博物院", 1, 1)],
        row(0, "ENTRY", "神武门", "午门进、神武门出")),
    ("北京。\nDay1：故宫博物院，不从午门进，从神武门进。", [("故宫博物院", 1, 1)],
        row(0, "ENTRY", "午门", "不从午门进")),
    ("北京。\nDay1：故宫博物院，不只看外观，还要进入内部。", [("故宫博物院", 1, 1)],
        row(0, "EXTERIOR_ONLY", "故宫博物院", "不只看外观，还要进入内部")),
    ("北京。\nDay1：故宫博物院，馆内有太和殿。", [("故宫博物院", 1, 1)],
        row(0, "VISIT", "太和殿", "馆内有太和殿")),
    ("北京。\nDay1：故宫博物院。然后去景山公园，从南门进入。",
        [("故宫博物院", 1, 1), ("景山公园", 1, 1)], row(0, "ENTRY", "南门", "从南门进入")),
    ("北京。\nDay1：故宫博物院，仅看外观。", [("故宫博物院", 1, 1)],
        row(99, "EXTERIOR_ONLY", "故宫博物院", "仅看外观")),
    ("北京。\nDay1：故宫博物院，仅看外观。", [("故宫博物院", 1, 1)],
        row(0, "UNKNOWN_KIND", "故宫博物院", "仅看外观")),
    ("北京。\nDay1：故宫博物院，参观展厅。", [("故宫博物院", 1, 1)],
        row(0, "EXTERIOR_ONLY", "故宫博物院", "参观展厅")),
    ("北京。\nDay1：人民广场，眺望上海大剧院，不进场馆，只在外面拍照。", [("人民广场", 1, 1)],
        row(0, "EXTERIOR_ONLY", "人民广场", "眺望上海大剧院，不进场馆，只在外面拍照")),
    ("北京。\nDay1：故宫博物院。随后去景山公园，仅看外观。",
        [("故宫博物院", 1, 1), ("景山公园", 1, 1)],
        row(0, "EXTERIOR_ONLY", "故宫博物院", "故宫博物院。随后去景山公园，仅看外观")),
])
def test_wrong_scope_or_unsupported_purpose_stays_unprocessed(source, parents, supplement):
    before = plan(source, parents)
    result = apply(source, before, [supplement])
    assert result.mentions == before.mentions
    assert result.unprocessed_count == before.unprocessed_count + 1
    assert result.diagnostics[-1].category == "SOURCE_VISIT_UNRESOLVED"
    assert apply(source, result, [supplement]).model_dump() == result.model_dump()


def test_wrong_gate_occurrence_cannot_bind_a_restaurant_name_and_valid_sibling_survives():
    source = "北京。\nDay1：南门涮肉。\nDay2：天坛公园，南门进北门出。"
    before = plan(source, [("南门涮肉", 1, 1), ("天坛公园", 2, 1)])
    after = apply(source, before, [row(1, "ENTRY", "南门", "南门进北门出"), row(1, "EXIT", "北门", "南门进北门出")])
    assert [m.detail_kind for m in after.mentions[2:]] == ["EXIT"]
    assert after.diagnostics[-1].span_start == source.index("南门进北门出")
    assert after.unprocessed_by_day == {2: 1}


@pytest.mark.asyncio
async def test_shared_exterior_clause_retains_two_parent_purposes_without_new_cards():
    source = "北京。\nDay1：鸟巢、水立方，不用买票进馆，只在外面广场拍照。"
    before = plan(source, [("鸟巢", 1, 1), ("水立方", 1, 1)])
    after = apply(source, before, [row(i, "EXTERIOR_ONLY", name, "不用买票进馆，只在外面广场拍照")
        for i, name in enumerate(("鸟巢", "水立方"))])
    assert len(after.mentions) == 4 and after.unprocessed_count == 0
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=after)
    assert [[d.name for d in c.source_details] for c in output.public_result.days[0].activities] == [
        ["仅看外观，不入内部"], ["仅看外观，不入内部"]]
    assert output.resolution_receipt["attempted_count"] == 2
    assert output.public_result.coverage.complete


def test_later_same_named_parent_cannot_borrow_first_occurrence_for_pickup():
    before = plan(SOURCE, [("故宫博物院", 1, 1), ("景山公园", 1, 1), ("故宫博物院", 2, 2)])
    wrong = dict(ROWS[-1], occurrence=1)
    after = apply(SOURCE, before, [wrong])
    assert after.mentions == before.mentions and after.unprocessed_count == 1


def test_cancelled_parent_and_cancelled_binding_are_never_promoted_to_success():
    source = "北京。\nDay1：故宫博物院，午门进。"
    before = plan(source, [("故宫博物院", 1, 1)])
    before.mentions[0].role = ActivityRole.EXCLUDED
    before.binding = {"outcome": "CANCELLED"}
    after = apply(source, before, [row(0, "ENTRY", "午门", "午门进")])
    assert after.mentions == before.mentions
    assert after.binding == {"outcome": "CANCELLED"}


def test_only_exact_covered_name_warning_is_removed_other_failures_stay():
    source = "北京。\nDay1：故宫博物院，园内参观太和殿、乾清宫。"
    before = plan(source, [("故宫博物院", 1, 1)])
    before.diagnostics = [SemanticDiagnostic(category="KNOWN_PLACE_UNCLASSIFIED", field="coverage",
        span_start=source.index(name), span_end=source.index(name) + len(name)) for name in ("太和殿", "乾清宫")]
    before.unprocessed_count = 2
    before.unprocessed_by_day = {1: 2}
    after = apply(source, before, [row(0, "VISIT", "太和殿", "园内参观太和殿、乾清宫")])
    assert after.unprocessed_count == 1 and after.unprocessed_by_day == {1: 1}
    assert after.diagnostics == before.diagnostics[1:]


def test_160_limit_is_explicit_failure_without_mutating_or_truncating_original():
    source = "北京。\nDay1：故宫博物院，园内参观太和殿。"
    before = plan(source, [("故宫博物院", 1, 1)])
    original = before.mentions[0]
    before.mentions += [original.model_copy(update={"mention_id": f"reference-{i}", "role": ActivityRole.REFERENCE}) for i in range(159)]
    snapshot = deepcopy(before.model_dump())
    with pytest.raises(InferenceProviderUnavailableError) as error:
        apply(source, before, [row(0, "VISIT", "太和殿", "园内参观太和殿")])
    assert error.value.category == INPUT_CAPACITY_EXCEEDED
    assert before.model_dump() == snapshot


@pytest.mark.asyncio
async def test_projection_dropping_formatted_instructions_remains_incomplete():
    class DropGate(PublicResultProjector):
        def project(self, *args, **kwargs):
            result = super().project(*args, **kwargs)
            for day in result.days:
                for card in day.activities:
                    card.source_details = [d for d in card.source_details if not d.name.startswith("入口：")]
            return result

    before = plan(SOURCE, [("故宫博物院", 1, 1), ("景山公园", 1, 1), ("故宫博物院", 2, 2)])
    after = apply(SOURCE, before, ROWS)
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces(), projector=DropGate()).run(SOURCE, prepared_plan=after)
    assert not output.public_result.coverage.complete
    assert output.resolution_receipt["semantic_diagnostic_counts"]["PUBLIC_PROJECTION_OMISSION"] == 1
