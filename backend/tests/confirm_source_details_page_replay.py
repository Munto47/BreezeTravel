"""Fixed external replies; actual provider, pipeline and confirmation/undo commands.

This is a browser fixture, not a new live model/POI or persistence measurement.
"""
from __future__ import annotations

import asyncio
import json

from app.trip_understanding.candidates import CandidatePlace, GCJ02Position
from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.models import PlaceConfirmCommand, UndoCommand
from app.trip_understanding.pipeline import TripUnderstandingPipeline
from tests.test_source_details_readback import CapturedClient, DETAILS, RecordingPlaces, provider, raw_response


SOURCE = (
    "Day1：故宫博物院，园内路线：太和殿、乾清宫。之后去景山公园。\n"
    "Day2：上海，去外滩；若有时间，可去豫园。"
)
CANDIDATE_TOKEN = "fixed-confirm-details-candidate-20260913"


async def build():
    raw = raw_response()
    raw["destination"] = "目的地待确认"
    raw["day_labels"] = ["Day1", "Day2"]
    for row in raw["activities"]:
        row["city"] = row["city_evidence"] = None
    raw["activities"].extend([
        {"source_quote": "外滩", "place_name": "外滩", "role": "PLANNED", "day_index": 2,
         "category": "景点", "city": "上海", "city_evidence": "上海"},
        {"source_quote": "豫园", "place_name": "豫园", "role": "OPTIONAL", "day_index": 2,
         "category": "景点", "city": "上海", "city_evidence": "上海",
         "role_evidence": "若有时间，可去豫园"},
    ])
    client = CapturedClient(raw)
    places = RecordingPlaces()
    output = await TripUnderstandingPipeline(provider(client), places).run(SOURCE)
    before = output.public_result
    parent = before.days[0].activities[0]
    assert parent.name == "故宫博物院" and parent.city is None and parent.status == "NEEDS_CONFIRMATION"
    assert [item.model_dump(mode="json") for item in parent.source_details] == DETAILS
    assert [item.name for item in before.days[1].activities] == ["外滩"]
    assert before.days[1].activities[0].status == "READY"
    assert [item.name for item in before.days[1].alternatives] == ["豫园"]
    selected = CandidatePlace(canonical_place_id="fixed-palace-canonical-id", name=parent.name,
        city="北京", category="景点", area_or_address="固定候选地址，仅用于页面验证",
        position=GCJ02Position(longitude=116.4, latitude=39.9))
    confirm = PlaceConfirmCommand(command_type="PLACE_CONFIRM", activity_token=parent.activity_token,
        candidate_token=CANDIDATE_TOKEN)
    after = apply_public_command(before, confirm, confirmed_place=selected, current_place_id=None).result
    undo_command = UndoCommand(command_type="UNDO")
    undone = apply_public_command(after, undo_command, undo_result=before).result
    for snapshot in (before, after, undone):
        assert [item.model_dump(mode="json") for item in snapshot.days[0].activities[0].source_details] == DETAILS
        assert [[item.name for item in day.activities] for day in snapshot.days] == [
            ["故宫博物院", "景山公园"], ["外滩"]]
        assert [item.name for item in snapshot.days[1].alternatives] == ["豫园"]
    assert after.days[0].activities[0].status == "READY" and after.days[0].activities[0].city == "北京"
    assert undone.days[0].activities[0].status == "NEEDS_CONFIRMATION" and undone.days[0].activities[0].city is None
    return {
        "states": {name: value.model_dump(mode="json") for name, value in
                   (("before", before), ("confirmed", after), ("undone", undone))},
        "edges": [
            {"start": "before", "end": "confirmed", "command": confirm.model_dump(mode="json")},
            {"start": "confirmed", "end": "undone", "command": undo_command.model_dump(mode="json")},
        ],
        "candidate": {"candidate_token": CANDIDATE_TOKEN, "name": selected.name, "category": selected.category,
                      "area_or_address": selected.area_or_address, "position": selected.position.model_dump(mode="json")},
        "evidence": {"source": SOURCE, "fixed_model_replies": len(client.calls),
                     "fixed_place_queries": places.calls, "external_http_calls": 0,
                     "snapshot_generation": "actual provider -> pipeline -> apply_public_command PLACE_CONFIRM -> UNDO"},
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(build()), ensure_ascii=False))
