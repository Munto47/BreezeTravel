"""Bounded nearby dining lookup. Coordinates and candidates stay server-authoritative."""
from __future__ import annotations

import re
import math
from datetime import datetime, timezone
from typing import Literal

import httpx
from pydantic import Field, field_validator

from app.config import get_settings
from app.constraints.amap_types import classify_amap_type_signals
from app.schemas.place import PlaceCategory
from app.trip_understanding.amap_place import _admin_matches, _coordinates, _PROVIDER_STATUS_SUFFIX_RE
from app.trip_understanding.candidates import CandidatePlace, DiningPOIInfo, GCJ02Position, PublicPlaceCandidate, _CITY_BOUNDS, verify_candidate
from app.trip_understanding.map_render import MapStop
from app.trip_understanding.models import StrictModel, DiningAccessView, DiningInsertCommand, PlaceConfirmCommand, LodgingRecoverCommand, SourceMealRef
from app.trip_understanding.pipeline import atomic_place_rejection_reason
from app.trip_understanding.stay import haversine_meters
from app.trip_understanding.city_scope import CityScope, CityScopeLookup
from app.trip_understanding.errors import CommandTargetChangedError, PlaceProviderUnavailableError

DINING_RADIUS_METERS = 1200
_SCOPES = CityScopeLookup()
_DINING_UNAVAILABLE_SUFFIX_RE = re.compile(
    r"[（(](?:装修中|停业装修|装修停业|停业整顿|已关闭|已停业|永久关闭)[）)]$"
)


def _explicitly_unavailable_dining_name(name: str) -> bool:
    # Only a complete supplier status annotation at the end is evidence.
    # Brand words, negation and weekly opening-hour text are not status checks.
    return bool(_PROVIDER_STATUS_SUFFIX_RE.search(name) or _DINING_UNAVAILABLE_SUFFIX_RE.search(name))


def meal_evidence_status(info: DiningPOIInfo | None) -> str:
    # Only complete, explicit supplier menu tags qualify. A mixed menu or a
    # broad restaurant type is not proof either for or against a full meal.
    light = {"下午茶", "茶", "茶饮", "茶点", "点心", "甜点", "甜品", "蛋糕", "咖啡", "奶茶", "果汁", "冰淇淋", "面包", "饼干"}
    tags = info.tags if info else []
    return "LIGHT_FOOD_ITEMS_ONLY" if 0 < len(tags) < 12 and all(tag in light for tag in tags) else "UNSPECIFIED"


def dining_metadata(place: CandidatePlace, row: dict) -> CandidatePlace:
    """Keep the same available supplier facts in search and nearby candidates."""
    def text(value, limit=60):
        return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= limit and not any(ord(c) < 32 for c in value) else None
    def number(value, maximum=None):
        try:
            parsed = float(value) if isinstance(value, (str, float, int)) and not isinstance(value, bool) else float("nan")
            return parsed if math.isfinite(parsed) and parsed > 0 and (maximum is None or parsed <= maximum) else None
        except ValueError:
            return None
    business = row.get("business") if isinstance(row.get("business"), dict) else {}
    raw_tags = business.get("tag") if isinstance(business.get("tag"), str) else ""
    tags = list(dict.fromkeys(tag for raw in raw_tags.split(",") if (tag := text(raw))))[:12]
    photos = row.get("photos")
    photos = photos if isinstance(photos, list) else [photos] if isinstance(photos, dict) else []
    from app.trip_understanding.models import safe_poi_photo_url
    photo = next((url for raw in photos if isinstance(raw, dict) and (url := safe_poi_photo_url(raw.get("url")))), None)
    info = DiningPOIInfo(photo_url=photo, cuisine=text(business.get("keytag")), tags=tags,
        rating=number(business.get("rating"), 5), cost=number(business.get("cost")), observed_at=datetime.now(timezone.utc))
    area = row.get("business_area") or business.get("business_area")
    area = area if isinstance(area, str) and re.fullmatch(r"[\u4e00-\u9fffA-Za-z0-9·、（）() -]{1,40}", area) else None
    place = place.model_copy(update={"dining_info": info, "meal_evidence_status": meal_evidence_status(info), "business_area": area})
    parent = row.get("parent")
    if isinstance(parent, str) and re.fullmatch(r"[A-Za-z0-9]{5,40}", parent.strip()):
        place.provider_parent_place_id = "amap:" + parent.strip()
        place.dining_access = DiningAccessView(status="NEEDS_REVIEW")
    return place


