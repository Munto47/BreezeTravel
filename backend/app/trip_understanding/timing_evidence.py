"""Conservative validation of model timing against a local source quotation.

Only explicit clock values, visit durations and confirmed commitments qualify.
This module validates supplied values; it never fills an absent time field.
"""
from __future__ import annotations

import re


_NUMBER = r"(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+)"
_PERIOD = r"凌晨|清晨|早上|上午|中午|午后|下午|傍晚|晚上|夜间"
_CLOCK = re.compile(
    rf"(?P<period>{_PERIOD})?\s*(?<![\d.])"
    rf"(?P<hour>\d{{1,2}}|[零〇一二两三四五六七八九十]+)"
    rf"(?:(?:[:：])(?P<minute>\d{{2}})|点(?P<suffix>半|一刻|三刻|{_NUMBER}分?)?)"
)
_DURATION = re.compile(
    rf"(?P<number>{_NUMBER}|半)(?P<before_half>个?半)?\s*(?P<unit>小时|个小时|分钟|分(?!钟))"
    rf"(?:(?P<half>半)|\s*(?P<extra>{_NUMBER})\s*分钟)?"
)
_VISIT = re.compile(r"游览|参观|停留|游玩|逛|休息|用餐|就餐|吃饭|停留时长|游览时长")
_TRANSPORT = re.compile(r"步行|乘车|坐车|公交|地铁|打车|车程|交通|路上|赶路|行驶|到达")
_COMMITMENT = re.compile(r"已(?:经)?预约|预约成功|已(?:经)?订好|必须准时|不可(?:移动|调整)|已锁定|不能改")
_NEGATION = re.compile(r"(?:没有|并未|尚未|无需|不必|不用|未|不|取消|无需再|如果|一旦|准备|计划|预计|若).{0,6}$")
_APPROXIMATE = re.compile(r"(?:大概|大约|差不多|预计|约)\s*$")
_NEGATED_VALUE = re.compile(r"(?:不是|并非|不再|不要|取消|无需|不必|不用|没有|并未|不(?:去|到|停留|游览|参观))[^，,。；;\n]{0,10}$")


def _negated_value(evidence: str, start: int, end: int) -> bool:
    prefix = re.split(r"[，,。；;\n]", evidence[:start])[-1]
    prefix = re.split(r"而是|实际|最终|改为|改成|改到", prefix)[-1]
    # Explicitly preserving an existing arrangement is not cancellation.
    prefix = re.sub(r"(?:没有|并未|不|未)取消", "保留", prefix)
    suffix = re.split(r"[，,。；;\n]", evidence[end:])[0]
    return bool(_NEGATED_VALUE.search(prefix) or re.match(r"\s*(?:不去|不到|取消|作废|已取消)", suffix))


