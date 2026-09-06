"""Live semantic extraction for the experience app.

The model owns meaning and order. This adapter validates source anchors and
structure; it never manufactures POIs or rewrites meaning with a local parser.
The legacy frozen adapter remains available for historical experiments.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from bisect import bisect_left
from pathlib import Path
from typing import Any, Literal

from openai import APIError, AsyncOpenAI
from pydantic import Field, ValidationError

from app.trip_understanding.errors import InferenceProviderUnavailableError
from app.trip_understanding.models import (
    ActivityRole, ActivityTiming, DestinationBasis, InferenceProposal,
    ProposedMention, StrictModel,
)
from app.trip_understanding.pipeline import DOMESTIC_CITY_NAMES, atomic_place_rejection_reason
from app.trip_understanding.timing_evidence import validated_timing


PROMPT_PATH = Path(__file__).with_name("experience_inference_prompt.md")
SEMANTIC_POLICY = "MODEL_MEANING_SOURCE_VALIDATED_V1"


class SemanticActivity(ActivityTiming):
    source_quote: str = Field(
        min_length=1, max_length=1000,
        description="能定位这一项的最短原文片段；有地点时优先只引用地点名，不复制整段说明。",
    )
    occurrence: int = Field(default=1, ge=1, le=160)
    place_name: str | None = Field(default=None, max_length=40)
    role: ActivityRole
    day_index: int | None = Field(default=None, ge=1, le=14)
    category: Literal["景点", "餐饮", "住宿", "交通节点", "地点"] = "地点"
    time_evidence: str | None = Field(default=None, max_length=500)
    city: str | None = Field(default=None, max_length=40)
    city_evidence: str | None = Field(default=None, max_length=500)


class SemanticDraft(StrictModel):
    destination: str = Field(min_length=1, max_length=40)
    day_labels: list[str | None] = Field(default_factory=list, max_length=14)
    activities: list[SemanticActivity] = Field(
        max_length=160,
        description="按执行顺序逐地点列出；同句并列的多个独立地点分别成项，二选一的两个地点都保留为OPTIONAL。",
    )
    unprocessed_quotes: list[str] = Field(default_factory=list, max_length=80)


class SourceAnchorValidationError(ValueError):
    """Only field locations and categories; source text never enters failure logs."""

    def __init__(self, issues: list[dict[str, object]]) -> None:
        self.issues = issues
        self.category = str(issues[0]["category"])
        super().__init__(self.category)


def _markdown_visible(source: str) -> tuple[str, list[int]]:
    """Remove paired inline decoration with a reversible character index.

    No word, punctuation, whitespace, link destination or Unicode character is
    corrected. A match can differ only by balanced Markdown delimiters. Place
    names must still occur literally inside the resulting original source span.
    """
    hidden: set[int] = set()
    # Strong/emphasis and inline code are presentation, not itinerary meaning.
    # Longest delimiters first handles ***text*** without consuming nested runs.
    for delimiter in ("***", "___", "**", "__", "`", "*", "_"):
        escaped = re.escape(delimiter)
        boundary = r"\\\w" if delimiter[0] == "_" else "\\" + re.escape(delimiter[0])
        pattern = re.compile(
            rf"(?<![{boundary}])(?P<open>{escaped})(?!{re.escape(delimiter[0])})(?=\S)"
            rf"(?P<body>[^\r\n]+?)(?<=\S)(?<!\\)(?P<close>{escaped})(?!{re.escape(delimiter[0])})"
        )
        for match in pattern.finditer(source):
            opened = range(*match.span("open"))
            closed = range(*match.span("close"))
            if any(index in hidden for index in (*opened, *closed)):
                continue
            hidden.update(opened)
            hidden.update(closed)
    indices = [index for index in range(len(source)) if index not in hidden]
    return "".join(source[index] for index in indices), indices


class SourceAnchorIndex:
    def __init__(self, source: str) -> None:
        self.source = source
        self.visible, self.indices = _markdown_visible(source)

    def locate(self, quote: str, occurrence: int = 1) -> tuple[int, int]:
        visible_quote, _indices = _markdown_visible(quote)
        if not visible_quote:
            raise ValueError("SOURCE_QUOTE_NOT_FOUND")
        start = -1
        for _ in range(occurrence):
            start = self.visible.find(visible_quote, start + 1)
            if start < 0:
                raise ValueError("SOURCE_QUOTE_NOT_FOUND")
        return self.indices[start], self.indices[start + len(visible_quote) - 1] + 1


def _source_occurrence(source: str, quote: str, occurrence: int) -> int:
    return SourceAnchorIndex(source).locate(quote, occurrence)[0]


def _omits_attached_place_qualifier(anchors: SourceAnchorIndex, place_end: int) -> bool:
    """Protect literal entrance/branch labels, without absorbing later actions.

    The shortest quote may end *inside* a qualified name. Check the original
    visible text after that endpoint too, including paired Markdown decoration.
    This only requests a semantic repair; it never invents or expands a POI.
    """
    tail = anchors.visible[bisect_left(anchors.indices, place_end):][:48]
    # Spaces/separators describe a new phrase, not an attached name component.
    # Parenthesized branch names are still literal parts of the same label.
    bracket = re.match(r"^[（(]([^（）()\n]{1,24})[）)]", tail)
    if bracket:
        label = bracket[1]
    else:
        label = re.split(r"[\s，,。；;：:、→/／（）()]|出来|出发|离开|之后|以后|随后|然后|接着|再去|再到|前往|参观|游览|集合|进入|游玩|打卡|入住|用餐|步行|返回|吃饭|喝咖啡", tail, maxsplit=1)[0]
    if re.match(r"^(?:的|里面|内有|外面|附近|旁边|是|有|包含|可以|需要|还|并|与|和|以及|到|去)", label):
        return False
    # A following action ("北门见", "分店吃饭") does not make an attached
    # qualifier disappear. Bracketed prose, however, must be a label in full.
    match = re.fullmatch if bracket else re.match
    return bool(match(
        r"(?:[东南西北]{1,2}(?:门|馆|院|区)|[总主新老本]馆|"
        r"[一二三四五六七八九十\d]+号(?:门|馆)|"
        r"[A-Za-z0-9\u4e00-\u9fff·]{0,12}(?:分馆|分院|分店|校区|馆区|院区)|"
        r"[A-Za-z0-9\u4e00-\u9fff·]{1,12}(?:馆|店))",
        label,
    ))


def _validation_issues(exc: ValueError) -> list[dict[str, object]]:
    if isinstance(exc, SourceAnchorValidationError):
        return exc.issues[:20]
    if isinstance(exc, ValidationError):
        known_fields = set(SemanticDraft.model_fields) | set(SemanticActivity.model_fields)
        issues = []
        for error in exc.errors(include_input=False, include_context=False, include_url=False)[:20]:
            field = ""
            for segment in error["loc"]:
                if isinstance(segment, int):
                    field += f"[{segment}]"
                else:
                    name = segment if segment in known_fields else "unknown_field"
                    field += ("." if field else "") + name
            issues.append({"field": field or "document", "category": str(error["type"])})
        return issues
    return [{"field": "document", "category": "OUTPUT_TRUNCATED"}]


def _explicit_markdown_place_groups(source: str) -> tuple[tuple[str, ...], ...]:
    """Find short, explicitly grouped Markdown labels without parsing prose."""

    groups: list[tuple[str, ...]] = []
    for match in re.finditer(r"(?:\*\*|__)(?P<body>[^\r\n]{3,80}?)(?:\*\*|__)", source):
        body = match.group("body").strip()
        lead_in = source[max(0, match.start() - 32):match.start()]
        if re.search(r"(?:不想|可以|可选|备选|推荐|例如|比如|隔壁)[^。！？；\n]{0,24}$", lead_in):
            continue
        if not re.search(r"\+|、|，|,|/|／", body) or re.search(r"[（）()：:；;。！？]", body):
            continue
        parts = tuple(part.strip() for part in re.split(r"\s*(?:\+|、|，|,|/|／)\s*", body))
        if 2 <= len(parts) <= 8 and all(
            part and len(part) <= 40 and atomic_place_rejection_reason(part) is None
            for part in parts
        ):
            groups.append(parts)
    return tuple(groups)


def _explicit_plain_place_groups(source: str, atomic_places: set[str]) -> tuple[tuple[str, ...], ...]:
    """Check short literal place lists, not descriptions or arbitrary prose."""
    # A lexical list cannot determine which historical plan survives an edit.
    # The semantic draft may legitimately omit cancelled places altogether.
    # Leave amended narratives to role/source validation instead of requiring
    # every place from the obsolete list to reappear in the final draft.
    if re.search(r"取消|更正|改到|改为|改成|恢复", source):
        return ()
    suffix = r"(?:博物院|博物馆|公园|景区|广场|古镇|步行街|寺|庙|湖|街)"
    atom = rf"[\u4e00-\u9fffA-Za-z0-9]{{1,18}}{suffix}"

    def strip_supported_prefix(value: str) -> str:
        first = re.sub(r"^(?:原计划|原安排|最初计划)\s*", "", value.strip())
        first = re.sub(
            r"^(?:第\s*(?:\d{1,3}|[一二两三四五六七八九十]{1,3})\s*天|"
            r"(?:Day|D)\s*\d{1,3}|\d{1,2}月\d{1,2}日)\s*[:：]?\s*", "", first, flags=re.I,
        )
        return re.sub(r"^(?:(?:先|再|计划|准备|打算)\s*)?(?:去|到|前往|参观|游览)\s*", "", first)

    groups = []
    for match in re.finditer(rf"(?P<body>{atom}(?:\s*[、+→]\s*{atom}){{1,7}})(?=[。；;！\n]|$)", source):
        before = re.split(r"[。；;！\n]", source[:match.start()])[-1]
        if re.search(r"介绍|说明|海拔|例如|比如|推荐|不去|取消|参考|附近|位于", before):
            continue
        # Do not recover a list by starting inside an arbitrary narrative.
        # Only a clause boundary or an already supported explicit prefix is
        # reliable enough for this supplementary completeness check.
        lead = re.split(r"[，,：:]", before)[-1]
        if strip_supported_prefix(lead).strip():
            continue
        parts = tuple(re.split(r"\s*[、+→]\s*", match["body"]))
        parts = (strip_supported_prefix(parts[0]), *parts[1:])
        # A source-bound atom surrounded by unrecognised text is a narrative
        # phrase, not an additional POI the model must emit. Skip this group
        # instead of growing an open-ended action-prefix dictionary.
        if any(name != part and name in part for part in parts for name in atomic_places):
            continue
        if all(atomic_place_rejection_reason(part) is None for part in parts):
            groups.append(parts)
    return tuple(groups)


def _validated_city(source: str, anchors: SourceAnchorIndex, item: SemanticActivity,
                    start: int, end: int, place_spans: list[tuple[int, int]]) -> tuple[str | None, str | None, bool]:
    if not item.city:
        return None, None, False
    city = item.city.strip().removesuffix("市")
    evidence = item.city_evidence or ""
    visible, _ = _markdown_visible(evidence)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,10}", city) or city not in visible:
        return None, evidence, True
    # A locality mentioned inside a POI (e.g. 广州北京路) is not city evidence.
    if city not in DOMESTIC_CITY_NAMES and f"{city}市" not in visible:
        return None, evidence, True
    occurrences = []
    for occurrence in range(1, 161):
        try:
            occurrences.append(anchors.locate(evidence, occurrence))
        except ValueError:
            break
    def city_offsets(name: str, left: int, right: int) -> list[int]:
        return [left + match.start() for match in re.finditer(re.escape(name), source[left:right])
                if not any(begin <= left + match.start() < finish for begin, finish in place_spans)
                and not re.match(r"(?:路|街|大学|博物馆|饭店|酒店)", source[left + match.end():])]

    for left, right in occurrences:
        other_cities = [name for name in DOMESTIC_CITY_NAMES if name != city and city_offsets(name, left, right)]
        if other_cities:
            continue
        if not city_offsets(city, left, right):
            continue
        if left <= start and end <= right:
            return city, evidence, False
        if right <= start and start - right <= 1500:
            gap = source[right:start]
            if re.search(r"第[^。\n]{1,5}天|(?:Day|D)\s*\d+|\d{1,2}月\d{1,2}日", gap, re.I):
                continue
            if any(name != city and city_offsets(name, right, start) for name in DOMESTIC_CITY_NAMES):
                continue
            # Only an isolated city heading or explicit day/destination framing
            # may lend its city to later activities.
            line_left = source.rfind("\n", 0, left) + 1
            line_right = source.find("\n", right)
            line = source[line_left:line_right if line_right >= 0 else len(source)].strip()
            if line.strip(" ：:") == evidence.strip(" ：:") or re.search(
                r"(?:第[^。\n]{1,5}天|(?:Day|D)\s*\d+|目的地|城市)", visible, re.I,
            ):
                return city, evidence, False
    return None, evidence, True


def _table_timing_evidence(source: str, start: int, left: int, right: int) -> str | None:
    """Bind timing to this row's labelled columns, never another row's values."""
    row_left = source.rfind("\n", 0, start) + 1
    row_right = source.find("\n", start)
    row = source[row_left:row_right if row_right >= 0 else len(source)]
    if "|" not in row or row.strip(" |\r\t") != source[left:right].strip(" |\r\t"):
        return None

    def cells(line: str) -> list[str]:
        return [_markdown_visible(cell.strip())[0] for cell in line.strip().strip("|").split("|")]

    values = cells(row)
    labels = {
        "时间": "时间", "时刻": "时间", "到达时间": "时间", "到访时间": "时间", "开始时间": "时间",
        "结束时间": "结束", "离开时间": "离开",
        "停留": "停留", "停留时间": "停留", "停留时长": "停留",
        "游览时长": "游览", "参观时长": "参观", "游玩时长": "游玩",
    }
    for line in reversed(source[:row_left].splitlines()):
        if "|" not in line:
            break
        header = cells(line)
        if len(header) != len(values):
            break
        if not any(cell in {"地点", "到访地点", "景点", "活动", "活动地点"} for cell in header):
            continue
        # Transport columns cannot contribute even when their cell contains a
        # visit word. Column semantics take precedence over free text patterns.
        return "；".join(f"{labels[label]}：{value}" for label, value in zip(header, values, strict=True) if label in labels)
    return None


def _local_timing_evidence(source: str, anchors: SourceAnchorIndex, item: SemanticActivity,
                           draft: SemanticDraft, located: list[tuple[int, int]],
                           start: int, end: int) -> str:
    if not item.time_evidence:
        return ""
    evidence_span = None
    for occurrence in range(1, 161):
        try:
            left, right = anchors.locate(item.time_evidence, occurrence)
        except ValueError:
            break
        if left <= start and end <= right:
            evidence_span = (left, right)
            break
    if evidence_span is None:
        return ""
    left = max(evidence_span[0], max(source.rfind(mark, 0, start) for mark in ("。", "；", ";", "\n")) + 1)
    stops = [source.find(mark, end) for mark in ("。", "；", ";", "\n")]
    right = min(evidence_span[1], min((position for position in stops if position >= 0), default=len(source)))
    for other, (quote_start, quote_end) in zip(draft.activities, located, strict=True):
        if not other.place_name or other.place_name not in source[quote_start:quote_end]:
            continue
        other_start = quote_start + source[quote_start:quote_end].index(other.place_name)
        other_end = other_start + len(other.place_name)
        if (other_start, other_end) == (start, end) or other_end <= left or other_start >= right:
            continue
        if other_end <= start:
            separators = [position for position in range(other_end, start) if source[position] in "，,。；;\n"]
            if not separators:
                return ""
            left = max(left, separators[-1] + 1)
        elif other_start >= end:
            separators = [position for position in range(end, other_start) if source[position] in "，,。；;\n"]
            if not separators:
                return ""
            right = min(right, separators[0])
    table_evidence = _table_timing_evidence(source, start, left, right)
    return table_evidence if table_evidence is not None else _markdown_visible(source[left:right])[0]


def _explicit_day_count(source: str) -> int:
    number = r"(?:\d{1,3}|[一二两三四五六七八九十]{1,3})"
    patterns = (
        rf"第\s*({number})\s*天",
        r"(?:Day|D)\s*(\d{1,3})(?!\d)",
        rf"({number})\s*(?:日|天)(?:游|行程|旅行|攻略)",
        rf"(?:^|[。\n])\s*(?:{'|'.join(DOMESTIC_CITY_NAMES)})\s*({number})\s*(?:日|天)(?=[，,。；;\s]|$)",
    )
    digits = {char: value for value, char in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    counts = []
    for pattern in patterns:
        for match in re.finditer(pattern, source, re.I):
            raw = match.group(1)
            if raw.isdigit():
                counts.append(int(raw))
            elif "十" in raw and raw.count("十") == 1:
                left, right = raw.split("十")
                counts.append(digits.get(left, 1) * 10 + digits.get(right, 0))
            elif raw in digits:
                counts.append(digits[raw])
    return max(counts, default=0)


def _unambiguous_literal_place_day(source: str, place: str | None) -> int | None:
    """Last-resort grounding for a missing field, never a change to a supplied day.

    Every literal occurrence must have the same preceding explicit day label.
    Moved/repeated places, references in other days and unscoped labels refuse
    this recovery. Calendar dates and unnamed activities stay with semantics.
    """
    if not place:
        return None
    # Literal occurrences cannot establish the final day after a pronoun-based
    # move or a whole-schedule swap. Leave those decisions to semantic repair.
    if re.search(r"对调|交换|顺延", source) or (
        re.search(r"前者|后者|它|该站|该地点|上述|上面|这些|这两|那两|两者", source)
        and re.search(r"移到|移至|改到|改为|改期|调整|推迟|提前", source)
    ):
        return None
    uncertain_day = (
        r"未定日期|日期未定|日期待定|哪一天|择日|其他天|另一天|某天|每天|改期|"
        r"次日|翌日|明天|后天|昨天|前天|今天|今晚|次晚|后一天|前一天|"
        r"第\s*[一二两三四五六七八九十\d]+\s*[晚日夜]|(?:周|星期)[一二三四五六日天]|"
        r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]|\d{1,4}\s*[-/.]\s*\d{1,2}"
    )
    days = []
    for match in re.finditer(re.escape(place), source):
        headings = list(re.finditer(r"第\s*(?:\d{1,2}|[一二两三四五六七八九十]{1,3})\s*天|(?<![A-Za-z0-9])(?:Day|D)\s*\d{1,2}(?![A-Za-z0-9])", source[:match.start()], re.I))
        if not headings:
            return None
        heading = headings[-1]
        # A day token inside a URL, order ID or ordinary sentence is not a
        # heading. Recovery only accepts a short, explicit clause introduction.
        intro = re.split(r"[\n。；;，,：:！？!?|→]", source[:heading.start()])[-1].strip(" #*_`\t")
        if not re.fullmatch(r"(?:(?:最终|最后|原定|计划|更正后|更新后)\s*)?(?:(?:北京|上海|杭州)市?\s*)?", intro):
            return None
        # Do not extend a heading through an explicitly unscoped reference or
        # into a date range that cannot bind one day to this particular noun.
        gap = source[heading.end():match.start()]
        if re.search(uncertain_day, gap):
            return None
        day = _explicit_day_count(heading[0])
        if not 1 <= day <= 14:
            return None
        tail = re.split(r"[。；;，,\n]", source[match.end():], maxsplit=1)[0]
        trailing_day = _explicit_day_count(tail)
        if (trailing_day and trailing_day != day) or re.search(uncertain_day, tail):
            return None
        days.append(day)
    return days[0] if days and len(set(days)) == 1 else None


def proposal_from_draft(source: str, draft: SemanticDraft) -> InferenceProposal:
    anchors = SourceAnchorIndex(source)
    explicit_days = _explicit_day_count(anchors.visible)
    issues: list[dict[str, object]] = []
    located: list[tuple[int, int]] = []
    proposed_atomic = {
        item.place_name.strip()
        for item in draft.activities
        if item.place_name and item.place_name.strip()
        and not re.search(r"\+|、|，|,|/|／", item.place_name)
    }
    groups = _explicit_markdown_place_groups(source) + _explicit_plain_place_groups(anchors.visible, proposed_atomic)
    for group_index, group in enumerate(groups):
        if not set(group).issubset(proposed_atomic):
            issues.append({
                "field": f"activities.parallel_group[{group_index}]",
                "category": "MISSING_EXPLICIT_PARALLEL_PLACE",
            })
    for index, quote in enumerate(draft.unprocessed_quotes):
        try:
            anchors.locate(quote)
        except ValueError:
            issues.append({"field": f"unprocessed_quotes[{index}]", "category": "SOURCE_QUOTE_NOT_FOUND"})
    for index, item in enumerate(draft.activities):
        if item.role == ActivityRole.PLANNED and item.day_index is None and explicit_days > 1:
            issues.append({"field": f"activities[{index}].day_index", "category": "MISSING_EXPLICIT_DAY"})
        try:
            start, end = anchors.locate(item.source_quote, item.occurrence)
            located.append((start, end))
        except ValueError:
            issues.append({"field": f"activities[{index}].source_quote", "category": "SOURCE_QUOTE_NOT_FOUND"})
            located.append((0, 0))
        else:
            place = item.place_name.strip() if item.place_name else None
            if place and place not in source[start:end]:
                issues.append({"field": f"activities[{index}].place_name", "category": "PLACE_NOT_IN_SOURCE_QUOTE"})
            elif place and item.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL}:
                place_end = start + source[start:end].index(place) + len(place)
                if _omits_attached_place_qualifier(anchors, place_end):
                    issues.append({"field": f"activities[{index}].place_name", "category": "PLACE_QUALIFIER_OMITTED"})
            # A planned sightseeing/location item containing an explicit list
            # must be returned one atomic place per activity. Rejecting the
            # bundled draft asks the model's bounded repair pass to preserve
            # every source-grounded place; the adapter still never guesses or
            # manufactures a POI from prose.
            visible_quote, _ = _markdown_visible(item.source_quote)
            atomic_siblings = {
                (sibling.place_name or "").strip()
                for sibling in draft.activities
                if sibling.source_quote == item.source_quote
                and sibling.occurrence == item.occurrence
                and sibling.day_index == item.day_index
                and sibling.role == item.role
                and (sibling.place_name or "").strip()
                and not re.search(r"\+|、|，|,|/|／", sibling.place_name or "")
            }
            if (
                item.role in {ActivityRole.PLANNED, ActivityRole.OPTIONAL}
                and item.category in {"景点", "地点", "交通节点", "住宿"}
                and re.search(r"\S\s*(?:\+|、|，|,|/|／)\s*\S", visible_quote)
                and (not place or re.search(r"\+|、|，|,|/|／", place))
                and len(atomic_siblings) < 2
            ):
                issues.append({
                    "field": f"activities[{index}].place_name",
                    "category": "NON_ATOMIC_PLACE_LIST",
                })
        has_timing = any(getattr(item, key) is not None for key in (
            "start_time", "end_time", "visit_duration_minutes",
        ))
        if has_timing or item.locked or item.fixed_commitment:
            try:
                if not item.time_evidence:
                    raise ValueError
                anchors.locate(item.time_evidence)
            except ValueError:
                issues.append({"field": f"activities[{index}].time_evidence", "category": (
                    "TIME_EVIDENCE_NOT_IN_SOURCE" if has_timing else "COMMITMENT_EVIDENCE_NOT_IN_SOURCE"
                )})
    if issues:
        raise SourceAnchorValidationError(issues)
    place_spans = [
        (start + source[start:end].index(item.place_name), start + source[start:end].index(item.place_name) + len(item.place_name))
        for item, (start, end) in zip(draft.activities, located, strict=True)
        if item.place_name and item.place_name in source[start:end]
    ]
    mentions: list[ProposedMention] = []
    seen: set[tuple[int, int, ActivityRole, int | None]] = set()
    sequences: dict[int, int] = {}
    unprocessed = len(draft.unprocessed_quotes)
    seen_places: set[tuple[str, int | None]] = set()
    for item, (start, end) in zip(draft.activities, located, strict=True):
        place = item.place_name.strip() if item.place_name else None
        if place is not None:
            relative = source[start:end].index(place)
            start += relative
            end = start + len(place)
            if atomic_place_rejection_reason(place) is not None:
                # Keep the intended arrangement pending without presenting a
                # description/URL as a real place or sending it to POI search.
                place = None
                unprocessed += 1
        day = item.day_index
        if item.role == ActivityRole.PLANNED and day is None:
            day = 1
            unprocessed += 1
        if place and item.role == ActivityRole.PLANNED and (place, day) in seen_places:
            line_start = max(source.rfind(mark, 0, start) for mark in ("\n", "。", "；", ";")) + 1
            prefix = source[line_start:start]
            if re.match(r"\s*(?:[-•●]\s*)?(?:说明|介绍|海拔高度|海拔表)\s*[:：]", prefix):
                # A labelled descriptive repeat is retained internally as a
                # reference; actual repeat visits elsewhere remain untouched.
                item = item.model_copy(update={"role": ActivityRole.REFERENCE})
                unprocessed += 1
        signature = (start, end, item.role, day)
        if signature in seen:
            continue
        seen.add(signature)
        if place and item.role == ActivityRole.PLANNED:
            seen_places.add((place, day))
        timing, timing_removed = validated_timing(
            item.model_dump(include=set(ActivityTiming.model_fields)),
            _local_timing_evidence(source, anchors, item, draft, located, start, end),
        )
        city, city_evidence, city_removed = _validated_city(source, anchors, item, start, end, place_spans)
        unprocessed += int(timing_removed) + int(city_removed)
        group = day or 0
        sequence = sequences.get(group, 0)
        sequences[group] = sequence + 1
        start_time, end_time = timing.get("start_time"), timing.get("end_time")
        hint = f"{start_time}–{end_time}" if start_time and end_time else start_time
        mentions.append(ProposedMention(
            mention_id=f"activity-{len(mentions) + 1}",
            raw_text=source[start:end], span_start=start, span_end=end,
            role=item.role, day_index=day, sequence_index=sequence,
            atomic_place_name=place, category_hint=item.category,
            time_hint=hint, city_hint=city, city_evidence=city_evidence, **timing,
        ))
    labels: dict[int, str] = {}
    for index, label in enumerate(draft.day_labels, 1):
        if label and label in source and label.strip() not in labels.values() and re.fullmatch(r"[\d年月日号./\-一二三四五六七八九十星期周\s]+", label):
            labels[index] = label.strip()
        elif label:
            unprocessed += 1
    supported_days = max(_explicit_day_count(anchors.visible), max(labels, default=0),
                         max((mention.day_index or 0 for mention in mentions), default=0), 1)
    if len(draft.day_labels) > supported_days or supported_days > 14:
        unprocessed += 1
    return InferenceProposal(
        source_hash=hashlib.sha256(source.encode()).hexdigest(),
        destination_name=draft.destination,
        destination_basis=(DestinationBasis.EXPLICIT if draft.destination in source
                           else DestinationBasis.SOFT_ASSUMPTION),
        day_labels=labels, day_count=min(supported_days, 14), unprocessed_count=unprocessed,
        mentions=mentions, binding={"semantic_policy": SEMANTIC_POLICY},
    )


class ExperienceQwenProvider:
    def __init__(
        self, *, api_key: str, base_url: str, model: str,
        deadline_seconds: float = 30, max_output_tokens: int = 4096,
        input_cny_per_million: float | None = None,
        output_cny_per_million: float | None = None,
        client: Any | None = None,
    ) -> None:
        if not api_key or not model or not base_url.startswith("https://"):
            raise ValueError("Live inference requires configured HTTPS credentials and model")
        if deadline_seconds <= 0 or max_output_tokens < 256:
            raise ValueError("Invalid inference budget")
        self.model = model
        self.deadline_seconds = deadline_seconds
        self.max_output_tokens = max_output_tokens
        self.rates = (input_cny_per_million, output_cny_per_million)
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8")
        self.schema = SemanticDraft.model_json_schema()
        self._owned = client is None
        self.client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=deadline_seconds, max_retries=0,
        )
        self._slots = asyncio.Semaphore(1)

    async def aclose(self) -> None:
        if self._owned:
            await self.client.close()
            self._owned = False

    async def propose(self, source_text: str) -> InferenceProposal:
        # The deadline measures a Provider run, excluding queue backpressure.
        async with self._slots:
            return await self._propose(source_text)

    async def _propose(self, source_text: str) -> InferenceProposal:
        started = time.perf_counter()
        calls: list[dict[str, object]] = []
        messages = [
            {"role": "system", "content": self.prompt + "\nJSON Schema:\n" + json.dumps(self.schema, ensure_ascii=False)},
            {"role": "user", "content": source_text},
        ]
        failure = "INVALID_STRUCTURED_OUTPUT"
        proposal: InferenceProposal | None = None
        degraded_timing = 0
        grounded_days = 0
        try:
            async with asyncio.timeout(self.deadline_seconds):
                for attempt in range(2):
                    call: dict[str, object] = {"attempt": attempt + 1, "input_tokens": None, "output_tokens": None, "outcome": "UNKNOWN"}
                    calls.append(call)
                    call_started = time.perf_counter()
                    try:
                        response = await self.client.chat.completions.create(
                            model=self.model, messages=messages, temperature=0.1,
                            max_tokens=self.max_output_tokens,
                            response_format={"type": "json_object"},
                            extra_body={"enable_thinking": False},
                        )
                    finally:
                        call["latency_ms"] = round((time.perf_counter() - call_started) * 1000, 2)
                    usage = getattr(response, "usage", None)
                    call["input_tokens"] = getattr(usage, "prompt_tokens", None)
                    call["output_tokens"] = getattr(usage, "completion_tokens", None)
                    call["reported_model"] = getattr(response, "model", None)
                    content = response.choices[0].message.content or ""
                    call["response_sha256"] = hashlib.sha256(content.encode()).hexdigest()
                    try:
                        if getattr(response.choices[0], "finish_reason", None) == "length":
                            raise ValueError("OUTPUT_TRUNCATED")
                        draft = SemanticDraft.model_validate_json(content)
                        proposal = proposal_from_draft(source_text, draft)
                    except (ValueError, ValidationError) as exc:
                        failure = "INVALID_STRUCTURED_OUTPUT" if isinstance(exc, ValidationError) else str(exc)
                        call["outcome"] = failure
                        call["validation_errors"] = _validation_issues(exc)
                        if attempt == 1 and isinstance(exc, SourceAnchorValidationError) and exc.issues and all(
                            issue["category"] == "MISSING_EXPLICIT_DAY" for issue in exc.issues
                        ):
                            affected = {int(re.fullmatch(r"activities\[(\d+)\]\.day_index", issue["field"])[1])
                                        for issue in exc.issues}
                            assigned = {index: _unambiguous_literal_place_day(source_text, draft.activities[index].place_name)
                                        for index in affected}
                            if all(day is not None for day in assigned.values()):
                                cleaned = draft.model_copy(update={"activities": [
                                    item.model_copy(update={"day_index": assigned[index]}) if index in assigned else item
                                    for index, item in enumerate(draft.activities)
                                ]})
                                proposal = proposal_from_draft(source_text, cleaned)
                                grounded_days = len(affected)
                                break
                        if attempt == 1 and isinstance(exc, SourceAnchorValidationError) and exc.issues and all(
                            issue["category"] in {"TIME_EVIDENCE_NOT_IN_SOURCE", "COMMITMENT_EVIDENCE_NOT_IN_SOURCE"}
                            for issue in exc.issues
                        ):
                            # Do not lose correctly source-bound places because a
                            # second model answer still invents timing/booking evidence.
                            # Keep the failures recorded and return explicitly partial
                            # cards with only those unsupported fields removed.
                            affected = {int(re.fullmatch(r"activities\[(\d+)\]\.time_evidence", issue["field"])[1])
                                        for issue in exc.issues}
                            cleaned = draft.model_copy(update={"activities": [
                                item.model_copy(update={"start_time": None, "end_time": None,
                                    "visit_duration_minutes": None, "timing_source": "UNSPECIFIED",
                                    "locked": False, "fixed_commitment": False, "time_evidence": None})
                                if index in affected else item
                                for index, item in enumerate(draft.activities)
                            ]})
                            proposal = proposal_from_draft(source_text, cleaned)
                            degraded_timing = len(affected)
                            proposal = proposal.model_copy(update={"unprocessed_count": proposal.unprocessed_count + degraded_timing})
                            break
                        if attempt == 0:
                            messages.extend([
                                {"role": "assistant", "content": content},
                                {"role": "user", "content": (
                                    "只修复以下字段，保留其他已正确整理的活动、顺序和角色，不要为绕过错误删除活动。"
                                    "source_quote 优先缩短为原文中该地点的逐字名称；occurrence 按去掉 Markdown 装饰后的可见片段计数。"
                                    "place_name 仍必须逐字出现在对应原文范围内，不得改写、补全或模糊猜测。"
                                    "PLACE_QUALIFIER_OMITTED 表示截掉了紧邻地点的北门、东馆、分馆或分店等限定；"
                                    "按原文保留完整限定名称，不能把后面的出来、再去等动作并入名称。"
                                    "MISSING_EXPLICIT_DAY 表示多日行程缺少本项日期归属；依据原文最终安排填写day_index，"
                                    "不要把第二天的地点默认放进第一天，也不要按更正段落出现的位置重新分日。"
                                    "NON_ATOMIC_PLACE_LIST 表示把多个地点压成了一项：请按原文顺序拆成多个活动，"
                                    "每项 source_quote 和 place_name 都使用该地点的逐字名称；二选一分别标 OPTIONAL。"
                                    "MISSING_EXPLICIT_PARALLEL_PLACE 表示 Markdown 强调的并列地点仍有遗漏；"
                                    "重新逐项核对所有加粗并列组，每个地点必须各有一项，不能只留第一项。"
                                    "时间没有原文依据就清除时间字段并把真实原文片段放入 unprocessed_quotes。"
                                    "字段错误（从0开始）：" + json.dumps(call["validation_errors"], ensure_ascii=False)
                                    + "。只返回修正后的完整 JSON，不要补造原文信息。"
                                )},
                            ])
                        continue
                    call["outcome"] = "SUCCESS"
                    break
        except TimeoutError:
            failure = "DEADLINE_EXCEEDED"
            if calls:
                calls[-1]["outcome"] = failure
        except APIError:
            failure = "PROVIDER_UNAVAILABLE"
            if calls:
                calls[-1]["outcome"] = failure
        known_usage = all(isinstance(c.get("input_tokens"), int) and isinstance(c.get("output_tokens"), int) for c in calls)
        input_tokens = sum(int(c["input_tokens"]) for c in calls) if known_usage else None
        output_tokens = sum(int(c["output_tokens"]) for c in calls) if known_usage else None
        cost = None
        if known_usage and all(rate is not None for rate in self.rates):
            cost = round((input_tokens * self.rates[0] + output_tokens * self.rates[1]) / 1_000_000, 8)
        binding = {
            "provider": "QWEN", "model": self.model, "semantic_policy": SEMANTIC_POLICY,
            "prompt_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
            "schema_sha256": hashlib.sha256(json.dumps(self.schema, sort_keys=True).encode()).hexdigest(),
            "deadline_ms": round(self.deadline_seconds * 1000), "max_output_tokens": self.max_output_tokens,
            "external_calls": len(calls), "repair_call_count": max(0, len(calls) - 1),
            "fallback_used": bool(degraded_timing or grounded_days), "degraded_timing_activities": degraded_timing,
            "source_grounded_day_activities": grounded_days,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "estimated_cost_cny": cost, "calls": calls,
            "outcome": ("PARTIAL_RESULT" if degraded_timing else "SUCCESS") if proposal is not None else failure,
        }
        if proposal is None:
            raise InferenceProviderUnavailableError(
                failure, provider_binding=binding, external_call_count=len(calls),
            ) from None
        return proposal.model_copy(update={"binding": binding})
