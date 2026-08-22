from app.constraints.base import RuleContext
from app.constraints.rules._utils import find_constraints, normalise
from app.schemas.verification import ConstraintCheck, ConstraintStatus


class WeatherRule:
    rule_id = "weather"

    def evaluate(self, context: RuleContext) -> list[ConstraintCheck]:
        constraints = find_constraints(context.task_spec, "avoid_outdoor_on_rain")
        if not constraints:
            return []
        constraint = constraints[0]
        system_mode = constraint.id.startswith("system:")
        checks = []
        for day in context.itinerary.days:
            weather = day.weather_summary
            if weather is None:
                checks.append(ConstraintCheck(
                    constraint_id=constraint.id,
                    status=ConstraintStatus.UNKNOWN,
                    reason_code="WEATHER_DATA_MISSING",
                    message=f"第 {day.day_index + 1} 天缺少天气数据",
                    day_index=day.day_index,
                ))
                continue
            rainy = any(token in weather.condition.lower() for token in ("雨", "rain", "storm"))
            outdoor = [slot for slot in day.slots if any(token in normalise((slot.place or {}).get("tags")) for token in ("户外", "公园", "徒步", "景区"))]
            if not system_mode:
                violated = rainy and bool(outdoor)
                unknown = False
            elif weather.precip_mm is None:
                violated = False
                unknown = True
            else:
                # This is the exact system-level boundary previously owned by
                # critic_v2.  Explicit user constraints above remain stricter.
                violated = weather.precip_mm > 5 and len(outdoor) >= 2
                unknown = False
            status = (
                ConstraintStatus.UNKNOWN
                if unknown
                else ConstraintStatus.VIOLATED
                if violated
                else ConstraintStatus.SATISFIED
            )
            checks.append(ConstraintCheck(
                constraint_id=constraint.id,
                status=status,
                reason_code=(
                    "WEATHER_PRECIPITATION_MISSING"
                    if unknown
                    else "RAIN_OUTDOOR_CONFLICT"
                    if violated
                    else "WEATHER_CONSTRAINT_OK"
                ),
                message=f"第 {day.day_index + 1} 天" + (
                    "缺少可验证降水量"
                    if unknown
                    else f"雨天仍有 {len(outdoor)} 个户外地点"
                    if violated
                    else "天气安排符合约束"
                ),
                day_index=day.day_index,
                place_id=outdoor[0].place_id if violated else None,
                repairable=violated,
            ))
        return checks
