"""Typed visit purposes bind through source evidence, never guessed occurrence."""
import json
from pathlib import Path

from jsonschema import Draft202012Validator
import pytest

from app.trip_understanding.models import ActivityRole
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_experience_strict_wire_contract import recorded_provider
from tests.test_semantic_supplement_budget import Client, provider
from tests.test_source_visit_supplement import plan


def purpose(parent=0, kind="EXTERIOR_ONLY", evidence="青溪公园，只看外观，不进园", **updates):
    return dict(parent_index=parent, kind=kind, optional=False, evidence=evidence) | updates


def apply(source, before, rows):
    return apply_source_visit_supplement(source, before, rows, parent_ids=[m.mention_id for m in before.mentions])


@pytest.mark.asyncio
async def test_saved_actual_typed_second_answer_reaches_seven_details_without_repeated_optional():
    sample = json.loads((Path(__file__).parent / "fixtures/live_source_typed_purpose.json").read_text(encoding="utf-8"))
    client = Client(*sample["responses"])
    output = await TripUnderstandingPipeline(provider(client), FixedReplayPlaces()).run(sample["source"])
    assert len(client.calls) == 2 and output.resolution_receipt["attempted_count"] == 4
    assert [len(day.activities) for day in output.public_result.days] == [2, 2]
    assert not any(day.alternatives for day in output.public_result.days)
    assert [detail.name for day in output.public_result.days for card in day.activities for detail in card.source_details] == [
        "入口：从南门进", "风筝广场", "邓小平雕像", "桃花林", "出口：南门", "仅看外观，不入内部", "仅取物，不参观"]
    assert output.public_result.coverage.unprocessed_count == 0 and output.public_result.coverage.complete
    assert [day.unprocessed_count for day in output.public_result.days] == [0, 0]
    for mention in output.proposal.mentions:
        assert sample["source"][mention.span_start:mention.span_end] == mention.raw_text


@pytest.mark.asyncio
async def test_sdk_second_request_uses_typed_strict_schema_but_keeps_city_fields_and_budget():
    source = "北京。Day1：青溪公园，只看外观，不进园。"
    first = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote="青溪公园", place_name="青溪公园", role="PLANNED", day_index=1, category="景点")])
    second = dict(city_fields=[], source_visits=[purpose()])
    async with recorded_provider(first, second) as (engine, requests):
        engine.enable_source_visits = True
        result = await engine.propose(source)
    assert len(requests) == result.binding["external_calls"] == 2
    wire = requests[1]["response_format"]
    assert wire["type"] == "json_schema" and wire["json_schema"]["strict"] is True
    assert wire["json_schema"]["name"] == "BreezeTravelSourceInstructions"
    assert requests[1]["max_tokens"] == 4096 and requests[1]["enable_thinking"] is False
    schema = wire["json_schema"]["schema"]
    validator = Draft202012Validator(schema)
    validator.validate(second)
    validator.validate(dict(city_fields=[dict(index=0, city=None, city_evidence=None)], source_visits=[
        dict(parent_index=0, kind="ENTRY", source_quote="南门", optional=False, evidence="从南门进")]))
    for invalid in [purpose(source_quote="青溪公园", occurrence=1), purpose(occurrence=1),
                    purpose(kind="VISIT"), purpose(kind="UNKNOWN"), {k: v for k, v in purpose().items() if k != "optional"}]:
        assert list(validator.iter_errors(dict(city_fields=[], source_visits=[invalid])))
    assert result.unprocessed_count == 0


@pytest.mark.parametrize("kind,ending", [
    ("EXTERIOR_ONLY", "只看外观，不进园"), ("PICKUP_ONLY", "仅取寄存的行李，不进园"),
])
def test_purpose_evidence_over_100_characters_keeps_exact_full_source_and_markdown_coordinates(kind, ending):
    evidence = "到**青溪公园**，" + "这里的历史介绍另有说明，" * 12 + ending
    assert 100 < len(evidence) < 500
    source = "北京。\nDay1：" + evidence + "。"
    before = plan(source, [("青溪公园", 1, 1)])
    after = apply(source, before, [purpose(kind=kind, evidence=evidence)])
    assert len(after.mentions) == 2 and after.unprocessed_count == 0
    child = after.mentions[1]
    assert source[child.span_start:child.span_end] == child.raw_text
    assert child.role_evidence == evidence
    assert source[child.role_evidence_start:child.role_evidence_end] == evidence
    assert child.parent_mention_id == before.mentions[0].mention_id


def test_same_name_revisit_binds_day2_action_without_global_name_occurrence():
    source = "北京。\nDay1：青溪公园，园内参观晨光亭。\nDay2：再到青溪公园，仅取寄存的行李，不进园。"
    before = plan(source, [("青溪公园", 1, 1), ("青溪公园", 2, 2)])
    evidence = "再到青溪公园，仅取寄存的行李，不进园"
    after = apply(source, before, [purpose(1, "PICKUP_ONLY", evidence)])
    assert after.mentions[:2] == before.mentions
    assert after.mentions[2].day_index == 2 and after.mentions[2].parent_mention_id == before.mentions[1].mention_id
    assert after.unprocessed_count == 0
    assert apply(source, after.model_copy(update={"mentions": after.mentions[:2]}), [purpose(0, "PICKUP_ONLY", evidence)]).unprocessed_count > 0
    legacy_wrong = purpose(1, "PICKUP_ONLY", evidence, source_quote="青溪公园", occurrence=1)
    wrong = apply(source, before, [legacy_wrong])
    assert wrong.mentions == before.mentions and wrong.unprocessed_count > 0
    legacy_correct = {**legacy_wrong, "occurrence": 2}
    assert len(apply(source, before, [legacy_correct]).mentions) == 3


