"""Sourced commercial destinations near a restaurant, never inferred membership."""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import get_settings
from app.trip_understanding.amap_place import _coordinates
from app.trip_understanding.candidates import CandidatePlace
from app.trip_understanding.city_knowledge import get_city_knowledge
from app.trip_understanding.city_scope import CityScope
from app.trip_understanding.stay import haversine_meters

AREA_FACTS_PATH = Path(__file__).with_name("dining_area_facts_v1.json")
NEARBY_AREA_RADIUS_METERS = 1200
AREA_CACHE_TTL_SECONDS = 900
AREA_CACHE_CAPACITY = 128
_AREA_CACHE: OrderedDict[str, tuple[float, tuple[float, float]]] = OrderedDict()


@dataclass(frozen=True)
class DiningAreaFact:
    fact_id: str
    city: str
    name: str
    poi_id: str
    longitude: float
    latitude: float
    adcode: str
    province: str
    district: str
    typecode: str
    type_label: str
    fingerprint: str


@lru_cache(maxsize=1)
def load_dining_area_facts(path: Path = AREA_FACTS_PATH) -> tuple[DiningAreaFact, ...]:
    """A malformed optional record must not remove a verified restaurant."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != "dining-area-positions-v1":
            return ()
        rows = payload.get("areas", [])
        if not isinstance(rows, list):
            return ()
    except (OSError, ValueError, AttributeError):
        return ()
    output = []
    seen = set()
    for row in rows:
        try:
            if not isinstance(row, dict) or row.get("review_status") != "MAP_IDENTITY_VERIFIED":
                continue
            city, name = row["city"], row["name"]
            if city not in get_city_knowledge().versions or row["cityname"] != f"{city}市":
                continue
            longitude, latitude = float(row["longitude"]), float(row["latitude"])
            if not (math.isfinite(longitude) and math.isfinite(latitude)
                    and 73 <= longitude <= 135 and 18 <= latitude <= 54):
                continue
            if not re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）(). -]{2,60}", name):
                continue
            if not re.fullmatch(r"[A-Za-z0-9]{8,20}", row["poi_id"]) or not re.fullmatch(r"\d{6}", row["adcode"]):
                continue
            if row["typecode"] not in {"060100", "060101", "060102", "061000", "061001"}:
                continue
            if not row["type_label"].startswith("购物服务;") or row.get("coordinate_system") != "GCJ02":
                continue
            sources = row["sources"]
            if not isinstance(sources, list) or len(sources) < 2:
                continue
            hosts = []
            for source in sources:
                url = urlsplit(source["url"])
                if url.scheme != "https" or not url.hostname or url.username or url.password or not source["evidence"]:
                    raise ValueError("Invalid source")
                date.fromisoformat(source["retrieved_at"])
                hosts.append(url.hostname)
            if "www.amap.com" not in hosts or not any(host.endswith(".gov.cn") for host in hosts):
                continue
            if row["fact_id"] in seen:
                continue
            fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            output.append(DiningAreaFact(row["fact_id"], city, name, row["poi_id"], longitude, latitude,
                                        row["adcode"], row["pname"], row["adname"],
                                        row["typecode"], row["type_label"], fingerprint))
            seen.add(row["fact_id"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return tuple(output)


def _verified_position(row: dict, fact: DiningAreaFact) -> tuple[float, float] | None:
    if not isinstance(row, dict) or row.get("id") != fact.poi_id or row.get("name") != fact.name:
        return None
    # The published position fact includes independently reviewed provider admin
    # fields. Reuse the shared consistency check without a second network lookup.
    municipality = fact.province == f"{fact.city}市"
    city_adcode = fact.adcode[:2] + "0000" if municipality else fact.adcode[:4] + "00"
    scope = CityScope(f"{fact.city}市", fact.province, city_adcode,
                      ((fact.district, fact.adcode),), (73, 135, 18, 54), municipality)
    if (row.get("adcode") != fact.adcode or row.get("typecode") != fact.typecode
            or row.get("type") != fact.type_label
            or not scope.matches(row)):
        return None
    position = _coordinates(row.get("location"))
    if not position or haversine_meters(fact.longitude, fact.latitude, *position) > 250:
        return None
    return position


async def nearby_dining_area(place: CandidatePlace, *, receipt: dict, timeout_seconds: float = 3) -> dict | None:
    """At most one extra POI request; current identity or network failures stay empty."""
    receipt.setdefault("area_http_attempts", 0)
    if place.business_area or timeout_seconds <= 0:
        return None
    settings = get_settings()
    if not settings.amap_api_key or settings.trip_understanding_provider_mode != "live":
        return None
    nearby = [(haversine_meters(place.position.longitude, place.position.latitude, fact.longitude, fact.latitude), fact)
              for fact in load_dining_area_facts() if fact.city == place.city]
    nearby = [(distance, fact) for distance, fact in nearby if distance <= NEARBY_AREA_RADIUS_METERS]
    if not nearby:
        return None
    _, fact = min(nearby, key=lambda item: (item[0], item[1].fact_id))
    cached = _AREA_CACHE.get(fact.fingerprint)
    if cached and cached[0] > time.monotonic():
        _AREA_CACHE.move_to_end(fact.fingerprint)
        position = cached[1]
    else:
        _AREA_CACHE.pop(fact.fingerprint, None)
        try:
            async with httpx.AsyncClient(timeout=min(3, timeout_seconds)) as client:
                receipt["area_http_attempts"] += 1
                response = await client.get("https://restapi.amap.com/v5/place/detail",
                                            params={"key": settings.amap_api_key, "id": fact.poi_id})
                response.raise_for_status()
                payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "1" or not isinstance(payload.get("pois"), list):
                return None
            matched = [position for row in payload["pois"] if (position := _verified_position(row, fact))]
            if len(matched) != 1:
                return None
            position = matched[0]
            _AREA_CACHE[fact.fingerprint] = (time.monotonic() + AREA_CACHE_TTL_SECONDS, position)
            while len(_AREA_CACHE) > AREA_CACHE_CAPACITY:
                _AREA_CACHE.popitem(last=False)
        except (httpx.HTTPError, ValueError, TypeError):
            return None
    distance = haversine_meters(place.position.longitude, place.position.latitude, *position)
    if distance > NEARBY_AREA_RADIUS_METERS:
        return None
    return {"area": fact.name, "area_relation": "NEARBY", "area_distance_m": round(distance)}
