"""Daily meal contexts and bounded, independently verifiable detour comparisons."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from app.trip_understanding.candidates import CandidatePlace, DiningPOIInfo, issue_candidate
from app.trip_understanding.dining import bind_dining_access, dining_binding, meal_evidence_status, search_dining, valid_anchor
from app.trip_understanding.dining_areas import nearby_dining_area
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.route_connection import compared_path_scope, route_segment
from app.trip_understanding.models import StrictModel, DiningAccessView


class DailyMealCandidate(StrictModel):
    candidate_token: str
    name: str
    area_or_address: str
    business_area: str | None = None
    reason: str
    extra_minutes: int | None = None
    route_coverage_scope: Literal["REQUESTED_POINTS", "RETURNED_SEGMENTS"] | None = None
    recommended: bool = False
    dining_info: DiningPOIInfo | None = None
    dining_access: DiningAccessView | None = None
    meal_evidence_status: Literal["LIGHT_FOOD_ITEMS_ONLY", "UNSPECIFIED"] = "UNSPECIFIED"


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
    area_relation: Literal["PROVIDER_AREA", "NEARBY"] | None = None
    area_distance_m: int | None = Field(default=None, ge=0)
    candidates: list[DailyMealCandidate] = Field(default_factory=list, max_length=3)


class DailyDiningView(StrictModel):
    status: Literal["PREPARING", "AVAILABLE", "NEEDS_UPDATE", "UNAVAILABLE"]
    message: str
    days: list[DailyMealView] = Field(default_factory=list)


def _unnamed_meal_card(card) -> bool:
    name = card.name.strip()
    return name in {"地点待确认", "用餐地点待确认", "午餐", "午饭", "用餐"} or bool(
        re.match(r"^(?:午餐|午饭|中午|用餐)(?:[：:、\s]|$)", name))


def source_lunch_gaps(result, records) -> dict[str, str]:
    """Recover only explicit lunch meaning; retain no source text in derived facts."""
    rows = {row["public_activity_token"]: row for row in records}
    gaps = {}
    for day in result.days:
        for card in day.activities:
            if card.category != "餐饮" or card.status == "READY" or not _unnamed_meal_card(card):
                continue
            if card.meal_role not in (None, "LUNCH"):
                continue
            row = rows.get(card.activity_token, {})
            receipt = row.get("resolver_receipt") or {}
            saved = receipt.get("source_lunch_gap")
            quote = str(row.get("mention_text") or "") if row.get("atomic_place_name") is None else ""
            explicit = bool(re.search(r"午餐|午饭|中午", quote)) and not re.search(r"不吃|不安排|不要|无需|取消|仅供参考", quote)
            if card.meal_role != "LUNCH" and not explicit and saved not in {"POSITIONAL", "DAY_MIDPOINT"}:
                continue
            midpoint = saved == "DAY_MIDPOINT" or bool(explicit and re.search(r"每天|每日|各天", quote))
            gaps[card.activity_token] = "DAY_MIDPOINT" if midpoint and not card.start_time and not day.meal_slots else "POSITIONAL"
    return gaps


def meal_context(day, stops: list[MapStop], *, source_gaps: dict[str, str] | None = None) -> tuple[dict, MapStop | None, MapStop | None]:
    """Only confirmed execution stops anchor suggestions; a named meal is retained."""
    view = {"day_index": 1, "label": day.label, "status": "NEEDS_CONFIRMATION",
            "message": "先确认当天地点，再补充中途用餐。", "candidates": []}
    by_token = {s.activity_token: s for s in stops if valid_anchor(s) and not s.is_stay_anchor
                and not (s.category == "住宿" and s.lodging_scope == "WHOLE_TRIP")}
    cards = day.activities
    source_gaps = source_gaps or {}
    explicit_slot = next((slot for slot in getattr(day, "meal_slots", []) if slot.meal_role == "LUNCH"), None)
    # A breakfast/dinner with an explicit hour is not treated as lunch.
    meals = [c for c in cards if c.category == "餐饮" and c.status == "READY"
             and (getattr(c,"meal_role",None) == "LUNCH" or
                  (getattr(c,"meal_role",None) is None and getattr(c, "meal_evidence_status", "UNSPECIFIED") != "LIGHT_FOOD_ITEMS_ONLY" and
                   ((not c.start_time and not explicit_slot) or (c.start_time and "10:30" <= c.start_time <= "15:00"))))]
    if meals:
        view.update(status="EXISTING", message=f"已安排{meals[0].name}，可在地点卡片更换。",
                    existing_activity_token=meals[0].activity_token)
        return view, None, None
    pending_meals = [c for c in cards if c.category == "餐饮" and c.status != "READY"
                     and not _unnamed_meal_card(c)
                     and (getattr(c, "meal_role", None) == "LUNCH" or
                          (getattr(c, "meal_role", None) is None and getattr(c, "meal_evidence_status", "UNSPECIFIED") != "LIGHT_FOOD_ITEMS_ONLY" and
                           ((not c.start_time and not explicit_slot) or
                            (c.start_time and "10:30" <= c.start_time <= "15:00"))))]
    if pending_meals:
        meal = pending_meals[0]
        view.update(message=f"原文用餐地点{meal.name}尚未确认，请先确认这处地点。",
                    existing_activity_token=meal.activity_token, meal_role="LUNCH")
        return view, None, None
    if sum(card.activity_token in source_gaps for card in cards) > 1:
        view["message"] = "原文有多处午餐待补充，请先明确要安排哪一处。"
        return view, None, None
    named = [c for c in cards if c.category not in ("餐饮", "住宿", "交通节点")]
    if not named:
        return view, None, None
    position = max(0, (len(named) - 1) // 2)
    def at_slot(after_token, before_token):
        view["meal_role"] = "LUNCH"
        after = by_token.get(after_token)
        before = by_token.get(before_token)
        if after:
            next_stop = before if before and before.city == after.city else None
            view.update(after_activity_token=after.activity_token,next_name=next_stop.name if next_stop else None)
            return view, after, next_stop
        if before:
            view.update(after_activity_token=before.activity_token,insert_before=True,next_name=before.name)
            return view, before, None
        # The source fixed this gap. Unrelated confirmed stops must not move it.
        view["message"] = "先确认午餐前后的地点，再补充这里的用餐建议。"
        return view, None, None

    if explicit_slot:
        return at_slot(explicit_slot.after_activity_token, explicit_slot.before_activity_token)
    unnamed = next((i for i, c in enumerate(cards) if c.category == "餐饮"
                    and c.status != "READY" and _unnamed_meal_card(c)
                    and getattr(c, "meal_role", None) in (None, "LUNCH")
                    and (not c.start_time or "10:30" <= c.start_time <= "15:00")
                    and source_gaps.get(c.activity_token) != "DAY_MIDPOINT"), None)
    if unnamed is not None:
        prior = [c for c in named if cards.index(c) < unnamed]
        following = [c for c in named if cards.index(c) > unnamed]
        return at_slot(prior[-1].activity_token if prior else None,
                       following[0].activity_token if following else None)
    if named[position].activity_token not in by_token:
        # Find a confirmed nearby search anchor without pretending an unknown
        # stop disappeared from the actual itinerary's route sequence.
        preceding = [i for i in range(position, -1, -1) if named[i].activity_token in by_token]
        following = [i for i in range(position + 1,len(named)) if named[i].activity_token in by_token]
        if not preceding and not following:
            return view, None, None
        position = (preceding or following)[0]
        if not preceding:
            anchor = by_token[named[position].activity_token]
            view.update(after_activity_token=anchor.activity_token, insert_before=True, next_name=anchor.name)
            return view, anchor, None
    anchor = by_token[named[position].activity_token]
    next_stop = by_token.get(named[position + 1].activity_token) if position + 1 < len(named) else None
    # A transfer to another city is not a local meal corridor.
    if next_stop and next_stop.city != anchor.city:
        next_stop = None
    view.update(after_activity_token=anchor.activity_token,
                next_name=next_stop.name if next_stop else None)
    return view, anchor, next_stop


async def build_daily_meals(result, plan, *, search=search_dining, routes=None, area_search=nearby_dining_area,
                            deadline_seconds: float = 80, stats: dict | None = None,
                            source_gaps: dict[str, str] | None = None) -> list[dict]:
    """Stores verified places, not expiring selection tokens or private source text."""
    output = []
    route_cache: dict[tuple, object | None] = {}
    route_count = 0
    stats = stats if stats is not None else {}
    stats.update(poi_http_attempts=0, district_http_attempts=0, area_http_attempts=0, route_dispatches=0,
                 route_http_calls=0, route_calls_unknown=0, estimated_cost_cny=None)
    deadline = time.monotonic() + deadline_seconds
    now = datetime.now(timezone.utc)

    async def duration(a: MapStop, b: MapStop):
        nonlocal route_count
        key = (a.canonical_place_id, a.longitude, a.latitude, b.canonical_place_id, b.longitude, b.latitude)
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
                        # Preserve the selected-mode policy. Unknown connection
                        # does not trigger a replacement route or extra calls.
                        answer = fact if route_segment(fact, a, b) else None
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
        view, anchor, next_stop = meal_context(day, stops, source_gaps=source_gaps)
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
                            **({"receipt": receipt, "meal_only":True} if search is search_dining else {}))
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
                place = bind_dining_access(place, stops=plan.stops, activity_token=anchor.activity_token,
                    before=bool(view.get("insert_before")))
                meal = MapStop(day_index=index, day_label=day.label, sequence_index=anchor.sequence_index + 1,
                    name=place.name, category="餐饮", canonical_place_id=place.canonical_place_id,
                    resolution_status="AUTO_MATCHED", city=place.city,
                    longitude=place.position.longitude, latitude=place.position.latitude)
                extra = None
                coverage_scope = None
                if baseline is not None and next_stop:
                    first, second = await duration(anchor, meal), await duration(meal, next_stop)
                    coverage_scope = compared_path_scope([baseline], [first, second])
                    if coverage_scope is not None:
                        extra = max(0, first.duration_minutes + second.duration_minutes - baseline.duration_minutes)
                ranked.append({"place": place.model_dump(mode="json"), "extra_minutes": extra,
                    "route_coverage_scope": coverage_scope,
                    "reason": (f"已返回路段比较，经此店约多{extra}分钟；未含未核实衔接，营业情况请到店前确认。" if coverage_scope == "RETURNED_SEGMENTS"
                    else f"经此店前往下一站约多{extra}分钟；营业情况请到店前确认。") if extra is not None
                    else f"在{anchor.name}附近；绕路时间及营业情况尚未确认。"})
            ranked.sort(key=lambda r: (r["extra_minutes"] is None, r["extra_minutes"] or 0, r["place"]["name"]))
            view.update(status="AVAILABLE", message="中途用餐建议，选择后才加入行程。",
                # A backup restaurant's area does not establish the top choice's area.
                area=ranked[0]["place"].get("business_area"),
                candidates=ranked)
            if view["area"]:
                view["area_relation"] = "PROVIDER_AREA"
            elif area_search and (search is search_dining or area_search is not nearby_dining_area):
                # Rank first: an area near a backup does not describe the top choice.
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    area_receipt = {}
                    try:
                        async with asyncio.timeout(min(3, remaining)):
                            area = await area_search(CandidatePlace.model_validate(ranked[0]["place"]),
                                                     receipt=area_receipt, timeout_seconds=min(3, remaining))
                        if area:
                            view.update(area)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass  # Optional area failure preserves verified restaurant candidates.
                    finally:
                        stats["area_http_attempts"] += area_receipt.get("area_http_attempts", 0)
        output.append(view)
    stats["days"] = len(output)
    stats["available_days"] = sum(row["status"] == "AVAILABLE" for row in output)
    stats["existing_meal_days"] = sum(row["status"] == "EXISTING" for row in output)
    return output


def project_daily_meals(rows: list[dict], *, public_resource_id: str, etag: str,
                        expires_at: datetime | None = None, now: datetime | None = None) -> list[DailyMealView]:
    issued_at = now or datetime.now(timezone.utc)
    output = []
    for raw in rows:
        row = dict(raw)
        candidates = []
        for index, item in enumerate(row.pop("candidates", [])):
            place = CandidatePlace.model_validate(item["place"])
            issued = issue_candidate(place, public_resource_id=public_resource_id,
                activity_token=dining_binding(row["after_activity_token"], before=row.get("insert_before",False)), expected_etag=etag,
                now=issued_at, expires_at=expires_at)
            scope = item.get("route_coverage_scope")
            scoped = scope in ("REQUESTED_POINTS", "RETURNED_SEGMENTS")
            candidates.append(DailyMealCandidate(candidate_token=issued.candidate_token, name=place.name,
                area_or_address=place.area_or_address, business_area=place.business_area,
                dining_info=place.dining_info, dining_access=place.dining_access,
                meal_evidence_status=meal_evidence_status(place.dining_info),
                reason=item["reason"] if scoped else "该店位置已保存；绕路时间及营业情况尚未确认。",
                extra_minutes=item.get("extra_minutes") if scoped else None,
                route_coverage_scope=scope if scoped else None, recommended=index == 0 and scoped
                and meal_evidence_status(place.dining_info) != "LIGHT_FOOD_ITEMS_ONLY"
                and (place.dining_access is None or place.dining_access.status == "DURING_VISIT")))
        output.append(DailyMealView(**row, candidates=candidates))
    return output
