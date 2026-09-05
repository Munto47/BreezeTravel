from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.constraints.amap_types import (
    classify_amap_type_signals,
    typecodes_for_category,
)
from app.schemas.place import PlaceCategory
from app.trip_understanding._three_city_place_lexicon import (
    LexiconMatchTier,
    get_three_city_place_lexicon,
    normalize_city_name,
    normalize_place_name,
    venue_kind,
    venue_suffix_conflicts,
    venue_suffix_equivalent,
)
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
_G01_ATTRACTION_ADDITIONAL_TYPECODES = ("061000",)
_PROVIDER_MATCH_TIERS = (
    "CANONICAL_EXACT",
    "SAFE_ALIAS_EXACT",
    "VENUE_SUFFIX_EQUIVALENT",
)
_STRICT_PROVIDER_TYPE_VENUE_KINDS = frozenset(
    {"博物馆", "美术馆", "纪念馆", "科技馆", "图书馆", "展览馆", "艺术馆"}
)
_CITY_ADMIN_RULES = {
    "北京": {"province": "北京", "adcode_prefix": "11", "municipality": True},
    "上海": {"province": "上海", "adcode_prefix": "31", "municipality": True},
    "杭州": {"province": "浙江", "adcode_prefix": "3301", "municipality": False},
}
_DISTRICT_ADCODES = {
    "北京": {
        "东城": "110101",
        "西城": "110102",
        "朝阳": "110105",
        "丰台": "110106",
        "石景山": "110107",
        "海淀": "110108",
        "门头沟": "110109",
        "房山": "110111",
        "通州": "110112",
        "顺义": "110113",
        "昌平": "110114",
        "大兴": "110115",
        "怀柔": "110116",
        "平谷": "110117",
        "密云": "110118",
        "延庆": "110119",
    },
    "上海": {
        "黄浦": "310101",
        "徐汇": "310104",
        "长宁": "310105",
        "静安": "310106",
        "普陀": "310107",
        "虹口": "310109",
        "杨浦": "310110",
        "闵行": "310112",
        "宝山": "310113",
        "嘉定": "310114",
        "浦东": "310115",
        "金山": "310116",
        "松江": "310117",
        "青浦": "310118",
        "奉贤": "310120",
        "崇明": "310151",
    },
    "杭州": {
        "上城": "330102",
        "拱墅": "330105",
        "西湖": "330106",
        "滨江": "330108",
        "萧山": "330109",
        "余杭": "330110",
        "富阳": "330111",
        "临安": "330112",
        "临平": "330113",
        "钱塘": "330114",
        "桐庐": "330122",
        "淳安": "330127",
        "建德": "330182",
    },
}
_LEXICAL_CATEGORY_MARKERS = (
    (
        PlaceCategory.TRANSPORT,
        ("地铁站", "火车站", "高铁站", "客运站", "汽车站", "机场", "码头"),
    ),
    (
        PlaceCategory.HOTEL,
        ("酒店", "宾馆", "旅馆", "民宿", "客栈", "度假村", "青旅"),
    ),
    (
        PlaceCategory.FOOD,
        (
            "餐厅",
            "饭店",
            "菜馆",
            "面馆",
            "小吃",
            "烧烤",
            "火锅",
            "咖啡",
            "茶馆",
            "馒头店",
            "酒家",
        ),
    ),
    (
        PlaceCategory.ATTRACTION,
        (
            "博物馆",
            "博物院",
            "美术馆",
            "纪念馆",
            "科技馆",
            "图书馆",
            "水族馆",
            "公园",
            "植物园",
            "湿地",
            "风景",
            "景区",
            "长城",
            "城墙",
            "遗址",
            "艺术区",
            "古街",
            "步行街",
            "大街",
            "斜街",
            "步道",
            "书院",
            "故居",
            "广场",
            "体育场",
            "体育馆",
            "运动中心",
            "文化街区",
            "滨江",
            "外滩",
            "什刹海",
        ),
    ),
)
_LEXICAL_TRANSPORT_SUFFIXES = ("站",)
_PRODUCT_SEMANTIC_TECHNICAL_COMPATIBILITY = (
    (
        PlaceCategory.ATTRACTION,
        ("0801",),
        ("体育场", "体育馆", "运动中心"),
    ),
    (
        PlaceCategory.ATTRACTION,
        ("1903",),
        (
            "御街",
            "古街",
            "步行街",
            "斜街",
            "文化街区",
        ),
    ),
)
_LEXICAL_ATTRACTION_SUFFIXES = (
    "寺",
    "宫",
    "塔",
    "陵",
    "坛",
    "祠",
    "园",
    "堤",
    "峰",
    "山",
    "坞",
    "村",
    "小镇",
    "大楼",
    "鼓楼",
    "钟楼",
    "御街",
    "坊",
    "岛",
    "桥",
)
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
    return normalize_place_name(value)


