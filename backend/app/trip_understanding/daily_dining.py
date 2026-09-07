"""Daily meal contexts and bounded, independently verifiable detour comparisons."""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from app.trip_understanding.candidates import CandidatePlace, issue_candidate
from app.trip_understanding.dining import dining_binding, search_dining, valid_anchor
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.models import StrictModel


class DailyMealCandidate(StrictModel):
    candidate_token: str
    name: str
    area_or_address: str
    business_area: str | None = None
    reason: str
    extra_minutes: int | None = None
    recommended: bool = False


class DailyMealView(StrictModel):
    day_index: int
    label: str
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE", "EXISTING", "NEEDS_CONFIRMATION"]
    message: str
    after_activity_token: str | None = None
    insert_before: bool = False
    meal_role: Literal["LUNCH"] | None = None
    next_name: str | None = None
    existing_activity_token: str | None = None
    area: str | None = None
    candidates: list[DailyMealCandidate] = Field(default_factory=list, max_length=3)


class DailyDiningView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "UNAVAILABLE"]
    message: str
    days: list[DailyMealView] = Field(default_factory=list)


def meal_context(day, stops: list[MapStop]) -> tuple[dict, MapStop | None, MapStop | None]:
    """Only confirmed execution stops anchor suggestions; a named meal is retained."""
    view = {"day_index": 1, "label": day.label, "status": "NEEDS_CONFIRMATION",
            "message": "先确认当天地点，再补充中途用餐。", "candidates": []}
    by_token = {s.activity_token: s for s in stops if valid_anchor(s) and not s.is_stay_anchor}
    cards = day.activities
    # A breakfast/dinner with an explicit hour is not treated as lunch.
    meals = [c for c in cards if c.category == "餐饮" and c.status == "READY"
             and (getattr(c,"meal_role",None) == "LUNCH" or
                  (getattr(c,"meal_role",None) is None and (not c.start_time or "10:30" <= c.start_time <= "15:00")))]
    if meals:
        view.update(status="EXISTING", message=f"已安排{meals[0].name}，可在地点卡片更换。",
                    existing_activity_token=meals[0].activity_token)
        return view, None, None
    named = [c for c in cards if c.activity_token in by_token and c.category not in ("餐饮", "住宿", "交通节点")]
    if not named:
        return view, None, None
    position = max(0, (len(named) - 1) // 2)
    explicit_slot = next((slot for slot in getattr(day, "meal_slots", []) if slot.meal_role == "LUNCH"), None)
    if explicit_slot:
        view["meal_role"] = "LUNCH"
        after = by_token.get(explicit_slot.after_activity_token)
        before = by_token.get(explicit_slot.before_activity_token)
        if after:
            view.update(after_activity_token=after.activity_token,next_name=before.name if before else None)
            return view, after, before if before and before.city == after.city else None
        if before:
            view.update(after_activity_token=before.activity_token,insert_before=True,next_name=before.name)
            return view, before, None
    unnamed = next((i for i, c in enumerate(cards) if c.category == "餐饮"
                    and c.status != "READY" and any(w in c.name for w in ("午餐", "午饭", "中午", "用餐"))), None)
    if unnamed is not None:
        prior = [i for i, c in enumerate(named) if cards.index(c) < unnamed]
        if prior:
            position = prior[-1]
    anchor = by_token[named[position].activity_token]
    next_stop = by_token[named[position + 1].activity_token] if position + 1 < len(named) else None
    # A transfer to another city is not a local meal corridor.
    if next_stop and next_stop.city != anchor.city:
        next_stop = None
    view.update(after_activity_token=anchor.activity_token,
                next_name=next_stop.name if next_stop else None)
    return view, anchor, next_stop


async def build_daily_meals(result, plan, *, search=search_dining, routes=None,
                            deadline_seconds: float = 80, stats: dict | None = None) -> list[dict]:
    """Stores verified places, not expiring selection tokens or private source text."""
    output = []
    route_cache: dict[tuple, int | None] = {}
    route_count = 0
    stats = stats if stats is not None else {}
    stats.update(poi_http_attempts=0, district_http_attempts=0, route_dispatches=0,
                 route_http_calls=0, route_calls_unknown=0, estimated_cost_cny=None)
    deadline = time.monotonic() + deadline_seconds
    now = datetime.now(timezone.utc)

    async def duration(a: MapStop, b: MapStop) -> int | None:
        nonlocal route_count
        key = (a.canonical_place_id, b.canonical_place_id)
        if key in route_cache:
            return route_cache[key]
        answer = None
        if routes:
            for mode in ("walking", "transit"):
                remaining = deadline - time.monotonic()
                if route_count >= 48 or remaining <= 0:
                    break
                route_count += 1
                stats["route_dispatches"] += 1
                try:
                    async with asyncio.timeout(min(3, remaining)):
                        fact = await routes.route(a, b, mode, observed_at=now)
                    calls = getattr(fact, "external_call_count", None)
                    if calls is None:
                        stats["route_calls_unknown"] += 1
                    else:
                        stats["route_http_calls"] += calls
                    minutes = fact.duration_minutes if fact.status == "AVAILABLE" else None
                    if minutes is not None and (mode == "transit" or minutes <= 30):
                        answer = minutes
                        break
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    calls = getattr(error, "external_call_count", None)
                    if calls is None:
                        stats["route_calls_unknown"] += 1
                    else:
                        stats["route_http_calls"] += calls
                    continue
        route_cache[key] = answer
        return answer

    for index, day in enumerate(result.days, 1):
        stops = [s for s in plan.stops if s.day_index == index]
        view, anchor, next_stop = meal_context(day, stops)
        view["day_index"] = index
        if not anchor:
            output.append(view)
            continue
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                places = None
            else:
                async with asyncio.timeout(min(10, remaining)):
                    receipt = {}
                    try:
                        places = await search(anchor=anchor, excluded_ids={s.canonical_place_id for s in stops if s.canonical_place_id},
                            **({"receipt": receipt} if search is search_dining else {}))
                    finally:
                        for key in ("poi_http_attempts", "district_http_attempts"):
                            stats[key] += receipt.get(key, 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            places = None
        if places is None:
            view.update(status="UNAVAILABLE", message="附近餐饮暂时无法查询，可稍后更新。")
        elif not places:
            view.update(status="EMPTY", message="附近暂未找到合适饭店，可选择其他地点查询。")
        else:
            baseline = await duration(anchor, next_stop) if next_stop else None
            ranked = []
            for place in places[:3]:
                meal = MapStop(day_index=index, day_label=day.label, sequence_index=anchor.sequence_index + 1,
                    name=place.name, category="餐饮", canonical_place_id=place.canonical_place_id,
                    resolution_status="AUTO_MATCHED", city=place.city,
                    longitude=place.position.longitude, latitude=place.position.latitude)
                extra = None
                if baseline is not None and next_stop:
                    first, second = await duration(anchor, meal), await duration(meal, next_stop)
                    if first is not None and second is not None:
                        extra = max(0, first + second - baseline)
                ranked.append({"place": place.model_dump(), "extra_minutes": extra,
                    "reason": f"经此店前往下一站约多{extra}分钟；营业情况请到店前确认。" if extra is not None
                    else f"在{anchor.name}附近；绕路时间及营业情况尚未确认。"})
            ranked.sort(key=lambda r: (r["extra_minutes"] is None, r["extra_minutes"] or 0, r["place"]["name"]))
            view.update(status="AVAILABLE", message="中途用餐建议，选择后才加入行程。",
                area=next((r["place"].get("business_area") for r in ranked if r["place"].get("business_area")), None),
                candidates=ranked)
        output.append(view)
    stats["days"] = len(output)
    stats["available_days"] = sum(row["status"] == "AVAILABLE" for row in output)
    stats["existing_meal_days"] = sum(row["status"] == "EXISTING" for row in output)
    return output


def project_daily_meals(rows: list[dict], *, public_resource_id: str, etag: str) -> list[DailyMealView]:
    output = []
    for raw in rows:
        row = dict(raw)
        candidates = []
        for index, item in enumerate(row.pop("candidates", [])):
            place = CandidatePlace.model_validate(item["place"])
            issued = issue_candidate(place, public_resource_id=public_resource_id,
                activity_token=dining_binding(row["after_activity_token"], before=row.get("insert_before",False)), expected_etag=etag,
                now=datetime.now(timezone.utc))
            candidates.append(DailyMealCandidate(candidate_token=issued.candidate_token, name=place.name,
                area_or_address=place.area_or_address, business_area=place.business_area,
                reason=item["reason"], extra_minutes=item["extra_minutes"], recommended=index == 0))
        output.append(DailyMealView(**row, candidates=candidates))
    return output
