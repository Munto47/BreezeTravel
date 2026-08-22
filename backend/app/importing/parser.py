from __future__ import annotations

import re
from uuid import uuid4

from app.importing.models import ImportParseDraft, RawStop, SourceSpan
from app.itineraries.models import CommitmentKind


_DAY_NUMBER = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
_DAY_PATTERN = re.compile(r"^\s*(?:第\s*([一二三四五1-5])\s*天|day\s*([1-5])|d\s*([1-5]))\s*[:：.、-]?\s*", re.I)
_CLOCK_POINT = (
    r"(?:(?:上午|下午|晚上|中午|早上)\s*\d{1,2}(?::\d{2}|点(?:半|\d{1,2}分)?)?"
    r"|\d{1,2}(?::\d{2}|点(?:半|\d{1,2}分)?))"
)
_TIME_PATTERN = re.compile(
    rf"(?:{_CLOCK_POINT}\s*(?:[-—~～至]\s*(?:次日)?\s*{_CLOCK_POINT})?)"
)
_DATE_PATTERN = re.compile(r"(?:20\d{2}[年/-])?\d{1,2}[月/-]\d{1,2}日?")
_FIXED_VISIT_TERMS = ("已预约", "预约", "已预订", "不可改", "固定")
_FIXED_TERMS = _FIXED_VISIT_TERMS + ("航班", "高铁", "火车", "返程", "接送")
_ARRIVAL_TERMS = ("到达", "抵达")
_RETURN_TERMS = ("返程", "航班", "高铁", "火车", "离开", "去机场", "前往机场")
_MEMBER_TERMS = ("同行", "成员", "老人", "长辈", "孩子", "儿童", "轮椅", "过敏", "午休", "服药")
_NON_STOP_PREFIXES = (
    "交通", "说明", "备注", "提示", "预算", "住宿建议", "行程概览", "注意",
    "不要把", "合理映射",
)
_KNOWN_TRIP_CITIES = ("北京", "上海", "杭州", "成都", "广州", "深圳", "厦门")
_UNTRUSTED_INSTRUCTION = re.compile(
    r"(?:忽略(?:以上|之前).{0,12}(?:指令|要求)|ignore\s+(?:all\s+)?previous|system\s*prompt|api[_ -]?key|调用.{0,8}工具)",
    re.I,
)
_INLINE_DAY_PATTERN = re.compile(
    r"(?:第\s*[一二三四五1-5]\s*天|day\s*[1-5]|d\s*[1-5])",
    re.I,
)
_LATEST_RETURN_PATTERN = re.compile(
    rf"(?:{_CLOCK_POINT})\s*前\s*(?:回|返回|到达)\s*(?:酒店|住宿|住处|民宿)\s*$"
)


def _table_columns(sentence: str) -> list[str] | None:
    """Return a supported TSV itinerary row without guessing arbitrary tables.

    Spreadsheet copies are a documented plain-text import shape.  Requiring a
    day cell, a time cell and a place cell keeps the parser deterministic while
    preventing the header and free-form tabbed prose from becoming POIs.
    """

    delimiter = "\t" if "\t" in sentence else "|" if "|" in sentence else None
    if delimiter is None:
        return None
    columns = [item.strip() for item in sentence.split(delimiter)]
    if len(columns) < 3:
        return None
    normalized = [re.sub(r"\s+", "", item).casefold() for item in columns]
    if (
        normalized[0] in {"天数", "日期", "day"}
        and normalized[1] in {"时间", "time"}
        and normalized[2] in {"地点", "景点", "place", "poi"}
    ):
        return []
    if _DAY_PATTERN.fullmatch(columns[0]) is None or not columns[2]:
        return None
    return columns


def _day_number(match: re.Match[str]) -> int:
    raw = next(group for group in match.groups() if group is not None)
    return int(raw) if raw.isdigit() else _DAY_NUMBER.get(raw, 1)


def _clean_name(segment: str) -> str:
    cleaned = _TIME_PATTERN.sub("", segment)
    cleaned = _DATE_PATTERN.sub("", cleaned)
    cleaned = re.sub(r"^[\s\-—•*\d.、]+", "", cleaned)
    cleaned = re.sub(r"[（(][^）)]*(?:分钟|小时|交通|地铁|步行|打车)[^）)]*[）)]", "", cleaned)
    cleaned = re.sub(r"[（(](?:已预约|预约|已预订|固定|不可改)[^）)]*[）)]", "", cleaned)
    cleaned = re.sub(r"^(?:上午|下午|晚上|早上|中午)\s*", "", cleaned)
    cleaned = re.sub(r"^(?:接着|然后|随后|再|想看|想去|计划去|从)\s*", "", cleaned)
    cleaned = re.sub(r"^(?:早餐|午餐|晚餐)\s*[:：-]?\s*", "", cleaned)
    cleaned = re.sub(r"^(?:返程|到达|抵达|到|出发|前往|去)\s*[:：-]?\s*", "", cleaned)
    cleaned = re.sub(r"^(?:(?:乘坐|搭乘)\s*)?(?:航班|高铁|火车|列车)?\s*离开\s*", "", cleaned)
    cleaned = re.sub(r"[，,](?:不要|备注|说明).*$", "", cleaned)
    cleaned = re.sub(r"\s*(?:入住|游览|参观|用餐|打卡|散步|自由活动|走)\s*$", "", cleaned)
    cleaned = re.sub(r"(?:返程|离开)\s*$", "", cleaned)
    return cleaned.strip(" \t，,。；;：:-—~～→>")


