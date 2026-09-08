"""New locations bind in unique evidence; explicit legacy occurrence is not repaired."""
from jsonschema import Draft202012Validator
import pytest

from app.trip_understanding.pipeline import TripUnderstandingPipeline
from app.trip_understanding.source_visit_supplement import apply_source_visit_supplement
from tests.semantic_page_replays import FixedReplayPlaces
from tests.test_experience_strict_wire_contract import recorded_provider
from tests.test_source_visit_supplement import plan


def location(quote="南门", kind="ENTRY", evidence="天坛公园，南门进北门出", **updates):
    return dict(parent_index=0, kind=kind, source_quote=quote, optional=False, evidence=evidence) | updates


def apply(source, before, rows):
    return apply_source_visit_supplement(source, before, rows,
        parent_ids=[m.mention_id for m in before.mentions if not m.parent_mention_id])


@pytest.mark.asyncio
async def test_gate_uses_unique_local_evidence_instead_of_unrelated_restaurant_name_occurrence():
    # Reduced from the recorded Beijing failure; this is a new typed fixed
    # response, not a claim that the unchanged old raw has become correct.
    source = "北京。\nDay1：午餐吃南门涮肉。\nDay2：天坛公园，南门进北门出。"
    before = plan(source, [("天坛公园", 2, 1)])
    result = apply(source, before, [location(), location("北门", "EXIT")])
    assert result.mentions[0] == before.mentions[0] and result.unprocessed_count == 0
    assert result.mentions[1].span_start == source.index("南门进")
    assert result.mentions[1].day_index == 2
    output = await TripUnderstandingPipeline(None, FixedReplayPlaces()).run(source, prepared_plan=result)
    assert [d.name for d in output.public_result.days[1].activities[0].source_details] == ["入口：南门", "出口：北门"]
    legacy = apply(source, before, [location(occurrence=1)])
    assert legacy.mentions == before.mentions and legacy.unprocessed_count > 0
    assert len(apply(source, before, [location(occurrence=2)]).mentions) == 2


@pytest.mark.parametrize("source,rows,names", [
    ("北京。\nDay1：青溪公园，园内先看**晨光亭**，最后从**南门出**。",
     [location("晨光亭", "VISIT", "园内先看晨光亭"), location("南门出", "EXIT", "最后从南门出")],
     ["晨光亭", "南门"]),
    ("北京。\nDay1：青溪公园，从南门进，最后从南门出。",
     [location("南门进", "ENTRY", "青溪公园，从南门进，最后从南门出"),
      location("南门出", "EXIT", "青溪公园，从南门进，最后从南门出")], ["南门", "南门"]),
])
def test_paired_markdown_and_unique_direction_quotes_keep_exact_coordinates(source, rows, names):
    before = plan(source, [("青溪公园", 1, 1)])
    result = apply(source, before, rows)
    assert result.unprocessed_count == 0
    assert [m.atomic_place_name for m in result.mentions[1:]] == names
    for mention in result.mentions[1:]:
        assert source[mention.span_start:mention.span_end] == mention.raw_text
        assert source[mention.role_evidence_start:mention.role_evidence_end] == mention.role_evidence


@pytest.mark.parametrize("source,parents,row", [
    ("北京。\nDay1：青溪公园，从南门进，从南门出。", [("青溪公园", 1, 1)],
     location(evidence="青溪公园，从南门进，从南门出")),
    ("北京。\nDay1：青溪公园，从南门进。\nDay2：青溪公园，从南门进。", [("青溪公园", 1, 1), ("青溪公园", 2, 2)],
     location(evidence="青溪公园，从南门进")),
    ("北京。\nDay1：青溪公园。\nDay2：星河公园，从南门进。", [("青溪公园", 1, 1), ("星河公园", 2, 1)],
     location(evidence="星河公园，从南门进")),
    ("北京。\nDay1：青溪公园。随后去星河公园，从南门进。", [("青溪公园", 1, 1), ("星河公园", 1, 1)],
     location(evidence="星河公园，从南门进")),
    ("北京。\nDay1：青溪公园，午餐吃南门涮肉。", [("青溪公园", 1, 1)],
     location(evidence="午餐吃南门涮肉")),
    ("北京。\nDay1：青溪公园，不从南门进，从北门进。", [("青溪公园", 1, 1)],
     location(evidence="南门进")),
    ("北京。\nDay1：青溪公园，不**从**南门进，从北门进。", [("青溪公园", 1, 1)],
     location(evidence="青溪公园，不从南门进，从北门进")),
    ("北京。\nDay1：青溪公园，眺望故宫全景。", [("青溪公园", 1, 1)],
     location("故宫", "VISIT", "眺望故宫全景")),
    ("北京。\nDay1：青溪公园，园内参观青溪公园。", [("青溪公园", 1, 1)],
     location("青溪公园", "VISIT", "园内参观青溪公园")),
    ("北京。\nDay1：青溪公园，取消参观晨光亭。", [("青溪公园", 1, 1)],
     location("晨光亭", "VISIT", "取消参观晨光亭")),
    ("北京。\nDay1：青溪公园，园内若有时间参观晨光亭。", [("青溪公园", 1, 1)],
     location("晨光亭", "VISIT", "园内若有时间参观晨光亭")),
    ("北京。\nDay1：青溪公园，从南门出。", [("青溪公园", 1, 1)],
     location(evidence="从南门出")),
    ("北京。\nDay1：青溪公园，从南门进。", [("青溪公园", 1, 1)],
     location(evidence="从南门进", occurrence=None)),
])
def test_ambiguous_or_wrong_local_scope_does_not_succeed(source, parents, row):
    before = plan(source, parents)
    after = apply(source, before, [row])
    assert after.mentions == before.mentions
    assert after.unprocessed_count > before.unprocessed_count


@pytest.mark.asyncio
async def test_strict_second_schema_requires_local_fields_and_forbids_new_global_occurrence():
    source = "北京。Day1：青溪公园，从南门进。"
    first = dict(destination="北京", day_labels=[None], unprocessed_quotes=[], activities=[
        dict(source_quote="青溪公园", place_name="青溪公园", role="PLANNED", day_index=1, category="景点")])
    second = dict(city_fields=[], source_visits=[location(evidence="从南门进")])
    async with recorded_provider(first, second) as (engine, requests):
        engine.enable_source_visits = True
        result = await engine.propose(source)
    assert len(requests) == 2 and result.unprocessed_count == 0
    wire = requests[1]["response_format"]
    assert wire["type"] == "json_schema" and wire["json_schema"]["strict"] is True
    assert requests[1]["max_tokens"] == 4096 and requests[1]["enable_thinking"] is False
    validator = Draft202012Validator(wire["json_schema"]["schema"])
    validator.validate(second)
    for invalid in (location(evidence="从南门进", occurrence=1), location(source_quote="名"*101),
                    location(evidence="证"*501), {k:v for k,v in location().items() if k!='optional'}):
        assert list(validator.iter_errors(dict(city_fields=[], source_visits=[invalid])))
    validator.validate(dict(city_fields=[dict(index=0,city=None,city_evidence=None)], source_visits=[]))
