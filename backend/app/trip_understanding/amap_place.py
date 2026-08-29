from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any

import httpx

from app.constraints.amap_types import classify_amap_type, typecodes_for_category
from app.schemas.place import PlaceCategory
from app.trip_understanding.errors import PlaceProviderUnavailableError
from app.trip_understanding.models import PlaceResolutionOutcome, ResolvedPlace
from app.trip_understanding.pipeline import canonical_sha256


AMAP_POI_V2_ENDPOINT = "https://restapi.amap.com/v5/place/text"
_DEEP_CITIES = frozenset({"北京", "上海", "杭州"})
_FORBIDDEN_MARKERS = ("预约", "说明", "网址", "链接", "http://", "https://")
_SENTENCE_MARKERS = frozenset("。！？；\n")
_PROVIDER_STATUS_SUFFIX_RE = re.compile(
    r"[（(](?:暂停开放|暂停营业|临时关闭|暂不开放|停止营业)[）)]$"
)
_PROVIDER_CATEGORY_SUFFIXES = ("博物馆", "风景区", "景区")
_PROVIDER_HIERARCHY_SEPARATORS = ("-", "—")
_G01_ATTRACTION_ADDITIONAL_TYPECODES = ("061000",)
_CATEGORY_LABELS = {
    PlaceCategory.ATTRACTION: "景点",
    PlaceCategory.FOOD: "餐饮",
    PlaceCategory.HOTEL: "住宿",
    PlaceCategory.TRANSPORT: "交通节点",
    PlaceCategory.UNKNOWN: "地点",
}
_HINT_CATEGORIES = {
    PlaceCategory.ATTRACTION: (
        "attraction",
        "scenic",
        "museum",
        "park",
        "temple",
        "garden",
        "zoo",
        "historical",
        "heritage",
        "landmark",
        "street",
        "village",
        "lake",
        "cave",
        "艺术",
        "文化",
        "景点",
        "景区",
        "博物馆",
        "公园",
        "古迹",
    ),
    PlaceCategory.FOOD: ("food", "restaurant", "餐饮", "餐厅", "饭店", "美食"),
    PlaceCategory.HOTEL: ("hotel", "resort", "住宿", "酒店", "宾馆", "民宿"),
    PlaceCategory.TRANSPORT: (
        "transport",
        "transit",
        "station",
        "交通",
        "地铁",
        "车站",
        "机场",
    ),
}


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _normalized_name(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _normalized_city(value: str) -> str:
    normalized = _normalized_name(value)
    for suffix in ("特别行政区", "自治区", "自治州", "地区", "盟", "省", "市"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _provider_aliases(raw: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for value in (raw.get("alias"),):
        values.extend(_string_values(value))
    business = raw.get("business")
    if isinstance(business, dict):
        values.extend(_string_values(business.get("alias")))
    expanded: list[str] = []
    for value in values:
        expanded.extend(
            item.strip()
            for item in re.split(r"[|;/；]", value)
            if item.strip()
        )
    return expanded


def _name_variants(value: str, *, city: str, provider_name: bool) -> set[str]:
    raw_values = {value.strip()}
    if provider_name:
        raw_values.add(_PROVIDER_STATUS_SUFFIX_RE.sub("", value).strip())
    variants: set[str] = set()
    city_prefixes = (city, f"{_normalized_city(city)}市")
    for raw in tuple(raw_values):
        if not raw:
            continue
        raw_values.update(
            raw[len(prefix) :]
            for prefix in city_prefixes
            if raw.startswith(prefix) and len(raw) > len(prefix)
        )
    if provider_name:
        for raw in tuple(raw_values):
            for separator in _PROVIDER_HIERARCHY_SEPARATORS:
                if separator in raw:
                    tail = raw.rsplit(separator, 1)[-1].strip()
                    if len(tail) >= 2:
                        raw_values.add(tail)
            for suffix in _PROVIDER_CATEGORY_SUFFIXES:
                if raw.endswith(suffix) and len(raw) > len(suffix) + 1:
                    raw_values.add(raw[: -len(suffix)])
    for raw in raw_values:
        normalized = _normalized_name(raw)
        if normalized:
            variants.add(normalized)
    return variants


def _name_matches(raw: dict[str, Any], atomic: str, city: str) -> tuple[bool, bool]:
    query_variants = _name_variants(atomic, city=city, provider_name=False)
    primary = raw.get("name")
    names = [primary] if isinstance(primary, str) else []
    aliases = _provider_aliases(raw)
    for index, name in enumerate([*names, *aliases]):
        provider_variants = _name_variants(name, city=city, provider_name=True)
        if query_variants & provider_variants:
            return True, index >= len(names)
    return False, False


def _primary_name_is_exact(raw: dict[str, Any], atomic: str, city: str) -> bool:
    primary = raw.get("name")
    if not isinstance(primary, str):
        return False
    provider_name = _PROVIDER_STATUS_SUFFIX_RE.sub("", primary).strip()
    provider_values = {provider_name}
    city_prefixes = (city, f"{_normalized_city(city)}市")
    provider_values.update(
        provider_name[len(prefix) :]
        for prefix in city_prefixes
        if provider_name.startswith(prefix) and len(provider_name) > len(prefix)
    )
    query = _normalized_name(atomic)
    return any(_normalized_name(value) == query for value in provider_values)


def _expected_category(category_hint: str | None) -> PlaceCategory | None:
    if not category_hint:
        return None
    normalized = _normalized_name(category_hint)
    for category, markers in _HINT_CATEGORIES.items():
        if any(marker in normalized for marker in markers):
            return category
    return None


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _city_matches(raw: dict[str, Any], expected_city: str) -> bool:
    expected = _normalized_city(expected_city)
    city_values = _string_values(raw.get("cityname"))
    if city_values:
        return any(_normalized_city(value) == expected for value in city_values)
    if expected in {_normalized_city("北京"), _normalized_city("上海")}:
        return any(
            _normalized_city(value) == expected
            for value in _string_values(raw.get("pname"))
        )
    return False


def _coordinates(value: object) -> tuple[float, float] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 2:
        return None
    try:
        longitude, latitude = (float(part) for part in parts)
    except ValueError:
        return None
    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        return None
    return longitude, latitude


def _provider_request_id_hash(response: httpx.Response) -> str:
    for header in ("x-request-id", "x-acs-request-id", "x-trace-id"):
        value = response.headers.get(header)
        if value:
            return _sha256_text(value)
    return "NOT_EXPOSED_BY_PROVIDER"


def _minimal_call_receipt(
    receipt: dict[str, object],
    *,
    purpose: str,
) -> dict[str, object]:
    return {
        "purpose": purpose,
        "request_sha256": receipt["request_sha256"],
        "response_sha256": receipt.get("response_sha256", "NOT_RECEIVED"),
        "provider_request_id_sha256": receipt.get(
            "provider_request_id_sha256",
            "NOT_RECEIVED",
        ),
        "http_status": receipt.get("http_status", "NOT_RECEIVED"),
        "latency_ms": receipt.get("latency_ms", 0.0),
        "observed_at": receipt.get("observed_at", "NOT_COMPLETED"),
        "typecodes": receipt.get("typecodes", []),
        "raw_provider_response_retained": False,
    }


def _combine_rewrite_receipts(
    primary: dict[str, object],
    rewrite: dict[str, object],
    *,
    accepted: bool,
) -> dict[str, object]:
    calls = [
        _minimal_call_receipt(primary, purpose="CATEGORY_FILTERED_PRIMARY"),
        _minimal_call_receipt(rewrite, purpose="UNTYPED_DETERMINISTIC_REWRITE"),
    ]
    return {
        **rewrite,
        "request_sha256": canonical_sha256(
            [str(call["request_sha256"]) for call in calls]
        ),
        "response_sha256": canonical_sha256(
            [str(call["response_sha256"]) for call in calls]
        ),
        "provider_request_id_sha256": canonical_sha256(
            [str(call["provider_request_id_sha256"]) for call in calls]
        ),
        "latency_ms": round(
            sum(float(call["latency_ms"]) for call in calls),
            3,
        ),
        "external_calls": 2,
        "rewrite_count": 1,
        "query_strategy": "CATEGORY_FILTERED_THEN_UNTYPED_LOCAL_CATEGORY_CHECK",
        "primary_typecodes": primary.get("typecodes", []),
        "category_compatible_candidate_count": (
            1 if accepted else 0
        ),
        "status": "AUTO_MATCHED" if accepted else "NO_UNIQUE_MATCH",
        "calls": calls,
        "raw_provider_response_retained": False,
    }


class AmapPlaceResolver:
    """Conservative POI 2.0 resolver for G01 executable place mentions."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = AMAP_POI_V2_ENDPOINT,
        deadline_seconds: float = 3.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Amap API key is required")
        if endpoint != AMAP_POI_V2_ENDPOINT:
            raise ValueError("G01 Amap POI endpoint must be the official v5 text API")
        if deadline_seconds <= 0:
            raise ValueError("Amap POI deadline must be positive")
        self.api_key = api_key
        self.endpoint = endpoint
        self.deadline_seconds = deadline_seconds
        self.client = client

    @staticmethod
    def _no_call_receipt(
        *,
        city: str,
        atomic_place_name: str,
        status: str,
    ) -> dict[str, object]:
        return {
            "provider": "AMAP_POI_V2",
            "execution_mode": "LIVE",
            "status": status,
            "city": city,
            "query_sha256": _sha256_text(atomic_place_name),
            "endpoint_sha256": _sha256_text(AMAP_POI_V2_ENDPOINT),
            "external_calls": 0,
            "raw_provider_response_retained": False,
        }

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> PlaceResolutionOutcome:
        atomic = atomic_place_name.strip()
        if (
            not atomic
            or len(atomic) > 40
            or any(marker in atomic.casefold() for marker in _FORBIDDEN_MARKERS)
            or any(marker in atomic for marker in _SENTENCE_MARKERS)
        ):
            return PlaceResolutionOutcome(
                receipt=self._no_call_receipt(
                    city=city,
                    atomic_place_name=atomic,
                    status="INVALID_ATOMIC_TEXT",
                )
            )
        normalized_city = _normalized_city(city)
        if normalized_city not in {_normalized_city(value) for value in _DEEP_CITIES}:
            return PlaceResolutionOutcome(
                receipt=self._no_call_receipt(
                    city=city,
                    atomic_place_name=atomic,
                    status="BASIC_CITY_CONFIRMATION_REQUIRED",
                )
            )

        expected_category = _expected_category(category_hint)
        typecodes = (
            typecodes_for_category(expected_category)
            if expected_category is not None
            else []
        )
        # Tourist commercial streets are useful for text-card resolution, but
        # the shared category list is also part of older suggestion query
        # contracts. Keep the G01 expansion local so those frozen contracts do
        # not drift.
        if expected_category == PlaceCategory.ATTRACTION:
            typecodes = [*_G01_ATTRACTION_ADDITIONAL_TYPECODES, *typecodes]
        safe_params: dict[str, object] = {
            "keywords_sha256": _sha256_text(atomic),
            "region": city,
            "city_limit": "true",
            "page_size": 25,
            "page_num": 1,
            "output": "json",
            "show_fields": "business",
            "types": typecodes,
        }
        params: dict[str, object] = {
            "key": self.api_key,
            "keywords": atomic,
            "region": city,
            "city_limit": "true",
            "page_size": 25,
            "page_num": 1,
            "output": "json",
            "show_fields": "business",
        }
        if typecodes:
            params["types"] = "|".join(typecodes)
        request_sha256 = canonical_sha256(
            {"method": "GET", "endpoint": self.endpoint, "params": safe_params}
        )
        request_binding: dict[str, object] = {
            "provider": "AMAP_POI_V2",
            "execution_mode": "LIVE",
            "endpoint_sha256": _sha256_text(self.endpoint),
            "request_sha256": request_sha256,
            "query_sha256": _sha256_text(atomic),
            "city": city,
            "city_limit": True,
            "typecodes": typecodes,
            "raw_provider_response_retained": False,
        }
        started = time.perf_counter()
        observed_at = datetime.now(UTC)
        try:
            if self.client is not None:
                response = await self.client.get(
                    self.endpoint,
                    params=params,
                    timeout=self.deadline_seconds,
                )
            else:
                async with httpx.AsyncClient(timeout=self.deadline_seconds) as client:
                    response = await client.get(self.endpoint, params=params)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise PlaceProviderUnavailableError(
                "DEADLINE_EXCEEDED",
                provider_binding={
                    **request_binding,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                },
                external_call_count=1,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PlaceProviderUnavailableError(
                "PROVIDER_UNAVAILABLE",
                provider_binding={
                    **request_binding,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                },
                external_call_count=1,
            ) from exc

        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        if not isinstance(payload, dict):
            raise PlaceProviderUnavailableError(
                "INVALID_PROVIDER_RESPONSE",
                provider_binding={
                    **request_binding,
                    "latency_ms": latency_ms,
                },
                external_call_count=1,
            )
        response_sha256 = canonical_sha256(payload)
        base_receipt: dict[str, object] = {
            **request_binding,
            "response_sha256": response_sha256,
            "provider_request_id_sha256": _provider_request_id_hash(response),
            "http_status": response.status_code,
            "latency_ms": latency_ms,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "external_calls": 1,
        }
        if payload.get("status") != "1" or payload.get("infocode") not in {None, "10000"}:
            raise PlaceProviderUnavailableError(
                "PROVIDER_STATUS_ERROR",
                provider_binding={
                    **base_receipt,
                    "infocode": str(payload.get("infocode") or "NOT_EXPOSED_BY_PROVIDER"),
                },
                external_call_count=1,
            )

        raw_pois = payload.get("pois")
        pois = [item for item in raw_pois if isinstance(item, dict)] if isinstance(raw_pois, list) else []
        name_matches: list[dict[str, Any]] = []
        alias_match_ids: set[str] = set()
        for item in pois:
            matched, via_alias = _name_matches(item, atomic, city)
            if not matched:
                continue
            name_matches.append(item)
            if via_alias:
                alias_match_ids.add(str(item.get("id") or ""))
        city_matches = [item for item in name_matches if _city_matches(item, city)]
        compatible: list[tuple[dict[str, Any], PlaceCategory, tuple[float, float]]] = []
        seen_ids: set[str] = set()
        for item in city_matches:
            category = classify_amap_type(
                str(item.get("typecode") or ""),
                str(item.get("type") or ""),
            )
            if expected_category is not None and category != expected_category:
                continue
            coordinates = _coordinates(item.get("location"))
            provider_id = str(item.get("id") or "").strip()
            if coordinates is None or not provider_id or provider_id in seen_ids:
                continue
            seen_ids.add(provider_id)
            compatible.append((item, category, coordinates))

        decision_receipt = {
            **base_receipt,
            "provider_result_count": len(pois),
            "exact_name_candidate_count": len(name_matches),
            "provider_alias_candidate_count": len(alias_match_ids - {""}),
            "city_consistent_candidate_count": len(city_matches),
            "category_compatible_candidate_count": len(compatible),
            "primary_exact_candidate_count": sum(
                _primary_name_is_exact(item, atomic, city)
                for item, _category, _coordinates_value in compatible
            ),
            "name_match_policy": "UNIQUE_PRIMARY_EXACT_THEN_UNIQUE_VARIANT_V3",
        }
        if len(compatible) == 0 and typecodes:
            try:
                rewrite = await self.resolve(
                    city=city,
                    atomic_place_name=atomic,
                    category_hint=None,
                )
            except PlaceProviderUnavailableError as exc:
                failure = dict(exc.provider_binding)
                raise PlaceProviderUnavailableError(
                    exc.category,
                    provider_binding={
                        **failure,
                        "primary_request_sha256": decision_receipt[
                            "request_sha256"
                        ],
                        "primary_response_sha256": decision_receipt[
                            "response_sha256"
                        ],
                        "external_calls": 1 + exc.external_call_count,
                        "rewrite_count": 1,
                        "query_strategy": (
                            "CATEGORY_FILTERED_THEN_UNTYPED_LOCAL_CATEGORY_CHECK"
                        ),
                        "raw_provider_response_retained": False,
                    },
                    external_call_count=1 + exc.external_call_count,
                ) from exc
            accepted_rewrite = (
                rewrite.place is not None
                and rewrite.place.category == _CATEGORY_LABELS[expected_category]
            )
            combined_receipt = _combine_rewrite_receipts(
                decision_receipt,
                rewrite.receipt,
                accepted=accepted_rewrite,
            )
            if accepted_rewrite and rewrite.place is not None:
                place = rewrite.place.model_copy(
                    update={"provider_binding": combined_receipt}
                )
                return PlaceResolutionOutcome(
                    place=place,
                    receipt=combined_receipt,
                )
            return PlaceResolutionOutcome(receipt=combined_receipt)

        selected = compatible
        selection_tier = "UNIQUE_VARIANT"
        if len(compatible) > 1:
            primary_exact = [
                item
                for item in compatible
                if _primary_name_is_exact(item[0], atomic, city)
            ]
            if len(primary_exact) == 1:
                selected = primary_exact
                selection_tier = "UNIQUE_PRIMARY_EXACT"
        if len(selected) != 1:
            return PlaceResolutionOutcome(
                receipt={
                    **decision_receipt,
                    "selection_tier": "AMBIGUOUS",
                    "status": "NO_UNIQUE_MATCH",
                }
            )

        raw, category, (longitude, latitude) = selected[0]
        address = raw.get("address")
        if isinstance(address, list):
            address = "".join(str(item) for item in address)
        if not isinstance(address, str) or not address.strip():
            district = raw.get("adname")
            address = district if isinstance(district, str) else "地点详情待确认"
        provider_binding = {
            **decision_receipt,
            "status": "AUTO_MATCHED",
            "selection_tier": selection_tier,
            "adcode": str(raw.get("adcode") or "NOT_EXPOSED_BY_PROVIDER"),
            "typecode": str(raw.get("typecode") or "NOT_EXPOSED_BY_PROVIDER"),
            "coordinates": {
                "longitude": longitude,
                "latitude": latitude,
            },
        }
        place = ResolvedPlace(
            canonical_place_id=str(raw["id"]),
            name=str(raw["name"]),
            category=_CATEGORY_LABELS[category],
            area_or_address=address.strip(),
            provider_binding=provider_binding,
        )
        return PlaceResolutionOutcome(place=place, receipt=provider_binding)
