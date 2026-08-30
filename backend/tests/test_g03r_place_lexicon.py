from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

from app.trip_understanding._three_city_place_lexicon import (
    LEXICON_PATH,
    LexiconMatchTier,
    LexiconValidationError,
    PlaceLexiconEntry,
    ThreeCityPlaceLexicon,
    load_three_city_place_lexicon,
    normalize_place_name,
    strip_complete_venue_suffix,
    venue_suffix_equivalent,
)


EXPECTED_FIELDS = {
    "id",
    "city",
    "canonical_name",
    "aliases",
    "category",
    "district",
    "sources",
    "verified_at",
}
FORBIDDEN_FIELDS = {
    "amap_id",
    "poi_id",
    "coordinates",
    "location",
    "address",
    "phone",
    "opening_hours",
    "hours",
    "price",
    "rating",
    "room_availability",
    "raw_response",
}


def _entry(
    entry_id: str,
    canonical_name: str,
    *,
    aliases: tuple[str, ...] = (),
) -> PlaceLexiconEntry:
    return PlaceLexiconEntry(
        entry_id=entry_id,
        city="北京",
        canonical_name=canonical_name,
        aliases=aliases,
        category="attraction",
        district="东城区",
        sources=(
            {
                "kind": "official_verification",
                "publisher": "test authority",
                "url": "https://example.test/place",
            },
        ),
        verified_at="2026-08-30",
    )


def test_versioned_lexicon_has_exact_three_city_quotas_and_safe_schema() -> None:
    raw_bytes = LEXICON_PATH.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r" not in raw_bytes
    records = [json.loads(line) for line in raw_bytes.decode("utf-8").splitlines()]

    assert len(records) == 900
    assert Counter(record["city"] for record in records) == {
        "北京": 300,
        "上海": 300,
        "杭州": 300,
    }
    assert Counter((record["city"], record["category"]) for record in records) == {
        (city, category): count
        for city in ("北京", "上海", "杭州")
        for category, count in (
            ("attraction", 210),
            ("transport", 60),
            ("food", 15),
            ("hotel", 15),
        )
    }

    ids: set[str] = set()
    aliases: dict[str, str] = {}
    canonical_names = {
        normalize_place_name(record["canonical_name"]) for record in records
    }
    city_indexes = {city: index for index, city in enumerate(("北京", "上海", "杭州"))}
    category_indexes = {
        category: index
        for index, category in enumerate(("attraction", "transport", "food", "hotel"))
    }
    for record in records:
        assert set(record) == EXPECTED_FIELDS
        assert not (set(record) & FORBIDDEN_FIELDS)
        assert record["id"] not in ids
        ids.add(record["id"])
        stable_input = f'{record["city"]}|{record["category"]}|{record["canonical_name"]}'
        digest = hashlib.sha256(stable_input.encode("utf-8")).hexdigest()[:20]
        assert record["id"] == (
            f'place-v1:{city_indexes[record["city"]]}:'
            f'{category_indexes[record["category"]]}:{digest}'
        )
        assert record["sources"]
        source_kinds = {source["kind"] for source in record["sources"]}
        assert {"wikidata_cc0_candidate", "official_verification"}.issubset(source_kinds)
        for source in record["sources"]:
            assert source["url"].startswith(("https://", "http://"))
        for alias in record["aliases"]:
            normalized = normalize_place_name(alias)
            assert normalized not in canonical_names
            assert not alias.endswith("站站")
            assert normalized not in aliases, (alias, record["id"], aliases.get(normalized))
            aliases[normalized] = record["id"]

    loaded = load_three_city_place_lexicon(strict=True)
    assert loaded.available is True
    assert len(loaded.entries) == 900


def test_normalization_preserves_identity_qualifiers_and_only_full_suffixes() -> None:
    assert normalize_place_name(" 杭州－奥体 中心（3号门） ") == "杭州·奥体中心(3号门)"
    assert normalize_place_name("T2航站楼") != normalize_place_name("T3航站楼")
    assert normalize_place_name("西溪店") != normalize_place_name("湖滨店")
    assert normalize_place_name("东校区") != normalize_place_name("西校区")
    assert normalize_place_name("体育场（北门）") != normalize_place_name("体育场（南门）")

    assert strip_complete_venue_suffix("首都博物馆") == normalize_place_name("首都")
    assert strip_complete_venue_suffix("国家体育场") == normalize_place_name("国家")
    assert strip_complete_venue_suffix("杭州站") is None
    assert strip_complete_venue_suffix("中山公园") is None
    assert strip_complete_venue_suffix("湖滨店") is None
    assert strip_complete_venue_suffix("书院") is None
    assert venue_suffix_equivalent("故宫", "故宫博物院") is True
    assert venue_suffix_equivalent("首都博物馆", "首都博物院") is True
    assert venue_suffix_equivalent("杭州站", "杭州") is False


def test_city_scoped_lookup_obeys_tier_priority_and_exposes_ambiguity() -> None:
    lexicon = ThreeCityPlaceLexicon(
        entries=(
            _entry("one", "故宫博物院", aliases=("故宫",)),
            _entry("two", "故宫", aliases=("紫禁城",)),
            _entry("three", "首都博物馆", aliases=("首博",)),
            _entry("four", "首都博物院", aliases=("首博",)),
        )
    )

    canonical = lexicon.lookup(city="北京市", name="故宫")
    assert canonical.tier is LexiconMatchTier.CANONICAL_EXACT
    assert [entry.entry_id for entry in canonical.matches] == ["two"]

    alias = lexicon.lookup(city="北京", name="紫禁城")
    assert alias.tier is LexiconMatchTier.SAFE_ALIAS_EXACT
    assert alias.unique is not None and alias.unique.entry_id == "two"

    ambiguous_alias = lexicon.lookup(city="北京", name="首博")
    assert ambiguous_alias.tier is LexiconMatchTier.SAFE_ALIAS_EXACT
    assert {entry.entry_id for entry in ambiguous_alias.matches} == {"three", "four"}

    suffix = lexicon.lookup(city="北京", name="首都")
    assert suffix.tier is LexiconMatchTier.VENUE_SUFFIX_EQUIVALENT
    assert {entry.entry_id for entry in suffix.matches} == {"three", "four"}
    assert lexicon.lookup(city="上海", name="故宫").tier is LexiconMatchTier.NONE


def test_missing_or_invalid_lexicon_fails_safe_without_startup_error(tmp_path: Path) -> None:
    missing = load_three_city_place_lexicon(tmp_path / "missing.jsonl")
    assert missing.available is False
    assert missing.entries == ()
    assert missing.error_code == "LEXICON_MISSING"

    invalid_path = tmp_path / "invalid.jsonl"
    invalid_path.write_text('{"not":"the schema"}\n', encoding="utf-8", newline="\n")
    invalid = load_three_city_place_lexicon(invalid_path)
    assert invalid.available is False
    assert invalid.entries == ()
    assert invalid.error_code == "LEXICON_INVALID"
    with pytest.raises(LexiconValidationError):
        load_three_city_place_lexicon(invalid_path, strict=True)
