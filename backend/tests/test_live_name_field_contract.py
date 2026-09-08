"""Live wire omissions stay incomplete; explicit null keeps unnamed activities."""
import json
from pathlib import Path

import pytest

from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_inference import Client, provider
from tests.test_semantic_day_sections import RecordingPlaces


@pytest.mark.asyncio
async def test_observed_all_missing_names_never_become_anonymous_success():
    # Actual completed model JSON from the bounded thinking experiment. The
    # replay exercises the provider and public result, not model accuracy.
    sample = json.loads((Path(__file__).parent / "fixtures/live_missing_place_name.json").read_text(encoding="utf-8"))
    content = json.dumps(sample["model_response"], ensure_ascii=False)
    places = RecordingPlaces()
    output = await TripUnderstandingPipeline(provider(Client(content, content)), places).run(sample["source"])
    assert places.calls == []
    assert output.public_result.status == "PARTIAL_RESULT"
    assert output.public_result.coverage.complete is False
    assert output.public_result.coverage.unprocessed_count >= 12
    assert len(output.public_result.days) == 2
    assert all(day.unprocessed_count > 0 for day in output.public_result.days)
    assert all(not day.activities for day in output.public_result.days)
    assert output.inference_binding["outcome"] == "PARTIAL_RESULT"
    assert output.inference_binding["external_calls"] == 2
    assert output.inference_binding["semantic_diagnostic_counts"]["MISSING_PLACE_NAME_FIELD"] == 12


@pytest.mark.asyncio
async def test_missing_name_stays_local_and_explicit_null_lunch_is_preserved():
    source = "成都两天。\n第一天去望江楼公园，中午午餐待定。\n第二天去金沙遗址博物馆。"
    payload = dict(destination="成都", day_labels=[None, None], activities=[
        dict(source_quote="望江楼公园", place_name="望江楼公园", role="PLANNED", day_index=1),
        dict(source_quote="午餐待定", place_name=None, role="PLANNED", day_index=1, category="餐饮", meal_role="LUNCH"),
        dict(source_quote="金沙遗址博物馆", role="PLANNED", day_index=2),
    ])
    content = json.dumps(payload, ensure_ascii=False)
    places = RecordingPlaces()
    output = await TripUnderstandingPipeline(provider(Client(content, content)), places).run(source)
    assert places.calls == [("成都", "望江楼公园")]
    mentions = [row.compiled.mention for row in output.activities]
    assert [(m.atomic_place_name, m.day_index) for m in mentions] == [("望江楼公园", 1), (None, 1)]
    assert mentions[1].category_hint == "餐饮" and mentions[1].meal_role == "LUNCH"
    assert output.public_result.status == "PARTIAL_RESULT"
    assert output.public_result.coverage.complete is False
    assert [day.unprocessed_count for day in output.public_result.days] == [0, 1]
    assert output.inference_binding["semantic_diagnostic_counts"]["MISSING_PLACE_NAME_FIELD"] == 1


@pytest.mark.asyncio
async def test_bounded_repair_can_supply_name_without_reclassifying_valid_lunch():
    source = "成都一天，先去望江楼公园，中午午餐待定，然后去金沙遗址博物馆。"
    payload = dict(destination="成都", day_labels=[None], activities=[
        dict(source_quote="望江楼公园", place_name="望江楼公园", role="PLANNED", day_index=1),
        dict(source_quote="午餐待定", place_name=None, role="PLANNED", day_index=1, category="餐饮", meal_role="LUNCH"),
        dict(source_quote="金沙遗址博物馆", role="PLANNED", day_index=1),
    ])
    repair = json.loads(json.dumps(payload))
    repair["activities"][2]["place_name"] = "金沙遗址博物馆"
    client = Client(json.dumps(payload), json.dumps(repair))
    places = RecordingPlaces()
    output = await TripUnderstandingPipeline(provider(client), places).run(source)
    assert places.calls == [("成都", "望江楼公园"), ("成都", "金沙遗址博物馆")]
    assert [row.compiled.mention.atomic_place_name for row in output.activities] == ["望江楼公园", None, "金沙遗址博物馆"]
    assert output.inference_binding["outcome"] == "SUCCESS"
    assert output.inference_binding["external_calls"] == 2
    assert output.public_result.coverage.unprocessed_count == 0
    assert [day.unprocessed_count for day in output.public_result.days] == [0]


@pytest.mark.asyncio
async def test_explicit_null_is_valid_on_the_first_call():
    source = "成都一天。上午去望江楼公园，中午午餐待定。"
    payload = dict(destination="成都", day_labels=[None], activities=[
        dict(source_quote="望江楼公园", place_name="望江楼公园", role="PLANNED", day_index=1),
        dict(source_quote="午餐待定", place_name=None, role="PLANNED", day_index=1, category="餐饮", meal_role="LUNCH"),
    ])
    client = Client(json.dumps(payload))
    output = await TripUnderstandingPipeline(provider(client), RecordingPlaces()).run(source)
    assert len(client.calls) == 1
    assert output.inference_binding["outcome"] == "SUCCESS"
    assert output.public_result.coverage.unprocessed_count == 0
    assert [row.compiled.mention.atomic_place_name for row in output.activities] == ["望江楼公园", None]


@pytest.mark.asyncio
async def test_repair_cannot_hide_the_missing_second_day_by_removing_its_row():
    source = "成都两天。\n第一天去望江楼公园。\n第二天去金沙遗址博物馆。"
    payload = dict(destination="成都", day_labels=[None, None], activities=[
        dict(source_quote="望江楼公园", place_name="望江楼公园", role="PLANNED", day_index=1),
        dict(source_quote="金沙遗址博物馆", role="PLANNED", day_index=2),
    ])
    repair = {**payload, "activities": payload["activities"][:1]}
    output = await TripUnderstandingPipeline(provider(Client(json.dumps(payload), json.dumps(repair))), RecordingPlaces()).run(source)
    assert output.public_result.status == "PARTIAL_RESULT"
    assert not output.public_result.coverage.complete
    assert [day.unprocessed_count for day in output.public_result.days] == [0, 1]
    assert output.inference_binding["semantic_diagnostic_counts"]["REPAIR_OMITTED_SOURCE_ITEM"] == 1
