"""Versioned city name knowledge. A mention is a clue, never a planned visit or POI.

Files contain attributable names and relationships, not coordinates, opening hours,
room availability or provider identities. Bad rows are isolated; an unreadable or
empty active pack falls back to the first usable older version for that city.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from app.trip_understanding._three_city_place_lexicon import (
    LEXICON_PATH, LexiconMatchTier, PlaceLexiconEntry, PlaceLexiconLookup,
    _parse_entry, normalize_city_name, normalize_place_name,
    venue_suffix_conflicts,
)

KNOWLEDGE_ROOT = Path(__file__).with_name("city_knowledge_data")
_KINDS = {"poi", "scenic_area", "commercial_area", "stay_area", "entrance", "campus"}
_CATEGORIES = {"attraction", "transport", "food", "hotel", "area"}
_RELATIONS = {"part_of", "entrance_of", "branch_of", "nearby"}
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"“”]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CityEntity:
    entity_id: str
    city: str
    canonical_name: str
    kind: str
    category: str
    aliases: tuple[str, ...] = ()
    district: str | None = None
    review_status: str = "lead"
    sources: tuple[dict, ...] = ()
    relations: tuple[dict, ...] = ()
    uses: tuple[str, ...] = ()
    provider_type_pairs: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class EntityMention:
    surface: str
    start: int
    end: int
    city: str | None
    candidate_ids: tuple[str, ...]
    ambiguous: bool
    entity_kind: str


@dataclass(frozen=True)
class CityKnowledge:
    entities: tuple[CityEntity, ...]
    issues: tuple[dict, ...] = ()
    versions: Mapping[str, str] = field(default_factory=dict)
    _index: Mapping[str, tuple[CityEntity, ...]] = field(init=False, repr=False)
    _first: Mapping[str, tuple[str, ...]] = field(init=False, repr=False)

    def __post_init__(self):
        index: dict[str, list[CityEntity]] = {}
        for entity in self.entities:
            for name in dict.fromkeys((entity.canonical_name, *entity.aliases)):
                normalized = normalize_place_name(name)
                if normalized:
                    values = index.setdefault(normalized, [])
                    if entity.entity_id not in {value.entity_id for value in values}:
                        values.append(entity)
        # Keep the original rows available for auditing, while repeated copies of
        # one canonical identity do not manufacture ambiguity in mention lookup.
        # Unverified legacy aliases remain leads rather than becoming verified.
        for name, values in index.items():
            identities: dict[tuple, CityEntity] = {}
            for value in values:
                key = (value.city, normalize_place_name(value.canonical_name), value.category)
                existing = identities.get(key)
                if existing is None or (existing.review_status == "lead" and value.review_status == "name_verified"):
                    identities[key] = value
            index[name] = list(identities.values())
        first: dict[str, list[str]] = {}
        # Literal matching deliberately keeps the source's original offsets.
        for entity in self.entities:
            for name in dict.fromkeys((entity.canonical_name, *entity.aliases)):
                if len(name) >= 2:
                    first.setdefault(name[0].casefold(), []).append(name)
        object.__setattr__(self, "_index", MappingProxyType({k: tuple(v) for k, v in index.items()}))
        object.__setattr__(self, "_first", MappingProxyType({k: tuple(sorted(set(v), key=lambda s: (-len(s), s))) for k, v in first.items()}))
        object.__setattr__(self, "versions", MappingProxyType(dict(self.versions)))

    def lookup(self, name: str, city: str | None = None) -> tuple[CityEntity, ...]:
        matches = self._index.get(normalize_place_name(name), ())
        if city:
            city = normalize_city_name(city)
            matches = tuple(e for e in matches if e.city == city)
        return matches

    def mentions(self, text: str, city: str | None = None, limit: int = 320) -> list[EntityMention]:
        limit = max(0, min(limit, 320))
        if not limit:
            return []
        urls = [(m.start(), m.end()) for m in _URL.finditer(text)]
        output = []
        offset = 0
        while offset < len(text) and len(output) < limit:
            if any(start <= offset < end for start, end in urls):
                offset = next(end for start, end in urls if start <= offset < end)
                continue
            matched = False
            for name in self._first.get(text[offset].casefold(), ()):
                end = offset + len(name)
                if text[offset:end].casefold() != name.casefold():
                    continue
                if name[0].isascii() and offset and text[offset - 1].isascii() and text[offset - 1].isalnum():
                    continue
                if name[-1].isascii() and end < len(text) and text[end].isascii() and text[end].isalnum():
                    continue
                candidates = self.lookup(name, city)
                if not candidates:
                    continue
                cities = {e.city for e in candidates}
                kinds = {e.kind for e in candidates}
                output.append(EntityMention(text[offset:end], offset, end,
                    next(iter(cities)) if len(cities) == 1 else None,
                    tuple(e.entity_id for e in candidates), len(candidates) > 1,
                    next(iter(kinds)) if len(kinds) == 1 else "ambiguous"))
                offset = end
                matched = True
                break
            if not matched:
                offset += 1
        return output

    def query_lookup(self, *, city: str, name: str) -> PlaceLexiconLookup:
        # New leads and broad areas cannot silently rewrite a search identity.
        values = tuple(e for e in self.lookup(name, city)
            if e.review_status == "name_verified" and e.kind in {"poi", "scenic_area", "entrance", "campus"}
            and e.category != "area" and not venue_suffix_conflicts(name, e.canonical_name))
        entries = tuple(PlaceLexiconEntry(e.entity_id, e.city, e.canonical_name, e.aliases,
            e.category, e.district, e.sources, "") for e in values)
        tier = LexiconMatchTier.CANONICAL_EXACT if any(normalize_place_name(e.canonical_name) == normalize_place_name(name)
            for e in values) else LexiconMatchTier.SAFE_ALIAS_EXACT if entries else LexiconMatchTier.NONE
        # A canonical/alias collision remains ambiguous rather than preferring one.
        return PlaceLexiconLookup(tier, entries)

    def technical_landmark(self, *, city: str, name: str) -> CityEntity | None:
        """Only a reviewed, district-qualified landmark may override a map type."""
        values = [e for e in self.lookup(name, city) if e.review_status == "name_verified"
                  and e.category == "attraction" and e.district and e.provider_type_pairs]
        return values[0] if len(values) == 1 else None

    def technical_type_matches(self, raw: dict, *, city: str, name: str) -> bool:
        entity = self.technical_landmark(city=city, name=name)
        return bool(entity and raw.get("adname") == entity.district
                    and normalize_place_name(str(raw.get("name") or "")) in {
                        normalize_place_name(v) for v in (entity.canonical_name, *entity.aliases)}
                    and any(raw.get("typecode") == pair["typecode"]
                            and raw.get("type") == pair["type_label"] for pair in entity.provider_type_pairs))


def _http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def _parse_record(raw: dict, city: str) -> CityEntity:
    allowed = {"id", "city", "canonical_name", "kind", "category", "aliases", "district", "review_status", "sources", "relations", "uses", "provider_type_pairs"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("INVALID_FIELDS")
    if raw.get("city") != city or raw.get("kind") not in _KINDS or raw.get("category") not in _CATEGORIES:
        raise ValueError("INVALID_CLASSIFICATION")
    if any(not isinstance(raw.get(key), str) or not raw[key].strip() for key in ("id", "canonical_name")):
        raise ValueError("MISSING_ID_OR_NAME")
    if len(raw["canonical_name"]) > 100 or raw.get("review_status") not in {"name_verified", "lead"}:
        raise ValueError("INVALID_NAME_OR_REVIEW")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("MISSING_SOURCE")
    for source in sources:
        if not isinstance(source, dict) or not _http_url(source.get("url")):
            raise ValueError("INVALID_SOURCE")
        if not all(isinstance(source.get(k), str) and source[k].strip() for k in ("title", "publisher", "retrieved_at", "evidence")):
            raise ValueError("INCOMPLETE_SOURCE")
        date.fromisoformat(source["retrieved_at"])
    source_urls = {s["url"] for s in sources}
    aliases = raw.get("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("INVALID_ALIASES")
    accepted = []
    for alias in aliases:
        if (not isinstance(alias, dict) or not isinstance(alias.get("name"), str) or not alias["name"].strip()
                or alias.get("relation") != "same_as" or alias.get("source_url") not in source_urls):
            raise ValueError("UNSUPPORTED_ALIAS")
        accepted.append(alias["name"].strip())
    relations = raw.get("relations", [])
    if not isinstance(relations, list) or any(not isinstance(r, dict) or r.get("type") not in _RELATIONS
            or not isinstance(r.get("target_id"), str) or not r["target_id"] or r.get("source_url") not in source_urls for r in relations):
        raise ValueError("INVALID_RELATION")
    district = raw.get("district")
    if district is not None and (not isinstance(district, str) or not district.strip()):
        raise ValueError("INVALID_DISTRICT")
    uses = raw.get("uses", [])
    if not isinstance(uses, list) or any(v not in {"stay_search", "dining_search"} for v in uses):
        raise ValueError("INVALID_USES")
    pairs = raw.get("provider_type_pairs", [])
    if not isinstance(pairs, list) or len(pairs) > 8:
        raise ValueError("INVALID_PROVIDER_TYPE_PAIRS")
    for pair in pairs:
        if (not isinstance(pair, dict) or set(pair) != {"typecode", "type_label", "source_url"}
                or not isinstance(pair["typecode"], str) or not re.fullmatch(r"\d{6}", pair["typecode"])
                or not isinstance(pair["type_label"], str) or len(pair["type_label"].split(";")) != 3
                or pair["source_url"] not in source_urls or not district
                or raw["review_status"] != "name_verified" or raw["category"] != "attraction"):
            raise ValueError("UNREVIEWED_PROVIDER_TYPE_PAIR")
    return CityEntity(raw["id"], city, raw["canonical_name"].strip(), raw["kind"], raw["category"],
        tuple(dict.fromkeys(accepted)), district, raw["review_status"], tuple(sources), tuple(relations), tuple(uses), tuple(pairs))


def _load_pack(path: Path, city: str) -> tuple[list[CityEntity], list[dict]]:
    entries, issues, duplicate_ids = {}, [], set()
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = _parse_record(json.loads(line), city)
            if entry.entity_id in entries or entry.entity_id in duplicate_ids:
                entries.pop(entry.entity_id, None)
                duplicate_ids.add(entry.entity_id)
                raise ValueError("DUPLICATE_ID")
            entries[entry.entity_id] = entry
        except (ValueError, TypeError, KeyError) as error:
            issues.append({"file": path.name, "line": number, "code": str(error) if str(error).isupper() else "INVALID_RECORD"})
    return list(entries.values()), issues


def load_city_knowledge(root: Path = KNOWLEDGE_ROOT, *, include_legacy: bool = True) -> CityKnowledge:
    entities, issues, versions = [], [], {}
    if include_legacy:
        # Preserve the immutable 900-row asset without repeating its unverified
        # historical 'official_verification' label as a new verification claim.
        try:
            for number, line in enumerate(LEXICON_PATH.read_text(encoding="utf-8").splitlines(), 1):
                try:
                    old = _parse_entry(json.loads(line))
                    entities.append(CityEntity(old.entry_id, old.city, old.canonical_name, "poi", old.category,
                        old.aliases, old.district, "lead", old.sources))
                except (ValueError, TypeError):
                    issues.append({"file": LEXICON_PATH.name, "line": number, "code": "INVALID_LEGACY_RECORD"})
        except OSError:
            issues.append({"file": LEXICON_PATH.name, "code": "LEGACY_UNAVAILABLE"})
        from app.trip_understanding.landmark_hints import HINTS
        for hint in HINTS:
            entities.append(CityEntity(f"legacy-landmark:{hint.city}:{hint.name}", hint.city, hint.name,
                "poi", "attraction", hint.aliases, hint.district, "lead",
                ({"url": hint.source, "kind": "historical_reviewed_search_hint"},)))
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "city-knowledge-v1" or not isinstance(manifest.get("cities"), dict):
            raise ValueError("INVALID_MANIFEST")
        for city, version in manifest["cities"].items():
            if not isinstance(city, str) or not isinstance(version, dict):
                issues.append({"code": "INVALID_CITY_VERSION"})
                continue
            fallbacks = version.get("fallbacks", [])
            if not isinstance(fallbacks, list):
                issues.append({"city": city, "code": "INVALID_FALLBACKS"})
                fallbacks = []
            candidates = [version.get("active"), *fallbacks]
            for candidate in candidates:
                try:
                    if not isinstance(candidate, str):
                        raise ValueError("INVALID_PACK_PATH")
                    path = (root / candidate).resolve()
                    if not path.is_relative_to(root.resolve()):
                        raise ValueError("INVALID_PACK_PATH")
                    records, bad = _load_pack(path, city)
                    issues.extend(bad)
                    if not records:
                        raise ValueError("EMPTY_PACK")
                    entities.extend(records)
                    versions[city] = candidate
                    break
                except (OSError, UnicodeError, ValueError, TypeError):
                    issues.append({"city": city, "code": "PACK_UNAVAILABLE"})
    except (OSError, UnicodeError, ValueError, TypeError):
        issues.append({"code": "MANIFEST_UNAVAILABLE"})
    return CityKnowledge(tuple(entities), tuple(issues), versions)


@lru_cache(maxsize=1)
def get_city_knowledge() -> CityKnowledge:
    return load_city_knowledge()


def lookup_entities(name: str, city: str | None = None) -> tuple[CityEntity, ...]:
    return get_city_knowledge().lookup(name, city)


def find_entity_mentions(text: str, city: str | None = None, limit: int = 320) -> list[EntityMention]:
    return get_city_knowledge().mentions(text, city, limit)


def source_place_hints(source: str, city: str | None = None, limit: int = 160) -> list[dict]:
    return [{"name": m.surface, "span_start": m.start, "span_end": m.end,
        "entity_id": m.candidate_ids[0] if not m.ambiguous else None, "candidate_ids": list(m.candidate_ids),
        "city": m.city, "entity_kind": m.entity_kind, "ambiguous": m.ambiguous}
        for m in find_entity_mentions(source, city, limit)]
