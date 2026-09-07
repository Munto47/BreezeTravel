"""Private, fail-safe loader for the versioned three-city place lexicon.

The lexicon is only a query-rewrite aid.  It deliberately contains no AMap
identity or mutable POI facts and cannot by itself resolve a place.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable


LEXICON_VERSION = "three-city-place-lexicon-v1"
LEXICON_PATH = Path(__file__).with_name("three_city_place_lexicon_v1.jsonl")
SUPPORTED_CITIES = ("北京", "上海", "杭州")
SUPPORTED_CATEGORIES = ("attraction", "transport", "food", "hotel")
_EXPECTED_CATEGORY_COUNTS = {
    "attraction": 210,
    "transport": 60,
    "food": 15,
    "hotel": 15,
}
_SUPPORTED_DISTRICTS = {
    "北京": frozenset(
        {
            "东城区",
            "西城区",
            "朝阳区",
            "丰台区",
            "石景山区",
            "海淀区",
            "门头沟区",
            "房山区",
            "通州区",
            "顺义区",
            "昌平区",
            "大兴区",
            "怀柔区",
            "平谷区",
            "密云区",
            "延庆区",
        }
    ),
    "上海": frozenset(
        {
            "黄浦区",
            "徐汇区",
            "长宁区",
            "静安区",
            "普陀区",
            "虹口区",
            "杨浦区",
            "闵行区",
            "宝山区",
            "嘉定区",
            "浦东新区",
            "金山区",
            "松江区",
            "青浦区",
            "奉贤区",
            "崇明区",
        }
    ),
    "杭州": frozenset(
        {
            "上城区",
            "拱墅区",
            "西湖区",
            "滨江区",
            "萧山区",
            "余杭区",
            "富阳区",
            "临安区",
            "临平区",
            "钱塘区",
            "桐庐县",
            "淳安县",
            "建德市",
        }
    ),
}
VENUE_SUFFIXES = (
    "博物馆",
    "博物院",
    "美术馆",
    "纪念馆",
    "科技馆",
    "图书馆",
    "展览馆",
    "艺术馆",
    "体育馆",
    "体育场",
    "风景区",
    "景区",
)
# These are equivalent venue kinds, not arbitrary removable name fragments.
# All other suffixes keep their own identity (e.g. a library is not a museum).
_VENUE_KIND_EQUIVALENTS = {"博物院": "博物馆", "风景区": "景区"}

_RECORD_FIELDS = frozenset(
    {
        "id",
        "city",
        "canonical_name",
        "aliases",
        "category",
        "district",
        "sources",
        "verified_at",
    }
)
_SOURCE_FIELDS = frozenset({"kind", "title", "publisher", "url", "license"})
_SEPARATOR_RE = re.compile(r"[·•・･|｜/／\\:：;；,，、—–－_\-]+")
_WHITESPACE_RE = re.compile(r"\s+")


class LexiconMatchTier(StrEnum):
    NONE = "NONE"
    CANONICAL_EXACT = "CANONICAL_EXACT"
    SAFE_ALIAS_EXACT = "SAFE_ALIAS_EXACT"
    VENUE_SUFFIX_EQUIVALENT = "VENUE_SUFFIX_EQUIVALENT"


@dataclass(frozen=True, slots=True)
class PlaceLexiconEntry:
    entry_id: str
    city: str
    canonical_name: str
    aliases: tuple[str, ...]
    category: str
    district: str | None
    sources: tuple[dict[str, str], ...]
    verified_at: str


@dataclass(frozen=True, slots=True)
class PlaceLexiconLookup:
    tier: LexiconMatchTier
    matches: tuple[PlaceLexiconEntry, ...]

    @property
    def unique(self) -> PlaceLexiconEntry | None:
        return self.matches[0] if len(self.matches) == 1 else None


@dataclass(frozen=True, slots=True)
class ThreeCityPlaceLexicon:
    entries: tuple[PlaceLexiconEntry, ...]
    available: bool = True
    error_code: str | None = None

    @classmethod
    def unavailable(cls, error_code: str) -> ThreeCityPlaceLexicon:
        return cls(entries=(), available=False, error_code=error_code)

    def lookup(self, *, city: str, name: str) -> PlaceLexiconLookup:
        normalized_city = normalize_city_name(city)
        normalized_name = normalize_place_name(name)
        if normalized_city not in SUPPORTED_CITIES or not normalized_name:
            return PlaceLexiconLookup(LexiconMatchTier.NONE, ())

        city_entries = tuple(
            entry for entry in self.entries
            if entry.city == normalized_city
            and not venue_suffix_conflicts(name, entry.canonical_name)
        )
        canonical = tuple(
            entry
            for entry in city_entries
            if normalize_place_name(entry.canonical_name) == normalized_name
        )
        if canonical:
            return PlaceLexiconLookup(LexiconMatchTier.CANONICAL_EXACT, canonical)

        aliases = tuple(
            entry
            for entry in city_entries
            if any(normalize_place_name(alias) == normalized_name for alias in entry.aliases)
        )
        if aliases:
            return PlaceLexiconLookup(LexiconMatchTier.SAFE_ALIAS_EXACT, aliases)

        suffix_matches = tuple(
            entry
            for entry in city_entries
            if venue_suffix_equivalent(name, entry.canonical_name)
            or any(venue_suffix_equivalent(name, alias) for alias in entry.aliases)
        )
        if suffix_matches:
            return PlaceLexiconLookup(
                LexiconMatchTier.VENUE_SUFFIX_EQUIVALENT,
                suffix_matches,
            )
        return PlaceLexiconLookup(LexiconMatchTier.NONE, ())


class LexiconValidationError(ValueError):
    """Raised only by strict validation tools and tests."""


def normalize_place_name(value: str) -> str:
    """Apply only conservative, identity-preserving comparison normalization."""

    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = _WHITESPACE_RE.sub("", normalized)
    normalized = _SEPARATOR_RE.sub("·", normalized).strip("·")
    return normalized


def normalize_city_name(value: str) -> str:
    normalized = normalize_place_name(value)
    for suffix in ("特别行政区", "自治区", "自治州", "地区", "盟", "省", "市"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def _venue_name_parts(value: str) -> tuple[str, str] | None:
    normalized = normalize_place_name(value)
    for suffix in sorted(VENUE_SUFFIXES, key=len, reverse=True):
        normalized_suffix = normalize_place_name(suffix)
        if normalized.endswith(normalized_suffix) and len(normalized) > len(normalized_suffix):
            return normalized[: -len(normalized_suffix)], normalized_suffix
    return None


def venue_kind(value: str) -> str | None:
    """Return an explicit venue kind, including a bare Provider type label."""

    normalized = normalize_place_name(value)
    for suffix in sorted(VENUE_SUFFIXES, key=len, reverse=True):
        normalized_suffix = normalize_place_name(suffix)
        if normalized.endswith(normalized_suffix):
            return _VENUE_KIND_EQUIVALENTS.get(normalized_suffix, normalized_suffix)
    return None


def strip_complete_venue_suffix(value: str) -> str | None:
    parts = _venue_name_parts(value)
    return parts[0] if parts is not None else None


def venue_suffix_conflicts(left: str, right: str) -> bool:
    """Explicit different venue kinds cannot be rescued by a shared short alias."""
    left_kind, right_kind = venue_kind(left), venue_kind(right)
    return left_kind is not None and right_kind is not None and left_kind != right_kind


def venue_suffix_equivalent(left: str, right: str) -> bool:
    left_normalized = normalize_place_name(left)
    right_normalized = normalize_place_name(right)
    if not left_normalized or not right_normalized or left_normalized == right_normalized:
        return False
    if venue_suffix_conflicts(left, right):
        return False
    left_base = strip_complete_venue_suffix(left)
    right_base = strip_complete_venue_suffix(right)
    return bool(
        (left_base is not None and left_base == right_normalized)
        or (right_base is not None and right_base == left_normalized)
        or (left_base is not None and right_base is not None and left_base == right_base)
    )


def _sort_key(entry: PlaceLexiconEntry) -> tuple[int, int, str, str]:
    return (
        SUPPORTED_CITIES.index(entry.city),
        SUPPORTED_CATEGORIES.index(entry.category),
        normalize_place_name(entry.canonical_name),
        entry.entry_id,
    )


def _expected_entry_id(entry: PlaceLexiconEntry) -> str:
    stable_input = f"{entry.city}|{entry.category}|{entry.canonical_name}"
    digest = hashlib.sha256(stable_input.encode("utf-8")).hexdigest()[:20]
    return (
        f"place-v1:{SUPPORTED_CITIES.index(entry.city)}:"
        f"{SUPPORTED_CATEGORIES.index(entry.category)}:{digest}"
    )


def _require_string(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LexiconValidationError(f"INVALID_{field.upper()}")
    return value.strip()


def _parse_sources(value: Any) -> tuple[dict[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise LexiconValidationError("INVALID_SOURCES")
    parsed: list[dict[str, str]] = []
    for source in value:
        if not isinstance(source, dict) or not set(source).issubset(_SOURCE_FIELDS):
            raise LexiconValidationError("INVALID_SOURCE_FIELDS")
        kind = source.get("kind")
        publisher = source.get("publisher")
        url = source.get("url")
        if not all(isinstance(item, str) and item.strip() for item in (kind, publisher, url)):
            raise LexiconValidationError("INCOMPLETE_SOURCE")
        if not str(url).startswith(("https://", "http://")):
            raise LexiconValidationError("INVALID_SOURCE_URL")
        parsed.append({str(key): str(item).strip() for key, item in source.items()})
    return tuple(parsed)


def _parse_entry(raw: Any) -> PlaceLexiconEntry:
    if not isinstance(raw, dict) or set(raw) != _RECORD_FIELDS:
        raise LexiconValidationError("INVALID_RECORD_FIELDS")
    city = _require_string(raw, "city")
    category = _require_string(raw, "category")
    if city not in SUPPORTED_CITIES or category not in SUPPORTED_CATEGORIES:
        raise LexiconValidationError("UNSUPPORTED_CITY_OR_CATEGORY")

    aliases_raw = raw.get("aliases")
    if not isinstance(aliases_raw, list) or any(
        not isinstance(alias, str) or not alias.strip() for alias in aliases_raw
    ):
        raise LexiconValidationError("INVALID_ALIASES")
    aliases = tuple(alias.strip() for alias in aliases_raw)
    normalized_aliases = tuple(normalize_place_name(alias) for alias in aliases)
    if len(set(normalized_aliases)) != len(normalized_aliases):
        raise LexiconValidationError("DUPLICATE_ALIAS")

    district_raw = raw.get("district")
    if district_raw is not None and (not isinstance(district_raw, str) or not district_raw.strip()):
        raise LexiconValidationError("INVALID_DISTRICT")
    verified_at = _require_string(raw, "verified_at")
    try:
        date.fromisoformat(verified_at)
    except ValueError as exc:
        raise LexiconValidationError("INVALID_VERIFIED_AT") from exc

    return PlaceLexiconEntry(
        entry_id=_require_string(raw, "id"),
        city=city,
        canonical_name=_require_string(raw, "canonical_name"),
        aliases=aliases,
        category=category,
        district=district_raw.strip() if isinstance(district_raw, str) else None,
        sources=_parse_sources(raw.get("sources")),
        verified_at=verified_at,
    )


def _read_entries(path: Path) -> tuple[PlaceLexiconEntry, ...]:
    raw_bytes = path.read_bytes()
    if raw_bytes.startswith(b"\xef\xbb\xbf") or b"\r" in raw_bytes:
        raise LexiconValidationError("INVALID_ENCODING_OR_LINE_ENDINGS")
    lines = raw_bytes.decode("utf-8").splitlines()
    if not lines:
        raise LexiconValidationError("EMPTY_LEXICON")
    entries: list[PlaceLexiconEntry] = []
    for line in lines:
        if not line.strip():
            raise LexiconValidationError("BLANK_JSONL_LINE")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LexiconValidationError("INVALID_JSONL") from exc
        entries.append(_parse_entry(raw))

    ids = [entry.entry_id for entry in entries]
    if len(ids) != len(set(ids)):
        raise LexiconValidationError("DUPLICATE_ID")
    if entries != sorted(entries, key=_sort_key):
        raise LexiconValidationError("UNSTABLE_SORT_ORDER")
    counts = {
        (city, category): sum(
            entry.city == city and entry.category == category for entry in entries
        )
        for city in SUPPORTED_CITIES
        for category in SUPPORTED_CATEGORIES
    }
    expected_counts = {
        (city, category): count
        for city in SUPPORTED_CITIES
        for category, count in _EXPECTED_CATEGORY_COUNTS.items()
    }
    if counts != expected_counts:
        raise LexiconValidationError("INVALID_CITY_CATEGORY_QUOTA")

    canonical_owners: dict[tuple[str, str], str] = {}
    all_canonical_names: set[str] = set()
    for entry in entries:
        canonical = normalize_place_name(entry.canonical_name)
        owner_key = (entry.city, canonical)
        if not canonical or owner_key in canonical_owners:
            raise LexiconValidationError("DUPLICATE_OR_EMPTY_CANONICAL_NAME")
        canonical_owners[owner_key] = entry.entry_id
        all_canonical_names.add(canonical)
        if entry.entry_id != _expected_entry_id(entry):
            raise LexiconValidationError("UNSTABLE_ID")
        if entry.district is not None and entry.district not in _SUPPORTED_DISTRICTS[entry.city]:
            raise LexiconValidationError("UNSUPPORTED_DISTRICT")
        source_kinds = {source["kind"] for source in entry.sources}
        if not {"wikidata_cc0_candidate", "official_verification"}.issubset(source_kinds):
            raise LexiconValidationError("INCOMPLETE_SOURCE_PROVENANCE")

    alias_owners: dict[str, str] = {}
    for entry in entries:
        for alias in entry.aliases:
            normalized_alias = normalize_place_name(alias)
            if not normalized_alias or normalized_alias in all_canonical_names:
                raise LexiconValidationError("UNSAFE_ALIAS_CANONICAL_COLLISION")
            if normalized_alias in alias_owners:
                raise LexiconValidationError("UNSAFE_SHARED_ALIAS")
            alias_owners[normalized_alias] = entry.entry_id
    return tuple(entries)


def load_three_city_place_lexicon(
    path: Path | str = LEXICON_PATH,
    *,
    strict: bool = False,
) -> ThreeCityPlaceLexicon:
    try:
        return ThreeCityPlaceLexicon(entries=_read_entries(Path(path)))
    except FileNotFoundError:
        if strict:
            raise LexiconValidationError("LEXICON_MISSING") from None
        return ThreeCityPlaceLexicon.unavailable("LEXICON_MISSING")
    except (OSError, UnicodeError, LexiconValidationError):
        if strict:
            raise
        return ThreeCityPlaceLexicon.unavailable("LEXICON_INVALID")


@lru_cache(maxsize=1)
def get_three_city_place_lexicon() -> ThreeCityPlaceLexicon:
    return load_three_city_place_lexicon()


def build_lexicon_for_testing(entries: Iterable[PlaceLexiconEntry]) -> ThreeCityPlaceLexicon:
    """Construct a small immutable lexicon for unit tests without filesystem I/O."""

    return ThreeCityPlaceLexicon(entries=tuple(entries))
