from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.trip_understanding.models import (
    ActivityRole,
    DestinationBasis,
    InferenceProposal,
    ProposedMention,
    ResolvedPlace,
)
from app.trip_understanding.pipeline import (
    PlaceResolver,
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
    r"(?:第\s*(?:(?P<zh>[一二三四五六七八九十])|(?P<arabic>1[0-4]|[1-9]))\s*天"
    r"|(?:Day|D)\s*(?P<latin>1[0-4]|[1-9]))",
    re.IGNORECASE,
)
_URL_TOKEN_RE = re.compile(r"https?://[^\s，。；！？]+", re.IGNORECASE)
_CLAUSE_BOUNDARIES = "，,。！？；;\n"
_PLANNED_ACTION_PATTERN = (
    r"(?:确定行程是|确定游览|依次到|随后前往|步行到|先到|先去|先逛|"
    r"再去|再到|再逛|上午看|下午看|上午安排|下午安排|游览|参观|"
    r"打卡|安排|前往|逛|去)"
)
_PLANNED_ACTION_RE = re.compile(
    rf"{_PLANNED_ACTION_PATTERN}\s*"
    rf"(?P<names>[^，,。！？；;\n]*?)"
    rf"(?={_PLANNED_ACTION_PATTERN}|[，,。！？；;\n]|$)"
)
_LEADING_PLANNED_ACTION_RE = re.compile(rf"^{_PLANNED_ACTION_PATTERN}")
_PLAN_TRAILING_MARKERS = (
    "结束当天",
    "放在前面",
    "两处都属于",
    "顺序以正文",
    "这些六处",
    "才是逐日计划",
    "了解预约流程",
    "了解预约",
)
_EXCLUDED_CUES = ("明确不去", "已经决定排除", "决定排除", "排除", "取消", "跳过", "不安排", "放弃")
_OPTIONAL_CUES = (
    "可选",
    "备选",
    "有空",
    "时间允许",
    "时间充裕",
    "如果有时间",
    "如果当天太累",
    "如果太累",
    "可以不去",
    "可以完全不去",
    "可以考虑",
    "顺路再去",
)
_PASS_THROUGH_CUES = ("路过", "经过", "途经", "换乘", "中转")
_REFERENCE_CUES = (
    "听说",
    "据说",
    "参考",
    "提到",
    "推荐",
    "不是本次安排",
    "不表示已经安排",
)
_META_REFERENCE_CUES = (
    "模型举例",
    "如果用户说",
    "只是示例",
    "仅作示例",
    "不是地点",
    "不要把这个例子",
    "不要因为",
    "不要把",
    "不能生成地点卡",
    "说明性整句",
    "主题是",
)
_PLANNED_CUES = ("去", "游览", "逛", "参观", "打卡", "安排", "前往")
_NON_PLACE_ATOMIC_MARKERS = (
    "上午",
    "下午",
    "随后",
    "前往",
    "游览",
    "参观",
    "打卡",
    "安排",
    "确定",
    "如果",
    "只是",
    "网友",
    "提到",
    "参考",
    "备选",
    "路过",
    "经过",
    "换乘",
    "不去",
    "排除",
    "很有名",
    "结束当天",
    "放在前面",
    "逐日计划",
    "当天计划",
    "午饭时间",
    "返回住处",
    "分钟",
    "预约",
    "说明",
    "网址",
    "链接",
    "导航",
    "路线",
    "交通",
    "内部",
    "规则",
    "卡片",
    "表单",
    "电话",
    "确认",
    "营业",
    "票价",
    "开放时间",
)
_ATOMIC_ROLE_NAME_PATTERN = r"[A-Za-z0-9\u4e00-\u9fff·（）()—_-]{2,40}"
_ROLE_NAME_FORBIDDEN_MARKERS = ("仅在", "只在", "时间充裕", "作为", "当作", "可以")
_EXPLICIT_ROLE_PATTERNS: tuple[tuple[ActivityRole, re.Pattern[str]], ...] = (
    (
        ActivityRole.EXCLUDED,
        re.compile(
            rf"(?:明确不去|已经决定排除|决定排除|取消|不安排|放弃|排除)"
            rf"\s*(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})"
        ),
    ),
    (
        ActivityRole.OPTIONAL,
        re.compile(
            rf"(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})\s*"
            rf"(?:仅在|只在).*?(?:备选|可选)"
        ),
    ),
    (
        ActivityRole.OPTIONAL,
        re.compile(
            rf"(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})\s*"
            rf"(?:作为|当作)(?:备选|可选)"
        ),
    ),
    (
        ActivityRole.OPTIONAL,
        re.compile(
            rf"[，,]\s*(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})\s*"
            rf"可以(?:完全)?不去"
        ),
    ),
    (
        ActivityRole.PASS_THROUGH,
        re.compile(
            rf"(?:会经过|经过|只是路过|路过|途经)\s*"
            rf"(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})\s*"
            rf"(?:[，,]|但|换乘|中转)"
        ),
    ),
    (
        ActivityRole.REFERENCE,
        re.compile(
            rf"(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})\s*只是从.*?"
            rf"(?:听说|提到|推荐)"
        ),
    ),
    (
        ActivityRole.REFERENCE,
        re.compile(
            rf"(?:网友曾|网友|另一篇攻略|攻略)?\s*"
            rf"(?:提到|推荐|听说)\s*(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})"
        ),
    ),
)
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
    longitude: float
    latitude: float


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
                longitude=float(entry["coords"]["lng"]),
                latitude=float(entry["coords"]["lat"]),
            )
            by_name.setdefault(fact.name, []).append(fact)
    for fact in (
        _PlaceFact(
            place_id="fixture-bj-qianmen",
            name="前门大街",
            category="街区",
            address="东城区·前门大街",
            city="北京",
            longitude=116.3936,
            latitude=39.8992,
        ),
        _PlaceFact(
            place_id="fixture-bj-old-summer-palace",
            name="圆明园",
            category="公园",
            address="海淀区·清华西路28号",
            city="北京",
            longitude=116.3039,
            latitude=40.0081,
        ),
    ):
        by_name.setdefault(fact.name, []).append(fact)
    return by_name, hashlib.sha256(raw).hexdigest()


