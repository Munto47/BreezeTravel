"""One source occurrence has one optional representation after parent validation."""
import json
from pathlib import Path

import pytest

from app.trip_understanding.experience_inference import SemanticDraft, _proposal_from_live_draft
from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_semantic_supplement_budget import Client, provider


SOURCE = "北京。\nDay1：青溪公园，园内先看晨光亭；如果有空，再看园内桃花林。"


def prepared(source=SOURCE):
    result = _proposal_from_live_draft(source, SemanticDraft.model_validate(dict(
        destination="北京", activities=[dict(source_quote="青溪公园", place_name="青溪公园",
            role="PLANNED", day_index=1, category="景点"), dict(source_quote="桃花林", place_name="桃花林",
            role="OPTIONAL", day_index=1, category="景点")])))
    return result


def detail(name="桃花林", **updates):
    return dict(parent_index=0, kind="VISIT", source_quote=name, occurrence=1,
        optional=name == "桃花林", evidence="如果有空，再看园内桃花林" if name == "桃花林" else "园内先看晨光亭") | updates


def apply(source, before, rows):
    return apply_source_visit_supplement(source, before, rows, parent_ids=[before.mentions[0].mention_id])


@pytest.mark.asyncio
async def test_saved_real_two_answers_keep_four_main_visits_six_details_and_no_duplicate_optional():
    sample = json.loads((Path(__file__).parent / "fixtures/live_source_optional_duplicate.json").read_text(encoding="utf-8"))
    client = Client(*sample["responses"])
    output = await TripUnderstandingPipeline(provider(client), FixedReplayPlaces()).run(sample["source"])
    assert len(client.calls) == 2  # Fixed raw replay, no real model or POI.
    assert output.resolution_receipt["attempted_count"] == 4
    assert [len(day.activities) for day in output.public_result.days] == [2, 2]
    assert not any(day.alternatives for day in output.public_result.days)
    details = [item for day in output.public_result.days for card in day.activities for item in card.source_details]
    assert len(details) == 6
    assert [(item.name, item.optional) for item in details] == [
        ("入口：从南门进", False), ("风筝广场", False), ("邓小平雕像", False),
        ("桃花林", True), ("出口：从南门出", False), ("仅看外观，不入内部", False)]
    represented = [m for m in output.proposal.mentions if m.atomic_place_name == "桃花林"]
    assert len(represented) == 1 and represented[0].mention_id == "activity-2"
    assert represented[0].parent_mention_id == "activity-1" and represented[0].role == ActivityRole.OPTIONAL
    assert output.public_result.coverage.unprocessed_count == 2 and not output.public_result.coverage.complete
    assert output.public_result.days[1].unprocessed_count == 2  # Wrong self-visit and pickup occurrence remain.


@pytest.mark.asyncio
async def test_same_optional_retains_id_source_day_role_and_uses_internal_order_without_mutating_input():
    before = prepared()
    snapshot = before.model_dump()
    after = apply(SOURCE, before, [detail("晨光亭"), detail()])
    assert before.model_dump() == snapshot
    assert after.mentions[0] == before.mentions[0]
    matches = [m for m in after.mentions if m.mention_id == before.mentions[1].mention_id]
    assert len(matches) == 1
    child = matches[0]
    for field in ("raw_text", "span_start", "span_end", "day_index", "role", "atomic_place_name"):
        assert getattr(child, field) == getattr(before.mentions[1], field)
    assert child.parent_mention_id == before.mentions[0].mention_id
    assert child.relation_type == "INTERNAL_DETAIL"
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(SOURCE, prepared_plan=after)
    assert not output.public_result.days[0].alternatives
    assert [d.name for d in output.public_result.days[0].activities[0].source_details] == ["晨光亭", "桃花林"]
    assert apply(SOURCE, after, [detail("晨光亭"), detail()]).model_dump() == after.model_dump()


