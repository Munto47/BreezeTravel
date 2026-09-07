"""Transport actions without a station name are source facts, not place cards."""
import pytest

from app.trip_understanding.map_repository import _plan_for_result
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_text_fidelity import DraftProvider, RecordingResolver, activity


@pytest.mark.asyncio
async def test_unnamed_intercity_transfer_is_retained_without_an_unmatched_card_or_false_partial():
    source = "Day1 北京：故宫博物院。\nDay2 上海：从北京乘高铁到上海，游览外滩。"
    transport = {"source_quote": "从北京乘高铁到上海", "place_name": None, "role": "PLANNED",
        "day_index": 2, "category": "交通节点"}
    rows = [activity("故宫博物院", city="北京", city_evidence="Day1 北京"), transport,
        activity("外滩", 2, city="上海", city_evidence="Day2 上海")]
    resolver = RecordingResolver()
    output = await TripUnderstandingPipeline(DraftProvider(rows), resolver).run(source)
    assert len(output.proposal.mentions) == 3
    assert any(claim.quote == "从北京乘高铁到上海" for claim in output.claims)
    assert [card.name for day in output.public_result.days for card in day.activities] == ["故宫博物院", "外滩"]
    assert output.public_result.coverage.recognized_place_count == 2
    assert output.public_result.coverage.unresolved_place_count == 0
    assert output.public_result.coverage.complete and output.public_result.status == "READY"
    assert len(resolver.calls) == 2


@pytest.mark.asyncio
async def test_named_station_still_requires_confirmation_and_remains_the_real_day_boundary():
    source = "Day1：月光车站。Day2：故宫博物院。"
    rows = [{**activity("月光车站"), "category": "交通节点"}, activity("故宫博物院", 2)]
    output = await TripUnderstandingPipeline(DraftProvider(rows), RecordingResolver()).run(source)
    assert output.public_result.days[0].activities[0].name == "月光车站"
    assert output.public_result.days[0].activities[0].status == "NEEDS_CONFIRMATION"
    assert output.public_result.coverage.recognized_place_count == 2
    assert output.public_result.coverage.unresolved_place_count == 1
    assert not output.public_result.coverage.complete
    plan = _plan_for_result("transport-test", 2, output.public_result, {})
    assert plan.stops[0].name == "月光车站" and plan.stops[0].day_index == 1
