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
_MULTI_DEEP_CITY_HEADER_RE = re.compile(
    r"^\s*(?P<cities>(?:北京|上海|杭州)(?:\s*[、，,和与/]\s*(?:北京|上海|杭州))+?)"
    r"\s*(?:两地|三地|多地)?(?:游|行程|攻略|旅行)"
)
_BASIC_CITY_HEADER_RE = re.compile(
    r"^\s*(?P<city>[\u4e00-\u9fff]{2,6}?)[一二两三四五六七八九十0-9]+"
    r"(?:日|天)(?:游|行程|攻略|旅行)"
)
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
_CLAUSE_BOUNDARIES = "，,。！？；;：:\n"
_PLANNED_ACTION_PATTERN = (
    r"(?:确定行程是|确定游览|依次到|随后前往|步行到|先到|先去|先逛|"
    r"再去|再到|再逛|上午看|下午看|上午安排|下午安排|来到|可游览?|"
    r"游览(?!线(?:路)?)|参观|打卡|安排|前往|逛|去(?!年|过))"
)
_PLANNED_ACTION_RE = re.compile(
    rf"{_PLANNED_ACTION_PATTERN}\s*"
    rf"(?P<names>[^，,。！？；;：:\n]*?)"
    rf"(?={_PLANNED_ACTION_PATTERN}|[，,。！？；;：:\n]|$)"
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
    "并参考",
    "并听说",
    "仅作备选",
    "只作参考",
    "作为备选",
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
    "可以选择",
    "可选择",
    "也可以",
    "可以继续",
    "后续可游",
    "若选择",
    "如果选择",
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
    "旅途",
    "游览线",
    "游览线路",
    "线路",
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
        ActivityRole.PASS_THROUGH,
        re.compile(
            rf"(?:经由|经)\s*(?P<name>{_ATOMIC_ROLE_NAME_PATTERN})\s*"
            rf"(?:往返|通过|前往)"
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


@dataclass(frozen=True)
class _SourceView:
    """Semantic view that retains a reversible code-point mapping.

    Screenshot OCR emits visual rows, so a place name may be split at a row
    boundary (for example ``门\n票站``).  Parsing the reversible view lets the
    semantic proposal use the intact atomic name while the evidence quote and
    half-open span still point at the exact original source text.
    """

    text: str
    source_positions: tuple[int, ...]

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        if start < 0 or end <= start or end > len(self.source_positions):
            raise ValueError("semantic span is outside the source view")
        return self.source_positions[start], self.source_positions[end - 1] + 1


@dataclass(frozen=True)
class _MentionCandidate:
    start: int
    end: int
    name: str
    cities: frozenset[str]
    role_hint: ActivityRole | None = None
    day_hint: int | None = None


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


def _semantic_source_view(source_text: str) -> _SourceView:
    text: list[str] = []
    source_positions: list[int] = []
    index = 0
    raw_line_start = 0
    while index < len(source_text):
        character = source_text[index]
        if character not in "\r\n":
            text.append(character)
            source_positions.append(index)
            index += 1
            continue

        newline_start = index
        visual_line_length = len(
            source_text[raw_line_start:newline_start].strip(" \t")
        )
        if character == "\r" and index + 1 < len(source_text) and source_text[index + 1] == "\n":
            index += 2
        else:
            index += 1
        raw_line_start = index
        next_content = index
        while next_content < len(source_text) and source_text[next_content] in " \t":
            next_content += 1
        previous = next((item for item in reversed(text) if not item.isspace()), "")
        following = source_text[next_content : next_content + 12]
        starts_bullet = bool(re.match(r"(?:[•*]|-\s)", following))
        starts_day = _DAY_HEADING_RE.match(source_text, next_content) is not None
        is_semantic_boundary = (
            not previous
            or previous in "。！？；;：:"
            or starts_bullet
            or starts_day
            or visual_line_length < 12
        )
        if is_semantic_boundary:
            text.append("\n")
            source_positions.append(newline_start)
        else:
            index = next_content

    return _SourceView(text="".join(text), source_positions=tuple(source_positions))


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


def _is_descriptive_reference_clause(clause: str) -> bool:
    if any(cue in clause for cue in ("预约说明", "预约流程")):
        return True
    if re.search(r"(?:相比|比|不如)[^。！？；;]*(?:更|较|热门|有名|适合|值得)", clause):
        return True
    if _PLANNED_ACTION_RE.search(clause):
        return False
    return any(
        cue in clause
        for cue in (
            "去年",
            "前年",
            "曾经",
            "历史上",
            "过去",
            "此前",
            "当年",
            "世界文化遗产",
            "游客很多",
        )
    ) or re.search(
        r"(?:是|为|位于|坐落于|建于|始建于|被誉为|属于|拥有)[^。！？；;]*$",
        clause,
    ) is not None


def _inside_quoted_text(source_text: str, position: int) -> bool:
    for opening, closing in (("“", "”"), ('"', '"'), ("‘", "’"), ("'", "'")):
        left = source_text.rfind(opening, 0, position)
        if left < 0:
            continue
        right = source_text.find(closing, left + 1)
        if right >= position:
            return True
    return False


def _role_for_context(
    source_text: str,
    start: int,
    end: int,
    *,
    has_day: bool,
) -> ActivityRole | None:
    clause = _clause_for_position(source_text, start, end)
    context = source_text[max(0, start - 24) : start]
    local_before = source_text[max(0, start - 4) : start]
    local_after = source_text[end : min(len(source_text), end + 6)]
    if _inside_quoted_text(source_text, start):
        return ActivityRole.REFERENCE
    if _is_meta_activity_clause(clause):
        return ActivityRole.REFERENCE
    if any(cue in clause for cue in _EXCLUDED_CUES):
        return ActivityRole.EXCLUDED
    if re.search(r"(?:经由|经)\s*$", local_before) and re.match(
        r"\s*(?:往返|通过|前往)", local_after
    ):
        return ActivityRole.PASS_THROUGH
    if any(cue in clause for cue in _OPTIONAL_CUES):
        return ActivityRole.OPTIONAL
    if any(cue in clause for cue in ("不去", "不要去")):
        return ActivityRole.EXCLUDED
    if any(cue in clause for cue in _PASS_THROUGH_CUES):
        return ActivityRole.PASS_THROUGH
    if any(cue in clause for cue in _REFERENCE_CUES):
        return ActivityRole.REFERENCE
    if _is_descriptive_reference_clause(clause):
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


def _candidate_cities(name: str) -> set[str]:
    controlled_cities = {fact.city for fact in _PLACES_BY_NAME.get(name, ())}
    if controlled_cities:
        return controlled_cities
    return {city for city in _DEEP_CITIES if city in name}


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
) -> list[_MentionCandidate]:
    candidates: list[_MentionCandidate] = []
    for clause_match in re.finditer(r"[^。；;\n]+", source_text):
        clause = clause_match.group(0)
        if _is_meta_activity_clause(clause):
            continue
        for role, pattern in _EXPLICIT_ROLE_PATTERNS:
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
                candidates.append(
                    _MentionCandidate(
                        start=start,
                        end=end,
                        name=name,
                        cities=frozenset(_candidate_cities(name)),
                        role_hint=role,
                    )
                )
    return candidates


def _trim_plan_capture(
    source_text: str,
    start: int,
    end: int,
) -> tuple[int, int] | None:
    selected = source_text[start:end]
    leading = len(selected) - len(selected.lstrip(" \t：:'‘\"“"))
    start += leading
    value = selected[leading:].rstrip(" \t：:'’\"”")
    end = start + len(value)
    if start >= end:
        return None
    cut = min(
        (
            position
            for marker in (*_PLAN_TRAILING_MARKERS, "（", "(")
            if (position := value.find(marker)) >= 0
        ),
        default=len(value),
    )
    value = value[:cut].rstrip(" \t：:'’\"”")
    end = start + len(value)
    return (start, end) if start < end else None


def _atomic_capture_pieces(
    source_text: str,
    start: int,
    end: int,
) -> list[tuple[int, int]]:
    memo: dict[tuple[int, int], list[tuple[int, int]] | None] = {}

    def split(piece_start: int, piece_end: int) -> list[tuple[int, int]] | None:
        key = (piece_start, piece_end)
        if key in memo:
            return memo[key]
        value = source_text[piece_start:piece_end]
        if value in _PLACES_BY_NAME:
            result = [(piece_start, piece_end)]
            memo[key] = result
            return result
        best: list[tuple[int, int]] | None = None
        for connector in re.finditer(r"、|→|⇒|->|及|与|和|或", value):
            left_end = piece_start + connector.start()
            right_start = piece_start + connector.end()
            if left_end <= piece_start or right_start >= piece_end:
                continue
            left = split(piece_start, left_end)
            right = split(right_start, piece_end)
            if left is None or right is None:
                continue
            combined = [*left, *right]
            if best is None or len(combined) > len(best):
                best = combined
        if best is None and _is_atomic_place_text(value):
            best = [(piece_start, piece_end)]
        memo[key] = best
        return best

    return split(start, end) or []


def _contains_non_atomic_choice(value: str) -> bool:
    return re.search(r"(?:或者|或是|还是|二选一|[/／])", value) is not None or (
        "或" in value and value not in _PLACES_BY_NAME
    )


def _planned_action_role_hint(
    source_text: str,
    action_start: int,
    name_start: int,
    name_end: int,
) -> ActivityRole | None:
    if _inside_quoted_text(source_text, action_start):
        return None
    clause = _clause_for_position(source_text, action_start, name_end)
    if re.search(r"(?:相比|比|不如)[^。！？；;]*(?:更|较|热门|有名|适合|值得)", clause):
        return None
    left = max(
        source_text.rfind(marker, 0, action_start)
        for marker in _CLAUSE_BOUNDARIES
    ) + 1
    prefix = source_text[left:name_start]
    for boundary in ("但是", "不过", "而是", "但"):
        position = prefix.rfind(boundary)
        if position >= 0:
            prefix = prefix[position + len(boundary) :]
            break
    blocking_cues = (
        *_EXCLUDED_CUES,
        *_OPTIONAL_CUES,
        *_PASS_THROUGH_CUES,
        *_REFERENCE_CUES,
        "预约说明",
        "预约流程",
        "不去",
        "不要去",
    )
    if any(cue in prefix for cue in blocking_cues):
        return None
    if prefix.rstrip().endswith(("不", "不要", "不准备", "不打算")):
        return None
    return ActivityRole.PLANNED


def _append_plan_capture(
    source_text: str,
    start: int,
    end: int,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
    candidates: list[_MentionCandidate],
    role_hint: ActivityRole | None = None,
    day_hint: int | None = None,
) -> None:
    trimmed = _trim_plan_capture(source_text, start, end)
    if trimmed is None:
        return
    start, end = trimmed
    if _contains_non_atomic_choice(source_text[start:end]):
        occupied.append((start, end))
        return
    pieces = _atomic_capture_pieces(source_text, start, end)
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
        candidates.append(
            _MentionCandidate(
                start=piece_start,
                end=piece_end,
                name=name,
                cities=frozenset(_candidate_cities(name)),
                role_hint=role_hint,
                day_hint=day_hint,
            )
        )


def _route_title_candidates(
    source_text: str,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
) -> tuple[list[_MentionCandidate], list[tuple[int, int]]]:
    candidates: list[_MentionCandidate] = []
    route_sections: list[tuple[int, int]] = []
    route_day = 0
    day_headings = _day_headings(source_text)
    pattern = re.compile(r"(?m)^[ \t]*[•*]\s*(?P<route>[^\n]+)")
    for match in pattern.finditer(source_text):
        route_start = match.start("route")
        route_end = match.end("route")
        route_text = source_text[route_start:route_end].strip()
        prefix = re.match(
            r"(?:第\s*[一二三四五六七八九十0-9]+\s*天\s*)?"
            r"(?:路线|行程)\s*[：:]\s*",
            route_text,
        )
        if prefix:
            route_start += prefix.end()
            route_text = source_text[route_start:route_end].strip()
            route_end = route_start + len(route_text)
        if (
            any(marker in route_text for marker in "，,。！？；;：:")
            or any(
                marker in route_text
                for marker in (
                    "早晨",
                    "上午",
                    "下午",
                    "海拔",
                    "时间",
                    "营地",
                    "距离",
                    "累计",
                    "小时",
                    "公里",
                    "旅途",
                )
            )
        ):
            continue
        token_matches = list(
            re.finditer(r"[A-Za-z\u4e00-\u9fff·]{2,20}", route_text)
        )
        if len(token_matches) < 2:
            continue
        between_values = [
            route_text[left.end() : right.start()]
            for left, right in zip(token_matches, token_matches[1:])
        ]
        if not all(re.fullmatch(r"(?:\s+|\s*(?:→|⇒|->)\s*)", item) for item in between_values):
            continue
        if len(token_matches) == 2 and not any(
            re.search(r"→|⇒|->", item) for item in between_values
        ):
            continue
        if not all(_is_atomic_place_text(item.group(0)) for item in token_matches):
            continue
        explicit_day = _day_for_position(match.end(), day_headings)
        route_day = explicit_day or route_day + 1
        route_sections.append((match.start(), route_day))
        for token in token_matches:
            _append_plan_capture(
                source_text,
                route_start + token.start(),
                route_start + token.end(),
                url_spans=url_spans,
                occupied=occupied,
                candidates=candidates,
                role_hint=ActivityRole.PLANNED,
                day_hint=route_day,
            )
    return candidates, route_sections


def _route_day_for_position(
    position: int,
    route_sections: list[tuple[int, int]],
) -> int | None:
    route_day = None
    for start, candidate in route_sections:
        if start > position:
            break
        route_day = candidate
    return route_day


def _natural_route_candidates(
    source_text: str,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
    route_sections: list[tuple[int, int]],
) -> list[_MentionCandidate]:
    candidates: list[_MentionCandidate] = []
    route_pair = re.compile(
        rf"(?:由|从)\s*(?P<origin>{_ATOMIC_ROLE_NAME_PATTERN}?)\s*"
        rf"(?P<travel_mode>乘(?:坐)?[A-Za-z\u4e00-\u9fff]{{0,8}}?|步行|驾车|走)\s*"
        rf"(?:前往|到达|到)\s*(?P<destination>{_ATOMIC_ROLE_NAME_PATTERN})"
    )
    for match in route_pair.finditer(source_text):
        day_hint = _route_day_for_position(match.start(), route_sections)
        for group in ("origin", "destination"):
            is_transfer_origin = group == "origin" and (
                match.group("travel_mode").startswith("乘")
                or match.group("travel_mode") == "驾车"
            )
            _append_plan_capture(
                source_text,
                match.start(group),
                match.end(group),
                url_spans=url_spans,
                occupied=occupied,
                candidates=candidates,
                role_hint=(
                    ActivityRole.PASS_THROUGH
                    if is_transfer_origin
                    else ActivityRole.PLANNED
                    if day_hint is not None
                    else None
                ),
                day_hint=None if is_transfer_origin else day_hint,
            )

    visit_destination = re.compile(
        rf"(?:到|前往)\s*(?P<name>{_ATOMIC_ROLE_NAME_PATTERN}?)\s*"
        rf"(?:游览|参观|打卡)"
    )
    for match in visit_destination.finditer(source_text):
        _append_plan_capture(
            source_text,
            match.start("name"),
            match.end("name"),
            url_spans=url_spans,
            occupied=occupied,
            candidates=candidates,
        )
    return candidates


def _elevation_route_candidates(
    source_text: str,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
    route_sections: list[tuple[int, int]],
) -> list[_MentionCandidate]:
    """Recover an explicit ordered hiking route from its elevation row.

    OCR engines commonly remove the visible spaces in a Chinese route title,
    while the following elevation row retains deterministic ``name(value)``
    separators.  The row is treated as a plan only when the immediately
    preceding context says that this is the current day's journey.  Standalone
    elevation descriptions remain references.
    """

    candidates: list[_MentionCandidate] = []
    headings = _day_headings(source_text)
    pattern = re.compile(
        r"(?:海拔(?:高度)?|海拔)\s*[：:]\s*(?P<body>[^\n。]+)"
    )
    for match in pattern.finditer(source_text):
        current_line_start = source_text.rfind("\n", 0, match.start()) + 1
        previous_line_end = max(0, current_line_start - 1)
        previous_line_start = source_text.rfind(
            "\n",
            0,
            previous_line_end,
        ) + 1
        context = source_text[previous_line_start : match.start()]
        if not (
            re.search(r"(?:这一天|第\s*[一二三四五六七八九十0-9]+\s*天)", context)
            and any(cue in context for cue in ("旅程", "旅途", "走到", "穿越"))
        ):
            continue
        explicit_day = _day_for_position(match.start(), headings)
        current_route_day = _route_day_for_position(match.start(), route_sections)
        prior_days = [
            day for start, day in route_sections if start <= match.start()
        ]
        day_hint = (
            explicit_day
            or current_route_day
            or (max(prior_days, default=0) + 1)
        )
        # The elevation row itself establishes the fallback route section.
        # Keeping the marker at this row prevents an earlier narrative sentence
        # from outranking the explicit ordered list when OCR collapsed the title.
        route_sections.append((match.start(), day_hint))
        body_start = match.start("body")
        for item in re.finditer(
            r"(?P<name>[A-Za-z\u4e00-\u9fff·]{2,20})\s*\([^()\n]{1,40}\)",
            match.group("body"),
        ):
            _append_plan_capture(
                source_text,
                body_start + item.start("name"),
                body_start + item.end("name"),
                url_spans=url_spans,
                occupied=occupied,
                candidates=candidates,
                role_hint=ActivityRole.PLANNED,
                day_hint=day_hint,
            )
    route_sections.sort()
    return candidates


def _planned_atomic_candidates(
    source_text: str,
    *,
    url_spans: list[tuple[int, int]],
    occupied: list[tuple[int, int]],
) -> list[_MentionCandidate]:
    candidates, route_sections = _route_title_candidates(
        source_text,
        url_spans=url_spans,
        occupied=occupied,
    )
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
            role_hint=_planned_action_role_hint(
                source_text,
                match.start(),
                match.start("names"),
                match.end("names"),
            ),
        )

    candidates.extend(
        _elevation_route_candidates(
            source_text,
            url_spans=url_spans,
            occupied=occupied,
            route_sections=route_sections,
        )
    )
    candidates.extend(
        _natural_route_candidates(
            source_text,
            url_spans=url_spans,
            occupied=occupied,
            route_sections=route_sections,
        )
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

        if (
            not _is_descriptive_reference_clause(segment)
            and not any(
                not (segment_end <= old_start or segment_start >= old_end)
                for old_start, old_end in occupied
            )
            and all(marker not in segment for marker in "，,；;")
        ):
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
    candidates: list[_MentionCandidate],
    headings: list[tuple[int, int]],
) -> tuple[str, DestinationBasis]:
    multi_city = _MULTI_DEEP_CITY_HEADER_RE.search(source_text)
    if multi_city:
        city_text = multi_city.group("cities")
        cities = sorted(
            (city for city in ("北京", "上海", "杭州") if city in city_text),
            key=city_text.index,
        )
        if len(cities) >= 2:
            return "、".join(cities), DestinationBasis.EXPLICIT
    basic_city = _BASIC_CITY_HEADER_RE.search(source_text)
    if basic_city:
        return basic_city.group("city").removesuffix("市"), DestinationBasis.EXPLICIT
    candidate_spans = [(item.start, item.end) for item in candidates]
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
        for candidate in candidates
        if (
            candidate.role_hint
            or _role_for_context(
                source_text,
                candidate.start,
                candidate.end,
                has_day=(
                    candidate.day_hint is not None
                    or _day_for_position(candidate.start, headings) is not None
                ),
            )
        )
        == ActivityRole.PLANNED
        for city in candidate.cities
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
        source_view = _semantic_source_view(source_text)
        semantic_text = source_view.text
        headings = _day_headings(semantic_text)
        url_spans = [
            (match.start(), match.end())
            for match in _URL_TOKEN_RE.finditer(semantic_text)
        ]
        occupied: list[tuple[int, int]] = []
        candidates = _planned_atomic_candidates(
            semantic_text,
            url_spans=url_spans,
            occupied=occupied,
        )
        candidates.extend(
            _explicit_role_candidates(
                semantic_text,
                url_spans=url_spans,
                occupied=occupied,
            )
        )
        for name in sorted(_PLACES_BY_NAME, key=len, reverse=True):
            cities = {item.city for item in _PLACES_BY_NAME[name]}
            for match in re.finditer(re.escape(name), semantic_text):
                span = (match.start(), match.end())
                if _inside_url(match.start(), url_spans):
                    continue
                if any(not (span[1] <= start or span[0] >= end) for start, end in occupied):
                    continue
                occupied.append(span)
                candidates.append(
                    _MentionCandidate(
                        start=span[0],
                        end=span[1],
                        name=name,
                        cities=frozenset(cities),
                    )
                )
        candidates.sort(key=lambda item: (item.start, item.end))

        destination, destination_basis = _destination(
            semantic_text,
            candidates,
            headings,
        )
        day_sequences: dict[int, int] = {}
        emitted: set[tuple[str, ActivityRole, int | None]] = set()
        mentions: list[ProposedMention] = []
        for candidate in candidates:
            explicit_day = candidate.day_hint or _day_for_position(
                candidate.start,
                headings,
            )
            role = candidate.role_hint or _role_for_context(
                semantic_text,
                candidate.start,
                candidate.end,
                has_day=explicit_day is not None,
            )
            if role is None:
                continue
            meta_description = _is_meta_activity_clause(
                _clause_for_position(
                    semantic_text,
                    candidate.start,
                    candidate.end,
                )
            )
            day_index = explicit_day if role == ActivityRole.PLANNED else None
            if role == ActivityRole.PLANNED and day_index is None:
                day_index = 1
            signature = (candidate.name, role, day_index)
            if signature in emitted:
                continue
            emitted.add(signature)
            sequence_index = day_sequences.get(day_index or 0, 0)
            day_sequences[day_index or 0] = sequence_index + 1
            facts = (
                _PLACES_BY_NAME.get(candidate.name, [])
                if not meta_description
                else []
            )
            category = (
                facts[0].category
                if facts and len({item.category for item in facts}) == 1
                else (
                    _atomic_category_hint(candidate.name)
                    if not meta_description
                    else None
                )
            )
            source_start, source_end = source_view.source_span(
                candidate.start,
                candidate.end,
            )
            mentions.append(
                ProposedMention(
                    mention_id=f"mention-{len(mentions) + 1}",
                    raw_text=source_text[source_start:source_end],
                    span_start=source_start,
                    span_end=source_end,
                    role=role,
                    day_index=day_index,
                    sequence_index=sequence_index,
                    atomic_place_name=(
                        None if meta_description else candidate.name
                    ),
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