def bind_dining_access(place: CandidatePlace, *, stops: list[MapStop], activity_token: str | None,
                       before: bool = False) -> CandidatePlace:
    """Bind an exact supplier parent to the actual immediately preceding visit."""
    place = place.model_copy(deep=True)
    place.meal_evidence_status = meal_evidence_status(place.dining_info)
    parent_id = place.provider_parent_place_id
    if not parent_id:
        # Missing legacy supplier metadata cannot be upgraded into free access.
        return place
    place.dining_parent_activity_token = None
    def same_parent(stop):
        # Historical resolver rows use both raw IDs and the amap: namespace.
        return bool(stop.canonical_place_id and stop.canonical_place_id.removeprefix("amap:") == parent_id.removeprefix("amap:"))
    names = {stop.name for stop in stops if same_parent(stop)}
    parent_name = next(iter(names)) if len(names) == 1 else None
    place.dining_access = DiningAccessView(status="NEEDS_REVIEW", parent_name=parent_name)
    anchors = [stop for stop in stops if stop.activity_token == activity_token]
    if len(anchors) != 1:
        return place
    anchor = anchors[0]
    day = sorted((stop for stop in stops if stop.day_index == anchor.day_index), key=lambda stop: stop.sequence_index)
    position = day.index(anchor) + (0 if before else 1)
    if position == 0:
        return place
    parent = day[position - 1]
    if (valid_anchor(parent) and same_parent(parent) and parent.activity_token
            and parent.city == place.city and not parent.is_stay_anchor):
        place.dining_parent_activity_token = parent.activity_token
        place.dining_access = DiningAccessView(status="DURING_VISIT", parent_name=parent.name)
    return place


def validate_dining_access(place: CandidatePlace, *, result, plan, day_index: int, position: int,
                           activity_token: str, before: bool) -> DiningAccessView | None:
    if not place.provider_parent_place_id:
        if place.dining_access is not None:
            raise CommandTargetChangedError("dining access evidence needs review")
        return None
    if plan is None:
        raise CommandTargetChangedError("dining parent visit needs verification")
    rebound = bind_dining_access(place, stops=plan.stops, activity_token=activity_token, before=before)
    cards = result.days[day_index].activities
    if (rebound.dining_access.status != "DURING_VISIT" or place.dining_access is None
            or place.dining_access.status != "DURING_VISIT"
            or rebound.dining_parent_activity_token != place.dining_parent_activity_token
            or position == 0 or cards[position - 1].activity_token != rebound.dining_parent_activity_token
            or cards[position - 1].status != "READY"):
        raise CommandTargetChangedError("dining must remain within this same parent visit before leaving")
    return rebound.dining_access


def validate_dining_meal_use(place: CandidatePlace, *, meal_role: str | None, source_slot: bool = False) -> None:
    if meal_evidence_status(place.dining_info) == "LIGHT_FOOD_ITEMS_ONLY" and meal_role in {"BREAKFAST", "LUNCH", "DINNER"}:
        raise CommandTargetChangedError("only light-food evidence is available; a full meal is not verified")


class DiningSearchRequest(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)


class DiningCandidateView(PublicPlaceCandidate):
    reason: str


class DiningCandidatesView(StrictModel):
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE", "NEEDS_CONFIRMATION"]
    message: str
    candidates: list[DiningCandidateView] = Field(default_factory=list, max_length=3)


class SourceMealPosition(StrictModel):
    activity_token: str = Field(min_length=20, max_length=80)
    insert_before: bool = False