def _normalized_city(value: str) -> str:
    return normalize_city_name(value)


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


def _comparison_values(value: str, *, city: str, provider_name: bool) -> set[str]:
    cleaned = _PROVIDER_STATUS_SUFFIX_RE.sub("", value).strip() if provider_name else value.strip()
    normalized = _normalized_name(cleaned)
    if not normalized:
        return set()
    values = {normalized}
    for prefix in {_normalized_city(city), _normalized_name(f"{_normalized_city(city)}市")}:
        if normalized.startswith(prefix) and len(normalized) > len(prefix):
            values.add(normalized[len(prefix) :])
    return values


def _name_match_tier(
    raw: dict[str, Any],
    *,
    canonical_name: str,
    safe_aliases: tuple[str, ...],
    city: str,
) -> str | None:
    primary = raw.get("name")
    if not isinstance(primary, str) or not primary.strip():
        return None
    if venue_suffix_conflicts(canonical_name, _PROVIDER_STATUS_SUFFIX_RE.sub("", primary).strip()):
        return None
    canonical_values = _comparison_values(canonical_name, city=city, provider_name=False)
    safe_alias_values = {
        value
        for alias in safe_aliases
        for value in _comparison_values(alias, city=city, provider_name=False)
    }
    primary_values = _comparison_values(primary, city=city, provider_name=True)
    if canonical_values & primary_values:
        return "CANONICAL_EXACT"

    provider_alias_values = {
        value
        for alias in _provider_aliases(raw)
        for value in _comparison_values(alias, city=city, provider_name=True)
    }
    if (safe_alias_values & primary_values) or (
        provider_alias_values & (canonical_values | safe_alias_values)
    ):
        return "SAFE_ALIAS_EXACT"

    expected_values = canonical_values | safe_alias_values
    provider_values = primary_values | provider_alias_values
    if any(
        venue_suffix_equivalent(provider_value, expected_value)
        for provider_value in provider_values
        for expected_value in expected_values
    ):
        return "VENUE_SUFFIX_EQUIVALENT"
    return None


def _expected_category(category_hint: str | None) -> PlaceCategory | None:
    if not category_hint:
        return None
    normalized = _normalized_name(category_hint)
    for category, markers in _HINT_CATEGORIES.items():
        if any(marker in normalized for marker in markers):
            return category
    return None


def _lexical_category(atomic: str) -> PlaceCategory | None:
    normalized = _normalized_name(atomic)
    if normalized.endswith(_LEXICAL_TRANSPORT_SUFFIXES):
        return PlaceCategory.TRANSPORT
    for category, markers in _LEXICAL_CATEGORY_MARKERS:
        if any(marker in normalized for marker in markers):
            return category
    if normalized.endswith(_LEXICAL_ATTRACTION_SUFFIXES):
        return PlaceCategory.ATTRACTION
    return None