@pytest.mark.parametrize("source,parents,row", [
    ("北京。\nDay1：青溪公园，只看外观，不进园。\nDay2：青溪公园，只看外观，不进园。",
     [("青溪公园", 1, 1), ("青溪公园", 2, 2)], purpose()),
    ("北京。\nDay1：青溪公园。\nDay2：星河公园，只看外观，不进园。",
     [("青溪公园", 1, 1), ("星河公园", 2, 1)], purpose(evidence="星河公园，只看外观，不进园")),
    ("北京。\nDay1：青溪公园。随后去星河公园，只看外观，不进园。",
     [("青溪公园", 1, 1), ("星河公园", 1, 1)], purpose(evidence="星河公园，只看外观，不进园")),
    ("北京。\nDay1：青溪公园，眺望星河博物馆，只看外观，不进馆。",
     [("青溪公园", 1, 1)], purpose(evidence="青溪公园，眺望星河博物馆，只看外观，不进馆")),
    ("北京。\nDay1：青溪公园，不只看外观，还要进入内部。",
     [("青溪公园", 1, 1)], purpose(evidence="青溪公园，不只看外观，还要进入内部")),
    ("北京。\nDay1：青溪公园，不只看外观，还要进入内部。",
     [("青溪公园", 1, 1)], purpose(evidence="只看外观")),
    ("北京。\nDay1：青溪公园，若有时间只看外观，不进园。",
     [("青溪公园", 1, 1)], purpose(evidence="青溪公园，若有时间只看外观，不进园")),
    ("北京。\nDay1：青溪公园，只看外观，不进园。",
     [("青溪公园", 1, 1)], purpose(optional=True)),
    ("北京。\nDay1：青溪公园，只看外观，不进园。",
     [("青溪公园", 1, 1)], purpose(kind="VISIT")),
    ("北京。\nDay1：青溪公园，只看外观，不进园。",
     [("青溪公园", 1, 1)], purpose(source_quote="青溪公园", occurrence=2)),
    ("北京。\nDay1：青溪公园，只看外观，不进园。",
     [("青溪公园", 1, 1)], purpose(occurrence=1)),
    ("北京。\nDay1：青溪公园，只看外观，不进园。",
     [("青溪公园", 1, 1)], purpose(evidence="证" * 501)),
    ("北京。\nDay1：青溪公园，只看外观，不进园。星河公园，只看外观，不进园。",
     [("青溪公园", 1, 1), ("星河公园", 1, 1)], purpose(evidence="青溪公园，只看外观，不进园。星河公园，只看外观，不进园")),
])
def test_ambiguous_wrong_scope_negated_or_malformed_purpose_remains_unprocessed(source, parents, row):
    before = plan(source, parents)
    after = apply(source, before, [row])
    assert after.mentions == before.mentions
    assert after.unprocessed_count > before.unprocessed_count


def test_cancelled_parent_and_self_parent_visit_are_not_restored_as_purposes_or_details():
    source = "北京。\nDay1：青溪公园，只看外观，不进园。"
    before = plan(source, [("青溪公园", 1, 1)])
    cancelled = before.model_copy(update={"mentions": [before.mentions[0].model_copy(update={"role": ActivityRole.EXCLUDED})]})
    assert apply(source, cancelled, [purpose()]).mentions == cancelled.mentions
    self_visit = purpose(kind="VISIT", source_quote="青溪公园", occurrence=1)
    result = apply(source, before, [self_visit])
    assert result.mentions == before.mentions and result.unprocessed_count > 0


@pytest.mark.asyncio
async def test_owner_bird_nest_and_water_cube_share_one_exterior_instruction_for_both_parents():
    source = "北京。\nDay1：鸟巢、水立方，不用买票进馆，外面广场看灯光。"
    evidence = "鸟巢、水立方，不用买票进馆，外面广场看灯光"
    before = plan(source, [("鸟巢", 1, 1), ("水立方", 1, 1)])
    after = apply(source, before, [purpose(index, evidence=evidence) for index in (0, 1)])
    assert after.mentions[:2] == before.mentions
    assert after.unprocessed_count == 0
    assert [m.parent_mention_id for m in after.mentions[2:]] == [m.mention_id for m in before.mentions]
    result = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=after)
    assert [[detail.name for detail in card.source_details] for card in result.public_result.days[0].activities] == [
        ["仅看外观，不入内部"], ["仅看外观，不入内部"]]


@pytest.mark.asyncio
async def test_second_schema_400_keeps_main_visit_and_unfinished_marker_without_third_call():
    source = "北京。Day1：青溪公园，只看外观，不进园。"
    first = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote="青溪公园", place_name="青溪公园", role="PLANNED", day_index=1, category="景点")])
    async with recorded_provider(first, (400, {"error": {"message": "schema unsupported", "type": "invalid_request_error"}})) as (engine, requests):
        engine.enable_source_visits = True
        result = await engine.propose(source)
    assert len(requests) == 2
    assert result.mentions[0].atomic_place_name == "青溪公园" and result.unprocessed_count > 0
    assert result.binding["calls"][1]["outcome"] == "PROVIDER_UNAVAILABLE"
    assert requests[1]["response_format"]["type"] == "json_schema"