class SourceMealSearchRequest(StrictModel):
    meal_slot: SourceMealRef
    query: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]+$")
    position: SourceMealPosition | None = None

    @field_validator("query")
    @classmethod
    def meaningful_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("a search term is required")
        return value


class SourceMealCandidatesView(StrictModel):
    status: Literal["AVAILABLE", "EMPTY", "UNAVAILABLE", "NEEDS_CONFIRMATION", "POSITION_REQUIRED", "EXISTING", "NEEDS_REVIEW"]
    message: str
    meal_slot: SourceMealRef
    preference_text: str | None = None
    meal_role: Literal["BREAKFAST", "LUNCH", "DINNER", "SNACK"] | None = None
    after_activity_token: str | None = None
    insert_before: bool = False
    anchor_name: str | None = None
    pending_activity_tokens: list[str] = Field(default_factory=list)
    candidates: list[DiningCandidateView] = Field(default_factory=list, max_length=3)


def source_meal_binding(activity_token: str, *, before: bool, meal_slot: SourceMealRef, meal_role: str | None) -> str:
    return f"source-meal:{meal_slot.day_index}:{meal_slot.slot_index}:{meal_role or 'UNSPECIFIED'}:{dining_binding(activity_token, before=before)}"


def source_meal_context(result, plan, meal_slot: SourceMealRef, position: SourceMealPosition | None = None) -> SourceMealCandidatesView:
    """Resolve one explicit source requirement in the authoritative version."""
    from app.trip_understanding.errors import CommandTargetChangedError
    if meal_slot.day_index > len(result.days):
        raise CommandTargetChangedError("source meal day changed")
    day = result.days[meal_slot.day_index - 1]
    if meal_slot.slot_index >= len(day.meal_slots):
        raise CommandTargetChangedError("source meal slot changed")
    slot = day.meal_slots[meal_slot.slot_index]
    view = SourceMealCandidatesView(status="AVAILABLE", message="按手动搜索词查找门店；原文意向仍供你核对。",
        meal_slot=meal_slot, preference_text=slot.preference_text,
        meal_role=None if slot.meal_role == "UNSPECIFIED" else slot.meal_role)
    cards = {card.activity_token: card for card in day.activities}
    if slot.selection_status == "SELECTED":
        selected = cards.get(slot.selected_activity_token)
        view.status = "EXISTING" if selected and selected.status == "READY" else "NEEDS_CONFIRMATION"
        view.message = "这顿用餐已经安排，请在原餐厅卡片调整。" if view.status == "EXISTING" else "先确认这顿用餐已选择的餐厅。"
        view.pending_activity_tokens = [selected.activity_token] if selected and selected.status != "READY" else []
        return view
    existing = [card for card in day.activities if card.category == "餐饮" and card.meal_role == view.meal_role and card.name not in {"地点待确认", "用餐地点待确认"}]
    if existing and slot.selection_status == "UNKNOWN":
        view.status, view.message = "NEEDS_REVIEW", "这份旧安排尚未关联餐位；当天已有同餐别地点，请先核对，避免重复加入。"
        return view
    after, before = slot.after_activity_token, slot.before_activity_token
    if position and (after or before):
        raise CommandTargetChangedError("source meal has an authoritative position")
    if after and before and (after not in cards or before not in cards or day.activities.index(cards[after]) >= day.activities.index(cards[before])):
        view.status, view.message = "POSITION_REQUIRED", "原文用餐位置已发生冲突，请先调整前后地点。"
        return view
    pending = [cards[token] for token in (after, before) if token in cards and cards[token].status != "READY"]
    if pending:
        view.status, view.message = "NEEDS_CONFIRMATION", "先确认原文用餐前后的地点，再查找这顿餐厅。"
        view.pending_activity_tokens = [card.activity_token for card in pending]
        return view
    selected_token, insert_before = (after, False) if after else (before, True)
    if not selected_token:
        if not position:
            view.status, view.message = "POSITION_REQUIRED", "原文未指定这餐的位置，请明确选择加入哪一站之前或之后。"
            return view
        selected_token, insert_before = position.activity_token, position.insert_before
    selected = cards.get(selected_token)
    if selected is None:
        raise CommandTargetChangedError("source meal anchor changed")
    if selected.status != "READY":
        view.status, view.message = "NEEDS_CONFIRMATION", f"先确认用餐位置「{selected.name}」，再查找这顿餐厅。"
        view.pending_activity_tokens = [selected.activity_token]
        return view
    stop = next((stop for stop in plan.stops if stop.day_index == meal_slot.day_index and stop.activity_token == selected_token), None)
    if not valid_anchor(stop) or stop.is_stay_anchor or (stop.category == "住宿" and stop.lodging_scope == "WHOLE_TRIP"):
        view.status, view.message = "NEEDS_CONFIRMATION", "这个用餐位置的地点资料尚未核验，请先确认地点。"
        view.pending_activity_tokens = [selected.activity_token]
        return view
    if after and before:
        other = cards[before]
        if other.city and other.city != stop.city:
            view.status, view.message = "POSITION_REQUIRED", "用餐前后地点不在同一城市，请先明确本餐所在城市与位置。"
            return view
    view.after_activity_token, view.insert_before, view.anchor_name = selected_token, insert_before, selected.name
    return view