def _product_semantic_technical_category_is_compatible(
    receipt: dict[str, object],
    *,
    atomic: str,
    expected_category: PlaceCategory,
) -> bool:
    typecode = str(receipt.get("typecode") or "").strip()
    normalized = _normalized_name(atomic)
    return any(
        expected_category == category
        and typecode.startswith(prefixes)
        and any(marker in normalized for marker in markers)
        for category, prefixes, markers in _PRODUCT_SEMANTIC_TECHNICAL_COMPATIBILITY
    )


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _one_provider_value(value: object) -> str | None:
    values = {item.strip() for item in _string_values(value) if item.strip()}
    return values.pop() if len(values) == 1 else None


def _normalized_district(value: str) -> str:
    normalized = _normalized_name(value)
    for suffix in ("自治县", "新区", "区", "县", "市"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _admin_matches(
    raw: dict[str, Any],
    *,
    expected_city: str,
    expected_district: str | None,
) -> bool:
    city = _normalized_city(expected_city)
    rule = _CITY_ADMIN_RULES.get(city)
    if rule is None:
        return False

    province = _one_provider_value(raw.get("pname"))
    district = _one_provider_value(raw.get("adname"))
    adcode = _one_provider_value(raw.get("adcode"))
    if province is None or district is None or adcode is None:
        return False
    if _normalized_city(province) != rule["province"]:
        return False
    if not re.fullmatch(r"\d{6}", adcode) or not adcode.startswith(str(rule["adcode_prefix"])):
        return False
    normalized_district = _normalized_district(district)
    if _DISTRICT_ADCODES[city].get(normalized_district) != adcode:
        return False

    city_values = {item.strip() for item in _string_values(raw.get("cityname")) if item.strip()}
    if city_values and any(_normalized_city(value) != city for value in city_values):
        return False
    if not city_values and not bool(rule["municipality"]):
        return False
    if expected_district is not None and normalized_district != _normalized_district(expected_district):
        return False
    return True


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


@dataclass(frozen=True, slots=True)
class _MatchedCandidate:
    raw: dict[str, Any]
    category: PlaceCategory
    coordinates: tuple[float, float]
    tier: str
    category_compatibility_basis: str


@dataclass(frozen=True, slots=True)
class _CandidateDecision:
    selected: _MatchedCandidate | None
    metrics: dict[str, object]


def _evaluate_candidates(
    pois: list[dict[str, Any]],
    *,
    city: str,
    canonical_name: str,
    safe_aliases: tuple[str, ...],
    expected_category: PlaceCategory | None,
    expected_district: str | None,
    atomic: str,
) -> _CandidateDecision:
    name_matches: list[tuple[dict[str, Any], str]] = []
    alias_match_ids: set[str] = set()
    for item in pois:
        tier = _name_match_tier(
            item,
            canonical_name=canonical_name,
            safe_aliases=safe_aliases,
            city=city,
        )
        if tier is None:
            continue
        name_matches.append((item, tier))
        if tier == "SAFE_ALIAS_EXACT":
            alias_match_ids.add(str(item.get("id") or "").strip())

    admin_match_ids: set[str] = set()
    category_conflict_ids: set[str] = set()
    category_incomplete_ids: set[str] = set()
    by_tier: dict[str, dict[str, _MatchedCandidate]] = {
        tier: {} for tier in _PROVIDER_MATCH_TIERS
    }
    for item, tier in name_matches:
        provider_id_raw = item.get("id")
        provider_id = provider_id_raw.strip() if isinstance(provider_id_raw, str) else ""
        if not provider_id or not _admin_matches(
            item,
            expected_city=city,
            expected_district=expected_district,
        ):
            continue
        admin_match_ids.add(provider_id)

        # AMap's broad attraction category cannot distinguish a museum from a
        # library (or a stadium from a gymnasium). Cross-check every explicit
        # venue kind carried by the provider name, aliases and type labels
        # before accepting an otherwise exact lexical match.
        expected_venue_kind = venue_kind(atomic) or venue_kind(canonical_name)
        provider_name_parts = [
            part.strip()
            for value in [*_string_values(item.get("name")), *_provider_aliases(item)]
            for part in re.split(r"[|;/；]", value)
            if part.strip()
        ]
        provider_type_parts = [
            part.strip()
            for value in _string_values(item.get("type"))
            for part in re.split(r"[|;/；]", value)
            if part.strip()
        ]
        name_identity_conflict = expected_venue_kind is not None and any(
            (candidate_kind := venue_kind(value)) is not None and candidate_kind != expected_venue_kind
            for value in provider_name_parts
        )
        strict_type_identity_conflict = (
            expected_venue_kind in _STRICT_PROVIDER_TYPE_VENUE_KINDS
            and any(
                (candidate_kind := venue_kind(value)) in _STRICT_PROVIDER_TYPE_VENUE_KINDS
                and candidate_kind != expected_venue_kind
                for value in provider_type_parts
            )
        )
        if name_identity_conflict or strict_type_identity_conflict:
            category_conflict_ids.add(provider_id)
            continue

        typecode_raw = item.get("typecode")
        type_label_raw = item.get("type")
        typecode = typecode_raw.strip() if isinstance(typecode_raw, str) else ""
        type_label = type_label_raw.strip() if isinstance(type_label_raw, str) else ""
        signals = classify_amap_type_signals(typecode, type_label)
        compatibility_basis = "PROVIDER_TYPECODE_AND_LABEL"
        if signals.conflict:
            category_conflict_ids.add(provider_id)
            continue
        if signals.complete:
            category = signals.category
        elif (
            expected_category is not None
            and typecode
            and type_label
            and _product_semantic_technical_category_is_compatible(
                item,
                atomic=atomic,
                expected_category=expected_category,
            )
        ):
            category = expected_category
            compatibility_basis = "G01_PRODUCT_SEMANTIC_TECHNICAL_COMPATIBILITY"
        else:
            category_incomplete_ids.add(provider_id)
            continue
        if category is PlaceCategory.UNKNOWN or (
            expected_category is not None and category is not expected_category
        ):
            continue

        coordinates = _coordinates(item.get("location"))
        if coordinates is None:
            continue
        by_tier[tier].setdefault(
            provider_id,
            _MatchedCandidate(
                raw=item,
                category=category,
                coordinates=coordinates,
                tier=tier,
                category_compatibility_basis=compatibility_basis,
            ),
        )

    selected: _MatchedCandidate | None = None
    selection_tier = "NO_VALID_CANDIDATE"
    for tier in _PROVIDER_MATCH_TIERS:
        candidates = tuple(by_tier[tier].values())
        if not candidates:
            continue
        selection_tier = tier if len(candidates) == 1 else f"AMBIGUOUS_{tier}"
        if len(candidates) == 1:
            selected = candidates[0]
        break

    compatible_ids = {
        provider_id
        for candidates in by_tier.values()
        for provider_id in candidates
    }
    metrics: dict[str, object] = {
        "provider_result_count": len(pois),
        "exact_name_candidate_count": len(name_matches),
        "provider_alias_candidate_count": len(alias_match_ids - {""}),
        "city_consistent_candidate_count": len(admin_match_ids),
        "category_compatible_candidate_count": len(compatible_ids),
        "primary_exact_candidate_count": len(by_tier["CANONICAL_EXACT"]),
        "provider_type_conflict_candidate_count": len(category_conflict_ids),
        "provider_type_incomplete_candidate_count": len(category_incomplete_ids),
        "name_match_policy": "HIGHEST_TIER_UNIQUE_POI_ID_V5_VENUE_IDENTITY",
        "selection_tier": selection_tier,
    }
    return _CandidateDecision(selected=selected, metrics=metrics)


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
        "category_basis": primary.get("category_basis", "NOT_AVAILABLE"),
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
        self._owned_client: httpx.AsyncClient | None = None

    def _http_client(self) -> httpx.AsyncClient:
        if self.client is not None:
            return self.client
        if self._owned_client is None:
            self._owned_client = httpx.AsyncClient(timeout=self.deadline_seconds)
        return self._owned_client

    async def aclose(self) -> None:
        if self._owned_client is not None:
            await self._owned_client.aclose()
            self._owned_client = None

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

    async def _query_provider(
        self,
        *,
        city: str,
        query_name: str,
        original_atomic: str,
        category_basis: str,
        typecodes: list[str],
        lexicon_binding: dict[str, object],
    ) -> tuple[list[dict[str, Any]], dict[str, object]]:
        safe_params: dict[str, object] = {
            "keywords_sha256": _sha256_text(query_name),
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
            "keywords": query_name,
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
            "query_sha256": _sha256_text(original_atomic),
            "provider_keywords_sha256": _sha256_text(query_name),
            "city": city,
            "city_limit": True,
            "category_basis": category_basis,
            "typecodes": typecodes,
            **lexicon_binding,
            "raw_provider_response_retained": False,
        }
        started = time.perf_counter()
        observed_at = datetime.now(UTC)
        try:
            response = await self._http_client().get(
                self.endpoint,
                params=params,
                timeout=self.deadline_seconds,
            )
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
                provider_binding={**request_binding, "latency_ms": latency_ms},
                external_call_count=1,
            )
        base_receipt: dict[str, object] = {
            **request_binding,
            "response_sha256": canonical_sha256(payload),
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
        return pois, base_receipt

    @staticmethod
    def _resolved_outcome(
        candidate: _MatchedCandidate,
        receipt: dict[str, object],
    ) -> PlaceResolutionOutcome:
        raw = candidate.raw
        longitude, latitude = candidate.coordinates
        address = raw.get("address")
        if isinstance(address, list):
            address = "".join(str(item) for item in address)
        if not isinstance(address, str) or not address.strip():
            district = raw.get("adname")
            address = district if isinstance(district, str) else "地点详情待确认"
        provider_binding = {
            **receipt,
            "status": "AUTO_MATCHED",
            "selection_tier": candidate.tier,
            "resolved_category": _CATEGORY_LABELS[candidate.category],
            "category_compatibility_basis": candidate.category_compatibility_basis,
            "adcode": str(raw.get("adcode") or "NOT_EXPOSED_BY_PROVIDER"),
            "typecode": str(raw.get("typecode") or "NOT_EXPOSED_BY_PROVIDER"),
            "coordinates": {"longitude": longitude, "latitude": latitude},
        }
        place = ResolvedPlace(
            canonical_place_id=str(raw["id"]),
            name=str(raw["name"]),
            category=_CATEGORY_LABELS[candidate.category],
            area_or_address=address.strip(),
            provider_binding=provider_binding,
        )
        return PlaceResolutionOutcome(place=place, receipt=provider_binding)

    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
        _allow_lexical_category: bool = True,
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

        lexicon = get_three_city_place_lexicon()
        lookup = lexicon.lookup(city=normalized_city, name=atomic) if lexicon.available else None
        lexicon_binding: dict[str, object] = {
            "lexicon_status": "UNAVAILABLE" if not lexicon.available else "MISS",
            "lexicon_match_tier": LexiconMatchTier.NONE.value,
            "lexicon_rewrite_applied": False,
        }
        query_name = atomic
        safe_aliases: tuple[str, ...] = ()
        expected_district: str | None = None
        lexicon_category: PlaceCategory | None = None
        if lookup is not None and lookup.matches:
            lexicon_binding["lexicon_match_tier"] = lookup.tier.value
            if lookup.unique is None:
                return PlaceResolutionOutcome(
                    receipt={
                        **self._no_call_receipt(
                            city=city,
                            atomic_place_name=atomic,
                            status="LEXICON_AMBIGUOUS",
                        ),
                        **lexicon_binding,
                        "lexicon_status": "AMBIGUOUS",
                    }
                )
            entry = lookup.unique
            query_name = entry.canonical_name
            safe_aliases = entry.aliases
            expected_district = entry.district
            lexicon_category = PlaceCategory(entry.category)
            lexicon_binding = {
                **lexicon_binding,
                "lexicon_status": "MATCHED",
                "lexicon_entry_id_sha256": _sha256_text(entry.entry_id),
                "lexicon_rewrite_applied": _normalized_name(query_name) != _normalized_name(atomic),
                "lexicon_category": entry.category,
                "lexicon_district_constraint": expected_district is not None,
            }

        expected_category = _expected_category(category_hint)
        category_basis = "EXPLICIT_SEMANTIC_HINT"
        if expected_category is None:
            category_basis = "NOT_AVAILABLE"
            if _allow_lexical_category and not category_hint:
                expected_category = _lexical_category(atomic)
                if expected_category is not None:
                    category_basis = "ATOMIC_NAME_LEXICAL"
        if (
            expected_category is not None
            and lexicon_category is not None
            and expected_category is not lexicon_category
        ):
            return PlaceResolutionOutcome(
                receipt={
                    **self._no_call_receipt(
                        city=city,
                        atomic_place_name=atomic,
                        status="LEXICON_CATEGORY_CONFLICT",
                    ),
                    **lexicon_binding,
                    "category_basis": category_basis,
                }
            )
        if expected_category is None and lexicon_category is not None:
            expected_category = lexicon_category
            category_basis = "LEXICON_CATEGORY"

        typecodes = typecodes_for_category(expected_category) if expected_category is not None else []
        # Tourist commercial streets are useful for text-card resolution, but
        # the shared category list is part of older suggestion query contracts.
        if expected_category == PlaceCategory.ATTRACTION:
            typecodes = [*_G01_ATTRACTION_ADDITIONAL_TYPECODES, *typecodes]

        pois, primary_base = await self._query_provider(
            city=city,
            query_name=query_name,
            original_atomic=atomic,
            category_basis=category_basis,
            typecodes=typecodes,
            lexicon_binding=lexicon_binding,
        )
        primary_decision = _evaluate_candidates(
            pois,
            city=city,
            canonical_name=query_name,
            safe_aliases=safe_aliases,
            expected_category=expected_category,
            expected_district=expected_district,
            atomic=atomic,
        )
        primary_receipt = {**primary_base, **primary_decision.metrics}
        if primary_decision.selected is not None:
            return self._resolved_outcome(primary_decision.selected, primary_receipt)

        compatible_count = int(primary_decision.metrics["category_compatible_candidate_count"])
        if compatible_count == 0 and typecodes:
            try:
                rewrite_pois, rewrite_base = await self._query_provider(
                    city=city,
                    query_name=query_name,
                    original_atomic=atomic,
                    category_basis=category_basis,
                    typecodes=[],
                    lexicon_binding=lexicon_binding,
                )
            except PlaceProviderUnavailableError as exc:
                failure = dict(exc.provider_binding)
                raise PlaceProviderUnavailableError(
                    exc.category,
                    provider_binding={
                        **failure,
                        "primary_request_sha256": primary_receipt["request_sha256"],
                        "primary_response_sha256": primary_receipt["response_sha256"],
                        "external_calls": 1 + exc.external_call_count,
                        "rewrite_count": 1,
                        "query_strategy": "CATEGORY_FILTERED_THEN_UNTYPED_LOCAL_CATEGORY_CHECK",
                        "raw_provider_response_retained": False,
                    },
                    external_call_count=1 + exc.external_call_count,
                ) from exc
            rewrite_decision = _evaluate_candidates(
                rewrite_pois,
                city=city,
                canonical_name=query_name,
                safe_aliases=safe_aliases,
                expected_category=expected_category,
                expected_district=expected_district,
                atomic=atomic,
            )
            rewrite_receipt = {**rewrite_base, **rewrite_decision.metrics}
            combined_receipt = _combine_rewrite_receipts(
                primary_receipt,
                rewrite_receipt,
                accepted=rewrite_decision.selected is not None,
            )
            if rewrite_decision.selected is not None:
                return self._resolved_outcome(rewrite_decision.selected, combined_receipt)
            return PlaceResolutionOutcome(receipt=combined_receipt)

        return PlaceResolutionOutcome(
            receipt={**primary_receipt, "status": "NO_UNIQUE_MATCH"}
        )