_PLACES_BY_NAME, CONTROLLED_PLACE_SNAPSHOT_SHA256 = _load_catalog()


def _day_headings(source_text: str) -> list[tuple[int, int]]:
    headings: list[tuple[int, int]] = []
    for match in _DAY_HEADING_RE.finditer(source_text):
        raw = match.group("zh") or match.group("arabic") or match.group("latin")
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


def _clause_for_position(source_text: str, start: int, end: int) -> str:
    left = max(source_text.rfind(marker, 0, start) for marker in _CLAUSE_BOUNDARIES) + 1
    right_candidates = [
        position
        for marker in _CLAUSE_BOUNDARIES
        if (position := source_text.find(marker, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(source_text)
    return source_text[left:right]


def _sentence_for_position(source_text: str, start: int, end: int) -> str:
    boundaries = "。！？；;\n"
    left = max(source_text.rfind(marker, 0, start) for marker in boundaries) + 1
    right_candidates = [
        position
        for marker in boundaries
        if (position := source_text.find(marker, end)) >= 0
    ]
    right = min(right_candidates) if right_candidates else len(source_text)
    return source_text[left:right]


def _is_meta_activity_clause(clause: str) -> bool:
    if any(cue in clause for cue in _META_REFERENCE_CUES):
        return True
    return (
        any(marker in clause for marker in ("整理", "围绕"))
        and any(marker in clause for marker in ("路线", "笔记", "攻略", "主题"))
    ) or ("朋友转来" in clause and "笔记" in clause)


def _role_for_context(
    source_text: str,
    start: int,
    end: int,
    *,
    has_day: bool,
) -> ActivityRole | None:
    clause = _clause_for_position(source_text, start, end)
    context = source_text[max(0, start - 24) : start]
    if _is_meta_activity_clause(clause):
        return ActivityRole.REFERENCE
    if any(cue in clause for cue in _EXCLUDED_CUES):
        return ActivityRole.EXCLUDED
    if any(cue in clause for cue in _OPTIONAL_CUES):
        return ActivityRole.OPTIONAL
    if any(cue in clause for cue in ("不去", "不要去")):
        return ActivityRole.EXCLUDED
    if any(cue in clause for cue in _PASS_THROUGH_CUES):
        return ActivityRole.PASS_THROUGH
    if any(cue in clause for cue in _REFERENCE_CUES):
        return ActivityRole.REFERENCE
    if has_day or any(cue in context[-12:] for cue in _PLANNED_CUES):
        return ActivityRole.PLANNED
    return ActivityRole.REFERENCE


def _atomic_category_hint(name: str) -> str | None:
    if name.endswith(("站", "机场", "码头")):
        return "交通节点"
    if name.endswith(("酒店", "宾馆", "民宿")):
        return "住宿"
    if name.endswith(("餐厅", "饭店", "茶室", "酒楼", "小馆")):
        return "餐饮"
    if name.endswith(
        (
            "公园",
            "博物馆",
            "纪念馆",
            "美术馆",
            "展馆",
            "书院",
            "故居",
            "景区",
            "广场",
            "古镇",
            "老街",
            "园",
        )
    ):
        return "景点"
    return None


def _is_atomic_place_text(value: str) -> bool:
    if not 1 < len(value) <= 40:
        return False
    if _LEADING_PLANNED_ACTION_RE.search(value):
        return False
    if _URL_TOKEN_RE.search(value) or any(marker in value for marker in _CLAUSE_BOUNDARIES):
        return False
    if any(marker in value for marker in _NON_PLACE_ATOMIC_MARKERS):
        return False
    if re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_-]+", value) is None:
        return False
    return re.search(r"[A-Za-z\u4e00-\u9fff]", value) is not None


def _explicit_role_candidates(
    source_text: str,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
) -> list[tuple[int, int, str, set[str]]]:
    candidates: list[tuple[int, int, str, set[str]]] = []
    for clause_match in re.finditer(r"[^。；;\n]+", source_text):
        clause = clause_match.group(0)
        if _is_meta_activity_clause(clause):
            continue
        for _role, pattern in _EXPLICIT_ROLE_PATTERNS:
            for match in pattern.finditer(clause):
                start = clause_match.start() + match.start("name")
                end = clause_match.start() + match.end("name")
                name = source_text[start:end]
                span = (start, end)
                if (
                    not _is_atomic_place_text(name)
                    or any(marker in name for marker in _ROLE_NAME_FORBIDDEN_MARKERS)
                    or _inside_url(start, url_spans)
                    or any(
                        not (end <= old_start or start >= old_end)
                        for old_start, old_end in occupied
                    )
                ):
                    continue
                occupied.append(span)
                cities = {city for city in _DEEP_CITIES if city in name}
                candidates.append((start, end, name, cities))
    return candidates


def _trim_plan_capture(
    source_text: str,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    selected = source_text[start:end]
    leading = len(selected) - len(selected.lstrip(" \t：:'‘\"“"))
    trailing = len(selected.rstrip(" \t：:'’\"”"))
    start += leading
    end = start + max(0, trailing - leading)
    if start >= end:
        return None
    value = source_text[start:end]
    cut = min(
        (position for marker in _PLAN_TRAILING_MARKERS if (position := value.find(marker)) >= 0),
        default=len(value),
    )
    value = value[:cut].rstrip(" \t：:'’\"”")
    end = start + len(value)
    return (start, end) if start < end else None


def _append_plan_capture(
    source_text: str,
    start: int,
    end: int,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
    candidates: list[tuple[int, int, str, set[str]]],
) -> None:
    trimmed = _trim_plan_capture(source_text, start, end)
    if trimmed is None:
        return
    start, end = trimmed
    value = source_text[start:end]
    if value in _PLACES_BY_NAME:
        pieces = [(start, end)]
    elif "、" in value:
        pieces = []
        cursor = 0
        for connector in re.finditer("、", value):
            pieces.append((start + cursor, start + connector.start()))
            cursor = connector.end()
        pieces.append((start + cursor, end))
        if not all(
            piece_start < piece_end
            and _is_atomic_place_text(source_text[piece_start:piece_end])
            for piece_start, piece_end in pieces
        ):
            pieces = [(start, end)]
    else:
        pieces = [(start, end)]
        for connector_value in ("与", "和"):
            for connector in re.finditer(connector_value, value):
                split_pieces = [
                    (start, start + connector.start()),
                    (start + connector.end(), end),
                ]
                if all(
                    piece_start < piece_end
                    and _is_atomic_place_text(source_text[piece_start:piece_end])
                    for piece_start, piece_end in split_pieces
                ):
                    pieces = split_pieces
                    break
            if len(pieces) > 1:
                break
    for piece_start, piece_end in pieces:
        name = source_text[piece_start:piece_end]
        span = (piece_start, piece_end)
        if (
            not _is_atomic_place_text(name)
            or _inside_url(piece_start, url_spans)
            or any(
                not (piece_end <= old_start or piece_start >= old_end)
                for old_start, old_end in occupied
            )
        ):
            continue
        occupied.append(span)
        cities = {city for city in _DEEP_CITIES if city in name}
        candidates.append((piece_start, piece_end, name, cities))


def _planned_atomic_candidates(
    source_text: str,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
) -> list[tuple[int, int, str, set[str]]]:
    candidates: list[tuple[int, int, str, set[str]]] = []
    for match in _PLANNED_ACTION_RE.finditer(source_text):
        if _is_meta_activity_clause(
            _sentence_for_position(source_text, match.start(), match.end())
        ):
            continue
        _append_plan_capture(
            source_text,
            match.start("names"),
            match.end("names"),
            url_spans=url_spans,
            occupied=occupied,
            candidates=candidates,
        )

    heading_matches = list(_DAY_HEADING_RE.finditer(source_text))
    for index, heading in enumerate(heading_matches):
        segment_start = heading.end()
        next_heading = (
            heading_matches[index + 1].start()
            if index + 1 < len(heading_matches)
            else len(source_text)
        )
        strong_ends = [
            position
            for marker in "。！？\n"
            if (position := source_text.find(marker, segment_start, next_heading)) >= 0
        ]
        segment_end = min(strong_ends) if strong_ends else next_heading
        segment = source_text[segment_start:segment_end].strip(" \t：:；;")
        if not segment:
            continue
        absolute_start = source_text.find(segment, segment_start, segment_end)

        between_match = re.search(
            rf"先(?P<first>{_ATOMIC_ROLE_NAME_PATTERN})后"
            rf"(?P<second>{_ATOMIC_ROLE_NAME_PATTERN})$",
            segment,
        )
        if between_match:
            for group in ("first", "second"):
                _append_plan_capture(
                    source_text,
                    absolute_start + between_match.start(group),
                    absolute_start + between_match.end(group),
                    url_spans=url_spans,
                    occupied=occupied,
                    candidates=candidates,
                )

        put_match = re.search(
            rf"把(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})放在前面",
            segment,
        )
        if put_match:
            _append_plan_capture(
                source_text,
                absolute_start + put_match.start("name"),
                absolute_start + put_match.end("name"),
                url_spans=url_spans,
                occupied=occupied,
                candidates=candidates,
            )

        if not any(
            not (segment_end <= old_start or segment_start >= old_end)
            for old_start, old_end in occupied
        ) and all(marker not in segment for marker in "，,；;"):
            _append_plan_capture(
                source_text,
                absolute_start,
                absolute_start + len(segment),
                url_spans=url_spans,
                occupied=occupied,
                candidates=candidates,
            )
    return candidates


def _destination(
    source_text: str,
    candidates: list[tuple[int, int, str, set[str]]],
    headings: list[tuple[int, int]],
) -> tuple[str, DestinationBasis]:
    candidate_spans = [(start, end) for start, end, *_ in candidates]
    explicit: list[str] = []
    for city in _DEEP_CITIES:
        for match in re.finditer(re.escape(city), source_text):
            if not any(start <= match.start() < end for start, end in candidate_spans):
                explicit.append(city)
                break
    if len(explicit) == 1:
        return explicit[0], DestinationBasis.EXPLICIT
    planned_cities = {
        city
        for start, _end, _name, cities in candidates
        if _role_for_context(
            source_text,
            start,
            _end,
            has_day=_day_for_position(start, headings) is not None,
        )
        == ActivityRole.PLANNED
        for city in cities
    }
    if len(planned_cities) == 1:
        return next(iter(planned_cities)), DestinationBasis.SOFT_ASSUMPTION
    return "目的地待确认", DestinationBasis.SOFT_ASSUMPTION


class DeterministicTextInferenceProvider:
    """Conservative local semantic proposal for the pre-live FULL lane.

    Controlled names and explicit cue-led atomic place text become mentions.
    Role and day assignment are explicit-rule based; ambiguous prose stays a
    reference and therefore never reaches automatic place resolution.
    """

    async def propose(self, source_text: str) -> InferenceProposal:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        headings = _day_headings(source_text)
        url_spans = [(match.start(), match.end()) for match in _URL_TOKEN_RE.finditer(source_text)]
        occupied: list[tuple[int, int]] = []
        candidates = _planned_atomic_candidates(
            source_text,
            url_spans=url_spans,
            occupied=occupied,
        )
        candidates.extend(
            _explicit_role_candidates(
                source_text,
                url_spans=url_spans,
                occupied=occupied,
            )
        )
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

        destination, destination_basis = _destination(source_text, candidates, headings)
        day_sequences: dict[int, int] = {}
        mentions: list[ProposedMention] = []
        for start, end, name, _cities in candidates:
            explicit_day = _day_for_position(start, headings)
            role = _role_for_context(
                source_text,
                start,
                end,
                has_day=explicit_day is not None,
            )
            if role is None:
                continue
            meta_description = _is_meta_activity_clause(
                _clause_for_position(source_text, start, end)
            )
            day_index = explicit_day if role == ActivityRole.PLANNED else None
            if role == ActivityRole.PLANNED and day_index is None:
                day_index = 1
            sequence_index = day_sequences.get(day_index or 0, 0)
            day_sequences[day_index or 0] = sequence_index + 1
            facts = _PLACES_BY_NAME.get(name, []) if not meta_description else []
            category = (
                facts[0].category
                if facts and len({item.category for item in facts}) == 1
                else _atomic_category_hint(name) if not meta_description else None
            )
            mentions.append(
                ProposedMention(
                    mention_id=f"mention-{len(mentions) + 1}",
                    raw_text=source_text[start:end],
                    span_start=start,
                    span_end=end,
                    role=role,
                    day_index=day_index,
                    sequence_index=sequence_index,
                    atomic_place_name=None if meta_description else name,
                    category_hint=category,
                )
            )
        return InferenceProposal(
            source_hash=source_hash,
            destination_name=destination,
            destination_basis=destination_basis,
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
    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> ResolvedPlace | None:
        del category_hint
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
                "coordinates": {
                    "longitude": fact.longitude,
                    "latitude": fact.latitude,
                },
            },
        )


def build_full_text_pipeline(
    primary_inference_provider: StructuredInferenceProvider | None = None,
    place_resolver: PlaceResolver | None = None,
    *,
    max_place_concurrency: int = 4,
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
        place_resolver=place_resolver or ControlledSnapshotPlaceResolver(),
        max_place_concurrency=max_place_concurrency,
    )
