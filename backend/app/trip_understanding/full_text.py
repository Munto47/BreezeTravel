from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.trip_understanding.models import (
    ActivityRole,
    InferenceProposal,
    ProposedMention,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import (
    ResilientStructuredInferenceProvider,
    StructuredInferenceProvider,
    TripUnderstandingPipeline,
)


_CONTROLLED_PLACE_PATH = Path(__file__).resolve().parents[1] / "data" / "amap_mock_places.json"
_DEEP_CITIES = frozenset({"北京", "上海", "杭州"})
_DAY_NUMBER = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}
_DAY_HEADING_RE = re.compile(
    r"(?:第\s*(?P<zh>[一二三四五六七八九十])\s*天|(?:Day|D)\s*(?P<num>1[0-4]|[1-9]))",
    re.IGNORECASE,
)
_URL_TOKEN_RE = re.compile(r"https?://[^\s，。；！？]+", re.IGNORECASE)
_EXCLUDED_CUES = ("不去", "排除", "取消", "不要", "跳过", "不安排", "放弃")
_OPTIONAL_CUES = ("可选", "备选", "有空", "时间允许", "如果有时间", "可以考虑", "顺路再去")
_PASS_THROUGH_CUES = ("路过", "经过", "途经")
_REFERENCE_CUES = ("听说", "据说", "参考", "攻略提到", "有人推荐")
_PLANNED_CUES = ("去", "游览", "逛", "参观", "打卡", "安排", "前往")
_PUBLIC_CATEGORY_LABELS = {
    "attraction": "景点",
    "food": "餐饮",
    "hotel": "住宿",
}


@dataclass(frozen=True)
class _PlaceFact:
    place_id: str
    name: str
    category: str
    address: str
    city: str


def _load_catalog() -> tuple[dict[str, list[_PlaceFact]], str]:
    raw = _CONTROLLED_PLACE_PATH.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    by_name: dict[str, list[_PlaceFact]] = {}
    for city, entries in payload.items():
        for entry in entries:
            fact = _PlaceFact(
                place_id=str(entry["place_id"]),
                name=str(entry["name"]),
                category=_PUBLIC_CATEGORY_LABELS.get(
                    str(entry["category"]),
                    str(entry["category"]),
                ),
                address=str(entry["address"]),
                city=str(city),
            )
            by_name.setdefault(fact.name, []).append(fact)
    for fact in (
        _PlaceFact(
            place_id="fixture-bj-qianmen",
            name="前门大街",
            category="街区",
            address="东城区·前门大街",
            city="北京",
        ),
        _PlaceFact(
            place_id="fixture-bj-old-summer-palace",
            name="圆明园",
            category="公园",
            address="海淀区·清华西路28号",
            city="北京",
        ),
    ):
        by_name.setdefault(fact.name, []).append(fact)
    return by_name, hashlib.sha256(raw).hexdigest()


_PLACES_BY_NAME, CONTROLLED_PLACE_SNAPSHOT_SHA256 = _load_catalog()


def _day_headings(source_text: str) -> list[tuple[int, int]]:
    headings: list[tuple[int, int]] = []
    for match in _DAY_HEADING_RE.finditer(source_text):
        raw = match.group("zh") or match.group("num")
        day = int(raw) if raw.isdigit() else _DAY_NUMBER[raw]
        headings.append((match.start(), day))
    return headings


def _day_for_position(position: int, headings: list[tuple[int, int]]) -> int | None:
    day = None
    for start, candidate in headings:
        if start > position:
            break
        day = candidate
    return day


def _inside_url(position: int, url_spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in url_spans)


def _role_for_context(source_text: str, start: int, *, has_day: bool) -> ActivityRole:
    context = source_text[max(0, start - 24) : start]
    if any(cue in context for cue in _EXCLUDED_CUES):
        return ActivityRole.EXCLUDED
    if any(cue in context for cue in _OPTIONAL_CUES):
        return ActivityRole.OPTIONAL
    if any(cue in context for cue in _PASS_THROUGH_CUES):
        return ActivityRole.PASS_THROUGH
    if any(cue in context for cue in _REFERENCE_CUES):
        return ActivityRole.REFERENCE
    if has_day or any(cue in context[-12:] for cue in _PLANNED_CUES):
        return ActivityRole.PLANNED
    return ActivityRole.REFERENCE