def _commitment_kind(segment: str) -> CommitmentKind | None:
    compact = re.sub(r"\s+", "", segment)
    if any(term in compact for term in _RETURN_TERMS):
        return CommitmentKind.RETURN_DEPARTURE
    if any(term in compact for term in _ARRIVAL_TERMS) or re.search(r"(?:^|\d)到[^达]", compact):
        return CommitmentKind.ARRIVAL
    if any(term in compact for term in _FIXED_VISIT_TERMS):
        return CommitmentKind.FIXED_VISIT
    return None


def _is_transport_event_without_place(name: str) -> bool:
    compact = re.sub(r"\s+", "", name)
    return bool(re.fullmatch(
        r"(?:航班|飞机|高铁|火车|列车)(?:已预约|已预订|不可改|固定)?",
        compact,
    ))


def _iter_segments(sentence: str):
    """Yield segments and their sentence offsets.

    Chinese commas are separators only when the following clause owns a new
    time expression. This keeps ordinary commas inside place descriptions but
    supports compact one-line itineraries such as ``14:00 A，14:30 B``.
    """

    for coarse in re.finditer(r"[^→>；;]+", sentence):
        text = coarse.group()
        split_at = [0]
        for delimiter in re.finditer(r"[，,]", text):
            tail = text[delimiter.end():]
            if _TIME_PATTERN.match(tail.lstrip()):
                split_at.append(delimiter.end())
        split_at.append(len(text))
        for start, end in zip(split_at, split_at[1:]):
            raw = text[start:end]
            leading = len(raw) - len(raw.lstrip(" ，,"))
            trailing = len(raw.rstrip(" ，,"))
            if trailing <= leading:
                continue
            yield raw[leading:trailing], coarse.start() + start + leading


def _iter_source_clauses(line: str):
    """Yield sentence/day clauses with offsets, including compact one-line days.

    Full stops delimit prose sentences.  A new explicit day marker also starts
    a clause, which makes ``第1天 A；第2天 B`` deterministic without treating
    the second day marker as part of a POI name.
    """

    for sentence in re.finditer(r"[^。！？!?]+", line):
        text = sentence.group()
        boundaries = [0]
        for match in _INLINE_DAY_PATTERN.finditer(text):
            if match.start() > 0:
                boundaries.append(match.start())
        boundaries.append(len(text))
        for start, end in zip(sorted(set(boundaries)), sorted(set(boundaries))[1:]):
            raw = text[start:end]
            leading = len(raw) - len(raw.lstrip(" \t；;，,"))
            trailing = len(raw.rstrip(" \t；;，,"))
            if trailing <= leading:
                continue
            yield raw[leading:trailing], sentence.start() + start + leading


def _iter_natural_segments(sentence: str):
    """Yield bounded POI phrases from a natural-language day clause.

    The grammar is intentionally narrow: punctuation-separated clauses,
    Chinese list separators, ``A和B``, and the explicit ``从A走到B`` form.
    It never splits on a bare ``去``/``走到``/``前回`` token in arbitrary
    prose, avoiding the giant/bogus POIs that motivated this parser path.
    """

    for comma_part in re.finditer(r"[^，,]+", sentence):
        raw_part = comma_part.group()
        part_leading = len(raw_part) - len(raw_part.lstrip())
        part = raw_part.strip()
        part_offset = comma_part.start() + part_leading
        if not part:
            continue
        route_match = re.fullmatch(
            r"(?:上午|下午|晚上|早上|中午)?\s*从\s*(.+?)\s*(?:步行|走)到\s*(.+)",
            part,
        )
        if route_match:
            for group_index in (1, 2):
                value = route_match.group(group_index).strip()
                offset = part_offset + route_match.start(group_index)
                yield value, offset
            continue
        cursor = 0
        for item in re.finditer(r"[^、]+", part):
            listed = item.group().strip()
            if not listed:
                continue
            listed_offset = part_offset + item.start() + (len(item.group()) - len(item.group().lstrip()))
            # ``灵隐寺和龙井村`` is a common compact list.  Keep the split
            # bounded to two non-empty phrases so names containing 和 are not
            # recursively fragmented.
            pair = re.fullmatch(r"(.{2,40}?)\s*和\s*(.{2,40})", listed)
            if pair:
                for group_index in (1, 2):
                    value = pair.group(group_index).strip()
                    yield value, listed_offset + pair.start(group_index)
            else:
                yield listed, listed_offset
            cursor = item.end()


