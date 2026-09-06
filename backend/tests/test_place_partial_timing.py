"""Failed optional timing evidence cannot destroy valid source-bound cards."""
import json

import pytest

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.full_text import ControlledSnapshotPlaceResolver
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_experience_inference import Client, provider


SOURCE = "北京一日。Day 1：全聚德（前门店）吃饭，然后14:00到景山公园游览60分钟，景山公园已预约必须准时。"


def draft(**bad_fields):
    return {"destination": "北京", "activities": [
        {"source_quote": "全聚德（前门店）", "place_name": "全聚德（前门店）", "day_index": 1,
         "category": "餐饮", "role": "PLANNED", **bad_fields},
        {"source_quote": "景山公园", "place_name": "景山公园", "day_index": 1, "role": "PLANNED",
         "category": "景点", "start_time": "14:00", "visit_duration_minutes": 60, "locked": True,
         "time_evidence": "14:00到景山公园游览60分钟，景山公园已预约必须准时"},
    ]}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_fields", [
    {"locked": True, "fixed_commitment": True},
    {"start_time": "11:00", "visit_duration_minutes": 90, "time_evidence": "这段证据没有出现在原文"},
])
async def test_two_bad_timing_answers_preserve_every_place_and_all_other_verified_timing(bad_fields):
    payload = json.dumps(draft(**bad_fields), ensure_ascii=False)
    client = Client(payload, payload)
    result = await TripUnderstandingPipeline(provider(client), ControlledSnapshotPlaceResolver()).run(SOURCE)
    mentions = result.proposal.mentions
    assert [item.atomic_place_name for item in mentions] == ["全聚德（前门店）", "景山公园"]
    first, second = mentions
    assert first.start_time is None and first.visit_duration_minutes is None
    assert first.locked is False and first.fixed_commitment is False
    assert second.start_time == "14:00" and second.visit_duration_minutes == 60 and second.locked is True
    assert len(result.public_result.days[0].activities) == 2
    assert result.proposal.unprocessed_count >= 1
    assert result.proposal.binding["outcome"] == "PARTIAL_RESULT"
    assert result.proposal.binding["fallback_used"] is True
    assert result.proposal.binding["degraded_timing_activities"] == 1
    assert len(client.calls) == 2
    assert all(call["outcome"] != "SUCCESS" for call in result.proposal.binding["calls"])


@pytest.mark.asyncio
async def test_unverified_timing_cannot_rescue_an_invented_place():
    bad = draft(locked=True)
    bad["activities"][0]["place_name"] = "原文不存在的分店"
    payload = json.dumps(bad, ensure_ascii=False)
    client = Client(payload, payload)
    with pytest.raises(InferenceProviderUnavailableError):
        await provider(client).propose(SOURCE)
    assert len(client.calls) == 2
