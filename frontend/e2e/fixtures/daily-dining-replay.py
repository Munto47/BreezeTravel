"""Fixed places only; current meal context and public commands remain real."""
import json
import sys
from types import SimpleNamespace

from pydantic import TypeAdapter

from app.trip_understanding.commands import apply_public_command
from app.trip_understanding.daily_dining import DailyDiningView, meal_context
from app.trip_understanding.models import TripUnderstandingCommand, UserFacingTripResult
from tests.test_dining_recommendations import anchor, restaurant


def plan_for(result):
    return SimpleNamespace(stops=[anchor().model_copy(update={
        "activity_token": card.activity_token, "name": card.name, "city": card.city,
        "day_index": i + 1, "day_label": day.label, "sequence_index": j,
        "category": card.category,
    }) for i, day in enumerate(result.days) for j, card in enumerate(day.activities)
        if card.status == "READY"])


def run(data):
    result = UserFacingTripResult.model_validate(data["result"])
    plan = plan_for(result)
    if data["operation"] == "daily":
        days = []
        for index, day in enumerate(result.days, 1):
            view, selected_anchor, _ = meal_context(day, [s for s in plan.stops if s.day_index == index])
            view["day_index"] = index
            if selected_anchor:
                view.update(status="AVAILABLE", message="固定餐饮响应：选择门店后才加入行程。",
                            area="固定附近用餐区", area_relation="NEARBY", area_distance_m=480,
                            candidates=data["candidates"])
            days.append(view)
        return DailyDiningView(status="AVAILABLE", message="固定地点服务，仅验证页面与采纳。", days=days).model_dump(mode="json")
    command = TypeAdapter(TripUnderstandingCommand).validate_python(data["command"])
    candidate = next((item for item in data["candidates"] if item["candidate_token"] == getattr(command, "candidate_token", None)), None)
    kwargs = {"dining_plan": plan}
    if candidate:
        kwargs["confirmed_place"] = restaurant().model_copy(update={
            "name": candidate["name"], "city": "北京", "area_or_address": candidate["area_or_address"],
        })
    if data.get("undo"):
        kwargs["undo_result"] = UserFacingTripResult.model_validate(data["undo"])
    changed = apply_public_command(result, command, **kwargs).result
    # Saved result must validate through a fresh reader, including slot links.
    return UserFacingTripResult.model_validate_json(changed.model_dump_json()).model_dump(mode="json")


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin)), ensure_ascii=False))