def dining_binding(activity_token: str, *, before: bool = False) -> str:
    return f"dining-{'before' if before else 'after'}:{activity_token}"


def verify_command_candidate(command, *, public_resource_id: str, expected_etag: str, now):
    if isinstance(command, LodgingRecoverCommand):
        from app.trip_understanding.lodging_recovery import recovery_binding
        return verify_candidate(command.candidate_token, public_resource_id=public_resource_id,
            activity_token=recovery_binding(command.pending_token, command.intent), expected_etag=expected_etag, now=now)
    if not isinstance(command, (DiningInsertCommand, PlaceConfirmCommand)):
        return None
    binding = (source_meal_binding(command.after_activity_token, before=command.insert_before, meal_slot=command.meal_slot, meal_role=command.meal_role)
        if isinstance(command, DiningInsertCommand) and command.meal_slot else
        dining_binding(command.after_activity_token, before=command.insert_before) if isinstance(command, DiningInsertCommand) else command.activity_token)
    place = verify_candidate(command.candidate_token, public_resource_id=public_resource_id,
        activity_token=binding, expected_etag=expected_etag, now=now)
    if (isinstance(command, DiningInsertCommand) or place.category == "餐饮") and _explicitly_unavailable_dining_name(place.name):
        # Also reject a previously issued, otherwise valid browser candidate.
        # Already committed requests still replay before this transaction check.
        raise CommandTargetChangedError("supplier marks the dining place unavailable")
    if isinstance(command, DiningInsertCommand):
        validate_dining_meal_use(place, meal_role=command.meal_role, source_slot=command.meal_slot is not None)
    return place


def valid_anchor(anchor: MapStop | None) -> bool:
    return bool(anchor and anchor.resolution_status == "AUTO_MATCHED" and anchor.canonical_place_id
                and anchor.city and re.fullmatch(r"[\u4e00-\u9fff]{2,20}", anchor.city)
                and anchor.city != "目的地待确认" and anchor.longitude is not None and anchor.latitude is not None)