@pytest.mark.parametrize("suffix,day", [
    ("\nDay2：星河公园，如果有空去桃花林。", 2),
    ("随后到星河公园，如果有空去桃花林。", 1),
    ("\n全程备选：桃花林。", None),
])
def test_other_day_occurrence_and_global_optional_are_not_consumed(suffix, day):
    source = SOURCE + suffix
    before = prepared(source)
    # The fixed plan deliberately retains a distinct, source-grounded occurrence.
    other = before.mentions[1].model_copy(update=dict(mention_id="other-optional", day_index=day,
        span_start=source.rindex("桃花林"), span_end=source.rindex("桃花林") + 3, sequence_index=2))
    before = before.model_copy(update={"mentions": [*before.mentions, other]})
    after = apply(source, before, [detail()])
    assert next(m for m in after.mentions if m.mention_id == other.mention_id) == other
    assert len([m for m in after.mentions if m.atomic_place_name == "桃花林"]) == 2
    assert len([m for m in after.mentions if m.atomic_place_name == "桃花林" and m.parent_mention_id]) == 1


@pytest.mark.parametrize("role", ["PLANNED", "REFERENCE", "EXCLUDED"])
def test_role_conflict_cannot_turn_an_existing_visit_into_optional_detail(role):
    before = prepared()
    before = before.model_copy(update={"mentions": [before.mentions[0],
        before.mentions[1].model_copy(update={"role": ActivityRole(role)})]})
    after = apply(SOURCE, before, [detail()])
    assert after.mentions == before.mentions
    assert after.unprocessed_count > before.unprocessed_count


def test_multiple_exact_optional_matches_are_ambiguous_and_stay_unprocessed():
    before = prepared()
    duplicate = before.mentions[1].model_copy(update={"mention_id": "another-optional"})
    before = before.model_copy(update={"mentions": [*before.mentions, duplicate]})
    after = apply(SOURCE, before, [detail()])
    assert after.mentions == before.mentions
    assert after.unprocessed_count > before.unprocessed_count


def test_failed_internal_order_keeps_original_independent_optional_and_unprocessed_marker():
    before = prepared()
    existing = apply(SOURCE, before, [detail("晨光亭")])
    after = apply(SOURCE, existing, [detail()])  # Omits the existing internal order anchor.
    assert after.mentions == existing.mentions
    assert after.unprocessed_count > existing.unprocessed_count
    assert not next(m for m in after.mentions if m.mention_id == before.mentions[1].mention_id).parent_mention_id


@pytest.mark.parametrize("updates", [
    {"occurrence": 2}, {"optional": False},
    {"parent_index": 99}, {"evidence": "明天再去桃花林"},
])
def test_invalid_or_nonexact_detail_does_not_consume_original_optional(updates):
    before = prepared()
    after = apply(SOURCE, before, [detail(**updates)])
    assert next(m for m in after.mentions if m.mention_id == before.mentions[1].mention_id) == before.mentions[1]
    assert after.unprocessed_count > before.unprocessed_count


def test_nonexact_name_or_span_never_consumes_original_optional():
    before = prepared()
    after = apply(SOURCE, before, [detail(source_quote="花林")])
    # This slice does not broaden name validation: a different literal span
    # cannot authorize replacing the independently retained 桃花林 occurrence.
    assert next(m for m in after.mentions if m.mention_id == before.mentions[1].mention_id) == before.mentions[1]


def test_reconciliation_at_160_keeps_every_existing_id_without_counting_same_occurrence_twice():
    before = prepared()
    before.mentions += [before.mentions[0].model_copy(update={
        "mention_id": f"reference-{index}", "role": ActivityRole.REFERENCE}) for index in range(158)]
    snapshot = before.model_dump()
    after = apply(SOURCE, before, [detail()])
    assert before.model_dump() == snapshot
    assert len(after.mentions) == 160
    assert {m.mention_id for m in after.mentions} == {m.mention_id for m in before.mentions}
    assert len([m for m in after.mentions if m.atomic_place_name == "桃花林"]) == 1
    assert next(m for m in after.mentions if m.mention_id == "activity-2").parent_mention_id == "activity-1"
