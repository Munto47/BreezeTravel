"""Provider-verified administrative scope for ordinary domestic POI search."""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass

import httpx

from app.trip_understanding.errors import PlaceProviderUnavailableError

DISTRICT_ENDPOINT = "https://restapi.amap.com/v3/config/district"


def city_name(value: str) -> str:
    return value.strip().removesuffix("市")


@dataclass(frozen=True)
class CityScope:
    name: str
    province: str
    adcode: str
    districts: tuple[tuple[str, str], ...]
    bounds: tuple[float, float, float, float]
    municipality: bool = False

    def matches(self, raw: dict, expected_district: str | None = None) -> bool:
        # Independent provider fields must agree with the administrative query.
        if raw.get("pname") != self.province:
            return False
        if (raw.get("adname"), raw.get("adcode")) not in self.districts:
            return False
        if raw.get("cityname") != self.name and not (self.municipality and raw.get("cityname") in (None, "", [])):
            return False
        if expected_district and city_name(str(raw.get("adname"))) != city_name(expected_district):
            return False
        return True


class CityScopeLookup:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[float, CityScope]] = {}
        self._lock = asyncio.Lock()

    async def get(self, city: str, *, client: httpx.AsyncClient, api_key: str, timeout: float, receipt: dict | None = None) -> CityScope | None:
        receipt = receipt if receipt is not None else {}
        receipt.update(provider="AMAP_DISTRICT", external_calls=0, calls=[], cache_hit=False)
        name = city_name(city)
        if name == "目的地待确认" or not re.fullmatch(r"[\u4e00-\u9fff]{2,20}", name):
            return None
        async with self._lock:
            cached = self._cache.get(name)
            if cached and time.monotonic() - cached[0] < 86400:
                receipt["cache_hit"] = True
                return cached[1]
            rows = await self._query(name, client=client, api_key=api_key, timeout=timeout, boundary=True, receipt=receipt)
            matches = [r for r in rows if city_name(str(r.get("name", ""))) == name and (
                r.get("level") == "city" or (name in {"北京", "上海", "天津", "重庆"} and r.get("level") == "province"))]
            if len(matches) != 1:
                return None
            row = matches[0]
            code = row.get("adcode")
            if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code):
                return None
            provinces = await self._query(code[:2] + "0000", client=client, api_key=api_key, timeout=timeout, boundary=False, receipt=receipt)
            provinces = [p for p in provinces if p.get("level") == "province" and p.get("adcode") == code[:2] + "0000"]
            if len(provinces) != 1 or not isinstance(provinces[0].get("name"), str):
                return None
            children = row.get("districts")
            if not isinstance(children, list):
                return None
            districts = tuple((d["name"], d["adcode"]) for d in children if isinstance(d, dict)
                and d.get("level") == "district" and isinstance(d.get("name"), str)
                and isinstance(d.get("adcode"), str) and re.fullmatch(r"\d{6}", d["adcode"])
                and d["adcode"].startswith(code[:2] if row["level"] == "province" else code[:4]))
            # Some prefecture cities administer streets directly.
            if not districts and children and row["level"] == "city" and all(isinstance(d, dict) and d.get("level") == "street" for d in children):
                districts = ((row["name"], code),)
            if not districts:
                return None
            try:
                points = [tuple(map(float, p.split(","))) for p in re.split(r"[;|]", row["polyline"]) if p]
                if len(points) < 3 or any(len(p) != 2 or not (73 <= p[0] <= 136 and 3 <= p[1] <= 54) for p in points):
                    return None
                bounds = (min(p[0] for p in points), max(p[0] for p in points), min(p[1] for p in points), max(p[1] for p in points))
            except (KeyError, TypeError, ValueError):
                return None
            scope = CityScope(row["name"], provinces[0]["name"], code, districts, bounds, row["level"] == "province")
            if len(self._cache) >= 512:
                self._cache.pop(next(iter(self._cache)))
            self._cache[name] = (time.monotonic(), scope)
            return scope

    @staticmethod
    async def _query(keyword: str, *, client: httpx.AsyncClient, api_key: str, timeout: float, boundary: bool, receipt: dict) -> list[dict]:
        receipt["external_calls"] += 1
        call = {"query_sha256": hashlib.sha256(keyword.encode()).hexdigest(), "boundary": boundary}
        receipt["calls"].append(call)
        try:
            response = await client.get(DISTRICT_ENDPOINT, params={"key": api_key, "keywords": keyword,
                "subdistrict": 1 if boundary else 0, "extensions": "all" if boundary else "base"}, timeout=timeout)
            response.raise_for_status()
            call["response_sha256"] = hashlib.sha256(response.content).hexdigest()
            payload = response.json()
            if not isinstance(payload, dict) or payload.get("status") != "1" or not isinstance(payload.get("districts"), list):
                raise ValueError("invalid administrative response")
            return [r for r in payload["districts"] if isinstance(r, dict)]
        except (httpx.HTTPError, ValueError) as exc:
            raise PlaceProviderUnavailableError("CITY_SCOPE_UNAVAILABLE", provider_binding={**receipt, "raw_provider_response_retained": False}, external_call_count=receipt["external_calls"]) from exc