def select_dining_rows(rows: list, *, anchor: MapStop, excluded_ids: set[str], scope: CityScope | None = None,
                       meal_only: bool = False, query: str | None = None) -> list[CandidatePlace]:
    if not valid_anchor(anchor):
        return []
    accepted: dict[str, tuple[float, CandidatePlace]] = {}
    bounds = scope.bounds if scope else _CITY_BOUNDS.get(anchor.city)
    if not bounds:
        return []
    west, east, south, north = bounds
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, identifier = str(row.get("name") or "").strip(), str(row.get("id") or "").strip()
        canonical = f"amap:{identifier}"
        if not identifier or canonical in excluded_ids or identifier in excluded_ids:
            continue
        if atomic_place_rejection_reason(name) or not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_ -]{1,40}", name):
            continue
        if _explicitly_unavailable_dining_name(name):
            continue
        if re.search(r"内部专用|内部食堂|不对外(?:开放|营业)|仅限(?:内部|员工|职工)", name):
            # Provider restaurant codes also include staff-only canteens.
            # Explicit access restrictions cannot be a visitor meal suggestion;
            # public restaurant brands containing 食堂 remain eligible.
            continue
        if not (scope.matches(row) if scope else _admin_matches(row, expected_city=anchor.city, expected_district=None)):
            continue
        signals = classify_amap_type_signals(str(row.get("typecode") or ""), str(row.get("type") or ""))
        if not signals.complete or signals.conflict or signals.category != PlaceCategory.FOOD:
            continue
        if meal_only and not re.fullmatch(r"050[123]\d{2}", str(row.get("typecode") or "")):
            # A generic food category also contains tea, drinks, food banks and
            # catering companies. Only a specific meal-serving POI qualifies.
            continue
        if meal_only and re.search(r"宴会厅|婚宴中心|婚宴会馆|婚礼宴会|团膳|中央厨房", name):
            # Group/event catering can have a normal restaurant type code.
            # Keep public hotel restaurants and ordinary restaurant names.
            continue
        coordinates = _coordinates(row.get("location"))
        if not coordinates or not (west <= coordinates[0] <= east and south <= coordinates[1] <= north):
            continue
        distance = haversine_meters(anchor.longitude, anchor.latitude, *coordinates)
        if not 0 <= distance <= DINING_RADIUS_METERS:
            continue
        address = row.get("address")
        place = CandidatePlace(canonical_place_id=canonical, city=anchor.city, name=name, category="餐饮",
            area_or_address=address[:120] if isinstance(address, str) and address else str(row.get("adname") or anchor.city)[:120],
            position=GCJ02Position(longitude=coordinates[0], latitude=coordinates[1]))
        place = dining_metadata(place, row)
        info = place.dining_info
        if query and re.sub(r"\s+", "", query).casefold() not in re.sub(r"\s+", "", " ".join([name, info.cuisine or "", *info.tags])).casefold():
            continue
        place = bind_dining_access(place, stops=[anchor], activity_token=anchor.activity_token)
        accepted[canonical] = (distance, place)
    return [place for _, place in sorted(accepted.values(), key=lambda item: (item[0], item[1].name))[:3]]


async def search_dining(*, anchor: MapStop, excluded_ids: set[str], receipt: dict | None = None,
                        meal_only: bool = False, query: str | None = None) -> list[CandidatePlace] | None:
    receipt = receipt if receipt is not None else {}
    receipt.update(poi_http_attempts=0, district_http_attempts=0)
    settings = get_settings()
    if not valid_anchor(anchor) or not settings.amap_api_key or settings.trip_understanding_provider_mode != "live":
        return None
    # Existing AMap POI 2.0, one request. No route, rating, price or opening-hour inference.
    params = {"key": settings.amap_api_key, "location": f"{anchor.longitude:.6f},{anchor.latitude:.6f}",
        "radius": DINING_RADIUS_METERS, "types": "050100|050200|050300" if meal_only else "050000", "sortrule": "distance", "page_size": 25, "page_num": 1,
        "region": anchor.city, "city_limit": "true", "output": "json", "show_fields": "business,photos"}
    if query:
        params["keywords"] = query
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            scope = None
            if anchor.city not in _CITY_BOUNDS:
                scope_receipt = {}
                try:
                    scope = await _SCOPES.get(anchor.city, client=client, api_key=settings.amap_api_key, timeout=4.0, receipt=scope_receipt)
                finally:
                    receipt["district_http_attempts"] = scope_receipt.get("external_calls", 0)
                if scope is None:
                    return None
            receipt["poi_http_attempts"] += 1
            response = await client.get("https://restapi.amap.com/v5/place/around", params=params)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "1" or not isinstance(payload.get("pois"), list):
            return None
    except (httpx.HTTPError, ValueError, PlaceProviderUnavailableError):
        return None
    return select_dining_rows(payload["pois"], anchor=anchor, excluded_ids=excluded_ids, scope=scope, meal_only=meal_only, query=query)