class ItineraryTextParser:
    version = "deterministic-cn-v1"
    max_stops = 50

    def parse(self, raw_text: str, *, import_id: str) -> ImportParseDraft:
        if not raw_text or not raw_text.strip():
            return ImportParseDraft(raw_stops=[], errors=["IMPORT_TEXT_EMPTY"])
        if len(raw_text) > 12000:
            return ImportParseDraft(raw_stops=[], errors=["IMPORT_TEXT_TOO_LONG"])

        raw_stops: list[RawStop] = []
        member_summary: list[str] = []
        current_day = 0
        cursor = 0
        for line in raw_text.splitlines(keepends=True):
            sentence = line.strip()
            line_start = cursor
            cursor += len(line)
            if not sentence:
                continue
            if _UNTRUSTED_INSTRUCTION.search(sentence):
                continue
            if re.fullmatch(
                r"(?:北京|上海|杭州)?\s*[2-5]\s*(?:日|天)(?:游|行程)"
                r"(?:[，,]\s*\d+\s*(?:位|人).*)?",
                sentence,
            ):
                continue
            if any(term in sentence for term in _MEMBER_TERMS):
                member_summary.append(sentence[:500])
                if re.match(r"^(?:同行|成员|出行人|人员)\s*[:：]", sentence):
                    continue

            table_columns = _table_columns(sentence)
            if table_columns == []:
                continue
            if table_columns is not None:
                day_match = _DAY_PATTERN.fullmatch(table_columns[0])
                assert day_match is not None
                current_day = _day_number(day_match) - 1
                raw_time = table_columns[1] or None
                raw_name = _clean_name(table_columns[2])
                if not raw_name or len(raw_name) > 160:
                    continue
                place_offset = line.find(table_columns[2])
                absolute_start = line_start + max(place_offset, 0)
                absolute_end = absolute_start + len(table_columns[2])
                commitment_text = " ".join(table_columns[2:])
                raw_stops.append(RawStop(
                    raw_stop_id=str(uuid4()),
                    import_id=import_id,
                    day_index=current_day,
                    raw_name=raw_name,
                    raw_time=raw_time,
                    source_span=SourceSpan(start=absolute_start, end=absolute_end),
                    source_sentence=raw_text[absolute_start:absolute_end],
                    commitment_kind=_commitment_kind(commitment_text),
                    fixed_commitment=any(term in commitment_text for term in _FIXED_TERMS),
                ))
                if len(raw_stops) >= self.max_stops:
                    return ImportParseDraft(
                        raw_stops=raw_stops,
                        member_summary=list(dict.fromkeys(member_summary)),
                        errors=["IMPORT_STOP_LIMIT_REACHED"],
                    )
                continue

            day_match = _DAY_PATTERN.match(sentence)
            if day_match:
                current_day = _day_number(day_match) - 1
                sentence = sentence[day_match.end():].strip()
                if not sentence:
                    continue
                # "Day 1 北京" / "第 1 天：杭州" is a day header, not a
                # stop named after the destination.  Keep the rule narrow so
                # "Day 1 故宫博物院" still works as a compact one-line stop.
                if sentence.strip(" ：:-—") in _KNOWN_TRIP_CITIES:
                    continue

            for segment, segment_start in _iter_segments(sentence):
                if not segment or segment.startswith(_NON_STOP_PREFIXES):
                    continue
                raw_time_match = _TIME_PATTERN.search(segment)
                name = _clean_name(segment)
                if not name or len(name) > 160:
                    continue
                if _is_transport_event_without_place(name):
                    # A flight/train number is a time commitment, not a POI.
                    # Bind it to the immediately preceding terminal stop when
                    # the compact source text expresses both separately.
                    if raw_stops:
                        previous = raw_stops[-1]
                        if previous.day_index == current_day and re.search(
                            r"(?:机场|火车站|高铁站|客运站|码头|航站楼)$",
                            previous.raw_name,
                        ):
                            raw_stops[-1] = previous.model_copy(update={
                                "commitment_kind": CommitmentKind.RETURN_DEPARTURE,
                                "fixed_commitment": True,
                            })
                    continue
                if re.fullmatch(r"(?:乘|坐|搭乘|步行|打车|地铁|公交).{0,20}", name):
                    continue
                relative_start = line.find(segment, segment_start)
                if relative_start < 0:
                    relative_start = segment_start
                absolute_start = line_start + relative_start
                absolute_end = absolute_start + len(segment)
                raw_stops.append(RawStop(
                    raw_stop_id=str(uuid4()),
                    import_id=import_id,
                    day_index=current_day,
                    raw_name=name,
                    raw_time=raw_time_match.group().strip() if raw_time_match else None,
                    source_span=SourceSpan(start=absolute_start, end=absolute_end),
                    source_sentence=segment[:1000],
                    commitment_kind=_commitment_kind(segment),
                    fixed_commitment=any(term in segment for term in _FIXED_TERMS),
                ))
                if len(raw_stops) >= self.max_stops:
                    return ImportParseDraft(
                        raw_stops=raw_stops,
                        member_summary=list(dict.fromkeys(member_summary)),
                        errors=["IMPORT_STOP_LIMIT_REACHED"],
                    )

        errors = [] if raw_stops else ["IMPORT_PARSE_FAILED"]
        return ImportParseDraft(
            raw_stops=raw_stops,
            member_summary=list(dict.fromkeys(member_summary)),
            errors=errors,
        )