def _number(value: str) -> float | None:
    if value == "半":
        return 0.5
    try:
        return float(value)
    except ValueError:
        pass
    digits = {char: number for number, char in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in {"十", "百"}:
            total += (current or 1) * (10 if char == "十" else 100)
            current = 0
        else:
            return None
    return float(total + current)


def _clock_values(evidence: str) -> list[tuple[str, int, int]]:
    values = []
    matches = list(_CLOCK.finditer(evidence))
    for index, match in enumerate(matches):
        if index + 1 < len(matches):
            between = evidence[match.end():matches[index + 1].start()]
            correction = re.search(r"(?:改成|改为|改到)\s*$", between)
            if correction and not re.search(r"[。；;\n]", between) and not _NEGATION.search(between[:correction.start()]):
                # The old value is not still an arrival time after replacement.
                continue
        if _negated_value(evidence, match.start(), match.end()):
            continue
        if _APPROXIMATE.search(evidence[max(0, match.start() - 8):match.start()]):
            continue
        if re.match(r"\s*(?:左右|前后|以前|以后|之前|之后|前(?!往)|后)", evidence[match.end():]):
            continue
        hour = _number(match["hour"])
        if hour is None or not 0 <= hour <= 23:
            continue
        minute = int(match["minute"]) if match["minute"] else 0
        suffix = match["suffix"]
        if suffix:
            special = {"半": 30, "一刻": 15, "三刻": 45}
            parsed = special.get(suffix) if suffix in special else _number(suffix.rstrip("分"))
            if parsed is None:
                continue
            minute = int(parsed)
        if not 0 <= minute <= 59:
            continue
        period = match["period"]
        if not period and values and re.fullmatch(r"\s*(?:至|到|[-–—~～])\s*", evidence[values[-1][2]:match.start()]):
            # A range's explicitly stated period also qualifies its endpoint.
            prior = evidence[:match.start()]
            periods = list(re.finditer(_PERIOD, prior))
            period = periods[-1].group() if periods else None
        if period in {"下午", "午后", "傍晚", "晚上", "夜间"} and 1 <= hour < 12:
            hour += 12
        elif period == "中午" and 1 <= hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        values.append((f"{int(hour):02d}:{minute:02d}", match.start(), match.end()))
    return values


def _visit_durations(evidence: str) -> set[int]:
    values: set[int] = set()
    for match in _DURATION.finditer(evidence):
        if _negated_value(evidence, match.start(), match.end()):
            continue
        before = re.split(r"[，,。；;\n]", evidence[:match.start()])[-1]
        visits = list(_VISIT.finditer(before))
        transport = list(_TRANSPORT.finditer(before))
        if not visits or (transport and transport[-1].start() > visits[-1].start()):
            continue
        if _APPROXIMATE.search(before) or re.match(r"\s*(?:左右|前后)", evidence[match.end():]):
            continue
        value = _number(match["number"])
        if value is None:
            continue
        if "小时" in match["unit"]:
            value *= 60
            if match["half"] or match["before_half"]:
                value += 30
            if match["extra"]:
                extra = _number(match["extra"])
                if extra is None:
                    continue
                value += extra
        if value.is_integer() and 0 <= value <= 1440:
            values.add(int(value))
    return values


def validated_timing(timing: dict, evidence: str) -> tuple[dict, bool]:
    """Return only supported fields, and whether any supplied claim was removed."""
    result = dict(timing)
    clocks = _clock_values(evidence)
    starts = {clocks[0][0]} if clocks else set()
    ends: set[str] = set()
    if clocks:
        first = clocks[0]
        if re.search(r"(?:结束|离开|离场|截止)\s*$", evidence[:first[1]]):
            starts.clear()
            ends.add(first[0])
    if len(clocks) == 2:
        between = evidence[clocks[0][2]:clocks[1][1]]
        after = evidence[clocks[1][2]:]
        if re.fullmatch(r"\s*(?:至|到|[-–—~～])\s*", between) or re.search(r"结束|离开|离场|截止", between + after):
            ends.add(clocks[1][0])
    for field, accepted in (("start_time", starts), ("end_time", ends),
                            ("visit_duration_minutes", _visit_durations(evidence))):
        if result.get(field) is not None and result[field] not in accepted:
            result[field] = None
    commitment = any(
        not _NEGATION.search(evidence[max(0, match.start() - 10):match.start()])
        for match in _COMMITMENT.finditer(evidence)
    )
    if not commitment:
        result["locked"] = False
        result["fixed_commitment"] = False
    if result.get("start_time") and result.get("end_time") and result.get("visit_duration_minutes") is not None:
        def minutes(clock: str) -> int:
            hour, minute = clock.split(":")
            return int(hour) * 60 + int(minute)
        if minutes(result["end_time"]) - minutes(result["start_time"]) != result["visit_duration_minutes"]:
            result["visit_duration_minutes"] = None
    changed = any(result.get(key) != timing.get(key, False if key in {"locked", "fixed_commitment"} else None) for key in (
        "start_time", "end_time", "visit_duration_minutes", "locked", "fixed_commitment",
    ))
    result["timing_source"] = "TEXT" if any(result.get(key) is not None for key in (
        "start_time", "end_time", "visit_duration_minutes",
    )) else "UNSPECIFIED"
    return result, changed