def _destination(
    source_text: str,
    candidates: list[tuple[int, int, str, set[str]]],
    headings: list[tuple[int, int]],
) -> str:
    candidate_spans = [(start, end) for start, end, *_ in candidates]
    explicit: list[str] = []
    for city in _DEEP_CITIES:
        for match in re.finditer(re.escape(city), source_text):
            if not any(start <= match.start() < end for start, end in candidate_spans):
                explicit.append(city)
                break
    if len(explicit) == 1:
        return explicit[0]
    planned_cities = {
        city
        for start, _end, _name, cities in candidates
        if _role_for_context(
            source_text,
            start,
            has_day=_day_for_position(start, headings) is not None,
        )
        == ActivityRole.PLANNED
        for city in cities
    }
    if len(planned_cities) == 1:
        return next(iter(planned_cities))
    return "目的地待确认"


class DeterministicTextInferenceProvider:
    """Conservative local semantic proposal for the pre-live FULL lane.

    Only names present verbatim in the controlled snapshot become mentions.
    Role and day assignment are explicit-rule based; ambiguous prose stays a
    reference and therefore never reaches automatic place resolution.
    """

    async def propose(self, source_text: str) -> InferenceProposal:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        headings = _day_headings(source_text)
        url_spans = [(match.start(), match.end()) for match in _URL_TOKEN_RE.finditer(source_text)]
        candidates: list[tuple[int, int, str, set[str]]] = []
        occupied: list[tuple[int, int]] = []
        for name in sorted(_PLACES_BY_NAME, key=len, reverse=True):
            cities = {item.city for item in _PLACES_BY_NAME[name]}
            for match in re.finditer(re.escape(name), source_text):
                span = (match.start(), match.end())
                if _inside_url(match.start(), url_spans):
                    continue
                if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
                    continue
                occupied.append(span)
                candidates.append((span[0], span[1], name, cities))
        candidates.sort(key=lambda item: (item[0], item[1]))

        destination = _destination(source_text, candidates, headings)
        day_sequences: dict[int, int] = {}
        mentions: list[ProposedMention] = []
        for index, (start, end, name, _cities) in enumerate(candidates, start=1):
            explicit_day = _day_for_position(start, headings)
            role = _role_for_context(source_text, start, has_day=explicit_day is not None)
            day_index = explicit_day if role == ActivityRole.PLANNED else None
            if role == ActivityRole.PLANNED and day_index is None:
                day_index = 1
            sequence_index = day_sequences.get(day_index or 0, 0)
            day_sequences[day_index or 0] = sequence_index + 1
            facts = _PLACES_BY_NAME[name]
            category = facts[0].category if len({item.category for item in facts}) == 1 else None
            mentions.append(
                ProposedMention(
                    mention_id=f"mention-{index}",
                    raw_text=source_text[start:end],
                    span_start=start,
                    span_end=end,
                    role=role,
                    day_index=day_index,
                    sequence_index=sequence_index,
                    atomic_place_name=name,
                    category_hint=category,
                )
            )
        return InferenceProposal(
            source_hash=source_hash,
            destination_name=destination,
            mentions=mentions,
            binding={
                "provider": "deterministic-controlled-text",
                "version": "v1",
                "snapshot_sha256": CONTROLLED_PLACE_SNAPSHOT_SHA256,
                "external_calls": 0,
                "quality_lane": "PRE_LIVE_CONSERVATIVE",
            },
        )


class ControlledSnapshotPlaceResolver:
    async def resolve(self, *, city: str, atomic_place_name: str) -> ResolvedPlace | None:
        if city not in _DEEP_CITIES:
            return None
        candidates = [fact for fact in _PLACES_BY_NAME.get(atomic_place_name, []) if fact.city == city]
        if len(candidates) != 1:
            return None
        fact = candidates[0]
        return ResolvedPlace(
            canonical_place_id=fact.place_id,
            name=fact.name,
            category=fact.category,
            area_or_address=fact.address,
            provider_binding={
                "provider": "controlled_fixture_snapshot",
                "snapshot_sha256": CONTROLLED_PLACE_SNAPSHOT_SHA256,
                "external_calls": 0,
            },
        )


def build_full_text_pipeline(
    primary_inference_provider: StructuredInferenceProvider | None = None,
) -> TripUnderstandingPipeline:
    deterministic_fallback = DeterministicTextInferenceProvider()
    return TripUnderstandingPipeline(
        inference_provider=(
            ResilientStructuredInferenceProvider(
                primary_inference_provider,
                deterministic_fallback,
            )
            if primary_inference_provider is not None
            else deterministic_fallback
        ),
        place_resolver=ControlledSnapshotPlaceResolver(),
    )
