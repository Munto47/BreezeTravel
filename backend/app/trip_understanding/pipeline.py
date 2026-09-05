from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
from collections.abc import Sequence
from typing import Protocol
from uuid import uuid4
from app.trip_understanding.timing import timing_values

from app.trip_understanding.errors import (
    InferenceProviderUnavailableError,
    PlaceProviderUnavailableError,
)
from app.trip_understanding.models import (
    ActivityCardView,
    ActivityRole,
    AssumptionChipView,
    CompiledActivity,
    DestinationBasis,
    InferenceProposal,
    MapReadinessView,
    PipelineOutput,
    PlaceResolutionOutcome,
    ResolutionStatus,
    ResolvedActivity,
    ResolvedPlace,
    SourceClaimRecord,
    StaySuggestionView,
    TripDayView,
    UserFacingTripResult,
)


URL_RE = re.compile(r"https?://", re.IGNORECASE)
SENTENCE_MARKERS = set("。！？；\n")
ATOMIC_PLACE_RE = re.compile(r"[A-Za-z0-9\u4e00-\u9fff·（）()—_-]+")
GENERIC_ACTIVITY_RE = re.compile(
    r"(?:"
    r"(?:吃|用|享用)?(?:早|午|晚)(?:饭|餐)|"
    r"(?:自由|自行)活动|"
    r"(?:在)?(?:酒店|宾馆|民宿|住处)?休息|"
    r"(?:随便)?看看?风景|"
    r"用餐|就餐|吃饭"
    r")"
)
DINING_CONTEXT_RE = re.compile(
    r"(?:早餐|早饭|午餐|午饭|晚餐|晚饭|用餐|就餐|吃饭|吃)"
    r"\s*(?:安排|选择|打算|准备)?\s*(?:去|到|在)?\s*$"
)
FORBIDDEN_PLACE_MARKERS = (
    "预约",
    "说明",
    "网址",
    "链接",
    "电话",
    "导航",
    "路线",
    "流程",
    "确认",
    "分钟",
)
DEEP_CITIES = ("北京", "上海", "杭州")
# This is deliberately a bounded lexical guard, not a complete statement of
# product coverage.  Live inference can preserve any domestic destination;
# source recovery only overrides it when the source contains an unambiguous,
# exact city token.  That prevents phrases such as ``一家三口`` and place names
# such as ``广州北京路`` from being promoted to a destination.
DOMESTIC_CITY_NAMES = (
    "北京",
    "上海",
    "杭州",
    "成都",
    "南京",
    "广州",
    "深圳",
    "苏州",
    "武汉",
    "西安",
    "重庆",
    "青岛",
    "厦门",
    "长沙",
    "天津",
    "昆明",
    "大理",
    "三亚",
    "哈尔滨",
    "沈阳",
    "郑州",
    "济南",
    "福州",
    "合肥",
    "南昌",
    "南宁",
    "贵阳",
    "兰州",
    "太原",
    "石家庄",
    "乌鲁木齐",
    "拉萨",
    "海口",
    "银川",
    "西宁",
    "呼和浩特",
    "长春",
)
_DOMESTIC_CITY_PATTERN = "(?:" + "|".join(
    re.escape(city) for city in sorted(DOMESTIC_CITY_NAMES, key=len, reverse=True)
) + ")"
MULTI_CITY_HEADER_RE = re.compile(
    rf"^\s*(?P<cities>{_DOMESTIC_CITY_PATTERN}(?:\s*[、，,和与/]\s*{_DOMESTIC_CITY_PATTERN})+?)"
    r"\s*(?:两地|三地|多地)?(?:游|行程|攻略|旅行)"
)
BASIC_CITY_HEADER_RE = re.compile(
    rf"^\s*(?P<city>(?:{_DOMESTIC_CITY_PATTERN})(?:市)?|[\u4e00-\u9fff]{{2,6}}市)"
    r"\s*[一二两三四五六七八九十0-9]+"
    r"(?:日|天)(?:游|行程|攻略|旅行)"
)
DESTINATION_CONTEXT_RE = re.compile(
    r"(?:围绕|一段|整理|关于)\s*"
    r"(?P<cities>[\u4e00-\u9fff]{2,20}(?:\s*[、，,和与/]\s*[\u4e00-\u9fff]{2,20}){0,2})"
    r"\s*的"
)
CITY_SEPARATOR_RE = re.compile(r"\s*[、，,和与/]\s*")
GENERIC_PLACE_NAMES = frozenset({"酒店", "宾馆", "民宿", "住处", "住宿"})
BARE_FACILITY_NAMES = frozenset(
    {"公厕", "卫生间", "洗手间", "停车场", "充电站", "服务台", "售票处"}
)
ACTION_SUFFIX_RE = re.compile(
    r"(?:游览|参观|讲解|拍照|打卡)(?:\s*\d+(?:\.\d+)?\s*(?:小时|分钟))?$"
)
VISIT_PERIODS = ("清晨", "早上", "上午", "中午", "午后", "下午", "傍晚", "晚上", "夜间")
_VISIT_PERIOD_PATTERN = "(?:" + "|".join(VISIT_PERIODS) + ")"
_CLOCK_PATTERN = r"(?<![A-Za-z0-9])(?:[01]?\d|2[0-3])[:：][0-5]\d"
_CHINESE_CLOCK_PATTERN = (
    rf"(?P<period>{_VISIT_PERIOD_PATTERN})?\s*"
    r"(?P<hour>[0-9]|1[0-9]|2[0-3])点(?:\s*(?P<minute>[0-5]?\d)分?)?"
)
_VISIT_TIME_TOKEN_RE = re.compile(
    rf"(?P<chinese>{_CHINESE_CLOCK_PATTERN})|"
    rf"(?P<clock>{_VISIT_PERIOD_PATTERN}?\s*{_CLOCK_PATTERN})|"
    rf"(?P<period_only>{_VISIT_PERIOD_PATTERN})"
)
_NEGATED_CANCELLATION_RE = re.compile(
    r"(?:并不是要|不是要|不想|不希望|不需要|没必要|别再|"
    r"并非|不是|并不|并未|没有|没|不会|不要|别|无需|不用|"
    r"不打算|不准备|不再|不得(?!不)|不能|无法|未|(?<!得)不)\s*"
    r"(?:取消|撤掉|删除|放弃|排除)"
    r"|(?:取消|撤掉|删除|放弃|排除)\s*不了"
)
_CANCELLATION_DAY_NUMBER = {
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
_CANCELLATION_DAY_HEADING_RE = re.compile(
    r"(?:第\s*(?:(?P<zh>[一二三四五六七八九十])|(?P<arabic>1[0-4]|[1-9]))\s*天"
    r"|(?:Day|D)\s*(?P<latin>1[0-4]|[1-9]))",
    re.IGNORECASE,
)
_CANCELLATION_VERB_PATTERN = (
    r"(?:取消|撤掉|删除|放弃|排除|跳过|不要去|不去|不安排)"
)
_CANCELLATION_ORDINAL_RE = re.compile(
    r"(?:第\s*)?(?P<ordinal>[一二三四五六七八九十]|[1-9])\s*次"
)
_CANCELLATION_LAST_RE = re.compile(
    r"(?:(?:最后|最末|末尾)\s*(?:一)?次|末次|上一次|上次)"
)
_CANCELLATION_UNCERTAIN_RE = re.compile(
    r"(?:如果|若|万一|可能|也许|或许|考虑|打算|准备|建议|可以|"
    r"看情况|视情况|暂定|待定|再决定|再说|"
    r"(?:还没|尚未|未)决定是否|(?:还在|尚在)考虑)"
)
_CANCELLATION_PREVIOUS_DAY_RE = re.compile(r"(?:前一|上一)(?:天|日)|昨天|昨日")
_CANCELLATION_RELATION_BOUNDARIES = "。！？；;\n，,"


def _ordered_deep_cities(value: str) -> tuple[str, ...]:
    positions = [
        (position, city)
        for city in DEEP_CITIES
        if (position := value.find(city)) >= 0
    ]
    return tuple(city for _position, city in sorted(positions))


def source_destination_cities(source_text: str) -> tuple[str, ...]:
    """Recover only source-explicit destination cities from itinerary framing.

    This intentionally does not treat every occurrence of a city token as the
    trip destination: names such as ``北京路步行街`` may be places in another
    city.  The accepted forms are itinerary headers and explicit framing such
    as ``围绕北京的`` or ``一段北京、上海的``.
    """

    multi_city = MULTI_CITY_HEADER_RE.search(source_text)
    if multi_city:
        return tuple(
            city.strip().removesuffix("市")
            for city in CITY_SEPARATOR_RE.split(multi_city.group("cities"))
        )
    contextual = DESTINATION_CONTEXT_RE.search(source_text)
    if contextual:
        cities = tuple(
            city.strip().removesuffix("市")
            for city in CITY_SEPARATOR_RE.split(contextual.group("cities"))
            if city.strip()
        )
        if cities and all(city in DOMESTIC_CITY_NAMES for city in cities):
            return cities
    basic_city = BASIC_CITY_HEADER_RE.search(source_text)
    if basic_city:
        return (basic_city.group("city").removesuffix("市"),)
    return ()


def normalized_destination_name(source_text: str, destination_name: str) -> str:
    source_cities = source_destination_cities(source_text)
    return "、".join(source_cities) if source_cities else destination_name


def resolution_cities(source_text: str, destination_name: str) -> tuple[str, ...]:
    """Return conservative city-limited search lanes for one itinerary.

    A multi-city destination is searched once per explicitly named deep city.
    If a model translated or softened the destination, source-verbatim city
    tokens recover the safe search boundary. Unknown cities remain a single
    basic-only lane and are rejected by the live resolver without a call.
    """

    header_cities = source_destination_cities(source_text)
    if header_cities:
        return header_cities
    destination_cities = _ordered_deep_cities(destination_name)
    if destination_cities:
        return destination_cities
    if re.search(r"[\u4e00-\u9fff]", destination_name):
        return (destination_name,)
    source_cities = _ordered_deep_cities(source_text)
    return source_cities or (destination_name,)


def canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class StructuredInferenceProvider(Protocol):
    async def propose(self, source_text: str) -> InferenceProposal: ...


class PlaceResolver(Protocol):
    async def resolve(
        self,
        *,
        city: str,
        atomic_place_name: str,
        category_hint: str | None = None,
    ) -> PlaceResolutionOutcome | ResolvedPlace | None: ...


class ResilientStructuredInferenceProvider:
    """Use one explicit local fallback only for a typed provider outage.

    Programming/schema errors still fail the job. The fallback binding records
    the primary attempt and never changes to the frozen DeepSeek baseline.
    """

    def __init__(
        self,
        primary: StructuredInferenceProvider,
        fallback: StructuredInferenceProvider,
    ) -> None:
        self.primary = primary
        self.fallback = fallback

    async def propose(self, source_text: str) -> InferenceProposal:
        try:
            return await self.primary.propose(source_text)
        except InferenceProviderUnavailableError as exc:
            proposal = await self.fallback.propose(source_text)
            fallback_binding = dict(proposal.binding)
            fallback_binding.update(
                {
                    "fallback_used": True,
                    "fallback_reason": exc.category,
                    "primary_provider_binding": exc.provider_binding,
                    "primary_external_call_count": exc.external_call_count,
                    "fallback_policy": "LOCAL_DETERMINISTIC_PARTIAL_RESULT",
                }
            )
            return proposal.model_copy(update={"binding": fallback_binding})

    async def aclose(self) -> None:
        await _close_async_resources(self.primary, self.fallback)


async def _close_async_resources(*resources: object) -> None:
    """Best-effort close every distinct async resource, then surface an error."""

    first_error: Exception | None = None
    seen: set[int] = set()
    for resource in resources:
        identity = id(resource)
        if identity in seen:
            continue
        seen.add(identity)
        close = getattr(resource, "aclose", None)
        if close is None:
            continue
        try:
            await close()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


def has_negated_cancellation(value: str) -> bool:
    """Return whether a local clause explicitly says a cancellation is not happening."""

    return _NEGATED_CANCELLATION_RE.search(value) is not None


def has_affirmed_cancellation(value: str) -> bool:
    """Treat ``不得不取消`` as affirmative while keeping ordinary negation safe."""

    return "取消" in value and not has_negated_cancellation(value)


def normalize_atomic_place_candidate(value: str) -> str | None:
    """Narrow bounded visit decorations without inventing a different place.

    This is intentionally lexical. It only removes a small set of adjacent
    time/arrival/action decorations and otherwise fails closed.
    """

    candidate = value.strip()
    candidate = re.sub(r"^[|｜]\s*", "", candidate)
    candidate = re.sub(
        rf"^(?:{_VISIT_PERIOD_PATTERN}\s*(?:"
        rf"(?:[0-9]|1[0-9]|2[0-3])点(?:\s*[0-5]?\d分?)?|"
        rf"{_CLOCK_PATTERN})|{_CLOCK_PATTERN}|{_VISIT_PERIOD_PATTERN})"
        r"\s*(?:到达|抵达|前往|去|到)?\s*",
        "",
        candidate,
    )
    candidate = re.sub(r"^(?:到达|抵达)\s*", "", candidate)
    candidate = re.sub(
        rf"\s*(?:{_VISIT_PERIOD_PATTERN}\s*)?{_CLOCK_PATTERN}\s*(?:到达|抵达)?$",
        "",
        candidate,
    )
    candidate = ACTION_SUFFIX_RE.sub("", candidate).strip()
    if not candidate or candidate in BARE_FACILITY_NAMES:
        return None
    if candidate.endswith(("入口", "出口")):
        return None
    return candidate


def atomic_place_rejection_reason(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return "EMPTY"
    if len(candidate) < 2:
        return "TOO_SHORT"
    if len(candidate) > 40:
        return "TOO_LONG"
    if URL_RE.search(candidate):
        return "URL"
    if candidate in GENERIC_PLACE_NAMES or candidate in BARE_FACILITY_NAMES:
        return "GENERIC_OR_FACILITY"
    if candidate.endswith(("入口", "出口")):
        return "ENTRANCE_OR_EXIT"
    if ACTION_SUFFIX_RE.search(candidate):
        return "ACTION_SUFFIX"
    if re.search(r"(?:\d{1,2}[:：][0-5]\d|\d+(?:\.\d+)?(?:小时|分钟))$", candidate):
        return "TIME_OR_DURATION"
    if re.search(r"(?:院|园|馆|街|寺|巷|店|场|站|门)\d+$", candidate):
        return "TRAILING_NUMBER"
    if any(period in candidate for period in VISIT_PERIODS):
        return "TIME_PERIOD"
    if any(marker in candidate for marker in SENTENCE_MARKERS):
        return "SENTENCE"
    if any(word in candidate for word in FORBIDDEN_PLACE_MARKERS):
        return "FORBIDDEN_MARKER"
    if GENERIC_ACTIVITY_RE.fullmatch(candidate):
        return "GENERIC_ACTIVITY"
    if ATOMIC_PLACE_RE.fullmatch(candidate) is None:
        return "NON_ATOMIC_CHARACTERS"
    if re.search(r"[A-Za-z\u4e00-\u9fff]", candidate) is None:
        return "NO_PLACE_CHARACTERS"
    return None


def _normalize_time_token(value: str) -> str | None:
    match = _VISIT_TIME_TOKEN_RE.fullmatch(value.strip())
    if match is None:
        return None
    if match.group("period_only") is not None:
        return match.group("period_only")
    if match.group("chinese") is not None:
        period = match.group("period")
        hour = int(match.group("hour"))
        minute = int(match.group("minute") or 0)
    else:
        raw = match.group("clock") or ""
        period = next((item for item in VISIT_PERIODS if raw.startswith(item)), None)
        clock = raw[len(period) :].strip() if period else raw.strip()
        hour_text, minute_text = re.split(r"[:：]", clock)
        hour = int(hour_text)
        minute = int(minute_text)
    if period in {"中午", "午后", "下午", "傍晚", "晚上", "夜间"} and hour < 12:
        hour += 12
    if period in {"清晨", "早上", "上午"} and hour == 12:
        hour = 0
    return f"{hour:02d}:{minute:02d}"


def derive_visit_time_hint(source_text: str, span_start: int, span_end: int) -> str | None:
    """Derive a visit-owned time immediately before or after one place span."""

    if not 0 <= span_start < span_end <= len(source_text):
        return None
    left_boundary = max(
        source_text.rfind(marker, 0, span_start)
        for marker in "。！？；;\n，,、→⇒➜⇨＞➔➡⟶"
    )
    before = source_text[left_boundary + 1 : span_start]
    if not any(
        marker in before
        for marker in (
            "开放时间",
            "营业时间",
            "排队",
            "步行",
            "公交",
            "地铁",
            "交通",
            "车程",
            "耗时",
            "路线",
            "驾车",
        )
    ):
        prefix = re.search(
            rf"(?P<time>{_VISIT_TIME_TOKEN_RE.pattern})\s*"
            r"(?:到达|抵达|前往|再次去|去|到|游览|参观|看|逛)?\s*$",
            before,
        )
        if prefix is not None:
            normalized = _normalize_time_token(prefix.group("time"))
            if normalized is not None:
                return normalized
    after = source_text[span_end : min(len(source_text), span_end + 20)]
    if re.search(r"(?:去|到|前往)\s*$", before):
        postfix = re.match(
            rf"\s*(?P<time>{_VISIT_TIME_TOKEN_RE.pattern})\s*(?:到达|抵达)",
            after,
        )
        if postfix is not None:
            return _normalize_time_token(postfix.group("time"))
    return None


def is_atomic_planned_place(mention) -> bool:
    if mention.role != ActivityRole.PLANNED or mention.day_index is None:
        return False
    candidate = (mention.atomic_place_name or "").strip()
    if atomic_place_rejection_reason(candidate) is not None:
        return False
    raw_candidate = re.sub(r"\r?\n[ \t]*", "", (mention.raw_text or "").strip())
    if candidate != raw_candidate:
        return False
    return True


def _apply_contextual_category_hints(
    source_text: str,
    proposal: InferenceProposal,
) -> InferenceProposal:
    """Recover a narrow category only when source wording is explicit.

    The hint constrains Provider selection; it never invents a place.  Name-
    based or model-supplied categories continue to win, while an explicit meal
    cue immediately before an otherwise ambiguous place name prevents a hotel
    or attraction with the same short name from being auto-selected.
    """

    mentions = []
    changed = False
    for mention in proposal.mentions:
        category_hint = mention.category_hint
        if mention.role == ActivityRole.PLANNED:
            local_before = source_text[max(0, mention.span_start - 18) : mention.span_start]
            if DINING_CONTEXT_RE.search(local_before):
                category_hint = "餐饮"
        if category_hint != mention.category_hint:
            changed = True
            mention = mention.model_copy(update={"category_hint": category_hint})
        mentions.append(mention)
    if not changed:
        return proposal
    return proposal.model_copy(update={"mentions": mentions})


def _cancellation_day_at(source_text: str, position: int) -> int | None:
    heading = next(
        iter(reversed(list(_CANCELLATION_DAY_HEADING_RE.finditer(source_text, 0, position)))),
        None,
    )
    if heading is None:
        return None
    value = heading.group("arabic") or heading.group("latin")
    if value is not None:
        return int(value)
    return _CANCELLATION_DAY_NUMBER[heading.group("zh")]


def _cancellation_target_day(
    source_text: str,
    position: int,
    selector_text: str,
) -> tuple[int | None, bool]:
    selector_headings = list(_CANCELLATION_DAY_HEADING_RE.finditer(selector_text))
    if selector_headings:
        heading = selector_headings[-1]
        value = heading.group("arabic") or heading.group("latin")
        if value is not None:
            return int(value), False
        return _CANCELLATION_DAY_NUMBER[heading.group("zh")], False

    current_day = _cancellation_day_at(source_text, position)
    if _CANCELLATION_PREVIOUS_DAY_RE.search(selector_text) is None:
        return current_day, False
    if current_day is None:
        return None, True
    target_day = current_day - 1
    return (target_day, False) if 1 <= target_day <= 14 else (None, True)


def _cancellation_relation_parts(
    source_text: str,
    span_start: int,
    span_end: int,
) -> tuple[str, str]:
    left = max(
        source_text.rfind(marker, 0, span_start)
        for marker in _CANCELLATION_RELATION_BOUNDARIES
    )
    right_positions = [
        position
        for marker in _CANCELLATION_RELATION_BOUNDARIES
        if (position := source_text.find(marker, span_end)) >= 0
    ]
    right = min(right_positions, default=len(source_text))
    return source_text[left + 1 : span_start], source_text[span_end:right]


def _cancellation_selector_text(
    source_text: str,
    span_start: int,
    span_end: int,
) -> tuple[str | None, bool]:
    before, after = _cancellation_relation_parts(source_text, span_start, span_end)
    local = f"{before}{source_text[span_start:span_end]}{after}"
    if has_negated_cancellation(local) or re.search(
        r"(?:可以|可|不妨)\s*(?:完全)?\s*(?:不去|不安排)",
        local,
    ):
        return None, False
    uncertain = _CANCELLATION_UNCERTAIN_RE.search(local) is not None

    before_matches = list(re.finditer(_CANCELLATION_VERB_PATTERN, before))
    if before_matches:
        cue = before_matches[-1]
        prefix = before[: cue.start()][-24:]
        if not uncertain and not (
            cue.group(0) == "取消" and re.search(r"不得不\s*$", prefix)
        ) and re.search(r"[不没未无别非]", prefix):
            return None, False
        return before[cue.end() :], uncertain

    after_match = re.match(
        rf"\s*(?P<selector>.{{0,18}}?)\s*"
        rf"(?P<verb>{_CANCELLATION_VERB_PATTERN})",
        after,
    )
    if after_match is not None:
        selector = f"{before[-18:]}{after_match.group('selector')}"
        if not uncertain and not (
            after_match.group("verb") == "取消"
            and re.search(r"不得不\s*$", selector)
        ) and re.search(r"[不没未无别非]", selector):
            return None, False
        return selector, uncertain
    return None, False


def _cancellation_target_time(selector_text: str) -> str | None:
    matches = [
        normalized
        for match in _VISIT_TIME_TOKEN_RE.finditer(selector_text)
        if (normalized := _normalize_time_token(match.group(0))) is not None
    ]
    return matches[0] if len(matches) == 1 else None


def _cancellation_target_ordinal(selector_text: str) -> int | str | None:
    if _CANCELLATION_LAST_RE.search(selector_text):
        return "LAST"
    match = _CANCELLATION_ORDINAL_RE.search(selector_text)
    if match is None:
        return None
    value = match.group("ordinal")
    return int(value) if value.isascii() else _CANCELLATION_DAY_NUMBER[value]


def _normalized_cancellation_target_name(raw_name: str) -> tuple[str, str]:
    """Remove only leading day, time, or occurrence selectors from a target."""

    target = raw_name.strip()
    stripped_selectors: list[str] = []
    selector_patterns = (
        _CANCELLATION_DAY_HEADING_RE,
        _CANCELLATION_PREVIOUS_DAY_RE,
        _VISIT_TIME_TOKEN_RE,
        _CANCELLATION_ORDINAL_RE,
        _CANCELLATION_LAST_RE,
    )
    while target:
        match = next(
            (
                candidate
                for pattern in selector_patterns
                if (candidate := pattern.match(target)) is not None
            ),
            None,
        )
        if match is None:
            break
        stripped_selectors.append(match.group(0))
        target = target[match.end() :].lstrip()
        if target.startswith("的"):
            target = target[1:].lstrip()
    return target.strip().casefold(), "".join(stripped_selectors)


def _apply_terminal_cancellations(
    source_text: str,
    proposal: InferenceProposal,
) -> tuple[InferenceProposal, frozenset[tuple[int, int]]]:
    """Bind an affirmed cancellation to one earlier visit, or fail closed.

    A cancellation never crosses a day boundary. Repeated same-day visits need
    an explicit time or occurrence selector; otherwise all possible targets are
    retained and surfaced as needing confirmation without a resolver call.
    """

    mentions = list(proposal.mentions)
    original_mentions = tuple(proposal.mentions)
    planned_names = {
        (mention.atomic_place_name or "").strip().casefold()
        for mention in original_mentions
        if mention.role == ActivityRole.PLANNED and mention.atomic_place_name
    }
    pending_spans: set[tuple[int, int]] = set()
    reclassified_count = 0

    for exclusion_index in sorted(
        (
            index
            for index, mention in enumerate(original_mentions)
            if mention.role == ActivityRole.EXCLUDED
        ),
        key=lambda index: original_mentions[index].span_start,
    ):
        exclusion = original_mentions[exclusion_index]
        selector_text, cancellation_is_uncertain = _cancellation_selector_text(
            source_text,
            exclusion.span_start,
            exclusion.span_end,
        )
        if selector_text is None:
            continue

        raw_name = (exclusion.atomic_place_name or exclusion.raw_text).strip()
        normalized_name = raw_name.casefold()
        embedded_selector_text = ""
        if normalized_name not in planned_names:
            normalized_name, embedded_selector_text = (
                _normalized_cancellation_target_name(raw_name)
            )
        if normalized_name not in planned_names:
            continue
        selector_scope = f"{selector_text}{embedded_selector_text}"

        possible_indices = [
            index
            for index, mention in enumerate(original_mentions)
            if (
                mention.role == ActivityRole.PLANNED
                and mention.span_start < exclusion.span_start
                and (mention.atomic_place_name or "").strip().casefold()
                == normalized_name
            )
        ]
        current_indices = [
            index
            for index in possible_indices
            if mentions[index].role == ActivityRole.PLANNED
        ]
        if not current_indices:
            continue

        target_day, unresolved_day_reference = _cancellation_target_day(
            source_text,
            exclusion.span_start,
            selector_scope,
        )
        candidate_days = {
            original_mentions[index].day_index
            for index in current_indices
            if original_mentions[index].day_index is not None
        }
        if (
            target_day is None
            and not unresolved_day_reference
            and len(candidate_days) == 1
        ):
            target_day = next(iter(candidate_days))
        if target_day is None:
            pending_spans.update(
                (original_mentions[index].span_start, original_mentions[index].span_end)
                for index in current_indices
            )
            continue

        current_indices = [
            index
            for index in current_indices
            if original_mentions[index].day_index == target_day
        ]
        if not current_indices:
            continue
        current_indices.sort(key=lambda index: original_mentions[index].span_start)
        if cancellation_is_uncertain:
            pending_spans.update(
                (original_mentions[index].span_start, original_mentions[index].span_end)
                for index in current_indices
            )
            continue

        target_time = _cancellation_target_time(selector_scope)
        target_ordinal = _cancellation_target_ordinal(selector_scope)
        time_indices: list[int] | None = None
        if target_time is not None:
            time_indices = [
                index
                for index in current_indices
                if original_mentions[index].time_hint == target_time
            ]
        ordinal_indices: list[int] | None = None
        if target_ordinal is not None:
            ordered_indices = sorted(
                (
                    index
                    for index in possible_indices
                    if original_mentions[index].day_index == target_day
                ),
                key=lambda index: original_mentions[index].span_start,
            )
            ordinal_index = (
                len(ordered_indices) - 1
                if target_ordinal == "LAST"
                else target_ordinal - 1
            )
            ordinal_indices = (
                [ordered_indices[ordinal_index]]
                if 0 <= ordinal_index < len(ordered_indices)
                and mentions[ordered_indices[ordinal_index]].role == ActivityRole.PLANNED
                else []
            )
        if time_indices is not None and ordinal_indices is not None:
            selected_indices = [
                index for index in ordinal_indices if index in time_indices
            ]
        elif time_indices is not None:
            selected_indices = time_indices
        elif ordinal_indices is not None:
            selected_indices = ordinal_indices
        else:
            selected_indices = current_indices

        if len(selected_indices) != 1:
            pending_indices = selected_indices or current_indices
            pending_spans.update(
                (original_mentions[index].span_start, original_mentions[index].span_end)
                for index in pending_indices
            )
            continue

        selected_index = selected_indices[0]
        mentions[selected_index] = mentions[selected_index].model_copy(
            update={
                "role": ActivityRole.REFERENCE,
                "day_index": None,
                "time_hint": None,
            }
        )
        reclassified_count += 1

    if not reclassified_count and not pending_spans:
        return proposal, frozenset()
    binding = dict(proposal.binding)
    if reclassified_count:
        binding["terminal_cancellation_reclassification_count"] = int(
            binding.get("terminal_cancellation_reclassification_count", 0)
        ) + reclassified_count
    if pending_spans:
        binding["ambiguous_cancellation_target_count"] = len(pending_spans)
    return (
        proposal.model_copy(update={"mentions": mentions, "binding": binding}),
        frozenset(pending_spans),
    )


class EvidenceCompiler:
    def compile(
        self,
        source_text: str,
        proposal: InferenceProposal,
    ) -> tuple[list[CompiledActivity], list[SourceClaimRecord], dict[str, object]]:
        source_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        if proposal.source_hash != source_hash:
            raise ValueError("proposal source binding mismatch")
        compiled: list[CompiledActivity] = []
        claims: list[SourceClaimRecord] = []
        mention_ids: set[str] = set()
        valid_spans = 0
        for mention in proposal.mentions:
            if mention.mention_id in mention_ids:
                raise ValueError("duplicate proposal mention ID")
            mention_ids.add(mention.mention_id)
            if source_text[mention.span_start : mention.span_end] != mention.raw_text:
                raise ValueError("proposal evidence span does not match source text")
            valid_spans += 1
            activity_id = str(uuid4())
            compiled_activity = CompiledActivity(
                activity_id=activity_id,
                public_activity_token=secrets.token_urlsafe(24),
                mention=mention,
                eligible_for_place_search=is_atomic_planned_place(mention),
            )
            compiled.append(compiled_activity)
            claim_type = "EXCLUSION" if mention.role == ActivityRole.EXCLUDED else "PLACE_MENTION"
            claims.append(
                SourceClaimRecord(
                    claim_id=str(uuid4()),
                    activity_id=activity_id,
                    claim_type=claim_type,
                    span_start=mention.span_start,
                    span_end=mention.span_end,
                    quote=mention.raw_text,
                )
            )
        return compiled, claims, {
            "compiler": "trip-understanding-evidence-compiler-v1",
            "unicode_basis": "CODE_POINT_HALF_OPEN",
            "mention_count": len(compiled),
            "valid_span_count": valid_spans,
            "eligible_place_count": sum(item.eligible_for_place_search for item in compiled),
        }


class PublicResultProjector:
    def project(
        self,
        destination_name: str,
        destination_basis: DestinationBasis,
        activities: list[ResolvedActivity],
    ) -> UserFacingTripResult:
        planned = [
            activity
            for activity in activities
            if activity.compiled.mention.role == ActivityRole.PLANNED
        ]
        day_count = max(
            (activity.compiled.mention.day_index or 1 for activity in planned),
            default=1,
        )
        day_views: list[TripDayView] = []
        for day_index in range(1, day_count + 1):
            cards = []
            for item in sorted(
                (
                    activity
                    for activity in activities
                    if activity.compiled.mention.role == ActivityRole.PLANNED
                    and activity.compiled.mention.day_index == day_index
                ),
                key=lambda activity: activity.compiled.mention.sequence_index,
            ):
                mention = item.compiled.mention
                place = item.place
                source_confirmation_required = (
                    item.resolver_receipt.get("status")
                    == "SOURCE_CONFIRMATION_REQUIRED"
                )
                cards.append(
                    ActivityCardView(
                        activity_token=item.compiled.public_activity_token,
                        name=(
                            place.name
                            if place
                            else (
                                "地点待确认"
                                if source_confirmation_required
                                else (mention.atomic_place_name if is_atomic_planned_place(mention) else "地点待确认")
                            )
                        ),
                        category=(
                            place.category
                            if place
                            else (
                                "地点"
                                if source_confirmation_required
                                else (mention.category_hint or "地点")
                            )
                        ),
                        area_or_address=place.area_or_address if place else "地点待确认",
                        time_hint=mention.time_hint,
                        **timing_values(mention),
                        status="READY" if place else "NEEDS_CONFIRMATION",
                        available_actions=["VIEW_DETAILS", "REPLACE", "DELETE", "MOVE"],
                    )
                )
            day_views.append(TripDayView(label=f"Day {day_index}", activities=cards))
        resolved_count = sum(item.place is not None for item in planned)
        if planned and resolved_count == len(planned):
            result_status = "READY"
        elif resolved_count:
            result_status = "PARTIAL_RESULT"
        else:
            result_status = "BASIC_ONLY"
        return UserFacingTripResult(
            status=result_status,
            assumptions=[
                AssumptionChipView(
                    key="destination",
                    label="目的地",
                    value=(
                        destination_name
                        if destination_basis == DestinationBasis.EXPLICIT
                        else f"暂按 {destination_name}"
                    ),
                    editable=True,
                ),
                AssumptionChipView(
                    key="calendar",
                    label="日期",
                    value=(
                        f"未填写，按 Day 1～Day {day_count} 展示"
                        if day_count > 1
                        else "未填写，按 Day 1 展示"
                    ),
                    editable=True,
                ),
                AssumptionChipView(
                    key="party_size",
                    label="同行人数",
                    value="暂按 2 人",
                    editable=True,
                ),
            ],
            days=day_views,
            map=MapReadinessView(
                status="UNAVAILABLE",
                message="路线地图暂不可用，不影响查看和编辑卡片",
            ),
            stay=StaySuggestionView(
                status="UNAVAILABLE",
                message="住宿待选择",
            ),
            available_actions=["EDIT_ASSUMPTIONS", "EDIT_CARDS"],
        )


class TripUnderstandingPipeline:
    def __init__(
        self,
        inference_provider: StructuredInferenceProvider,
        place_resolver: PlaceResolver,
        compiler: EvidenceCompiler | None = None,
        projector: PublicResultProjector | None = None,
        max_executable_activities: int = 80,
        max_place_concurrency: int = 4,
    ) -> None:
        if max_place_concurrency < 1 or max_place_concurrency > 8:
            raise ValueError("place concurrency must be between 1 and 8")
        self.inference_provider = inference_provider
        self.place_resolver = place_resolver
        self.compiler = compiler or EvidenceCompiler()
        self.projector = projector or PublicResultProjector()
        self.max_executable_activities = max_executable_activities
        self.max_place_concurrency = max_place_concurrency

    async def aclose(self) -> None:
        await _close_async_resources(self.inference_provider, self.place_resolver)

    async def _resolve_place(
        self,
        item: CompiledActivity,
        *,
        city: str,
        semaphore: asyncio.Semaphore,
    ) -> tuple[PlaceResolutionOutcome, bool]:
        try:
            async with semaphore:
                raw_outcome = await self.place_resolver.resolve(
                    city=city,
                    atomic_place_name=item.mention.atomic_place_name or "",
                    category_hint=item.mention.category_hint,
                )
        except PlaceProviderUnavailableError as exc:
            return (
                PlaceResolutionOutcome(
                    receipt={
                        "status": "UNAVAILABLE",
                        "failure_category": exc.category,
                        "provider_binding": exc.provider_binding,
                        "external_calls": exc.external_call_count,
                    }
                ),
                True,
            )
        if isinstance(raw_outcome, PlaceResolutionOutcome):
            outcome = raw_outcome
        elif isinstance(raw_outcome, ResolvedPlace):
            outcome = PlaceResolutionOutcome(
                place=raw_outcome,
                receipt={
                    "status": "AUTO_MATCHED",
                    **raw_outcome.provider_binding,
                },
            )
        else:
            outcome = PlaceResolutionOutcome(
                receipt={
                    "status": "NO_UNIQUE_MATCH",
                    "external_calls": 0,
                }
            )
        reported_cities = [outcome.receipt.get("city")]
        if outcome.place is not None:
            reported_cities.append(outcome.place.provider_binding.get("city"))
        mismatched_city = next(
            (
                reported
                for reported in reported_cities
                if isinstance(reported, str)
                and reported.strip().removesuffix("市")
                != city.strip().removesuffix("市")
            ),
            None,
        )
        if outcome.place is not None and mismatched_city is not None:
            return (
                PlaceResolutionOutcome(
                    receipt={
                        **outcome.receipt,
                        "status": "NO_UNIQUE_MATCH",
                        "failure_category": "CROSS_CITY_PROVIDER_RESULT",
                        "requested_city": city,
                        "reported_city": mismatched_city,
                    }
                ),
                False,
            )
        return outcome, False

    async def _resolve_place_across_cities(
        self,
        item: CompiledActivity,
        *,
        cities: tuple[str, ...],
        semaphore: asyncio.Semaphore,
    ) -> tuple[PlaceResolutionOutcome, bool]:
        results = await asyncio.gather(
            *(
                self._resolve_place(item, city=city, semaphore=semaphore)
                for city in cities
            )
        )
        if len(results) == 1:
            return results[0]

        external_calls = sum(
            int(outcome.receipt.get("external_calls", 0))
            for outcome, _unavailable in results
            if isinstance(outcome.receipt.get("external_calls", 0), int)
        )
        unavailable = any(value for _outcome, value in results)
        ambiguous = any(
            outcome.receipt.get("status") == "AMBIGUOUS"
            or (
                outcome.receipt.get("status") == "NO_UNIQUE_MATCH"
                and isinstance(
                    outcome.receipt.get("category_compatible_candidate_count"),
                    int,
                )
                and int(outcome.receipt["category_compatible_candidate_count"]) > 1
            )
            for outcome, _unavailable in results
        )
        matches = [
            (city, outcome)
            for city, (outcome, _unavailable) in zip(cities, results, strict=True)
            if outcome.place is not None
        ]
        receipt_hashes = [
            canonical_sha256(
                {
                    "city": city,
                    "receipt": outcome.receipt,
                }
            )
            for city, (outcome, _unavailable) in zip(cities, results, strict=True)
        ]
        successful_place_candidates = [
            {
                "city": city,
                "place": outcome.place.model_dump(mode="json"),
                "receipt": outcome.receipt,
            }
            for city, (outcome, _unavailable) in zip(cities, results, strict=True)
            if outcome.place is not None
        ]
        if not unavailable and not ambiguous and len(matches) == 1:
            selected_city, selected = matches[0]
            return (
                PlaceResolutionOutcome(
                    place=selected.place,
                    receipt={
                        **selected.receipt,
                        "multi_city_resolution": True,
                        "queried_cities": list(cities),
                        "selected_city": selected_city,
                        "city_receipt_sha256": receipt_hashes,
                        "external_calls": external_calls,
                    },
                ),
                False,
            )

        return (
            PlaceResolutionOutcome(
                receipt={
                    "provider": "MULTI_CITY_CONSERVATIVE_RESOLUTION",
                    "status": (
                        "UNAVAILABLE"
                        if unavailable
                        else "NO_UNIQUE_MATCH"
                    ),
                    "multi_city_resolution": True,
                    "queried_cities": list(cities),
                    "city_receipt_sha256": receipt_hashes,
                    "successful_place_candidates": successful_place_candidates,
                    "external_calls": external_calls,
                }
            ),
            unavailable,
        )

    async def run(
        self,
        source_text: str,
        *,
        requires_confirmation_spans: Sequence[tuple[int, int]] = (),
        partial_source: bool = False,
    ) -> PipelineOutput:
        confirmation_spans = tuple(requires_confirmation_spans)
        if any(
            start < 0 or end <= start or end > len(source_text)
            for start, end in confirmation_spans
        ):
            raise ValueError("confirmation spans must be valid source code-point ranges")
        proposal = await self.inference_provider.propose(source_text)
        model_meaning = proposal.binding.get("semantic_policy") == "MODEL_MEANING_SOURCE_VALIDATED_V1"
        cancellation_pending_spans: set[tuple[int, int]] = set()
        if not model_meaning:
            # Historical rule-based experiments keep their original behavior.
            # The live experience adapter supplies day/order/roles directly;
            # lexical recovery must not rewrite those meanings a second time.
            proposal, cancellation_pending_spans = _apply_terminal_cancellations(
                source_text, _apply_contextual_category_hints(source_text, proposal),
            )
        destination_name = (
            proposal.destination_name if model_meaning else normalized_destination_name(
                source_text, proposal.destination_name,
            )
        )
        if destination_name != proposal.destination_name:
            binding = dict(proposal.binding)
            binding["destination_source_recovery_count"] = int(
                binding.get("destination_source_recovery_count", 0)
            ) + 1
            proposal = proposal.model_copy(
                update={"destination_name": destination_name, "binding": binding}
            )
        search_cities = ((proposal.destination_name.removesuffix("市"),) if model_meaning
                         else resolution_cities(source_text, proposal.destination_name))
        compiled, claims, compiler_receipt = self.compiler.compile(source_text, proposal)
        confirmation_activity_ids: set[str] = set()
        cancellation_pending_activity_ids: set[str] = set()
        guarded_compiled: list[CompiledActivity] = []
        for item in compiled:
            mention = item.mention
            intersects_confirmation = any(
                mention.span_start < end and start < mention.span_end
                for start, end in confirmation_spans
            )
            if item.eligible_for_place_search and intersects_confirmation:
                confirmation_activity_ids.add(item.activity_id)
                item = item.model_copy(update={"eligible_for_place_search": False})
            elif (
                item.eligible_for_place_search
                and (mention.span_start, mention.span_end) in cancellation_pending_spans
            ):
                cancellation_pending_activity_ids.add(item.activity_id)
                item = item.model_copy(update={"eligible_for_place_search": False})
            guarded_compiled.append(item)
        compiled = guarded_compiled
        resolved: list[ResolvedActivity] = []
        attempted_count = 0
        unavailable_count = 0
        budget_limited_count = 0
        deduplicated_count = 0
        semaphore = asyncio.Semaphore(self.max_place_concurrency)
        tasks_by_key: dict[
            tuple[tuple[str, ...], str, str],
            asyncio.Task[tuple[PlaceResolutionOutcome, bool]],
        ] = {}
        resolution_slots: list[
            tuple[str, asyncio.Task[tuple[PlaceResolutionOutcome, bool]] | None, bool, str]
        ] = []
        for item in compiled:
            if item.activity_id in confirmation_activity_ids:
                resolution_slots.append(("CONFIRMATION_REQUIRED", None, False, ""))
                continue
            if item.activity_id in cancellation_pending_activity_ids:
                resolution_slots.append(("CANCELLATION_PENDING", None, False, ""))
                continue
            if not item.eligible_for_place_search:
                resolution_slots.append(("NOT_ELIGIBLE", None, False, ""))
                continue
            if attempted_count >= self.max_executable_activities:
                budget_limited_count += 1
                resolution_slots.append(("BUDGET_LIMITED", None, False, ""))
                continue
            attempted_count += 1
            resolution_key = (
                tuple(city.strip().casefold() for city in search_cities),
                (item.mention.atomic_place_name or "").strip().casefold(),
                (item.mention.category_hint or "").strip().casefold(),
            )
            task = tasks_by_key.get(resolution_key)
            is_owner = task is None
            if task is None:
                task = asyncio.create_task(
                    self._resolve_place_across_cities(
                        item,
                        cities=search_cities,
                        semaphore=semaphore,
                    )
                )
                tasks_by_key[resolution_key] = task
            else:
                deduplicated_count += 1
            resolution_slots.append(
                (
                    "RESOLVE",
                    task,
                    is_owner,
                    canonical_sha256(resolution_key),
                )
            )

        if tasks_by_key:
            await asyncio.gather(*tasks_by_key.values())

        for item, (slot_type, task, is_owner, resolution_key_sha256) in zip(
            compiled,
            resolution_slots,
            strict=True,
        ):
            if slot_type == "CONFIRMATION_REQUIRED":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.NEEDS_CONFIRMATION,
                        resolver_receipt={
                            "status": "SOURCE_CONFIRMATION_REQUIRED",
                            "external_calls": 0,
                        },
                    )
                )
                continue
            if slot_type == "CANCELLATION_PENDING":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.NEEDS_CONFIRMATION,
                        resolver_receipt={
                            "status": "CANCELLATION_TARGET_AMBIGUOUS",
                            "external_calls": 0,
                        },
                    )
                )
                continue
            if slot_type == "NOT_ELIGIBLE":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.NOT_ELIGIBLE,
                        resolver_receipt={
                            "status": "NOT_ELIGIBLE",
                            "external_calls": 0,
                        },
                    )
                )
                continue
            if slot_type == "BUDGET_LIMITED":
                resolved.append(
                    ResolvedActivity(
                        compiled=item,
                        resolution_status=ResolutionStatus.UNRESOLVED,
                        resolver_receipt={
                            "status": "BUDGET_LIMITED",
                            "budget": "max_executable_activities",
                            "limit": self.max_executable_activities,
                            "external_calls": 0,
                        },
                    )
                )
                continue
            assert task is not None
            outcome, provider_unavailable = task.result()
            unavailable_count += int(provider_unavailable)
            receipt = dict(outcome.receipt)
            if not is_owner:
                receipt.update(
                    {
                        "external_calls": 0,
                        "deduplicated": True,
                        "resolution_key_sha256": resolution_key_sha256,
                    }
                )
            place = outcome.place
            resolved.append(
                ResolvedActivity(
                    compiled=item,
                    resolution_status=(
                        ResolutionStatus.AUTO_MATCHED if place else ResolutionStatus.NEEDS_CONFIRMATION
                    ),
                    place=place,
                    resolver_receipt=receipt,
                )
            )
        public_result = self.projector.project(
            proposal.destination_name,
            proposal.destination_basis,
            resolved,
        )
        if proposal.day_labels:
            days = [day.model_copy(update={"label": proposal.day_labels.get(index, day.label)})
                    for index, day in enumerate(public_result.days, 1)]
            calendar = "、".join(day.label for day in days)
            public_result = public_result.model_copy(update={
                "days": days,
                "assumptions": [chip.model_copy(update={"value": calendar}) if chip.key == "calendar" else chip
                                for chip in public_result.assumptions],
            })
        fallback_used = proposal.binding.get("fallback_used") is True
        if partial_source or proposal.unprocessed_count:
            public_result = public_result.model_copy(update={"status": "PARTIAL_RESULT"})
        elif budget_limited_count:
            public_result = public_result.model_copy(update={"status": "LIMITED"})
        elif (fallback_used or unavailable_count) and public_result.status != "PARTIAL_RESULT":
            public_result = public_result.model_copy(update={"status": "PARTIAL_RESULT"})
        resolution_receipt = {
            "policy": "atomic-planned-place-resolution-v1",
            "eligible_count": sum(item.eligible_for_place_search for item in compiled),
            "attempted_count": attempted_count,
            "auto_matched_count": sum(item.place is not None for item in resolved),
            "needs_confirmation_count": sum(
                item.resolution_status == ResolutionStatus.NEEDS_CONFIRMATION for item in resolved
            ),
            "provider_unavailable_count": unavailable_count,
            "unique_resolution_count": len(tasks_by_key),
            "deduplicated_resolution_count": deduplicated_count,
            "place_external_call_count": sum(
                int(item.resolver_receipt.get("external_calls", 0))
                for item in resolved
                if isinstance(item.resolver_receipt.get("external_calls"), int)
            ),
            "max_place_concurrency": self.max_place_concurrency,
            "budget_limited_count": budget_limited_count,
            "max_executable_activities": self.max_executable_activities,
            "inference_fallback_used": fallback_used,
            "source_confirmation_required_count": len(confirmation_activity_ids),
            "cancellation_target_ambiguous_count": len(
                cancellation_pending_activity_ids
            ),
            "partial_source": partial_source,
            "unprocessed_count": proposal.unprocessed_count,
            "provider_failures_exposed_publicly": 0,
        }
        internal_content = {
            "source_hash": proposal.source_hash,
            "destination": proposal.destination_name,
            "proposal": proposal.model_dump(mode="json"),
            "compiler_receipt": compiler_receipt,
            "resolution_receipt": resolution_receipt,
            "activities": [item.model_dump(mode="json") for item in resolved],
        }
        return PipelineOutput(
            source_hash=proposal.source_hash,
            destination={
                "name": proposal.destination_name,
                "status": proposal.destination_basis.value,
            },
            assumptions=[
                {"key": "calendar", "value": "DAY_INDEX_ONLY", "source": "SOFT_ASSUMPTION"},
                {"key": "party_size", "value": 2, "source": "SOFT_ASSUMPTION"},
            ],
            proposal=proposal,
            inference_binding=proposal.binding,
            compiler_receipt=compiler_receipt,
            resolution_receipt=resolution_receipt,
            activities=resolved,
            claims=claims,
            public_result=public_result,
            content_hash=canonical_sha256(internal_content),
        )
